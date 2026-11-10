from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata
from pathlib import Path

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], ["idol.main"]
for name in ["fastembed", "lancedb", "jieba", "langgraph", "langgraph.checkpoint.sqlite", "tzdata"]:
    data, binary, hidden = collect_all(name)
    datas += data
    binaries += binary
    hiddenimports += hidden
hiddenimports += collect_submodules("uvicorn")
for name in ["langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite", "langchain-core", "fastembed", "lancedb"]:
    datas += copy_metadata(name)
datas += [(str(root / "resources"), "resources")]
a = Analysis([str(root / "freeze_entry.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[],
             excludes=["tkinter", "matplotlib", "torch", "tensorflow", "IPython", "notebook"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="idol-backend", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="idol-backend")
