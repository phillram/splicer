"""Build Splicer.exe.

    python tools/build_exe.py

Puts Splicer.exe straight into the project folder: one file that runs on any
Windows PC without Python or anything else installed. Everything it uses is
inside it: reading and writing video (PyAV, FFmpeg's libraries) and drag and
drop (tkinterdnd2). Run this again after changing any of the code. It's
gitignored.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # tools/ -> project root
OUT_DIR = ROOT
EXE_PATH = OUT_DIR / "Splicer.exe"
WORK_DIR = ROOT / "build"
ICON = ROOT / "assets" / "splicer.ico"

# Never imported by Splicer, but can get dragged in transitively from the venv
EXCLUDES = ("matplotlib", "numpy", "scipy", "pandas", "PIL", "PyQt5", "PyQt6", "PySide2",
            "PySide6", "pytest", "IPython", "notebook", "sympy")


def _is_running() -> bool:
    """Windows locks a running exe, so the build couldn't replace it."""
    if not EXE_PATH.exists():
        return False
    try:
        with EXE_PATH.open("ab"):
            return False
    except OSError:
        return True


def main() -> int:
    try:
        import av  # noqa: F401
        import PyInstaller  # noqa: F401
        import tkinterdnd2  # noqa: F401
    except ImportError as e:
        print(f"Missing {e.name}. Run:")
        print(f"    {sys.executable} -m pip install pyinstaller -r requirements.txt")
        return 1

    # PyInstaller quietly leaves out a module that doesn't compile.
    # compile() checks without writing __pycache__ folders.
    for source in (ROOT / "splicer").glob("*.py"):
        try:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")
        except SyntaxError as e:
            print(f"Syntax error, fix it first: {e}")
            return 1

    if _is_running():
        print("Splicer.exe is running, so it can't be replaced. Close it and run this again.")
        return 1

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile", "--windowed", "--name", "Splicer",
        "--icon", str(ICON), "--add-data", f"{ICON};.",
        "--paths", str(ROOT),
        # The tkdnd library drag and drop runs on, and FFmpeg's DLLs inside PyAV
        "--collect-all", "tkinterdnd2",
        "--collect-binaries", "av",
        "--distpath", str(OUT_DIR), "--workpath", str(WORK_DIR), "--specpath", str(WORK_DIR),
    ]
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / "splicer" / "__main__.py"))

    print("Building Splicer.exe. This takes a minute or two.\n")
    started = time.time()
    # No __pycache__ folders left in the project by the analysis
    env = os.environ | {"PYTHONDONTWRITEBYTECODE": "1"}
    if subprocess.run(command, cwd=ROOT, env=env).returncode != 0:
        print("\nBuild failed: see the PyInstaller output above.")
        return 1

    shutil.rmtree(WORK_DIR, ignore_errors=True)  # PyInstaller's scratch; keep the folder flat

    print(f"\nBuilt {EXE_PATH} ({EXE_PATH.stat().st_size / 1e6:.0f} MB) "
          f"in {time.time() - started:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
