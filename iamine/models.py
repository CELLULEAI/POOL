"""Registre de modèles et sélection automatique selon la puissance du pool."""

from __future__ import annotations

from dataclasses import dataclass

# Tiers de modèles classés par puissance requise (croissant)
# Chaque tier définit les specs minimum d'un WORKER INDIVIDUEL pour charger ce modèle

@dataclass
class ModelTier:
    id: str                   # identifiant unique
    name: str                 # nom lisible
    hf_repo: str              # repo HuggingFace
    hf_file: str              # fichier GGUF à télécharger
    size_gb: float            # taille du fichier sur disque
    ram_required_gb: float    # RAM minimum pour charger le modèle
    ctx_default: int          # contexte recommandé
    params: str               # taille du modèle (affichage)
    quality_score: int        # score de qualité relative (1-100)
    min_tps_useful: float     # tokens/sec minimum pour être utile
    earn_per_100tok: float = 1.0   # $IAMINE gagnes pour 100 tokens servis
    cost_per_request: float = 1.0  # $IAMINE pour utiliser ce modele via l'API
    model_type: str = "dense"      # "dense" ou "moe"
    active_params_b: float = 0.0   # params actifs en milliards (MoE), 0 = tous
    generation: str = "2.5"        # "2.5" ou "3.5" — pour la transition


# === FAMILLES DE MODELES ===
# Chaque famille est un registre complet, selectionnable depuis le dashboard admin.
# La famille active est stockee en DB (pool_config.active_family).

FAMILY_QWEN35: list[ModelTier] = [
    ModelTier(
        id="qwen3.5-2b-q4", name="Qwen 3.5 2B",
        hf_repo="bartowski/Qwen_Qwen3.5-2B-GGUF",
        hf_file="Qwen_Qwen3.5-2B-Q4_K_M.gguf",
        size_gb=1.3, ram_required_gb=2.5, ctx_default=4096,
        params="2B", quality_score=35, min_tps_useful=8.0,
        earn_per_100tok=1.0, cost_per_request=2.0, generation="3.5",
    ),
    ModelTier(
        id="qwen3.5-4b-q4", name="Qwen 3.5 4B",
        hf_repo="bartowski/Qwen_Qwen3.5-4B-GGUF",
        hf_file="Qwen_Qwen3.5-4B-Q4_K_M.gguf",
        size_gb=2.7, ram_required_gb=5.0, ctx_default=8192,
        params="4B", quality_score=55, min_tps_useful=8.0,
        earn_per_100tok=2.0, cost_per_request=3.0, generation="3.5",
    ),
    ModelTier(
        id="qwen3.5-9b-q4", name="Qwen 3.5 9B",
        hf_repo="bartowski/Qwen_Qwen3.5-9B-GGUF",
        hf_file="Qwen_Qwen3.5-9B-Q4_K_M.gguf",
        size_gb=5.5, ram_required_gb=8.0, ctx_default=16384,
        params="9B", quality_score=75, min_tps_useful=8.0,
        earn_per_100tok=4.0, cost_per_request=5.0, generation="3.5",
    ),
    ModelTier(
        id="qwen3.5-27b-q4", name="Qwen 3.5 27B",
        hf_repo="bartowski/Qwen_Qwen3.5-27B-GGUF",
        hf_file="Qwen_Qwen3.5-27B-Q4_K_M.gguf",
        size_gb=16.0, ram_required_gb=20.0, ctx_default=32768,
        params="27B", quality_score=92, min_tps_useful=4.0,
        earn_per_100tok=15.0, cost_per_request=30.0, generation="3.5",
    ),
    ModelTier(
        id="qwen3.5-35b-a3b-q4", name="Qwen 3.5 35B-A3B",
        hf_repo="bartowski/Qwen_Qwen3.5-35B-A3B-GGUF",
        hf_file="Qwen_Qwen3.5-35B-A3B-Q4_K_M.gguf",
        size_gb=21.0, ram_required_gb=25.0, ctx_default=32768,
        params="35B", quality_score=95, min_tps_useful=4.0,
        earn_per_100tok=12.0, cost_per_request=20.0,
        model_type="moe", active_params_b=3.0, generation="3.5",
    ),
]

FAMILY_QWEN25: list[ModelTier] = [
    ModelTier(
        id="qwen2.5-3b-q4", name="Qwen 2.5 3B",
        hf_repo="bartowski/Qwen2.5-3B-Instruct-GGUF",
        hf_file="Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        size_gb=1.9, ram_required_gb=3.5, ctx_default=4096,
        params="3B", quality_score=40, min_tps_useful=8.0,
        earn_per_100tok=1.0, cost_per_request=2.0, generation="2.5",
    ),
    ModelTier(
        id="qwen2.5-7b-q4", name="Qwen 2.5 7B",
        hf_repo="bartowski/Qwen2.5-7B-Instruct-GGUF",
        hf_file="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        size_gb=4.4, ram_required_gb=7.0, ctx_default=8192,
        params="7B", quality_score=65, min_tps_useful=6.0,
        earn_per_100tok=3.0, cost_per_request=4.0, generation="2.5",
    ),
    ModelTier(
        id="qwen2.5-14b-q4", name="Qwen 2.5 14B",
        hf_repo="bartowski/Qwen2.5-14B-Instruct-GGUF",
        hf_file="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
        size_gb=8.7, ram_required_gb=12.0, ctx_default=16384,
        params="14B", quality_score=82, min_tps_useful=6.0,
        earn_per_100tok=8.0, cost_per_request=12.0, generation="2.5",
    ),
    ModelTier(
        id="qwen2.5-32b-q4", name="Qwen 2.5 32B",
        hf_repo="bartowski/Qwen2.5-32B-Instruct-GGUF",
        hf_file="Qwen2.5-32B-Instruct-Q4_K_M.gguf",
        size_gb=19.0, ram_required_gb=24.0, ctx_default=32768,
        params="32B", quality_score=90, min_tps_useful=4.0,
        earn_per_100tok=15.0, cost_per_request=25.0, generation="2.5",
    ),
]

# Catalogue des familles disponibles
MODEL_FAMILIES: dict[str, list[ModelTier]] = {
    "qwen3.5": FAMILY_QWEN35,
    "qwen2.5": FAMILY_QWEN25,
}

# Famille active par defaut (sera surchargee par la DB au startup)
_active_family: str = "qwen3.5"

def get_active_family() -> str:
    return _active_family

def set_active_family(family: str) -> bool:
    """Change la famille active. Retourne False si famille inconnue."""
    global _active_family, MODEL_REGISTRY, UNLOCK_THRESHOLDS, SINGLE_WORKER_MIN_RAM, REGISTRY_BY_ID
    if family not in MODEL_FAMILIES:
        return False
    _active_family = family
    MODEL_REGISTRY = MODEL_FAMILIES[family]
    REGISTRY_BY_ID = {m.id: m for m in MODEL_REGISTRY}
    SINGLE_WORKER_MIN_RAM = {m.id: m.ram_required_gb for m in MODEL_REGISTRY}
    UNLOCK_THRESHOLDS = {m.id: (100 if m.quality_score >= 90 else 0) for m in MODEL_REGISTRY}
    return True

# Registre actif — initialise sur la famille par defaut
MODEL_REGISTRY: list[ModelTier] = MODEL_FAMILIES[_active_family]

UNLOCK_THRESHOLDS = {m.id: (100 if m.quality_score >= 90 else 0) for m in MODEL_REGISTRY}

SINGLE_WORKER_MIN_RAM = {m.id: m.ram_required_gb for m in MODEL_REGISTRY}

REGISTRY_BY_ID = {m.id: m for m in MODEL_REGISTRY}


def best_model_from_bench(bench_tps: float, ram_gb: float, has_gpu: bool = False, gpu_vram_gb: float = 0) -> tuple[ModelTier, int]:
    """Attribue le meilleur modele a partir du bench sur 2B (depuis v0.2.85).

    Equation : tps_estime = bench_2B * (BENCH_MODEL_SIZE / size_gb_modele)
    On prend le plus gros modele qui tient en VRAM (GPU) ou RAM (CPU)
    avec tps_estime >= seuil par tier.
    """
    MIN_TPS_BY_SIZE = {1.3: 8, 2.7: 8, 5.5: 8, 16: 4, 21: 4}
    DEFAULT_MIN_TPS = 6.0
    BENCH_MODEL_SIZE = 1.3  # taille du 2B Q4 en GB (modele de bench)

    CTX_BY_PARAMS = {
        "2B": 2048, "4B": 2048,
        "9B": 4096, "27B": 4096, "35B": 4096,
        "3B": 2048, "7B": 2048, "14B": 4096, "32B": 4096,
    }

    # GPU : prendre le plus gros qui rentre en VRAM
    # Marge proportionnelle : 20% du modele (min 0.5 GB) pour KV cache + runtime
    if has_gpu and gpu_vram_gb > 2:
        GPU_BOOST = 3.0
        best = MODEL_REGISTRY[0]
        for m in MODEL_REGISTRY:
            tps_est = bench_tps * (BENCH_MODEL_SIZE / m.size_gb) * GPU_BOOST if m.size_gb > 0 else 0
            min_tps = MIN_TPS_BY_SIZE.get(m.size_gb, DEFAULT_MIN_TPS)
            vram_margin = max(0.4, m.size_gb * 0.05)
            if (m.size_gb + vram_margin <= gpu_vram_gb
                    and m.quality_score > best.quality_score
                    and tps_est >= min_tps):
                best = m
        return best, CTX_BY_PARAMS.get(best.params, 4096)

    best = MODEL_REGISTRY[0]  # fallback = smallest in registry (2B)
    for m in MODEL_REGISTRY:
        # RAM reelle avec petit ctx : modele + KV cache minimal + 1.5G marge OS
        ctx = CTX_BY_PARAMS.get(m.params, 2048)
        kv_small = 0.1 * (ctx / 2048) * (m.size_gb / 0.5)  # KV proportionnel
        ram_needed = m.size_gb + kv_small + 1.5
        if ram_needed > ram_gb:
            continue
        # Estimation des t/s sur ce modele
        # CPU scale mieux que lineaire grace au cache L3 (facteur 1.5x pour 2B-9B)
        raw_est = bench_tps * (BENCH_MODEL_SIZE / m.size_gb) if m.size_gb > 0 else bench_tps
        cpu_boost = 1.5 if m.size_gb <= 5.5 else 1.2
        estimated_tps = raw_est * cpu_boost
        min_tps = MIN_TPS_BY_SIZE.get(m.size_gb, DEFAULT_MIN_TPS)
        if estimated_tps < min_tps:
            continue
        if m.quality_score > best.quality_score:
            best = m

    return best, CTX_BY_PARAMS.get(best.params, 4096)


def _total_params_b(tier: ModelTier) -> float:
    """Extrait le nombre total de params (milliards) depuis tier.params.
    
    Ex: 35B -> 35.0, 0.8B -> 0.8
    Fallback sur size_gb * 1.5 si parsing echoue.
    """
    p = tier.params.upper().replace("B", "").strip()
    try:
        return float(p)
    except ValueError:
        return tier.size_gb * 1.5


def promote_from_real_tps(
    real_tps: float, current_model_size_gb: float,
    ram_gb: float, has_gpu: bool = False, gpu_vram_gb: float = 0
) -> tuple[ModelTier, int] | None:
    """Promote a worker to a bigger model based on real performance.

    Uses actual throughput on current model to estimate performance
    on larger models. More accurate than bench_0.8B extrapolation
    because it reflects real hardware behavior.

    Returns (model, ctx) if a better model is found, None otherwise.
    """
    MIN_TPS_BY_SIZE = {0.5: 8, 1.3: 6, 2.7: 5, 5.5: 5, 16: 5, 21: 5}
    DEFAULT_MIN_TPS = 6.0
    CTX_BY_PARAMS = {
        "0.8B": 2048, "2B": 2048, "4B": 2048,
        "9B": 4096, "27B": 4096, "35B": 4096,
    }

    if real_tps <= 0 or current_model_size_gb <= 0:
        return None

    best = None
    for m in MODEL_REGISTRY:
        # Skip models smaller or equal to current
        if m.size_gb <= current_model_size_gb:
            continue

        # RAM/VRAM check
        if has_gpu and gpu_vram_gb > 2:
            # GPU workers: modele DOIT tenir en VRAM, jamais de fallback CPU
            if m.size_gb + 0.3 > gpu_vram_gb:
                continue  # trop gros pour la VRAM → skip (pas de fallback)
        else:
            ctx = CTX_BY_PARAMS.get(m.params, 2048)
            kv = 0.1 * (ctx / 2048) * (m.size_gb / 0.5)
            if m.size_gb + kv + 1.5 > ram_gb:
                continue

        # Estimate tps on this model from real performance on current
        # MoE: la vitesse depend des params actifs, pas du poids total
        # Un 35B-A3B (21 GB) tourne a la vitesse d un 3B (~2 GB) grace au routing MoE
        if m.model_type == "moe" and m.active_params_b > 0:
            # Convertir active_params en taille effective pour l estimation
            # Ratio approximatif: active_params_b / total_params -> fraction de size_gb
            # Mais plus simple: utiliser le ratio size du modele courant vs active_size
            active_size_gb = m.size_gb * (m.active_params_b / _total_params_b(m))
            estimated_tps = real_tps * (current_model_size_gb / active_size_gb)
        else:
            estimated_tps = real_tps * (current_model_size_gb / m.size_gb)

        # GPU: pas de boost artificiel — le real_tps est deja mesure sur GPU
        # L estimation real_tps * (current_size / target_size) est fiable sur GPU

        min_tps = MIN_TPS_BY_SIZE.get(m.size_gb, DEFAULT_MIN_TPS)
        if estimated_tps < min_tps:
            continue

        if best is None or m.quality_score > best.quality_score:
            best = m

    if best is None:
        return None

    return best, CTX_BY_PARAMS.get(best.params, 4096)


def recommend_model_for_worker(
    ram_available_gb: float,
    cpu_threads: int,
    bench_tps: float | None = None,
    has_gpu: bool = False,
    gpu_vram_gb: float = 0,
) -> tuple[ModelTier, int]:
    """Recommande le meilleur modele + contexte pour un worker.

    Philosophie IAMINE :
    - Preferer Qwen 3.5 sur 2.5 a capacite egale
    - MoE privilegie sur GPU (vitesse d'un 3B, qualite d'un 32B)
    - Petit ctx + compactage distribue = contexte infini via la DB
    - La vitesse (t/s) doit rester fluide (>8 t/s sur CPU, >30 sur GPU)

    Retourne (ModelTier, ctx_size_recommande)
    """
    # Contexte adaptatif — proportionnel à la puissance du modèle
    # Petit ctx = inference rapide, compactage distribue compense via DB (L3)
    # Le 35B MoE a 32K : compacteur ideal (qualite 32B, vitesse 3B)
    CTX_BY_PARAMS = {
        "0.8B":  4096,   # 4K — assez pour 2-3 echanges avant compactage
        "2B":    4096,
        "4B":    8192,
        "9B":   16384,
        "27B":  32768,
        "35B":  32768,   # MoE — compacteur principal du pool
    }

    # KV cache estime par modele ET par ctx
    # Pour MoE, le KV cache depend des params totaux, pas des actifs
    def _kv_cache(tier: ModelTier, ctx: int) -> float:
        base = {
            "0.5B": 0.1, "0.8B": 0.1,
            "1.5B": 0.4, "2B": 0.5,
            "3B": 0.8, "4B": 1.0,
            "7B": 1.4, "9B": 1.8,
            "14B": 2.0,
            "27B": 3.0, "32B": 3.5, "35B": 3.5,
            "72B": 6.0,
        }
        return base.get(tier.params, 1.0) * (ctx / 4096)

    if has_gpu:
        vram = gpu_vram_gb if gpu_vram_gb > 0 else ram_available_gb
        best_gpu = MODEL_REGISTRY[0]
        for tier in MODEL_REGISTRY:
            if tier.size_gb + 1.0 > vram:
                continue
            if tier.quality_score > best_gpu.quality_score:
                best_gpu = tier
            elif (tier.quality_score == best_gpu.quality_score
                  and tier.model_type == "moe" and best_gpu.model_type == "dense"):
                best_gpu = tier  # preferer MoE a qualite egale
        vram_free = vram - best_gpu.size_gb
        if vram_free >= 4.0:
            gpu_ctx = 32768
        elif vram_free >= 2.5:
            gpu_ctx = 16384
        elif vram_free >= 1.5:
            gpu_ctx = 8192
        else:
            gpu_ctx = 4096
        return best_gpu, gpu_ctx

    # CPU : trouver le meilleur modele qui rentre en RAM
    # Machines a RAM limitee (<5G) : preferer 0.8B pour la vitesse (3x plus rapide que 2B)
    # Le 2B passe en RAM mais laisse trop peu de marge → lenteur et swapping
    tight_ram = ram_available_gb < 5.0
    best = MODEL_REGISTRY[0]
    for tier in MODEL_REGISTRY:
        if cpu_threads < 2:
            break
        ctx = CTX_BY_PARAMS.get(tier.params, 4096)
        kv = _kv_cache(tier, ctx)
        margin = 2.0 if tier.size_gb < 5 else 3.0
        if tight_ram:
            margin = 2.5  # marge plus conservatrice sur petites machines
        total_ram_needed = tier.size_gb + kv + margin
        if ram_available_gb < total_ram_needed:
            continue
        if tier.model_type == "moe":
            real_tps = 50.0 / (tier.active_params_b + 0.5)
        else:
            real_tps = bench_tps if bench_tps else 50.0 / (tier.size_gb + 0.5)
        if real_tps < 3:
            continue
        if tier.quality_score > best.quality_score:
            best = tier

    return best, CTX_BY_PARAMS.get(best.params, 4096)


def recommend_pool_model(workers: list[dict]) -> dict:
    """Analyse la puissance totale du pool et recommande une stratégie.

    Retourne un dict avec :
    - best_individual : meilleur modèle qu'un seul worker peut servir
    - pool_capacity : estimation de la capacité totale du réseau
    - strategy : stratégie recommandée
    - tiers : répartition des workers par tier

    workers = [{"ram_gb": 15.8, "cpu_threads": 8, "bench_tps": 16.0}, ...]
    """
    if not workers:
        return {
            "best_individual": None,
            "pool_capacity_tps": 0,
            "strategy": "no_workers",
            "tiers": [],
            "recommendation": "Aucun worker connecté.",
        }

    # Pour chaque worker, trouver son tier optimal
    worker_tiers = []
    total_tps = 0

    for w in workers:
        tier, _ = recommend_model_for_worker(
            ram_available_gb=w.get("ram_gb", 4),
            cpu_threads=w.get("cpu_threads", 4),
            bench_tps=w.get("bench_tps"),
        )
        tps = w.get("bench_tps") or _estimate_tps(tier, w.get("cpu_threads", 4))
        worker_tiers.append({
            "worker_id": w.get("worker_id", "?"),
            "recommended_model": tier.id,
            "model_name": tier.name,
            "quality_score": tier.quality_score,
            "estimated_tps": round(tps, 1),
        })
        total_tps += tps

    # Trouver le meilleur tier disponible sur au moins 1 worker
    best_quality = max(worker_tiers, key=lambda x: x["quality_score"])

    # Compter les workers par tier
    tier_counts: dict[str, int] = {}
    for wt in worker_tiers:
        tid = wt["recommended_model"]
        tier_counts[tid] = tier_counts.get(tid, 0) + 1

    # Stratégie
    n = len(workers)
    if n == 1:
        strategy = "single"
        rec = f"1 worker → {best_quality['model_name']} ({best_quality['estimated_tps']} t/s)"
    elif n <= 5:
        strategy = "homogeneous"
        rec = (
            f"{n} workers → chacun sert {best_quality['model_name']}. "
            f"Capacité réseau : ~{total_tps:.0f} t/s ({n} requêtes parallèles)"
        )
    else:
        strategy = "tiered"
        rec = (
            f"{n} workers → stratégie tiered. "
            f"Les machines puissantes servent les gros modèles, les autres les petits. "
            f"Capacité réseau : ~{total_tps:.0f} t/s"
        )

    return {
        "best_individual": best_quality,
        "pool_capacity_tps": round(total_tps, 1),
        "total_workers": n,
        "strategy": strategy,
        "tiers": [
            {"model": tid, "workers": count}
            for tid, count in sorted(tier_counts.items())
        ],
        "worker_details": worker_tiers,
        "recommendation": rec,
    }


def get_unlocked_models(pool_total_tps: float, max_worker_ram_gb: float = 0) -> list[dict]:
    """Retourne la liste des modeles debloques selon la puissance du pool.

    Un modele est debloque si :
    1. La puissance totale du pool depasse le seuil requis
    2. Au moins un worker a assez de RAM (ou pipeline parallelism futur)
    """
    result = []
    for m in MODEL_REGISTRY:
        threshold = UNLOCK_THRESHOLDS.get(m.id, 0)
        unlocked = pool_total_tps >= threshold
        can_run = max_worker_ram_gb >= m.ram_required_gb if max_worker_ram_gb > 0 else unlocked
        # Un modele peut etre "debloque" (seuil atteint) mais pas "executable" (pas assez de RAM)
        result.append({
            "id": m.id,
            "name": m.name,
            "params": m.params,
            "size_gb": m.size_gb,
            "ram_required_gb": m.ram_required_gb,
            "quality_score": m.quality_score,
            "unlock_threshold_tps": threshold,
            "unlocked": unlocked,
            "can_run": can_run,
            "status": "active" if (unlocked and can_run) else "unlocked" if unlocked else "locked",
            "missing_tps": max(0, threshold - pool_total_tps) if not unlocked else 0,
            "earn_per_100tok": m.earn_per_100tok,
            "cost_per_request": m.cost_per_request,
        })
    return result


def _estimate_tps(tier: ModelTier, cpu_threads: int) -> float:
    """Estime grossièrement les tokens/sec si pas de benchmark.

    Heuristique basée sur :
    - taille du modèle (plus gros = plus lent)
    - nombre de threads CPU
    - MoE : vitesse basee sur active_params, pas total
    """
    base_tps = 20.0
    thread_factor = min(cpu_threads / 4, 2.0)
    if tier.model_type == "moe" and tier.active_params_b > 0:
        # MoE : la vitesse depend des params actifs par token
        size_factor = 0.5 / (tier.active_params_b + 0.5)
    else:
        size_factor = 0.5 / (tier.size_gb or 0.5)
    return base_tps * thread_factor * size_factor
