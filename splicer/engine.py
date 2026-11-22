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
import os
import re
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av

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
            codec = ctx.name or ""
        except Exception:
            codec = ""
        codec = codec.upper() if codec in ("aac", "ac3", "eac3", "dts", "flac", "mp3", "alac") \
            else {"libdav1d": "AV1", "av1": "AV1", "h264": "H.264", "hevc": "HEVC",
                  "vp9": "VP9", "vp8": "VP8", "opus": "Opus", "vorbis": "Vorbis",
                  "subrip": "SRT", "srt": "SRT", "ass": "ASS", "hdmv_pgs_subtitle": "PGS",
                  "dvd_subtitle": "VobSub", "mov_text": "MP4 text", "webvtt": "WebVTT",
                  "mjpeg": "JPEG", "png": "PNG", "truehd": "TrueHD",
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
    try:
        return stream.codec_context.name or ""
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


def cut(info: Info, start: float, end: float, tracks: list[int], dest: Path,
        title: str = "", progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None, tags: dict | None = None) -> Path:
    """Copy the stretch from start to end (seconds, already on keyframes) of the
    chosen tracks into dest, a file of the same kind as the original. Written to
    a temporary name first, so a stopped or failed cut never leaves half a file."""
    dest = Path(dest)
    tmp = dest.with_name(f"{dest.stem}.splicing{dest.suffix}")
    abs_start, abs_end = start + info.origin, end + info.origin
    to_end = end >= info.duration - EPS
    try:
        _write(info, abs_start, abs_end, to_end, set(tracks), tmp, title,
               end - start, progress, cancel, tags or {})
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest


def _write(info, abs_start, abs_end, to_end, chosen, tmp, title, length, progress, cancel,
           extra):
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

            _copy_packets(source, streams, mapping, output, abs_start, abs_end, to_end,
                          length, progress, cancel)


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


def _copy_packets(source, streams, mapping, output, abs_start, abs_end, to_end,
                  length, progress, cancel):
    kinds = {s.index: _kind(s) for s in source.streams}
    offsets = {s.index: Fraction(abs_start) for s in streams}
    # Picture and sound arrive steadily; a part is finished once each has passed the end
    steady = {s.index for s in streams if kinds[s.index] in ("video", "audio")}
    finished: set[int] = set()
    started: set[int] = set()     # video tracks that have reached their first keyframe
    covers_done: set[int] = set()

    # Start reading a little early when there are subtitles, to catch a line that's
    # already on screen when the part begins
    lookback = SUBTITLE_LOOKBACK if any(kinds[s.index] == "subtitle" for s in streams) else 0
    source.seek(int(max(abs_start - lookback, 0) * av.time_base), backward=True)
    last_report = -1.0
    try:
        demuxer = source.demux()
        for packet in demuxer:
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
            # Done once picture and sound are past the end; and give up waiting on a
            # track that stops early
            if not to_end and float_t >= abs_end + 2.0 and (steady <= finished or
                                                           float_t >= abs_end + 30.0):
                break
            if index not in mapping:
                continue
            if kind == "video":
                if index not in started:
                    if not packet.is_keyframe or float_t < abs_start - EPS:
                        continue
                    started.add(index)
                if not to_end and packet.is_keyframe and float_t >= abs_end - EPS:
                    finished.add(index)
                if index in finished or float_t < abs_start - EPS:
                    continue
            else:
                if not to_end and float_t >= abs_end - EPS:
                    finished.add(index)
                    continue
                if float_t < abs_start - EPS:
                    # A subtitle that's still on screen when the part begins
                    if kind == "subtitle" and packet.duration and \
                            float((stamp + packet.duration) * tb) > abs_start + EPS:
                        cut_ticks = int(round(Fraction(abs_start) / tb))
                        packet.duration = stamp + packet.duration - cut_ticks
                        if packet.dts is not None:
                            packet.dts = cut_ticks
                        packet.pts = cut_ticks
                    else:
                        continue
            shift = int(round(offsets[index] / tb))
            if packet.pts is not None:
                packet.pts -= shift
            if packet.dts is not None:
                packet.dts -= shift
            packet.stream = mapping[index]
            output.mux(packet)
            if progress and length > 0:
                done = min(max((float_t - abs_start) / length, 0.0), 1.0)
                if done - last_report >= 0.005:
                    last_report = done
                    progress(done)
    except av.FFmpegError as e:
        raise SpliceError(f"Copying failed: {e}") from None
    if progress:
        progress(1.0)


# --- Sound only --------------------------------------------------------------------

SOUND_EXTS = {"opus": ".opus", "aac": ".m4a", "mp3": ".mp3", "flac": ".flac",
              "vorbis": ".ogg", "alac": ".m4a"}


def sound_ext(track: Track) -> str:
    """The file a sound track goes into on its own, untouched: Opus in .opus, AAC
    in .m4a, MP3 in .mp3, FLAC in .flac; anything else in .mka (Matroska sound)."""
    return SOUND_EXTS.get(track.codec.lower(), ".mka")
