"""Everything Splicer does to a file: read what's in it, find its keyframes, and
copy a stretch of it into a new file.

Nothing is ever decoded and re-encoded on the way to a new file: every packet of
picture, sound and subtitles is copied over byte for byte. That's why a cut can
only start on a keyframe, the one kind of frame a video can begin with: a time
asked for is moved to the nearest one.

All in-process, with PyAV (FFmpeg's libraries, built into Splicer). Times here
are seconds from the start of the video as a player shows it.
"""

from __future__ import annotations

import bisect
from array import array
import os
import re
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av


def _quiet_svt() -> None:
    """SVT-AV1 (re-encoding near cuts) prints a banner each time unless SVT_LOG says
    otherwise, read from the older C runtime's own copy of the environment."""
    try:
        import ctypes
        ctypes.cdll.msvcrt._putenv(b"SVT_LOG=1")  # errors only
    except (OSError, AttributeError):
        pass


_quiet_svt()

from splicer.subtitles import TEXT_CODECS, Cue, packet_text, tidy

# A hair under a millisecond: two times this close are the same moment
EPS = 0.0005
SUBTITLE_LOOKBACK = 10.0  # seconds

VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".mov", ".webm", ".avi", ".ts", ".m2ts", ".mts",
              ".mpg", ".mpeg", ".wmv", ".flv", ".3gp", ".mka", ".m4a", ".m4b", ".ogg",
              ".opus", ".flac", ".mp3")

# Per-stream tags that describe the whole original and would be wrong on a part
STALE_TAGS = re.compile(r"^(DURATION|NUMBER_OF_FRAMES|NUMBER_OF_BYTES|BPS|_STATISTICS_.*)(-.*)?$",
                        re.IGNORECASE)

KIND_LABEL = {"video": "Video", "audio": "Audio", "subtitle": "Subtitles",
              "cover": "Cover art", "attachment": "Attachment", "data": "Data"}


class SpliceError(Exception):
    """Something that stops a file being read or a part being written."""


class Cancelled(Exception):
    pass


@dataclass
class Track:
    index: int
    kind: str                     # video, audio, subtitle, cover, attachment, data
    codec: str
    language: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    detail: str = ""              # 3840×2160, 2 channels, a font's file name…
    chosen: bool = False          # what Splicer picks before you change anything
    text: bool = False            # subtitles the player can show

    @property
    def label(self) -> str:
        parts = [KIND_LABEL[self.kind]]
        if self.language and self.language != "und":
            parts.append(language_name(self.language))
        if self.title:
            parts.append(f"“{self.title}”")
        parts.append(self.codec)
        if self.detail:
            parts.append(self.detail)
        flags = [f for f, on in (("default", self.default), ("forced", self.forced)) if on]
        text = "  ·  ".join(parts)
        return f"{text}  ({', '.join(flags)})" if flags else text


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class Info:
    path: Path
    duration: float
    origin: float                 # the file's own timestamp for "0:00"
    tracks: list[Track]
    chapters: list[Chapter]
    keyframes: list[float] = field(default_factory=list)  # empty: cut anywhere
    has_video: bool = False
    frames: list[float] = field(default_factory=list)  # every picture's time, in order
    video_delay: float = 0.0       # how far decoding runs ahead of showing (B-frames)
    tags: dict = field(default_factory=dict)                  # the file's own details
    subtitles: dict[int, list[Cue]] = field(default_factory=dict)  # text tracks' lines

    @property
    def title(self) -> str:
        return self.path.stem


# --- Reading a file ----------------------------------------------------------------

LANGUAGES = {
    "eng": "English", "en": "English", "jpn": "Japanese", "ja": "Japanese", "fre": "French",
    "fra": "French", "fr": "French", "ger": "German", "deu": "German", "de": "German",
    "spa": "Spanish", "es": "Spanish", "ita": "Italian", "it": "Italian", "por": "Portuguese",
    "pt": "Portuguese", "rus": "Russian", "ru": "Russian", "chi": "Chinese", "zho": "Chinese",
    "zh": "Chinese", "kor": "Korean", "ko": "Korean", "dut": "Dutch", "nld": "Dutch",
    "nl": "Dutch", "swe": "Swedish", "sv": "Swedish", "nor": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish", "fin": "Finnish", "fi": "Finnish", "pol": "Polish",
    "pl": "Polish", "tur": "Turkish", "tr": "Turkish", "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi", "heb": "Hebrew", "he": "Hebrew", "gre": "Greek",
    "ell": "Greek", "el": "Greek", "cze": "Czech", "ces": "Czech", "cs": "Czech",
    "hun": "Hungarian", "hu": "Hungarian", "tha": "Thai", "th": "Thai", "vie": "Vietnamese",
    "vi": "Vietnamese", "ukr": "Ukrainian", "uk": "Ukrainian", "ind": "Indonesian",
    "id": "Indonesian", "may": "Malay", "msa": "Malay", "rum": "Romanian", "ron": "Romanian",
    "ro": "Romanian", "bul": "Bulgarian", "hrv": "Croatian", "srp": "Serbian",
    "cat": "Catalan", "baq": "Basque", "eus": "Basque", "glg": "Galician", "ice": "Icelandic",
    "isl": "Icelandic", "per": "Persian", "fas": "Persian", "fil": "Filipino", "tgl": "Tagalog",
}


def language_name(code: str) -> str:
    return LANGUAGES.get(code.lower(), code)


def _open(path: Path):
    try:
        return av.open(str(path))
    except (av.FFmpegError, OSError) as e:
        raise SpliceError(f"Can't open it: {e}") from None


def _kind(stream) -> str:
    if stream.type == "video":
        return "cover" if stream.disposition & av.stream.Disposition.attached_pic else "video"
    if stream.type in ("audio", "subtitle", "attachment"):
        return stream.type
    return "data"


def _track(stream) -> Track:
    kind = _kind(stream)
    meta = {k.lower(): v for k, v in stream.metadata.items()}
    disposition = stream.disposition
    codec, detail = "", ""
    if kind == "attachment":
        codec = meta.get("mimetype", "file")
        detail = meta.get("filename", "")
    else:
        try:
            ctx = stream.codec_context
            codec = codec_name(stream)
        except Exception:
            codec = ""
        codec = codec.upper() if codec in ("aac", "ac3", "eac3", "dts", "flac", "mp3", "alac") \
            else {"libdav1d": "AV1", "av1": "AV1", "h264": "H.264", "hevc": "HEVC",
                  "vp9": "VP9", "vp8": "VP8", "opus": "Opus", "vorbis": "Vorbis",
                  "subrip": "SRT", "srt": "SRT", "ass": "ASS", "hdmv_pgs_subtitle": "PGS",
                  "dvd_subtitle": "VobSub", "mov_text": "MP4 text", "webvtt": "WebVTT",
                  "mjpeg": "JPEG", "png": "PNG", "truehd": "TrueHD", "dts": "DTS", "mp2": "MP2",
                  "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4"}.get(codec, codec)
        if kind == "video":
            ctx = stream.codec_context
            rate = stream.average_rate or stream.guessed_rate
            detail = f"{ctx.width}×{ctx.height}"
            if rate:
                detail += f"  {float(rate):.3g} fps"
        elif kind == "audio":
            channels = stream.codec_context.channels
            detail = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(channels,
                                                                     f"{channels} channels")
        elif kind == "cover":
            detail = meta.get("filename", "")
    return Track(
        index=stream.index, kind=kind, codec=codec,
        language=meta.get("language", ""),
        title=meta.get("title", ""),
        default=bool(disposition & av.stream.Disposition.default),
        forced=bool(disposition & av.stream.Disposition.forced),
        detail=detail,
        text=kind == "subtitle" and codec_name(stream) in TEXT_CODECS,
    )


def codec_name(stream) -> str:
    """The codec's own name (mp3, dts, av1), not the decoder's (mp3float, dca,
    libdav1d)."""
    try:
        codec = stream.codec_context.codec
        return getattr(codec, "canonical_name", "") or codec.name or ""
    except Exception:
        return ""


def choose_defaults(tracks: list[Track]) -> None:
    """What a player would show: the default picture and sound, default or forced
    subtitles, and everything the file carries along (cover art, fonts)."""
    for kind in ("video", "audio"):
        group = [t for t in tracks if t.kind == kind]
        marked = [t for t in group if t.default]
        for t in marked or group[:1]:
            t.chosen = True
    for t in tracks:
        if t.kind == "subtitle" and (t.default or t.forced):
            t.chosen = True
        if t.kind in ("cover", "attachment"):
            t.chosen = True


def _chapters(container, origin: float, duration: float) -> list[Chapter]:
    found = []
    for n, ch in enumerate(container.chapters(), 1):
        tb = ch["time_base"]
        start = float(ch["start"] * tb) - origin
        end = float(ch["end"] * tb) - origin
        title = (ch.get("metadata") or {}).get("title", "").strip() or f"Chapter {n}"
        start = max(0.0, start)
        end = min(duration, end) if end > start else duration
        if end - start > EPS:
            found.append(Chapter(start, end, title))
    found.sort(key=lambda c: c.start)
    return found


def probe(path: Path, progress: Callable[[float], None] | None = None,
          cancel: threading.Event | None = None) -> Info:
    """What's in the file: its tracks, chapters and where every keyframe is."""
    path = Path(path)
    with _open(path) as container:
        origin = (container.start_time or 0) / av.time_base
        if container.duration:
            duration = container.duration / av.time_base
        else:
            duration = max((float(s.duration * s.time_base) for s in container.streams
                            if s.duration), default=0.0)
        tracks = [_track(s) for s in container.streams]
        choose_defaults(tracks)
        chapters = _chapters(container, origin, duration)
        info = Info(path, duration, origin, tracks, chapters,
                    tags={k: v for k, v in container.metadata.items() if v})

        main = [s for s in container.streams.video
                if not s.disposition & av.stream.Disposition.attached_pic]
        if not main:
            return info  # sound only: a cut can go anywhere
        info.has_video = True
        video = main[0]
        size = max(path.stat().st_size, 1)
        keys = set()
        times: list[float] = []
        delay = 0.0
        # Text subtitles come along in the same pass, for the player to show
        texts = {s.index: codec_name(s) for s in container.streams.subtitles
                 if codec_name(s) in TEXT_CODECS}
        cues: dict[int, list[Cue]] = {i: [] for i in texts}
        streams = [video] + [container.streams[i] for i in texts]
        try:
            for n, packet in enumerate(container.demux(*streams)):
                index = packet.stream.index
                if index in texts:
                    if packet.size and packet.pts is not None:
                        start = float(packet.pts * packet.time_base) - origin
                        end = start + float((packet.duration or 0) * packet.time_base)
                        cues[index].append(Cue(start, end, packet_text(texts[index],
                                                                       bytes(packet))))
                    continue
                if packet.pts is not None:
                    t = float(packet.pts * packet.time_base) - origin
                    times.append(t)
                    if packet.dts is not None:
                        delay = max(delay, float((packet.pts - packet.dts) * packet.time_base))
                if packet.is_keyframe and packet.pts is not None:
                    keys.add(round(float(packet.pts * packet.time_base) - origin, 6))
                if n % 500 == 0:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    if progress and packet.pos is not None and packet.pos >= 0:
                        progress(min(packet.pos / size, 1.0))
        except av.FFmpegError as e:
            if not keys:
                raise SpliceError(f"Can't read it: {e}") from None
        info.keyframes = sorted(max(k, 0.0) for k in keys)
        info.frames = sorted({round(max(t, 0.0), 6) for t in times})
        info.video_delay = min(delay, 2.0)
        info.subtitles = {i: tidy(c) for i, c in cues.items()}
        if not info.keyframes:
            info.keyframes = [0.0]
        if not duration:
            info.duration = info.keyframes[-1]
    return info


# --- Keyframes ---------------------------------------------------------------------

def snap(info: Info, t: float) -> float:
    """Where a cut asked for at t really happens: the nearest keyframe. The very
    start and end of the video are always allowed."""
    if t <= EPS:
        return 0.0
    if t >= info.duration - EPS:
        return info.duration
    keys = info.keyframes
    if not keys:
        return t
    i = bisect.bisect_left(keys, t)
    # The very end is a cut point too: a chapter ending a moment before it ends there
    candidates = keys[max(i - 1, 0):i + 1] + ([info.duration] if i >= len(keys) - 1 else [])
    best = min(candidates, key=lambda k: (abs(k - t), k))
    return 0.0 if best <= EPS else best


def snap_frame(info: Info, t: float) -> float:
    """Where a cut asked for at t happens when cuts can go anywhere: the start of
    the nearest picture (or t itself, for sound only)."""
    if t <= EPS:
        return 0.0
    if t >= info.duration - EPS:
        return info.duration
    frames = info.frames
    if not frames:
        return t
    i = bisect.bisect_left(frames, t)
    candidates = frames[max(i - 1, 0):i + 1] + ([info.duration] if i >= len(frames) - 1 else [])
    best = min(candidates, key=lambda f: (abs(f - t), f))
    return 0.0 if best <= EPS else best


def previous_frame(info: Info, t: float) -> float:
    if not info.frames:
        return max(t - 0.04, 0.0)
    i = bisect.bisect_left(info.frames, t - EPS)
    return info.frames[i - 1] if i > 0 else 0.0


def next_frame(info: Info, t: float) -> float:
    if not info.frames:
        return min(t + 0.04, info.duration)
    i = bisect.bisect_right(info.frames, t + EPS)
    return info.frames[i] if i < len(info.frames) else info.duration


def is_keyframe(info: Info, t: float) -> bool:
    """Can a cut at t be a straight copy? (Always, without a picture.)"""
    if not info.has_video or t <= EPS or t >= info.duration - EPS:
        return True
    i = bisect.bisect_left(info.keyframes, t - EPS)
    return i < len(info.keyframes) and abs(info.keyframes[i] - t) <= EPS


def reencoded_spans(info: Info, kept: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The stretches of picture re-encoded to cut these kept stretches where they are:
    from each cut that isn't on a keyframe to the keyframe beside it."""
    if not info.has_video:
        return []
    spans = []
    for start, end in kept:
        first = next((k for k in info.keyframes if k >= start - EPS), None)
        last = next((k for k in reversed(info.keyframes) if k <= end + EPS), None)
        if end >= info.duration - EPS:
            last = end
        if first is None or last is None or first >= end - EPS or last <= start + EPS \
                or first > last + EPS:
            spans.append((start, end))
            continue
        if first - start > EPS:
            spans.append((start, first))
        if end - last > EPS:
            spans.append((last, end))
    return spans


def reencoded(info: Info, kept: list[tuple[float, float]]) -> float:
    """Seconds of picture re-encoded to cut these stretches where they are."""
    return sum(e - s for s, e in reencoded_spans(info, kept))


def previous_keyframe(info: Info, t: float) -> float:
    """The cut point before t (0:00 if there's none)."""
    if not info.keyframes:
        return max(t - 1.0, 0.0)
    i = bisect.bisect_left(info.keyframes, t - EPS)
    return info.keyframes[i - 1] if i > 0 else 0.0


def next_keyframe(info: Info, t: float) -> float:
    """The cut point after t (the end of the video if there's none)."""
    if not info.keyframes:
        return min(t + 1.0, info.duration)
    i = bisect.bisect_right(info.keyframes, t + EPS)
    return info.keyframes[i] if i < len(info.keyframes) else info.duration


# --- Names -------------------------------------------------------------------------

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """A file name Windows accepts: no forbidden characters, no trailing dots or
    spaces, not a reserved device name."""
    name = _BAD.sub(lambda m: {":": " -", '"': "'", "/": "-", "\\": "-", "|": "-"}
                    .get(m.group(), ""), name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = "_" + name
    return name[:180] or "Part"


# --- Cutting -----------------------------------------------------------------------

def _clean_tags(tags: dict) -> dict:
    return {k: v for k, v in tags.items() if not STALE_TAGS.match(k)}


def part_chapters(info: Info, kept: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """The original chapters that fall in a part, placed in the part's own time
    (stretches left out taken away). Only when there are two or more: a part that's
    one chapter needs none."""
    def inside(t: float) -> float:
        return sum(min(max(t - a, 0.0), b - a) for a, b in kept)
    if not kept:
        return []
    found = []
    for ch in info.chapters:
        a, b = inside(max(ch.start, kept[0][0])), inside(min(ch.end, kept[-1][1]))
        if b - a > 1.0:
            found.append((a, b, ch.title))
    return found if len(found) > 1 else []


def seek_before(container, stream, want: float, margin: float = 0.0) -> None:
    """Seek so reading starts at or before want (absolute seconds), a margin earlier
    still. Matroska and MP4 land where asked; MPEG-TS seeks by decoding time and can
    land well after it, so peek at the first keyframe and step further back until it
    comes in time. Without a picture to go by, just seek."""
    step = 1.0
    while True:
        target = max(want - margin, 0.0)
        container.seek(int(target * av.time_base), backward=True)
        if stream is None or target <= 0:
            return
        # Decoding starts at the first keyframe read, not just the first picture
        first = None
        for n, packet in enumerate(container.demux(stream)):
            if packet.pts is not None and packet.is_keyframe:
                first = float(packet.pts * packet.time_base)
                break
            if n > 5000:
                break
        container.seek(int(target * av.time_base), backward=True)  # back to the start
        if first is None or first <= want + EPS or margin > 300:
            return
        margin += step
        step *= 2


def _copies_picture(info: Info, tracks) -> bool:
    return any(t.kind == "video" and t.index in set(tracks) for t in info.tracks)


def segments(start: float, end: float, removed=()) -> list[tuple[float, float]]:
    """What's kept of start..end once the removed stretches are taken out."""
    kept, at = [], start
    for r0, r1 in sorted(removed):
        r0, r1 = max(r0, start), min(r1, end)
        if r1 - r0 <= EPS:
            continue
        if r0 - at > EPS:
            kept.append((at, r0))
        at = max(at, r1)
    if end - at > EPS:
        kept.append((at, end))
    return kept


def cut(info: Info, start: float, end: float, tracks: list[int], dest: Path,
        title: str = "", progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None, tags: dict | None = None,
        removed=(), exact: bool = False,
        note: Callable[[str], None] | None = None) -> Path:
    """Copy the stretch from start to end (seconds, already on keyframes) of the
    chosen tracks into dest, a file of the same kind as the original, leaving out
    any removed stretches (also on keyframes): what's left is joined seamlessly.
    With exact, the times can be anywhere: the pictures between a cut and the
    nearest keyframe are re-encoded, at very high quality, in the same format;
    everything else is still copied. Written to a temporary name first, so a
    stopped or failed cut never leaves half a file."""
    dest = Path(dest)
    tmp = dest.with_name(f"{dest.stem}.splicing{dest.suffix}")
    if not exact and info.has_video and _copies_picture(info, tracks):
        # Copying the picture can only start on a keyframe: put every cut on one, or
        # the sound would start before the picture does
        start, end = snap(info, start), snap(info, end)
        removed = [(snap(info, a), snap(info, b)) for a, b in removed]
    kept = segments(start, end, removed)
    if not kept:
        raise SpliceError("Nothing is left of this part once the removed stretches are "
                          "taken out.")
    spans = [(s + info.origin, e + info.origin, e >= info.duration - EPS) for s, e in kept]
    try:
        info_chapters = part_chapters(info, kept)
        _write(info, spans, set(tracks), tmp, title,
               sum(e - s for s, e in kept), progress, cancel, tags or {}, exact, note,
               info_chapters)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest


def _write(info, spans, chosen, tmp, title, length, progress, cancel, extra, exact=False,
           note=None, chapters=()):
    with _open(info.path) as source:
        streams = [s for s in source.streams if s.index in chosen]
        if not streams:
            raise SpliceError("No tracks are checked.")
        try:
            output = av.open(str(tmp), "w")
        except (av.FFmpegError, OSError) as e:
            raise SpliceError(f"Can't create {tmp.name}: {e}") from None
        with output:
            tags = _clean_tags(dict(source.metadata))
            if title:
                tags["title"] = title
            tags.update(extra)
            output.metadata.update(tags)
            if chapters:
                try:
                    output.set_chapters([
                        {"id": n, "start": int(round(a * 1000)), "end": int(round(b * 1000)),
                         "time_base": Fraction(1, 1000), "metadata": {"title": name}}
                        for n, (a, b, name) in enumerate(chapters, 1)])
                except (av.FFmpegError, ValueError, TypeError, AttributeError):
                    pass  # a kind of file without chapters
            matroska = output.format.name == "matroska"
            mapping = {}
            files = [s for s in streams if _kind(s) in ("cover", "attachment")]
            if matroska:
                # Cover art and fonts are files inside a Matroska file, not tracks: the
                # muxer would turn a copied cover into a one-frame video track
                for name, mimetype, data in _attached_files(source, files):
                    output.add_attachment(name, mimetype, data)
            elif any(_kind(s) == "attachment" for s in files):
                raise SpliceError(f"A {tmp.suffix} file can't hold attachments (fonts and "
                                  "other files). Uncheck them.")
            mp4 = output.format.name in ("mp4", "mov", "ipod", "3gp")
            for s in streams:
                if s in files and (matroska or not mp4):
                    continue  # already a file, or (AVI, TS…) nowhere for cover art to go
                try:
                    o = output.add_stream_from_template(s, opaque=True)
                except (av.FFmpegError, ValueError, TypeError, NotImplementedError) as e:
                    raise SpliceError(
                        f"{KIND_LABEL[_kind(s)]} track {s.index} can't go into a "
                        f"{tmp.suffix} file: {e}") from None
                o.metadata.update(_clean_tags(dict(s.metadata)))
                try:
                    o.disposition = s.disposition
                except (AttributeError, TypeError, ValueError):
                    pass
                mapping[s.index] = o

            redo = None
            if exact and info.has_video:
                main = next((st for st in streams if _kind(st) == "video"), None)
                if main is not None and main.index in mapping:
                    redo = Reencoder(info, main, mapping[main.index], output, note)
            try:
                _copy_packets(source, streams, mapping, output, spans, length, progress,
                              cancel, redo, info)
            finally:
                if redo:
                    redo.close()


def _attached_files(source, streams) -> list[tuple[str, str, bytes]]:
    """(file name, MIME type, contents) of each attachment and cover picture."""
    found, covers = [], {}
    wanted = {s.index for s in streams if _kind(s) == "cover"}
    if wanted:
        # A cover's picture is the first packet its track gives after opening
        for packet in source.demux(*[source.streams[i] for i in wanted]):
            if packet.size and packet.stream.index not in covers:
                covers[packet.stream.index] = bytes(packet)
            if len(covers) == len(wanted):
                break
    for n, s in enumerate(streams, 1):
        meta = {k.lower(): v for k, v in s.metadata.items()}
        if _kind(s) == "cover":
            data = covers.get(s.index)
            if not data:
                continue
            mimetype = meta.get("mimetype") or ("image/png" if data[:4] == b"\x89PNG"
                                                else "image/jpeg")
            name = meta.get("filename") or ("cover.png" if mimetype == "image/png"
                                            else "cover.jpg")
        else:
            data = bytes(getattr(s, "data", b"") or b"")
            mimetype = getattr(s, "mimetype", None) or meta.get("mimetype") or \
                "application/octet-stream"
            name = getattr(s, "name", None) or meta.get("filename") or f"attachment{n}"
        found.append((name, mimetype, data))
    return found


def _copy_packets(source, streams, mapping, output, spans, length, progress, cancel,
                  redo=None, info=None):
    """Copy each stretch (absolute start, absolute end, runs to the file's end) in
    turn, the next one's timestamps carrying on where the last one stopped."""
    kinds = {s.index: _kind(s) for s in source.streams}
    # Picture and sound arrive steadily; a stretch is done once each has passed its end
    steady = {s.index for s in streams if kinds[s.index] in ("video", "audio")}
    covers_done: set[int] = set()
    last_dts: dict[int, int] = {}
    # Start reading a little early when there are subtitles, to catch a line that's
    # already on screen when a stretch begins
    lookback = SUBTITLE_LOOKBACK if any(kinds[s.index] == "subtitle" for s in streams) else 0
    written = Fraction(0)          # how much of the part is already copied
    last_report = -1.0
    try:
        for abs_start, abs_end, to_end in spans:
            start = Fraction(abs_start)
            offset = start - written  # take this off a time to place it in the part
            finished: set[int] = set()
            started: set[int] = set()  # video tracks that have reached their first keyframe
            copied_to = None           # the last main picture copied (absolute)
            # Where the main picture is copied; around that, it's re-encoded
            copy_from, copy_to, tail = abs_start, abs_end, None
            if redo is not None:
                copy_from, copy_to, head, tail = redo.plan(abs_start, abs_end, to_end)
                if head:
                    redo.encode(head[0], head[1], offset, last_dts, cancel)
                if copy_from is None:
                    finished.add(redo.index)  # all of it re-encoded
            main = next((st for st in source.streams.video if st.index in mapping
                         and kinds[st.index] == "video"), None)
            want = copy_from if (redo is not None and copy_from is not None) else abs_start
            seek_before(source, main, min(want, abs_start), lookback)
            for packet in source.demux():
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                index = packet.stream.index
                if packet.size == 0:
                    continue
                kind = kinds.get(index)
                if kind == "cover":
                    if index in mapping and index not in covers_done:
                        covers_done.add(index)
                        packet.pts = packet.dts = 0
                        packet.stream = mapping[index]
                        output.mux(packet)
                    continue
                stamp = packet.pts if packet.pts is not None else packet.dts
                if stamp is None:
                    continue
                tb = packet.time_base
                t = stamp * tb
                float_t = float(t)
                # Done once picture and sound are past the end; and give up waiting on
                # a track that stops early
                if not to_end and float_t >= abs_end + 2.0 and (steady <= finished or
                                                               float_t >= abs_end + 30.0):
                    break
                if index not in mapping:
                    continue
                if kind == "video":
                    lo, hi = abs_start, abs_end
                    if redo is not None and index == redo.index:
                        if copy_from is None:
                            continue
                        lo, hi = copy_from, copy_to
                    if index not in started:
                        if not packet.is_keyframe or float_t < lo - EPS:
                            continue
                        started.add(index)
                    if not to_end and packet.is_keyframe and float_t >= hi - EPS:
                        finished.add(index)
                    if index in finished or float_t < lo - EPS:
                        continue
                else:
                    if not to_end and float_t >= abs_end - EPS:
                        finished.add(index)
                        continue
                    if float_t < abs_start - EPS:
                        # A subtitle that's still on screen when the stretch begins
                        if kind == "subtitle" and packet.duration and \
                                float((stamp + packet.duration) * tb) > abs_start + EPS:
                            cut_ticks = int(round(start / tb))
                            packet.duration = stamp + packet.duration - cut_ticks
                            if packet.dts is not None:
                                packet.dts = cut_ticks
                            packet.pts = cut_ticks
                        else:
                            continue
                if redo is not None and index == redo.index:
                    copied_to = float_t if copied_to is None else max(copied_to, float_t)
                    if redo.restore:
                        packet = redo.restored(packet)
                shift = int(round(offset / tb))
                if packet.pts is not None:
                    packet.pts -= shift
                if packet.dts is not None:
                    packet.dts -= shift
                elif index in last_dts and kind == "video":
                    # Matroska often leaves it out after a jump; at a join the muxer
                    # would guess it wrong, so carry on from the last one
                    packet.dts = last_dts[index] + max(packet.duration or 1, 1)
                if packet.dts is not None:
                    # Where two stretches meet, never let decoding time go backwards
                    if index in last_dts and packet.dts <= last_dts[index]:
                        packet.dts = last_dts[index] + 1
                        if packet.pts is not None and packet.pts < packet.dts:
                            packet.pts = packet.dts
                    last_dts[index] = packet.dts
                if kind == "subtitle" and packet.duration and packet.pts is not None \
                        and not to_end:
                    # A line on screen when the stretch ends stops there
                    stop = int(round((Fraction(abs_end) - offset) / tb))
                    if packet.pts + packet.duration > stop:
                        packet.duration = max(stop - packet.pts, 1)
                packet.stream = mapping[index]
                output.mux(packet)
                if progress and length > 0:
                    done = min(max(float(written + t - start) / length, 0.0), 1.0)
                    if done - last_report >= 0.005:
                        last_report = done
                        progress(done)
            if redo is not None and copy_from is not None and not to_end:
                # With open GOPs, the pictures shown just before the keyframe where
                # copying stops are stored after it, so weren't copied: re-encode them
                after = (copied_to + redo.frame) if copied_to is not None else copy_from
                until = tail[0] if tail else abs_end
                if after < until - redo.frame / 2:
                    tail = (after, tail[1] if tail else abs_end)
            if tail:
                redo.encode(tail[0], tail[1], offset, last_dts, cancel)
            written += Fraction(abs_end) - start
    except av.FFmpegError as e:
        raise SpliceError(f"Copying failed: {e}") from None
    if progress:
        progress(1.0)


# --- Re-encoding the pictures next to a cut ---------------------------------------------

# Encoders and settings that keep the result as close to the original as can be: a
# very high quality, no reordered frames (so it joins the copied pictures cleanly), and
# the format's own settings carried in the pictures themselves
ENCODERS = {
    "h264": ("libx264", {"crf": "8", "preset": "slow", "bf": "0", "x264-params":
                         "bframes=0:keyint=infinite:scenecut=0:repeat-headers=1"}),
    "hevc": ("libx265", {"crf": "8", "preset": "slow", "x265-params":
                         "bframes=0:keyint=-1:scenecut=0:repeat-headers=1:log-level=error"}),
    "av1": ("libsvtav1", {"crf": "8", "preset": "6", "svtav1-params": "keyint=-1"}),
    "vp9": ("libvpx-vp9", {"crf": "4", "b": "0", "deadline": "good", "cpu-used": "2",
                           "auto-alt-ref": "0", "lag-in-frames": "0"}),
    "vp8": ("libvpx", {"crf": "4", "b": "50M", "deadline": "good", "cpu-used": "1",
                       "auto-alt-ref": "0", "lag-in-frames": "0"}),
    "mpeg2video": ("mpeg2video", {"qscale": "2", "bf": "0"}),
    "mpeg4": ("mpeg4", {"qscale": "2", "bf": "0"}),
}


def can_reencode(info: Info) -> str:
    """"" if cuts can go anywhere in this video; otherwise why not."""
    if not info.has_video:
        return ""
    with _open(info.path) as c:
        main = next((s for s in c.streams.video
                     if not s.disposition & av.stream.Disposition.attached_pic), None)
        name = codec_name(main) if main is not None else ""
        pix = main.codec_context.pix_fmt if main is not None else None
    name = {"libdav1d": "av1", "libaom-av1": "av1"}.get(name, name)
    if name not in ENCODERS:
        return f"Splicer can't re-encode {name or 'this'} video, so its cuts stay on keyframes."
    try:
        formats = [f.name for f in (av.codec.Codec(ENCODERS[name][0], "w").video_formats or [])]
    except (av.FFmpegError, ValueError):
        formats = []
    if pix and formats and pix not in formats:
        return (f"Splicer can't re-encode this video's picture format ({pix}) without "
                "changing it, so its cuts stay on keyframes.")
    return ""


class Reencoder:
    """Re-encodes the main picture for the bits of a part between a cut and the
    nearest keyframe, and hands them to the output in place of the original."""

    def __init__(self, info: Info, stream, out_stream, output, note=None):
        self.info, self.index, self.out_stream, self.output = info, stream.index, out_stream, output
        self.note = note
        self.source = _open(info.path)
        self.stream = self.source.streams[stream.index]
        self.stream.thread_type = "AUTO"
        self.tb = self.stream.time_base
        name = codec_name(stream)
        self.codec = {"libdav1d": "av1", "libaom-av1": "av1"}.get(name, name)
        if self.codec not in ENCODERS:
            self.source.close()
            raise SpliceError(f"Splicer can't re-encode {self.codec or 'this'} video. Choose "
                              "Keyframes only for this one.")
        self.delay = int(round(Fraction(info.video_delay).limit_denominator(100000) / self.tb))
        extra = bytes(stream.codec_context.extradata or b"")
        # MP4 and Matroska store H.264/HEVC pictures as length-prefixed pieces; MPEG-TS
        # and raw files keep the start codes the encoder writes
        self.lengths = 0
        if self.codec == "h264" and len(extra) > 4 and extra[0] == 1:
            self.lengths = (extra[4] & 3) + 1
        elif self.codec == "hevc" and len(extra) > 22 and extra[0] == 1:
            self.lengths = (extra[21] & 3) + 1
        rate = stream.average_rate or stream.guessed_rate
        self.frame = float(1 / rate) if rate else 1 / 30
        # MPEG-2 only counts time in frames; everything else takes the file's own units
        self.enc_tb = Fraction(rate.denominator, rate.numerator)             if self.codec == "mpeg2video" and rate else self.tb
        self.settings = self._original_settings(extra)
        self.restore = False       # put them back on the next copied picture

    def _original_settings(self, extra: bytes) -> bytes:
        """The original's own format settings (H.264/HEVC parameter sets, the AV1
        sequence header, the MPEG sequence header) as they'd appear in a picture.
        A re-encoded piece brings its own; copied pictures after it need these
        back."""
        if not extra:
            return b""
        try:
            if self.codec == "h264" and self.lengths:
                units, i = [], 5
                for count_at_mask in (0x1F, 0xFF):
                    count = extra[i] & count_at_mask
                    i += 1
                    for _ in range(count):
                        size = int.from_bytes(extra[i:i + 2], "big")
                        units.append(extra[i + 2:i + 2 + size])
                        i += 2 + size
                return b"".join(len(u).to_bytes(self.lengths, "big") + u for u in units)
            if self.codec == "hevc" and self.lengths:
                units, i = [], 23
                for _ in range(extra[22]):
                    count = int.from_bytes(extra[i + 1:i + 3], "big")
                    i += 3
                    for _ in range(count):
                        size = int.from_bytes(extra[i:i + 2], "big")
                        units.append(extra[i + 2:i + 2 + size])
                        i += 2 + size
                return b"".join(len(u).to_bytes(self.lengths, "big") + u for u in units)
            if self.codec == "av1":
                return extra[4:]  # av1C's header, then the sequence header itself
            if self.codec in ("h264", "hevc", "mpeg2video", "mpeg4"):
                return extra      # already as it appears in the pictures
        except IndexError:
            return b""
        return b""

    def restored(self, packet):
        """The first copied picture after a re-encoded piece, with the original's
        settings in front of it."""
        self.restore = False
        if not self.settings:
            return packet
        out = av.Packet(self.settings + bytes(packet))
        out.pts, out.dts, out.time_base = packet.pts, packet.dts, packet.time_base
        out.is_keyframe = packet.is_keyframe
        if packet.duration:
            out.duration = packet.duration
        return out

    def close(self):
        self.source.close()

    def plan(self, start: float, end: float, to_end: bool):
        """(copy from, copy to, re-encode head, re-encode tail), all absolute. copy
        from is None when the whole stretch is re-encoded."""
        origin, keys = self.info.origin, self.info.keyframes
        first = next((k + origin for k in keys if k + origin >= start - EPS), None)
        last = end if to_end else \
            next((k + origin for k in reversed(keys) if k + origin <= end + EPS), None)
        if first is None or last is None or first >= end - EPS or last <= start + EPS \
                or first > last + EPS:
            return None, None, (start, end), None
        head = (start, first) if first - start > EPS else None
        tail = (last, end) if end - last > EPS and not to_end else None
        return first, last, head, tail

    def _encoder(self):
        name, options = ENCODERS[self.codec]
        src = self.stream.codec_context
        enc = av.CodecContext.create(name, "w")
        enc.width, enc.height = src.width, src.height
        formats = [f.name for f in (enc.codec.video_formats or [])]
        enc.pix_fmt = src.pix_fmt if not formats or src.pix_fmt in formats else formats[0]
        enc.time_base = self.enc_tb
        rate = self.stream.average_rate or self.stream.guessed_rate
        if rate:
            enc.framerate = rate
        if src.sample_aspect_ratio:
            enc.sample_aspect_ratio = src.sample_aspect_ratio
        for attr in ("color_range", "color_primaries", "color_trc", "colorspace"):
            try:
                setattr(enc, attr, getattr(src, attr))
            except (AttributeError, TypeError, ValueError):
                pass
        enc.options = dict(options)
        if self.codec in ("mpeg2video", "mpeg4"):
            enc.bit_rate = max(int(src.bit_rate or 0) * 3, 40_000_000)
            enc.options = {"bf": "0"}
            enc.qmin, enc.qmax = 1, 3
        return enc

    def encode(self, start: float, end: float, offset: Fraction, last_dts: dict, cancel):
        """Re-encode the pictures shown from start up to end (absolute seconds) and
        write them, shifted by offset like everything copied around them."""
        half = self.frame / 2
        if self.note:
            self.note(f"re-encoding {end - start:.1f} s of picture near a cut")
        seek_before(self.source, self.stream, start)
        enc = self._encoder()
        shift = int(round(offset / self.tb))
        try:
            for frame in self.source.decode(self.stream):
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                if frame.pts is None:
                    continue
                t = float(frame.pts * self.tb)
                if t < start - half:
                    continue
                if t >= end - half:
                    break
                if frame.format.name != enc.pix_fmt:
                    pts = frame.pts
                    frame = frame.reformat(format=enc.pix_fmt)
                    frame.pts = pts
                frame.pts -= shift
                if self.enc_tb != self.tb:
                    frame.pts = int(round(frame.pts * self.tb / self.enc_tb))
                    frame.time_base = self.enc_tb
                frame.pict_type = av.video.frame.PictureType.NONE
                for packet in enc.encode(frame):
                    self._write(packet, last_dts)
            for packet in enc.encode(None):
                self._write(packet, last_dts)
            self.restore = True
        except av.FFmpegError as e:
            raise SpliceError(f"Re-encoding the picture near a cut failed: {e}") from None

    def _write(self, packet, last_dts: dict):
        if packet.time_base and packet.time_base != self.tb:  # MPEG-2's frame counting
            ratio = packet.time_base / self.tb
            for attr in ("pts", "dts", "duration"):
                value = getattr(packet, attr)
                if value is not None:
                    setattr(packet, attr, int(round(value * ratio)))
            packet.time_base = self.tb
        if not packet.duration:
            # Encoders leave it out; MP4 then counts the last picture as lasting no
            # time, and players skip it
            packet.duration = max(int(round(Fraction(self.frame).limit_denominator(1_000_000)
                                            / self.tb)), 1)
        if self.lengths:
            packet = self._with_lengths(packet)
        if packet.pts is not None:
            packet.dts = packet.pts - self.delay  # in step with the copied pictures
            if self.index in last_dts and packet.dts <= last_dts[self.index]:
                packet.dts = last_dts[self.index] + 1
            if packet.dts > packet.pts:
                packet.pts = packet.dts
            last_dts[self.index] = packet.dts
        packet.stream = self.out_stream
        self.output.mux(packet)

    def _with_lengths(self, packet):
        """Start codes (00 00 01) to the length-prefixed pieces the file uses."""
        data = bytes(packet)
        units, i, n = [], 0, len(data)
        starts = []
        while True:
            j = data.find(b"\x00\x00\x01", i)
            if j < 0:
                break
            starts.append(j + 3)
            i = j + 3
        for k, begin in enumerate(starts):
            stop = starts[k + 1] - 3 if k + 1 < len(starts) else n
            unit = data[begin:stop].rstrip(b"\x00") if k + 1 < len(starts) else data[begin:stop]
            if unit:
                units.append(len(unit).to_bytes(self.lengths, "big") + unit)
        out = av.Packet(b"".join(units))
        out.pts, out.dts, out.time_base = packet.pts, packet.dts, packet.time_base
        out.is_keyframe = packet.is_keyframe
        if packet.duration:
            out.duration = packet.duration
        return out


# --- The sound's shape, for the timeline -------------------------------------------------

WAVE_STEP = 0.05  # seconds per bar


def waveform(path: Path, index: int, origin: float, duration: float,
             cancel: threading.Event | None = None) -> list[float] | None:
    """How loud one sound track is, as its peak (0 to 1) every WAVE_STEP seconds.
    The sound is brought down to 4 kHz mono first: plenty for its shape, and quick
    to go through without any number-crunching library. None if stopped."""
    rate = 4000
    per = int(rate * WAVE_STEP)
    peaks = [0.0] * (int(duration / WAVE_STEP) + 2)
    with _open(path) as c:
        stream = next((s for s in c.streams.audio if s.index == index), None)
        if stream is None:
            return []
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        pending = array("h")
        slot = None
        try:
            for n, frame in enumerate(c.decode(stream)):
                if n % 200 == 0 and cancel is not None and cancel.is_set():
                    return None
                if slot is None and frame.pts is not None:
                    slot = max(int((float(frame.pts * frame.time_base) - origin) / WAVE_STEP), 0)
                for out in resampler.resample(frame):
                    pending.frombytes(bytes(out.planes[0])[:out.samples * 2])
                while len(pending) >= per and slot is not None:
                    chunk = pending[:per]
                    del pending[:per]
                    if slot < len(peaks):
                        peaks[slot] = max(max(chunk), -min(chunk)) / 32768
                    slot += 1
        except av.FFmpegError:
            pass
    return peaks


# --- Sound only --------------------------------------------------------------------

SOUND_EXTS = {"opus": ".opus", "aac": ".m4a", "mp3": ".mp3", "flac": ".flac",
              "vorbis": ".ogg", "alac": ".m4a"}


def sound_ext(track: Track) -> str:
    """The file a sound track goes into on its own, untouched: Opus in .opus, AAC
    in .m4a, MP3 in .mp3, FLAC in .flac; anything else in .mka (Matroska sound)."""
    return SOUND_EXTS.get(track.codec.lower(), ".mka")
