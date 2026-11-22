"""Text subtitles, read so the player can show them: SRT, ASS/SSA, MP4 text and
WebVTT. Picture subtitles (Blu-ray PGS, DVD VobSub) still go into the parts
untouched, but the player can't show them."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

_ASS_TAGS = re.compile(r"\{[^}]*\}")
_HTML_TAGS = re.compile(r"</?[a-zA-Z][^>]*>")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def packet_text(codec: str, data: bytes) -> str:
    """The words in one subtitle packet, without styling."""
    if codec == "mov_text":
        if len(data) < 2:
            return ""
        length = int.from_bytes(data[:2], "big")
        data = data[2:2 + length]
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if codec in ("ass", "ssa"):
        # Matroska keeps "ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text";
        # a full "Dialogue:" line has one more field at the front
        if text.startswith("Dialogue:"):
            text = text.split(":", 1)[1].split(",", 9)[-1]
        else:
            text = text.split(",", 8)[-1]
        if "\\p1" in text or "\\p2" in text:  # a drawing, not words
            return ""
        text = _ASS_TAGS.sub("", text)
        text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    else:
        text = _HTML_TAGS.sub("", text)
        text = _ASS_TAGS.sub("", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def tidy(cues: list[Cue]) -> list[Cue]:
    """Sorted, with any cue that has no end of its own lasting until the next."""
    cues = sorted((c for c in cues if c.text), key=lambda c: c.start)
    for i, cue in enumerate(cues):
        if cue.end <= cue.start:
            nxt = cues[i + 1].start if i + 1 < len(cues) else cue.start + 4.0
            cue.end = min(cue.start + 4.0, max(nxt, cue.start + 0.5))
    return cues


def longest(cues: list[Cue]) -> float:
    """How long the longest line stays on screen."""
    return max((c.end - c.start for c in cues), default=0.0)


def active(cues: list[Cue], starts: list[float], t: float,
           longest_cue: float | None = None) -> str:
    """Everything on screen at t, oldest first. starts: [c.start for c in cues];
    longest_cue: longest(cues), worked out once, as it decides how far back to look."""
    if longest_cue is None:
        longest_cue = longest(cues)
    i = bisect.bisect_right(starts, t)
    # Lines can overlap: anything that started within the longest line's time of t
    j = bisect.bisect_left(starts, t - longest_cue, 0, i)
    return "\n".join(c.text for c in cues[j:i] if c.start <= t < c.end)
