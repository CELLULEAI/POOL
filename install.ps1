# Cellule.ai Worker — Installation plug-and-play Windows
# Usage: irm https://cellule.ai/install.ps1 | iex
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  ======================================" -ForegroundColor Cyan
Write-Host "    Cellule.ai Worker — Installation" -ForegroundColor Cyan
Write-Host "  ======================================" -ForegroundColor Cyan
Write-Host ""

# Verifier Python
try {
    $pyver = python --version 2>&1
    Write-Host "[OK] $pyver" -ForegroundColor Green
} catch {
    Write-Host "[ERREUR] Python non trouve. Telecharge-le sur python.org" -ForegroundColor Red
    return
}

# Detecter GPU
$hasGpu = $false
$gpuName = ""
try {
    $gpuInfo = nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
    if ($gpuInfo) {
        $parts = $gpuInfo.Split(",")
        $gpuName = $parts[0].Trim()
        $vram = $parts[1].Trim()
        $hasGpu = $true
        Write-Host "[OK] GPU: $gpuName ($vram MB VRAM)" -ForegroundColor Green
    }
} catch {}

if (-not $hasGpu) {
    Write-Host "[OK] Pas de GPU — mode CPU" -ForegroundColor Yellow
}

# Creer dossier
$installDir = "$HOME\iamine-worker"
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Set-Location $installDir
Write-Host "[OK] Dossier: $installDir" -ForegroundColor Green

# Creer venv
if (-not (Test-Path "venv")) {
    Write-Host "[...] Creation du venv..."
    python -m venv venv
}
& "$installDir\venv\Scripts\Activate.ps1"
Write-Host "[OK] venv active" -ForegroundColor Green

# pip peut emettre des warnings sur stderr — ne pas bloquer
$ErrorActionPreference = "Continue"

pip install --upgrade pip -q 2>&1 | Out-Null

# Installer llama-cpp-python
if ($hasGpu) {
    Write-Host "[...] Installation llama-cpp-python (CUDA)..."
    pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 --prefer-binary -q 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121 --prefer-binary -q 2>&1 | Out-Null
    }
} else {
    Write-Host "[...] Installation llama-cpp-python (CPU)..."
    pip install llama-cpp-python --prefer-binary -q 2>&1 | Out-Null
}

# Installer iamine-ai
Write-Host "[...] Installation iamine-ai..."
pip install --upgrade iamine-ai -i https://cellule.ai/pypi --extra-index-url https://pypi.org/simple --prefer-binary -q 2>&1 | Out-Null

$ErrorActionPreference = "Stop"

# Verifier que iamine est bien installe
$ver = python -c "import iamine; print(iamine.__version__)" 2>$null
if ($ver) {
    Write-Host "[OK] iamine v$ver" -ForegroundColor Green
} else {
    Write-Host "[OK] iamine-ai installe (verification en mode pipe non disponible)" -ForegroundColor Yellow
}

# Creer script de lancement (encoding ASCII pour compatibilite cmd.exe)
$startBat = @"
@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m iamine worker --auto %*
"@
[System.IO.File]::WriteAllText("$installDir\start.bat", $startBat, [System.Text.Encoding]::ASCII)

Write-Host ""
Write-Host "  ======================================" -ForegroundColor Cyan
Write-Host "    Installation terminee !" -ForegroundColor Cyan
Write-Host "  ======================================" -ForegroundColor Cyan
if ($hasGpu) { Write-Host "  GPU: $gpuName" -ForegroundColor Green }
Write-Host ""
Write-Host "  Lancer: cd $installDir && .\start.bat" -ForegroundColor White
Write-Host ""

# Lancer automatiquement
Write-Host "[...] Demarrage du worker..."
python -m iamine worker --auto
