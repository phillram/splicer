"""Splicer's engine on small real files (made with FFmpeg, in fixtures/).

chaptered.mkv: 24 s, H.264 with B-frames and a keyframe every 1.5 s, two AAC
tracks (English default, Japanese "Commentary"), two SRT tracks (English, French
forced), one attached file, three chapters (0-10, 10-20, 20-24 s).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import av
import pytest

from splicer import engine

FIXTURES = Path(__file__).parent / "fixtures"
MKV = FIXTURES / "chaptered.mkv"
MP4 = FIXTURES / "chaptered.mp4"
COVER = FIXTURES / "withcover.mp4"


@pytest.fixture(scope="module")
def mkv():
    return engine.probe(MKV)


def packets(path: Path, start: float = 0.0, end: float = 1e9):
    """{stream index: [(time, md5)]} for picture, sound and subtitle packets in
    [start, end), in the order the file stores them."""
    found: dict[int, list] = {}
    with av.open(str(path)) as c:
        for p in c.demux():
            if p.size == 0 or p.pts is None or p.stream.type not in ("video", "audio",
                                                                      "subtitle"):
                continue
            if p.stream.disposition & av.stream.Disposition.attached_pic:
                continue
            t = float(p.pts * p.time_base)
            if start - 0.0005 <= t < end - 0.0005:
                found.setdefault(p.stream.index, []).append(
                    (round(t - start, 3), hashlib.md5(bytes(p)).hexdigest()))
    return found


def cut_parts(info, tmp_path, tracks=None):
    tracks = tracks if tracks is not None else [t.index for t in info.tracks if t.chosen]
    bounds = [engine.snap(info, c.start) for c in info.chapters] + [info.duration]
    out = []
    for n, (s, e) in enumerate(zip(bounds, bounds[1:]), 1):
        dest = tmp_path / f"{n:02} - {engine.safe_name(info.chapters[n - 1].title)}{MKV.suffix}"
        out.append((s, e, engine.cut(info, s, e, tracks, dest, info.chapters[n - 1].title)))
    return out


# --- Reading -----------------------------------------------------------------------

def test_tracks_and_defaults(mkv):
    kinds = [(t.kind, t.language, t.chosen) for t in mkv.tracks]
    assert kinds == [
        ("video", "", True),        # nothing is marked default: the first is taken
        ("audio", "eng", True),     # default
        ("audio", "jpn", False),
        ("subtitle", "eng", False),
        ("subtitle", "fre", True),  # forced
        ("attachment", "", True),
    ]
    assert mkv.tracks[2].title == "Commentary"
    assert "Japanese" in mkv.tracks[2].label and "Commentary" in mkv.tracks[2].label


def test_chapters(mkv):
    assert [(c.start, c.end, c.title) for c in mkv.chapters] == [
        (0.0, 10.0, 'Opening: The "Start"'), (10.0, 20.0, "Middle/Part?"),
        (20.0, 24.0, "Finale")]


def test_keyframes(mkv):
    assert mkv.has_video
    assert mkv.keyframes[:4] == [0.0, 1.5, 3.0, 4.5]
    assert abs(mkv.duration - 24.0) < 0.1


def test_snap_and_step(mkv):
    assert engine.snap(mkv, 10.0) == 10.5
    assert engine.snap(mkv, 9.6) == 9.0
    assert engine.snap(mkv, 0.3) == 0.0
    assert engine.snap(mkv, 1000) == mkv.duration
    # The last chapter ends at 24.0, a moment before the video does (24.023)
    assert engine.snap(mkv, 24.0) == mkv.duration
    assert engine.snap(mkv, 22.6) == 22.5
    assert engine.previous_keyframe(mkv, 10.5) == 9.0
    assert engine.next_keyframe(mkv, 10.5) == 12.0
    assert engine.previous_keyframe(mkv, 0.0) == 0.0
    assert engine.next_keyframe(mkv, mkv.keyframes[-1]) == mkv.duration


def test_sound_only_cuts_anywhere(tmp_path):
    with av.open(str(MKV)) as c, av.open(str(tmp_path / "a.mka"), "w") as o:
        out = o.add_stream_from_template(c.streams.audio[0], opaque=True)
        for p in c.demux(c.streams.audio[0]):
            if p.size:
                p.stream = out
                o.mux(p)
    info = engine.probe(tmp_path / "a.mka")
    assert not info.has_video and info.keyframes == []
    assert engine.snap(info, 7.25) == 7.25


def test_preview(mkv):
    data = engine.preview(MKV, 10.5, mkv.origin, 160, 160)
    assert data.startswith(b"P6 160 90 255 ")
    assert len(data) == len(b"P6 160 90 255 ") + 160 * 90 * 3


# --- Cutting -----------------------------------------------------------------------

def test_parts_are_byte_identical(mkv, tmp_path):
    every = [t.index for t in mkv.tracks]
    original = packets(MKV)
    rebuilt: dict[int, list] = {}
    for s, e, path in cut_parts(mkv, tmp_path, every):
        source = packets(MKV, s, e)
        part = packets(path)
        for index in (0, 1, 2):  # picture and both sound tracks: exactly the same bytes
            assert part[index] == source[index], (path.name, index)
            rebuilt.setdefault(index, []).extend(h for _, h in part[index])
    # Together the parts hold every packet of the original, once
    for index in (0, 1, 2):
        assert rebuilt[index] == [h for _, h in original[index]]


def test_parts_play_from_the_start(mkv, tmp_path):
    s, e, path = cut_parts(mkv, tmp_path)[1]
    with av.open(str(path)) as c:
        frames = list(c.decode(video=0))
    assert len(frames) == round((e - s) * 30)
    assert frames[0].key_frame and float(frames[0].pts * frames[0].time_base) < 0.05


def test_names_tags_and_attachment(mkv, tmp_path):
    parts = cut_parts(mkv, tmp_path)
    assert [p.name for _, _, p in parts] == [
        "01 - Opening - The 'Start'.mkv", "02 - Middle-Part.mkv", "03 - Finale.mkv"]
    with av.open(str(parts[1][2])) as c:
        assert c.metadata.get("title") == "Middle/Part?"
        assert c.metadata.get("ARTIST", c.metadata.get("artist")) == "Splicer Tests"
        assert not c.chapters()
        kinds = [s.type for s in c.streams]
        assert kinds == ["video", "audio", "subtitle", "attachment"]
        assert c.streams[1].metadata.get("language") == "eng"
        assert c.streams[2].metadata.get("language") == "fre"
        assert c.streams[2].disposition & av.stream.Disposition.forced
        assert bytes(c.streams[3].data).startswith(b"Splicer test attachment")
        assert not any(k.upper().startswith("DURATION") and v.startswith("00:00:24")
                       for s in c.streams for k, v in s.metadata.items())
    assert not list(tmp_path.glob("*.splicing.*"))


def test_subtitle_on_screen_at_the_cut_carries_over(mkv, tmp_path):
    # "This one spans the cut" is on 9-13 s; part two starts at 10.5 s
    _, _, path = cut_parts(mkv, tmp_path, [0, 3])[1]
    with av.open(str(path)) as c:
        first = next(p for p in c.demux(c.streams.subtitles[0]) if p.size)
        assert float(first.pts * first.time_base) == 0.0
        assert abs(float(first.duration * first.time_base) - 2.5) < 0.002
        assert b"spans the cut" in bytes(first)


def test_mp4(tmp_path):
    info = engine.probe(MP4)
    assert [t.kind for t in info.tracks] == ["video", "audio", "audio", "subtitle", "data"]
    assert not info.tracks[4].chosen        # the chapter-title track
    assert len(info.chapters) == 3
    s, e = engine.snap(info, 10), engine.snap(info, 20)
    tracks = [0, 1, 3]
    path = engine.cut(info, s, e, tracks, tmp_path / "part.mp4")
    source, part = packets(MP4, s, e), packets(path)
    assert part[0] == source[0] and part[1] == source[1]
    with av.open(str(path)) as c:
        assert [s.type for s in c.streams] == ["video", "audio", "subtitle"]
        assert abs(c.duration / av.time_base - (e - s)) < 0.1


def test_mp4_cover_art_stays_cover_art(tmp_path):
    info = engine.probe(COVER)
    assert [t.kind for t in info.tracks] == ["video", "audio", "cover"]
    assert all(t.chosen for t in info.tracks)
    path = engine.cut(info, 0, engine.snap(info, 3), [0, 1, 2], tmp_path / "part.mp4")
    with av.open(str(path)) as c:
        assert len(c.streams.video) == 2
        assert c.streams.video[1].disposition & av.stream.Disposition.attached_pic


def test_whole_file_to_the_end(mkv, tmp_path):
    path = engine.cut(mkv, 0, mkv.duration, [0, 1], tmp_path / "all.mkv")
    original, copy = packets(MKV), packets(path)
    assert copy[0] == original[0] and copy[1] == original[1]


def test_stop_leaves_nothing(mkv, tmp_path):
    stop = threading.Event()
    stop.set()
    with pytest.raises(engine.Cancelled):
        engine.cut(mkv, 0, mkv.duration, [0, 1], tmp_path / "x.mkv", cancel=stop)
    assert list(tmp_path.iterdir()) == []


def test_no_tracks(mkv, tmp_path):
    with pytest.raises(engine.SpliceError):
        engine.cut(mkv, 0, 5, [], tmp_path / "x.mkv")


@pytest.mark.parametrize("name, safe", [
    ("Saria’s Song", "Saria’s Song"),
    ('A: "B"/C\\D|E?*<>', "A - 'B'-C-D-E"),
    ("trailing dots...", "trailing dots"),
    ("CON", "_CON"),
    ("", "Part"),
])
def test_safe_name(name, safe):
    assert engine.safe_name(name) == safe
