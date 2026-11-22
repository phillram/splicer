"""Remembering your parts for each video, so reopening it carries on where you
left off. Kept in %APPDATA%\\Splicer\\projects, one small file per video, found
again by the video's path, size and date."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from splicer.paths import DATA_DIR

PROJECTS_DIR = DATA_DIR / "projects"
KEEP = 300  # the most recent videos remembered


def _key(path: Path) -> str:
    stat = path.stat()
    ident = f"{str(path.resolve()).lower()}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()[:20]


def load(path: Path, folder: Path = PROJECTS_DIR) -> dict | None:
    try:
        data = json.loads((folder / f"{_key(path)}.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("parts"), list) else None
    except (OSError, ValueError):
        return None


def save(path: Path, data: dict, folder: Path = PROJECTS_DIR) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{_key(path)}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({"video": str(path), **data}, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def forget(path: Path, folder: Path = PROJECTS_DIR) -> None:
    try:
        (folder / f"{_key(path)}.json").unlink(missing_ok=True)
    except OSError:
        pass
