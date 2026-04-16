# Plan B — Post-install source patch for llama-cpp-python
#
# Rationale : PyInstaller fails to bundle llama_cpp correctly because
# the package folder llama_cpp/ contains a file llama_cpp.py with the
# SAME name as the parent package. At runtime PyInstaller treats
# llama_cpp.py as a flat module, breaking imports like
# llama_cpp._ctypes_extensions.
#
# Plan B renames the conflicting file to _native_bindings.py and updates
# every reference to it within the package. After this patch there is
# NO more name collision, so PyInstaller bundles correctly without any
# hook or spec file tricks.

$llamaPath = python -c "import importlib.util, os; spec = importlib.util.find_spec('llama_cpp'); print(os.path.dirname(spec.origin))"
Write-Host "Patching llama_cpp at: $llamaPath"

$oldFile = Join-Path $llamaPath "llama_cpp.py"
$newFile = Join-Path $llamaPath "_native_bindings.py"

if (-not (Test-Path $oldFile)) {
    Write-Error "llama_cpp/llama_cpp.py not found at $oldFile"
    exit 1
}

Write-Host "  Renaming llama_cpp.py -> _native_bindings.py"
Rename-Item -Path $oldFile -NewName "_native_bindings.py"

# Update __init__.py : relative import
$initPath = Join-Path $llamaPath "__init__.py"
$initContent = Get-Content $initPath -Raw
$initContent = $initContent -replace 'from \.llama_cpp import', 'from ._native_bindings import'
Set-Content -Path $initPath -Value $initContent -NoNewline
Write-Host "  Patched __init__.py"

# Update any other .py in the package that imports from the old name
$pyFiles = Get-ChildItem -Path $llamaPath -Filter *.py -Recurse
foreach ($f in $pyFiles) {
    $content = Get-Content $f.FullName -Raw
    $original = $content
    # Absolute imports : from llama_cpp.llama_cpp import ... or import llama_cpp.llama_cpp
    $content = $content -replace 'from llama_cpp\.llama_cpp import', 'from llama_cpp._native_bindings import'
    $content = $content -replace 'import llama_cpp\.llama_cpp', 'import llama_cpp._native_bindings'
    # Relative imports inside submodules
    $content = $content -replace 'from \.llama_cpp import', 'from ._native_bindings import'
    if ($content -ne $original) {
        Set-Content -Path $f.FullName -Value $content -NoNewline
        Write-Host "  Patched $($f.Name)"
    }
}

# Nuke bytecode caches so Python re-compiles from patched sources
Get-ChildItem -Path $llamaPath -Filter __pycache__ -Recurse -Directory | Remove-Item -Recurse -Force

# Verify : Python should import llama_cpp as package correctly.
# CUDA builds need cudart/cublas DLLs reachable when llama.dll loads.
# Python 3.8+ on Windows DOES NOT consult $env:PATH for ctypes.CDLL — it
# requires os.add_dll_directory(). We therefore patch both the PowerShell
# PATH (for subprocesses) AND the Python DLL search path explicitly.
$cudaBin = $null
if ($env:CUDA_PATH) {
    $cudaBin = Join-Path $env:CUDA_PATH "bin"
    if (Test-Path $cudaBin) {
        $env:Path = "$cudaBin;$env:Path"
        Write-Host "CUDA bin prepended to PATH: $cudaBin"
    } else {
        Write-Host "CUDA_PATH set but bin dir missing: $cudaBin"
        $cudaBin = $null
    }
} else {
    Write-Host "CUDA_PATH not set — assuming CPU/proxy build"
}

$verifyScript = @"
import os, sys
cuda_bin = os.environ.get('CUDA_PATH')
if cuda_bin:
    cuda_bin_path = os.path.join(cuda_bin, 'bin')
    if os.path.isdir(cuda_bin_path):
        os.add_dll_directory(cuda_bin_path)
        print(f'os.add_dll_directory({cuda_bin_path})')
import llama_cpp
import llama_cpp._ctypes_extensions
import llama_cpp._native_bindings
print('llama_cpp as package: OK')
print('llama_cpp._native_bindings loaded')
print('llama_cpp._ctypes_extensions loaded')
"@
$verifyScript | python -
if ($LASTEXITCODE -ne 0) {
    Write-Error "plan B verify failed"
    exit 1
}
Write-Host "Plan B patch applied successfully"
