# PyInstaller hook for llama_cpp
# Fixes : "ModuleNotFoundError: llama_cpp._ctypes_extensions; llama_cpp is not a package"
# Root cause : llama-cpp-python has both llama_cpp/ (package dir) AND
# llama_cpp/llama_cpp.py (module file with same name). PyInstaller default
# collection mode picks the module instead of the package.
# The module_collection_mode="pyz+py" tells PyInstaller to collect BOTH the
# bytecode AND the source, preserving the package/dir structure at runtime.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all("llama_cpp")
hiddenimports += collect_submodules("llama_cpp")
module_collection_mode = {"llama_cpp": "pyz+py"}
