#!/bin/bash
# Cellule.ai Worker — Installation plug-and-play pour Linux x86_64
# Usage: curl -sL https://cellule.ai/install.sh | bash
set -e

POOL_URL="https://cellule.ai"
PYPI_URL="$POOL_URL/pypi"
MIN_PYTHON="3.10"

echo "========================================"
echo "  Cellule.ai Worker — Installation"
echo "========================================"
echo ""

# Vérifier Python
if ! command -v python3 &>/dev/null; then
    echo "[ERREUR] Python 3 non trouvé. Installe-le avec:"
    echo "  sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[OK] Python $PY_VER détecté"

PY_OK=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)")
if [ "$PY_OK" != "1" ]; then
    echo "[ERREUR] Python >= $MIN_PYTHON requis (trouvé: $PY_VER)"
    exit 1
fi

# Détecter le GPU
HAS_GPU=0
GPU_NAME=""
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    if [ -n "$GPU_NAME" ]; then
        HAS_GPU=1
        GPU_VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
        echo "[OK] GPU détecté: $GPU_NAME (${GPU_VRAM} MB VRAM)"
    fi
fi

if [ "$HAS_GPU" = "0" ]; then
    echo "[OK] Pas de GPU — mode CPU"
else
    # Vérifier les deps CUDA runtime
    if ! ldconfig -p 2>/dev/null | grep -q libcudart; then
        echo "[...] Installation des deps CUDA runtime..."
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y cuda-cudart-12-6 libcublas-12-6 2>/dev/null \
                || sudo apt-get install -y cuda-cudart-12-4 libcublas-12-4 2>/dev/null \
                || echo "[WARN] Deps CUDA non trouvees — pip tentera quand meme"
        fi
    fi
fi

# Créer le dossier de travail
INSTALL_DIR="$HOME/iamine-worker"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo "[OK] Dossier: $INSTALL_DIR"

# Créer un venv
if [ ! -d "venv" ]; then
    echo "[...] Création du venv..."
    python3 -m venv venv
fi
source venv/bin/activate
echo "[OK] venv activé"

pip install --upgrade pip -q

# Installer llama-cpp-python avec le bon backend
if [ "$HAS_GPU" = "1" ]; then
    # Détecter la version CUDA
    CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9]+' | head -1)
    echo "[OK] CUDA version majeure: ${CUDA_VER:-inconnue}"

    if [ "${CUDA_VER:-0}" -ge 13 ]; then
        # CUDA 13+ : pas de wheel précompilé, compiler nativement
        echo "[...] Installation llama-cpp-python (compilation native CUDA $CUDA_VER)..."
        CMAKE_ARGS="-DGGML_CUDA=on" pip install --force-reinstall --no-cache-dir llama-cpp-python -q 2>&1 | tail -3
        echo "[OK] llama-cpp-python CUDA natif"
    else
        # CUDA 12 ou 11 : essayer les wheels précompilés
        echo "[...] Installation llama-cpp-python (wheel CUDA précompilé)..."
        pip install llama-cpp-python \
            --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
            --prefer-binary -q 2>/dev/null \
        && echo "[OK] llama-cpp-python CUDA installé" \
        || {
            echo "[WARN] Wheel cu124 non trouvé, tentative cu121..."
            pip install llama-cpp-python \
                --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 \
                --prefer-binary -q 2>/dev/null \
            && echo "[OK] llama-cpp-python CUDA installé" \
            || {
                echo "[...] Compilation native CUDA..."
                CMAKE_ARGS="-DGGML_CUDA=on" pip install --force-reinstall --no-cache-dir llama-cpp-python -q 2>&1 | tail -3
            }
        }
    fi
else
    echo "[...] Installation llama-cpp-python (CPU)..."
    pip install llama-cpp-python \
        --extra-index-url https://pypi.org/simple/ \
        --prefer-binary -q 2>/dev/null \
    || echo "[WARN] llama-cpp-python sera compilé au premier lancement"
fi

# Installer iamine-ai
echo "[...] Installation de iamine-ai..."
pip install --upgrade iamine-ai \
    --index-url "$PYPI_URL" \
    --extra-index-url https://pypi.org/simple/ \
    --prefer-binary -q

echo "[OK] iamine-ai installé"

# Créer un script de lancement
cat > start.sh << 'SCRIPT'
#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
python -m iamine worker --auto "$@"
SCRIPT
chmod +x start.sh

echo ""
echo "========================================"
echo "  Installation terminée !"
echo "========================================"
if [ "$HAS_GPU" = "1" ]; then
    echo "  GPU: $GPU_NAME"
fi
echo ""
echo "  Lancer le worker :"
echo "    cd $INSTALL_DIR && ./start.sh"
echo ""

# Lancer automatiquement le worker
echo "[...] Démarrage du worker..."
exec ./start.sh
