"""Where Splicer keeps things, whether run from source or as Splicer.exe."""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# From source: the project root. Built: the folder holding Splicer.exe.
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent

# Read-only files shipped with the app (the icon)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR / "assets"))
ICON = RESOURCE_DIR / "splicer.ico"

DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Splicer"
SETTINGS_FILE = DATA_DIR / "settings.json"
