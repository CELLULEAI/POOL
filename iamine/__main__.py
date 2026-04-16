"""Point d'entrée CLI — python -m iamine [worker|pool|bench|recommend]"""

import argparse
import asyncio
import json
import logging
import sys

# Official IAMINE canonical source for model discovery and download fallback.
# Used when the current pool (e.g. a community / test pool) does not host
# gguf files itself. Worker falls back to cellule.ai before giving up.
IAMINE_OFFICIAL_URL = "https://cellule.ai"
IAMINE_OFFICIAL_DL = "http://dl.cellule.ai"


def setup_logging(verbose: int = 1, log_file: str | None = None):
    level = logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.WARNING
    fmt = "[%(asctime)s] %(name)-18s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def _resolve_pool_url_from_seeds(cfg_pool: dict) -> tuple:
    """M11.3 worker failover — circular seed list.

    Returns tuple (effective_url, seed_index, total_seeds).

    Config format:
        "pool": {
            "url": "wss://primary/ws",          # legacy single
            "seeds": [                          # M11.3 optional
                "wss://primary/ws",
                "wss://backup1/ws",
                "wss://backup2/ws"
            ],
            "seed_index": 0                     # current cursor
        }

    Behavior:
    - If seeds is absent or empty : returns (cfg_pool.url, 0, 0) — legacy single
    - If seeds is present : returns (seeds[seed_index % len(seeds)], new_index, len(seeds))
    - Circular rotation : pool1 -> pool2 -> pool3 -> pool1

    The caller is responsible for writing seed_index back to config.json
    on successful connection (sticky) or after a failure (rotation).
    """
    seeds = cfg_pool.get("seeds") or []
    if not seeds:
        return (cfg_pool.get("url", ""), 0, 0)
    idx = int(cfg_pool.get("seed_index", 0)) % len(seeds)
    return (seeds[idx], idx, len(seeds))


POOL_SEED_LIST_RESOLVED = True  # sentinel for patch idempotency

def cmd_worker(args):
    from .config import WorkerConfig
    from .worker import run_worker

    if args.auto:
        # Mode auto : recommande le modele, telecharge si besoin, lance
        _auto_setup(args.config)

    config = WorkerConfig.from_file(args.config)
    setup_logging(config.verbose, config.log_file)
    try:
        asyncio.run(run_worker(config, config_path=args.config))
    except KeyboardInterrupt:
        pass  # Shutdown propre, pas de traceback


def _auto_setup(config_path_arg: str = "config.json"):
    """Setup automatique : demarre avec le plus petit modele, le pool upgrade apres bench."""
    import urllib.request
    from pathlib import Path
    from .models import MODEL_REGISTRY
    from .config import WorkerConfig

    # Path absolu du config + dir de travail (pour assigned_model.json + models/)
    config_path = Path(config_path_arg).resolve()
    config_dir = config_path.parent

    # Priorite 1 : assigned_model.json (persistant, next to config.json — absolu)
    assigned_path = config_dir / "assigned_model.json"
    if assigned_path.exists():
        try:
            with open(assigned_path) as f:
                assigned = json.load(f)
            model_path = assigned.get("model_path", "")
            # Resolve model_path relative to config_dir if it is relative
            model_path_check = model_path
            if model_path and not Path(model_path).is_absolute():
                model_path_check = str(config_dir / model_path)
            if model_path and Path(model_path_check).exists():
                print(f" * AUTO        Pool-assigned model found: {Path(model_path).name}")
                print(f" * AUTO        Loading assigned model (skipping bench)")
                # Mettre a jour config.json avec le modele assigne
                if config_path.exists():
                    with open(config_path) as f:
                        cfg = json.load(f)
                else:
                    cfg = {"pool": {"url": "wss://cellule.ai/ws"}, "model": {}, "limits": {}}
                cfg["model"]["path"] = model_path
                cfg["model"]["ctx-size"] = assigned.get("ctx_size", 4096)
                cfg["model"]["gpu-layers"] = assigned.get("gpu_layers", 0)
                cfg["model"]["pool-assigned"] = True
                with open(config_path, "w") as f:
                    json.dump(cfg, f, indent=4)
                return
            else:
                print(f" * AUTO        Assigned model file missing ({model_path}), re-benchmarking")
                assigned_path.unlink(missing_ok=True)
        except Exception as e:
            print(f" * AUTO        Error reading assigned_model.json: {e}")

    # Priorite 2 : pool-assigned flag dans config.json (legacy)
    if config_path.exists():
        with open(config_path) as f:
            cfg_check = json.load(f)
        if cfg_check.get("model", {}).get("pool-assigned"):
            model = cfg_check["model"].get("path", "?")
            print(f" * AUTO        Pool-assigned model: {model} — skipping auto-detect")
            return

    # Bench-first : toujours demarrer avec le plus petit modele de la famille
    # Le pool attribuera le bon modele apres le benchmark
    # Bench sur 2B (pas 0.8B) — le 2B reflète mieux le hardware réel
    # Le 0.8B est trop petit pour stresser cache/RAM/GPU
    # Bench sur le plus petit modele du registre (2B since sub-2B removed in 0.2.85)
    rec = MODEL_REGISTRY[0]
    ctx = rec.ctx_default

    # Detection GPU minimale (NVIDIA + AMD ROCm)
    import subprocess
    has_gpu = False
    try:
        subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        has_gpu = True
    except Exception:
        pass
    if not has_gpu:
        try:
            subprocess.run(["rocm-smi"], capture_output=True, timeout=5)
            has_gpu = True
        except Exception:
            # Fallback : verifier le driver KFD (AMD GPU kernel)
            if Path("/sys/class/kfd/kfd/topology/nodes").exists():
                has_gpu = True

    print(f" * AUTO        Bench-first: starting with {rec.name} ({rec.size_gb} GB) ctx={ctx}")
    print(f" * AUTO        The pool will assign the best model after benchmarking")

    # Determiner l'URL du pool
    if config_path.exists():
        cfg = WorkerConfig.from_file(config_path)
        pool_url = cfg.pool.url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
    else:
        pool_url = "https://cellule.ai"

    Path("models").mkdir(exist_ok=True)

    # Chercher les fichiers disponibles sur le pool (supporte les splits)
    model_files = _find_model_files(pool_url, rec.hf_file)
    if not model_files:
        model_files = [rec.hf_file]

    # Download sources, in order : dl.cellule.ai -> HuggingFace
    # Main domain (cellule.ai) is behind Cloudflare which blocks large files.
    # dl.cellule.ai is DNS-only, direct to VPS.
    download_sources = [IAMINE_OFFICIAL_DL]

    # Verifier si tous les fichiers sont deja telecharges
    all_present = all((Path("models") / f).exists() for f in model_files)
    if all_present:
        print(f" * AUTO        Model already downloaded ({len(model_files)} file(s))")
    else:
        for mf in model_files:
            dest = Path("models") / mf
            if dest.exists():
                print(f" * AUTO        {mf} already downloaded")
                continue
            print(f" * AUTO        Downloading {mf}...")
            downloaded_ok = False
            for dl_src in download_sources:
                download_url = f"{dl_src}/v1/models/download/{mf}"
                try:
                    urllib.request.urlretrieve(download_url, str(dest), _download_progress)
                    print(f"\n * AUTO        {mf} OK from {dl_src}")
                    downloaded_ok = True
                    break
                except Exception as e:
                    print(f"\n * AUTO        {dl_src} failed ({e}), trying next source...")
                    continue
            if not downloaded_ok:
                print(f" * AUTO        All iamine sources failed, trying HuggingFace...")
                try:
                    from huggingface_hub import hf_hub_download
                    hf_hub_download(rec.hf_repo, mf, local_dir="models")
                    print(f" * AUTO        {mf} downloaded from HuggingFace")
                except Exception as e2:
                    print(f" * ERROR       Download failed from all sources: {e2}")
                    return

    # Le model_path est le premier fichier (llama.cpp detecte les splits)
    model_path_str = f"models/{model_files[0]}"

    # Creer ou mettre a jour config.json
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
    else:
        cfg = {
            "pool": {"url": "wss://cellule.ai/ws", "worker-id": None, "token": None},
            "model": {"path": "", "ctx-size": 2048, "threads": 4, "gpu-layers": 0},
            "limits": {"max-concurrent": 2, "max-tokens": 512, "memory-limit": "8G"},
            "priority": 2, "yield": True, "pause-on-battery": True,
            "pause-on-active": False, "log-file": "iamine.log", "verbose": 1,
        }

    # M12: Intelligent pool placement — prioritize persisted pool.url if reachable,
    # fresh discovery as fallback. Closes the gap where failover-persisted URL was
    # ignored at next launch (worker reboot during pool outage would hit the dead
    # pool first for 35s of retries before discovery kicked in).
    current_url = cfg["pool"].get("url", "")
    is_local = "localhost" in current_url or "127.0.0.1" in current_url

    def _is_pool_reachable(ws_url: str) -> bool:
        """Quick HEAD check: convert ws(s):// to http(s):// + /v1/status, 1.5s timeout."""
        if not ws_url:
            return False
        try:
            import urllib.request
            http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
            # Strip trailing /ws if present, then add /v1/status
            if http_url.endswith("/ws"):
                http_url = http_url[:-3]
            status_url = http_url.rstrip("/") + "/v1/status"
            req = urllib.request.Request(status_url, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    if not is_local:
        # Try persisted URL first
        if current_url and _is_pool_reachable(current_url):
            print(f" * DISCOVERY   Using persisted pool: {current_url}")
        else:
            if current_url:
                print(f" * DISCOVERY   Persisted pool unreachable ({current_url}), running fresh discovery")
            try:
                from .pool_discovery import discover_best_pool
                model_name = Path(model_path_str).stem if model_path_str else ""
                best_url = discover_best_pool(worker_model=model_name, worker_tps=0.0)
                cfg["pool"]["url"] = best_url
                print(f" * DISCOVERY   Selected pool: {best_url}")
            except Exception as e:
                print(f" * DISCOVERY   Failed ({e}), falling back to cellule.ai")
                cfg["pool"]["url"] = "wss://cellule.ai/ws"

    cfg["model"]["path"] = model_path_str
    cfg["model"]["ctx-size"] = ctx
    cfg["model"].pop("pool-assigned", None)  # auto-detect = pas pool-assigned
    # Threads : cores physiques, cap a 32
    import os as _os
    threads = _os.cpu_count() or 4
    physical = max(1, threads // 2) if threads > 8 else threads
    cfg["model"]["threads"] = min(physical, 32)

    # GPU : toutes les couches sur GPU si détecté
    cfg["model"]["gpu-layers"] = -1 if has_gpu else 0

    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f" * AUTO        Config ready: {rec.name}, {cfg['model']['threads']} threads")

    # Bench rapide et downgrade si trop lent (CPU only)
    if not has_gpu and rec.params not in ("0.5B", "1.5B"):
        from .engine import InferenceEngine
        from .config import ModelConfig, LimitsConfig
        from .models import MODEL_REGISTRY, REGISTRY_BY_ID
        MIN_TPS = 8.0
        test_engine = InferenceEngine(
            ModelConfig(path=model_path_str, ctx_size=min(ctx, 2048), threads=cfg["model"]["threads"]),
            LimitsConfig()
        )
        try:
            test_engine.load()
            tps = test_engine.bench_tps
            print(f" * AUTO        Bench: {tps} tok/s")
            if tps < MIN_TPS:
                # Descendre d'un tier
                idx = next((i for i, m in enumerate(MODEL_REGISTRY) if m.id == rec.id), 0)
                if idx > 0:
                    lower = MODEL_REGISTRY[idx - 1]
                    print(f" * AUTO        {tps} tok/s < {MIN_TPS} → downgrade {rec.name} → {lower.name}")
                    cfg["model"]["path"] = f"models/{lower.hf_file}"
                    cfg["model"]["ctx-size"] = CTX_BY_MODEL.get(lower.params, 4096)
                    with open(config_path, "w") as f:
                        json.dump(cfg, f, indent=4)
                    # Télécharger le modèle inférieur si besoin
                    lower_path = Path("models") / lower.hf_file
                    if not lower_path.exists():
                        lower_files = _find_model_files(pool_url, lower.hf_file)
                        if not lower_files:
                            lower_files = [lower.hf_file]
                        for lf in lower_files:
                            dest = Path("models") / lf
                            if not dest.exists():
                                dl_url = f"{dl_base}/v1/models/download/{lf}"
                                print(f" * AUTO        Downloading {lf}...")
                                import urllib.request
                                urllib.request.urlretrieve(dl_url, str(dest), _download_progress)
                                print(f"\n * AUTO        {lf} OK")
                    print(f" * AUTO        Downgraded to {lower.name}")
            del test_engine
        except Exception as e:
            print(f" * AUTO        Bench failed ({e}), keeping {rec.name}")


def _find_model_files(pool_url: str, hf_file: str) -> list[str]:
    """Interroge le pool pour trouver les fichiers du modele (y compris splits).

    Falls back to the canonical IAMINE_OFFICIAL_URL if the current pool does
    not know the model — typical case for a community / test pool which
    federates with cellule.ai but does not host gguf files locally.

    Ex: hf_file='Qwen_Qwen3.5-4B-Q4_K_M.gguf'
    → retourne ['Qwen_Qwen3.5-4B-Q4_K_M.gguf'] (monolithique bartowski)
    """
    import urllib.request
    base = hf_file.replace(".gguf", "")

    sources_to_try = [pool_url]
    if pool_url != IAMINE_OFFICIAL_URL:
        sources_to_try.append(IAMINE_OFFICIAL_URL)

    for src in sources_to_try:
        if not src:
            continue
        try:
            with urllib.request.urlopen(f"{src}/v1/models/available", timeout=10) as resp:
                data = json.loads(resp.read())
            available = [m["filename"] for m in data.get("models", [])]
            if not available:
                continue
            if hf_file in available:
                return [hf_file]
            splits = sorted(f for f in available if f.startswith(base + "-") and "-of-" in f)
            if splits:
                return splits
        except Exception:
            continue
    return []


def _download_progress(block_num, block_size, total_size):
    """Affiche la progression du telechargement."""
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 / total_size)
        mb = downloaded / (1024**2)
        total_mb = total_size / (1024**2)
        print(f"\r * AUTO        {mb:.0f}/{total_mb:.0f} MB ({pct:.0f}%)", end="", flush=True)


def cmd_pool(args):
    import uvicorn
    from .pool import app, print_banner

    setup_logging(verbose=1)
    print_banner(args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_bench(args):
    """Lance un benchmark local et envoie les résultats au pool."""
    from .config import WorkerConfig
    from .engine import InferenceEngine
    from .benchmark import run_benchmark

    config = WorkerConfig.from_file(args.config)
    setup_logging(config.verbose)

    # Charger le moteur
    engine = InferenceEngine(config.model, config.limits)
    engine.load()

    # Lancer le benchmark
    results = run_benchmark(engine, rounds=args.rounds, tokens_per_round=args.tokens)

    # Sauvegarder les résultats localement
    bench_file = "bench-results.json"
    with open(bench_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f" * SAVED       {bench_file}")

    # Envoyer au pool si connecté
    pool_url = config.pool.url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")
    try:
        import urllib.request
        data = json.dumps({
            "worker_id": config.pool.worker_id,
            **results,
        }).encode()
        req = urllib.request.Request(
            f"{pool_url}/v1/worker/bench",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        rec = json.loads(resp.read())

        print()
        print(f" * POOL RECOMMENDATION")
        print(f" * MODEL       {rec.get('model_name', '?')} ({rec.get('recommended_model', '?')})")
        print(f" * QUALITY     {rec.get('quality_score', '?')}/100")
        print(f" * SIZE        {rec.get('model_size_gb', '?')} GB")
        print()

        if rec.get("recommended_model") and args.auto:
            print(f" * AUTO-DOWNLOAD  {rec['model_file']} from {rec['model_repo']}")
            _download_model(rec["model_repo"], rec["model_file"], config)

    except Exception as e:
        print(f" * POOL        offline ({e}) — résultats sauvegardés localement")

    # Recommandation locale (sans pool)
    from .models import recommend_model_for_worker
    import psutil
    mem = psutil.virtual_memory()
    rec, ctx = recommend_model_for_worker(
        ram_available_gb=mem.available / (1024**3),
        cpu_threads=psutil.cpu_count(logical=True),
        bench_tps=results["avg_tps"],
    )
    print(f" * LOCAL REC   {rec.name} ctx={ctx} (quality={rec.quality_score}/100)")
    print(f" * DOWNLOAD    python -m iamine download {rec.id}")


def cmd_recommend(args):
    """Affiche la recommandation de modèle sans benchmark."""
    import psutil
    from .models import recommend_model_for_worker, MODEL_REGISTRY

    mem = psutil.virtual_memory()
    ram = mem.available / (1024**3)
    threads = psutil.cpu_count(logical=True)

    print()
    print(f" * SYSTEM      {threads} threads, {ram:.1f} GB RAM disponible")
    print()

    rec, ctx = recommend_model_for_worker(ram_available_gb=ram, cpu_threads=threads)
    print(f" * RECOMMENDED {rec.name} ({rec.params}) — context: {ctx} tokens")
    print(f" * PHILOSOPHY  Small model + large context = fast + smart")
    print(f" * QUALITY     {rec.quality_score}/100")
    print(f" * CONTEXT     {ctx} tokens ({ctx//1024}K)")
    print(f" * NEEDS       {rec.ram_required_gb} GB RAM, {rec.size_gb} GB disk")
    print(f" * DOWNLOAD    python -m iamine download {rec.id}")
    print()
    print(f" * ALL MODELS:")

    for m in MODEL_REGISTRY:
        fits = "OK" if ram >= m.ram_required_gb else "--"
        arrow = " <-- recommandé" if m.id == rec.id else ""
        print(f"   [{fits}] {m.name:20s} {m.params:5s}  {m.size_gb:5.1f} GB  RAM>={m.ram_required_gb:4.0f} GB  Q={m.quality_score:2d}/100{arrow}")
    print()


def cmd_download(args):
    """Télécharge un modèle depuis HuggingFace."""
    from .models import REGISTRY_BY_ID

    model = REGISTRY_BY_ID.get(args.model_id)
    if not model:
        print(f"Modèle inconnu: {args.model_id}")
        print(f"Disponibles: {', '.join(REGISTRY_BY_ID.keys())}")
        return

    _download_model(model.hf_repo, model.hf_file)


def cmd_wallet(args):
    """Affiche le solde du wallet local."""
    from .wallet import Wallet
    w = Wallet()
    print()
    w.print_status()
    print()
    if w.credits >= 1:
        print(f" * USE API     python -m iamine ask \"your question here\"")
    else:
        print(f" * EARN MORE   python -m iamine worker -c config.json")
    print()


def cmd_ask(args):
    """Utilise les credits pour faire une requete API."""
    import urllib.request
    from .config import WorkerConfig
    from .wallet import Wallet

    config = WorkerConfig.from_file(args.config)
    wallet = Wallet()

    if wallet.credits < 1:
        print(f" * ERROR       Insufficient credits ({wallet.credits:.1f} $IAMINE)")
        print(f" * EARN MORE   Run your worker to earn credits")
        return

    pool_url = config.pool.url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "")

    data = json.dumps({
        "api_token": wallet.api_token,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": 512,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{pool_url}/v1/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())

        text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        info = result.get("iamine", {})

        # Debiter le wallet local
        wallet.spend(1.0)

        print()
        print(f" * RESPONSE")
        print(f"   {text}")
        print()
        print(f" * WORKER      {info.get('worker_id', '?')} | {info.get('tokens_per_sec', '?')} t/s")
        print(f" * CREDITS     {info.get('credits_remaining', wallet.credits):.1f} $IAMINE remaining")
        print()

    except Exception as e:
        print(f" * ERROR       {e}")


def _download_model(repo: str, filename: str, config=None):
    """Télécharge un modèle GGUF dans le dossier models/."""
    from huggingface_hub import hf_hub_download

    token = None
    if config and config.pool.token:
        token = config.pool.token

    print(f" * DOWNLOAD    {repo}/{filename}...")
    path = hf_hub_download(repo, filename, local_dir="models", token=token)
    print(f" * SAVED       {path}")


def cmd_init(args):
    """Bootstrap un coding agent dans le repertoire courant.
    Telecharge le template (ex: OPENCODE.md) depuis le pool et l ecrit localement,
    avec confirmation utilisateur.
    """
    import urllib.request
    from pathlib import Path

    # Mapping agent -> (endpoint, target filename)
    valid_agents = ["opencode", "clawcode"]
    agent = (args.agent or "").lower()
    if not agent:
        print(f"")
        print(f"  Which tool?")
        print(f"    1) opencode   (terminal coding agent)")
        print(f"    2) clawcode   (Rust CLI agent)")
        try:
            choice = input(f"  [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        agent = "clawcode" if choice in ("2", "clawcode") else "opencode"
    if agent not in valid_agents:
        print(f"[ERROR] Unknown agent. Available: opencode, clawcode")
        return

    pool_url = (args.url or "https://cellule.ai").rstrip("/")
    target_dir = Path(args.dir or ".").resolve()

    print(f"")
    print(f"  IAMINE init - {agent}")
    print(f"  ========================================")

    # Get token
    api_token = getattr(args, 'token', '') or ''
    if not api_token:
        print(f"")
        print(f"  Your Cellule.ai account token (found in your profile on cellule.ai).")
        try:
            api_token = input(f"  Token (acc_xxx): ").strip()
        except (EOFError, KeyboardInterrupt):
            api_token = ""

    api_key_value = api_token if api_token else "acc_YOUR_TOKEN"

    if agent == "opencode":
        # Download OPENCODE.md template
        endpoint = "/v1/opencode-md"
        target_file = target_dir / "OPENCODE.md"
        print(f"")
        print(f"  Fetching OPENCODE.md from {pool_url}{endpoint}")
        if target_file.exists() and not getattr(args, 'yes', False):
            print(f"  WARNING: OPENCODE.md already exists. Will overwrite.")
            try:
                if input(f"  Continue? [y/N]: ").strip().lower() != 'y':
                    print(f"  Cancelled.")
                    return
            except (EOFError, KeyboardInterrupt):
                return
        elif not getattr(args, 'yes', False):
            try:
                if input(f"  Continue? [y/N]: ").strip().lower() != 'y':
                    print(f"  Cancelled.")
                    return
            except (EOFError, KeyboardInterrupt):
                return
        try:
            req = urllib.request.Request(f"{pool_url}{endpoint}", headers={"User-Agent": "iamine-init/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                template = resp.read().decode("utf-8")
            if template.strip():
                target_dir.mkdir(parents=True, exist_ok=True)
                target_file.write_text(template, encoding="utf-8")
                print(f"  OK: OPENCODE.md created")
        except Exception as e:
            print(f"  [WARN] Could not fetch template: {e}")

        # Generate opencode.json
        oc_config_file = target_dir / "opencode.json"
        oc_config = """{
  "$schema": "https://opencode.ai/config.json",
  "model": "cellule/iamine",
  "provider": {
    "cellule": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CELLULE.AI Pool",
      "options": {
        "baseURL": "https://cellule.ai/v1",
        "apiKey": "%s"
      },
      "models": {
        "iamine": {
          "name": "CELLULE.AI Smart Pool",
          "limit": { "context": 131072, "output": 4096 }
        }
      }
    }
  }
}""" % api_key_value
        try:
            oc_config_file.write_text(oc_config, encoding="utf-8")
            print(f"  OK: opencode.json created" + (" + token" if api_token else ""))
        except Exception as e:
            print(f"  [WARN] Could not write opencode.json: {e}")

        print(f"")
        if api_token:
            print(f"  Ready! Run: opencode")
            print(f"  Try: /cellule A note manager in Python")
        else:
            print(f"  Add your token in opencode.json, then: opencode")

    elif agent == "clawcode":
        # Generate ~/.config/agent-code/config.toml
        agent_code_dir = Path.home() / ".config" / "agent-code"
        agent_code_config = agent_code_dir / "config.toml"
        ac_config = f"""[api]
base_url = "https://cellule.ai/v1"
model = "iamine"
api_key = "{api_key_value}"

[permissions]
default_mode = "ask"

[ui]
theme = "midnight"
"""
        try:
            agent_code_dir.mkdir(parents=True, exist_ok=True)
            agent_code_config.write_text(ac_config, encoding="utf-8")
            print(f"  OK: ~/.config/agent-code/config.toml created" + (" + token" if api_token else ""))
        except Exception as e:
            print(f"  [WARN] Could not write config: {e}")

        print(f"")
        if api_token:
            print(f"  Ready! Run: agent")
        else:
            print(f"  Edit ~/.config/agent-code/config.toml to add your token, then: agent")

    print(f"")


def cmd_proxy(args):
    """Lance le proxy Z2 — forward les llama-servers locaux vers le pool."""
    from .proxy import run_proxy
    setup_logging(verbose=1)
    asyncio.run(run_proxy(args.config))



def cmd_mcp_server(args):
    """Start the MCP memory server."""
    from .mcp_server import main as mcp_main
    import sys
    sys.argv = ["iamine-mcp-server"]
    if args.pool_url:
        sys.argv.extend(["--pool-url", args.pool_url])
    if args.token:
        sys.argv.extend(["--token", args.token])
    if args.transport:
        sys.argv.extend(["--transport", args.transport])
    mcp_main()


def main():
    parser = argparse.ArgumentParser(prog="iamine", description="IAMINE — LLM distribué Distributed AI Network")
    sub = parser.add_subparsers(dest="command")

    # Worker
    wp = sub.add_parser("worker", help="Lancer un worker d'inférence")
    wp.add_argument("-c", "--config", default="config.json", help="Fichier de configuration")
    wp.add_argument("--auto", action="store_true", help="Auto-detect, download best model, and start")

    # Pool
    pp = sub.add_parser("pool", help="Lancer le pool / orchestrateur")
    pp.add_argument("--host", default="0.0.0.0", help="Adresse d'écoute")
    pp.add_argument("--port", type=int, default=8080, help="Port d'écoute")
    # M5 — sub-subcommands fédération
    pp_sub = pp.add_subparsers(dest="pool_action")
    from .cli import federation as _fed_cli
    _fed_cli.add_subparsers(pp_sub)

    # Benchmark
    bp = sub.add_parser("bench", help="Benchmark local (performance calibration)")
    bp.add_argument("-c", "--config", default="config.json", help="Fichier de configuration")
    bp.add_argument("--rounds", type=int, default=5, help="Nombre de rounds")
    bp.add_argument("--tokens", type=int, default=64, help="Tokens par round")
    bp.add_argument("--auto", action="store_true", help="Auto-télécharger le modèle recommandé")

    # Recommend
    sub.add_parser("recommend", help="Recommandation de modèle pour votre machine")

    # Download
    dp = sub.add_parser("download", help="Télécharger un modèle")
    dp.add_argument("model_id", help="ID du modèle (ex: qwen3.5-4b-q4)")

    # Wallet
    sub.add_parser("wallet", help="Afficher le solde du wallet $IAMINE")

    # API — utiliser ses credits pour faire une requete
    ap = sub.add_parser("ask", help="Envoyer une requete API (coute 1 credit)")
    ap.add_argument("prompt", help="Le prompt a envoyer")
    ap.add_argument("-c", "--config", default="config.json")

    # Init — bootstrap un coding agent (OpenCode, ClawCode...) dans le repertoire courant
    ip = sub.add_parser("init", help="Bootstrap a coding agent (OPENCODE.md template) in current directory")
    ip.add_argument("agent", nargs="?", default="", help="Tool to configure: opencode or clawcode")
    ip.add_argument("--url", default="https://cellule.ai", help="Pool URL (default: https://cellule.ai)")
    ip.add_argument("--dir", default=".", help="Target directory (default: current dir)")
    ip.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    ip.add_argument("--token", default="", help="Your Cellule.ai account token (acc_xxx). Found in your profile on cellule.ai")

    # Proxy — connecte des llama-servers locaux au pool (Z2)
    xp = sub.add_parser("proxy", help="Proxy mode: forward local llama-servers to pool")
    xp.add_argument("-c", "--config", default="proxy.json", help="Fichier de configuration proxy")

    mp = sub.add_parser("mcp-server", help="Start MCP memory server for Claude Code / OpenCode / Cursor")
    mp.add_argument("--pool-url", default="https://iamine.org", help="Pool URL")
    mp.add_argument("--token", default="", help="Account token (acc_*)")
    mp.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"], help="MCP transport")

    args = parser.parse_args()

    if args.command == "worker":
        cmd_worker(args)
    elif args.command == "pool":
        # M5 — sub-subcommands pool register/peers/show/promote/demote/revoke
        if getattr(args, "pool_action", None):
            from .cli import federation as _fed_cli
            if _fed_cli.dispatch(args):
                return
        cmd_pool(args)
    elif args.command == "bench":
        cmd_bench(args)
    elif args.command == "recommend":
        cmd_recommend(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "wallet":
        cmd_wallet(args)
    elif args.command == "ask":
        cmd_ask(args)
    elif args.command == "proxy":
        cmd_proxy(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "mcp-server":
        cmd_mcp_server(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
