"""Chargement et validation de la configuration worker."""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import uuid
from dataclasses import dataclass, field

from . import __version__
from pathlib import Path

# Noms de héros / références pop culture pour les worker IDs
_HEROES = [
    "Phoenix", "Titan", "Nebula", "Orion", "Valkyrie", "Atlas", "Nova",
    "Sphinx", "Athena", "Odin", "Loki", "Thor", "Freya", "Merlin",
    "Gandalf", "Morpheus", "Neo", "Trinity", "Cypher", "Blade",
    "Storm", "Rogue", "Wolverine", "Cyclops", "Magneto", "Mystique",
    "Panther", "Hawkeye", "Falcon", "Vision", "Ultron", "Jarvis",
    "Cortana", "Oracle", "Sentinel", "Spectre", "Phantom", "Shadow",
    "Viper", "Cobra", "Raptor", "Griffin", "Hydra", "Kraken",
    "Zenith", "Apex", "Vertex", "Pulse", "Blaze", "Frost",
    "Eclipse", "Solaris", "Cosmos", "Quantum", "Photon", "Neutron",
    "Spartan", "Gladiator", "Ronin", "Samurai", "Shogun", "Sensei",
    "Druid", "Paladin", "Warlock", "Sorcerer", "Templar", "Crusader",
]


def _generate_hero_id() -> str:
    """Génère un worker ID unique basé sur un nom de héros + hash court."""
    machine_hash = hashlib.md5(
        f"{socket.gethostname()}-{uuid.getnode()}".encode()
    ).hexdigest()
    # Choisir un héros de manière déterministe par machine
    index = int(machine_hash[:8], 16) % len(_HEROES)
    hero = _HEROES[index]
    suffix = machine_hash[:4]
    return f"{hero}-{suffix}"


@dataclass
class PoolConfig:
    url: str = "ws://localhost:8080/ws"
    worker_id: str = ""
    token: str | None = None

    def __post_init__(self):
        if not self.worker_id:
            self.worker_id = _generate_hero_id()


@dataclass
class ModelConfig:
    path: str = ""
    ctx_size: int = 2048
    threads: int = 4
    gpu_layers: int = 0


@dataclass
class LimitsConfig:
    max_concurrent: int = 2
    max_tokens: int = 512
    memory_limit: str = "8G"


@dataclass
class WorkerConfig:
    pool: PoolConfig = field(default_factory=PoolConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    priority: int = 2
    yield_cpu: bool = True
    pause_on_battery: bool = True
    pause_on_active: bool = False
    log_file: str | None = "iamine.log"
    verbose: int = 1

    @staticmethod
    def from_file(path: str | Path) -> WorkerConfig:
        """Charge la config depuis un fichier JSON."""
        with open(path) as f:
            raw = json.load(f)

        pool_raw = raw.get("pool", {})
        model_raw = raw.get("model", {})
        limits_raw = raw.get("limits", {})

        return WorkerConfig(
            pool=PoolConfig(
                url=pool_raw.get("url", "ws://localhost:8080/ws"),
                worker_id=pool_raw.get("worker-id") or "",
                token=pool_raw.get("token"),
            ),
            model=ModelConfig(
                path=model_raw.get("path", ""),
                ctx_size=model_raw.get("ctx-size", 2048),
                threads=model_raw.get("threads", 4),
                gpu_layers=model_raw.get("gpu-layers", 0),
            ),
            limits=LimitsConfig(
                max_concurrent=limits_raw.get("max-concurrent", 2),
                max_tokens=limits_raw.get("max-tokens", 512),
                memory_limit=limits_raw.get("memory-limit", "8G"),
            ),
            priority=raw.get("priority", 2),
            yield_cpu=raw.get("yield", True),
            pause_on_battery=raw.get("pause-on-battery", True),
            pause_on_active=raw.get("pause-on-active", False),
            log_file=raw.get("log-file"),
            verbose=raw.get("verbose", 1),
        )

    def system_info(self) -> dict:
        """Infos système du worker, envoyées au pool à la connexion."""
        import psutil

        mem = psutil.virtual_memory()
        gpu_info = _detect_gpu()
        return {
            "worker_id": self.pool.worker_id,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "cpu": _detect_cpu_model(),
            "cpu_count": psutil.cpu_count(logical=False),
            "cpu_threads": psutil.cpu_count(logical=True),
            "ram_total_gb": round(mem.total / (1024**3), 1),
            "ram_available_gb": round(mem.available / (1024**3), 1),
            "model_path": self.model.path,
            "ctx_size": self.model.ctx_size,
            "gpu_layers": self.model.gpu_layers,
            "gpu": gpu_info.get("name", ""),
            "gpu_vram_gb": gpu_info.get("vram_gb", 0),
            "has_gpu": gpu_info.get("available", False),
            "max_concurrent": self.limits.max_concurrent,
            "max_tokens": self.limits.max_tokens,
            "version": __version__,
        }


def _detect_cpu_model() -> str:
    """Détecte le nom complet du CPU."""
    # Linux : /proc/cpuinfo
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    return line.split(":")[1].strip()
    except (FileNotFoundError, Exception):
        pass
    # Windows : platform.processor() ou wmic
    try:
        import subprocess
        r = subprocess.run(["wmic", "cpu", "get", "name"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip() and l.strip() != "Name"]
            if lines:
                return lines[0]
    except (FileNotFoundError, Exception):
        pass
    return platform.processor() or platform.machine()


def _detect_gpu() -> dict:
    """Détecte le GPU disponible (CUDA/ROCm/Metal)."""
    import subprocess
    # NVIDIA (CUDA) — chercher nvidia-smi dans le PATH et les chemins Windows standards
    nvidia_smi_paths = ["nvidia-smi"]
    if platform.system() == "Windows":
        import glob
        # Windows : nvidia-smi souvent cache dans DriverStore
        for p in glob.glob(r"C:\Windows\System32\DriverStore\FileRepository\nv*\nvidia-smi.exe"):
            nvidia_smi_paths.append(p)
        # Aussi dans Program Files
        nvidia_smi_paths.append(r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe")
    for smi in nvidia_smi_paths:
        try:
            result = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                name = parts[0].strip()
                vram_mb = int(parts[1].strip()) if len(parts) > 1 else 0
                return {"available": True, "name": name, "vram_gb": round(vram_mb / 1024, 1)}
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue
    # AMD (ROCm) — /dev/kfd + sysfs (fiable) puis rocm-smi pour le nom
    import pathlib
    if pathlib.Path("/dev/kfd").exists() and pathlib.Path("/dev/dri/renderD128").exists():
        vram_gb = 0
        name = "AMD GPU"
        try:
            # VRAM totale via sysfs (toujours correct)
            for mem_file in pathlib.Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
                vram_bytes = int(mem_file.read_text().strip())
                gtt_file = mem_file.parent / "mem_info_gtt_total"
                gtt_bytes = int(gtt_file.read_text().strip()) if gtt_file.exists() else 0
                total_bytes = vram_bytes + gtt_bytes
                total_gb = total_bytes / (1024**3)
                # iGPU AMD : petite VRAM partagee (< 8 Go total) = pas un vrai GPU
                # Vrai GPU : Z2 Radeon 8060S = 88 Go, RTX = 6-24 Go
                if total_gb < 8:
                    return {"available": False, "name": "", "vram_gb": 0}
                vram_gb = round(total_gb, 1)
                break
            # Nom du GPU via rocm-smi ou sysfs
            try:
                result = subprocess.run(
                    ["/opt/rocm/bin/rocm-smi", "--showproductname"],
                    capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.split("\n"):
                    if "Card" in line or "GPU" in line:
                        # ex: "GPU[0]		: Card Series:		gfx1151"
                        if ":" in line:
                            name = line.split(":")[-1].strip()
            except Exception:
                pass
        except Exception:
            pass
        if vram_gb > 0:
            return {"available": True, "name": name, "vram_gb": vram_gb}
    # Fallback ancien : check /dev/dri/renderD128 sans /dev/kfd
    if pathlib.Path("/dev/dri/renderD128").exists():
        vram_gb = 0
        name = "AMD GPU"
        try:
            for mem_file in pathlib.Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
                vram_bytes = int(mem_file.read_text().strip())
                vram_gb = round(vram_bytes / (1024**3), 1)
            for name_file in pathlib.Path("/sys/class/drm").glob("card*/device/product_name"):
                name = name_file.read_text().strip()
        except Exception:
            pass
        if vram_gb > 0:
            return {"available": True, "name": name, "vram_gb": vram_gb}
    # Apple Silicon (Metal) — memoire unifiee, toute la RAM est VRAM
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        import psutil as _ps
        ram_gb = round(_ps.virtual_memory().total / (1024**3), 1)
        try:
            import subprocess as _sp
            r = _sp.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                        capture_output=True, text=True, timeout=5)
            chip_name = r.stdout.strip() if r.returncode == 0 else "Apple Silicon"
        except Exception:
            chip_name = "Apple Silicon"
        return {"available": True, "name": f"{chip_name} (Metal)", "vram_gb": ram_gb}
    return {"available": False, "name": "", "vram_gb": 0}
