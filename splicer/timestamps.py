"""Timestamp lists, as in video descriptions:

    00:00 Saria's Song
    4:38 - Zelda's Lullaby
    1:02:03.5 The Last One
    Song Of Storms (13:07)

Reading them into (time, title) pairs, and writing parts back out that way.
"""

from __future__ import annotations

import re

_TIME = r"(?<![\d:.])(\d{1,2}:)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?(?![\d:])"
_TIME_RE = re.compile(_TIME)
# Bits around a time or title that aren't part of the title
_EDGES = " \t-–—|:•*·>»).]}[({#"
_NUMBERED = re.compile(r"^\d{1,3}[.)]\s+")


def _seconds(m: re.Match) -> float:
    hours = int(m.group(1)[:-1]) if m.group(1) else 0
    minutes, seconds = int(m.group(2)), int(m.group(3))
    fraction = float("0." + m.group(4)) if m.group(4) else 0.0
    return hours * 3600 + minutes * 60 + seconds + fraction


def parse(text: str) -> list[tuple[float, str]]:
    """(seconds, title) for every line with a time in it, in time order. A line
    with two times ("4:38 - 6:44 Title") is taken by its first."""
    found = []
    for line in text.splitlines():
        m = _TIME_RE.search(line)
        if not m:
            continue
        minutes, seconds = int(m.group(2)), int(m.group(3))
        if seconds >= 60 or (m.group(1) and minutes >= 60):
            continue
        title = (line[:m.start()] + " " + line[m.end():])
        title = _TIME_RE.sub(" ", title)  # a second time on the line (an end time)
        title = _NUMBERED.sub("", title.strip())
        title = re.sub(r"\s+", " ", title).strip(_EDGES).strip()
        found.append((_seconds(m), title))
    found.sort(key=lambda f: f[0])
    # The same time twice: keep the first title
    unique, seen = [], set()
    for t, title in found:
        if round(t, 3) not in seen:
            seen.add(round(t, 3))
            unique.append((t, title))
    return unique


def best_source(tags: dict) -> str:
    """The file's own detail most likely to hold a timestamp list (a YouTube
    download keeps the description), or ""."""
    best, count = "", 1  # one lone time isn't a list
    for key, value in tags.items():
        if key.lower() in ("description", "synopsis", "comment", "comments", "lyrics",
                           "summary", "purl") or len(str(value)) > 40:
            n = len(parse(str(value)))
            if n > count:
                best, count = str(value), n
    return best


def stamp(t: float) -> str:
    """0:00, 4:38 or 1:02:03: whole seconds, as descriptions write them."""
    s = int(t + 1e-6)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"
