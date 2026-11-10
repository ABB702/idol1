"""Build the Python sidecar using the current virtual environment."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
subprocess.run(
    [sys.executable, "-m", "PyInstaller", "--noconfirm", str(root / "idol-backend.spec")],
    cwd=root,
    check=True,
)
