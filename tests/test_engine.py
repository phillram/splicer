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


def test_text_subtitles_and_tags_are_read(mkv):
    assert [t.text for t in mkv.tracks] == [False, False, False, True, True, False]
    lines = [(c.start, c.end, c.text) for c in mkv.subtitles[4]]
    assert lines[0] == (1.0, 4.5, "Bonjour from part one")
    assert lines[2] == (9.0, 13.0, "This one spans the cut at 10s")
    assert mkv.tags.get("title") == "Test Movie"


def test_sound_only(mkv, tmp_path):
    track = mkv.tracks[1]
    assert engine.sound_ext(track) == ".m4a"
    s, e = engine.snap(mkv, 10), engine.snap(mkv, 20)
    path = engine.cut(mkv, s, e, [track.index], tmp_path / "part.m4a", "Middle",
                      tags={"track": "2/3", "album": "Test Movie"})
    assert packets(path)[0] == packets(MKV, s, e)[1]
    with av.open(str(path)) as c:
        assert [st.type for st in c.streams] == ["audio"]
        assert c.metadata.get("track") == "2/3" and c.metadata.get("album") == "Test Movie"


def test_segments():
    assert engine.segments(0, 10, [(3, 4), (6, 7)]) == [(0, 3), (4, 6), (7, 10)]
    assert engine.segments(0, 10, [(-1, 2), (9, 12)]) == [(2, 9)]
    assert engine.segments(0, 10, [(0, 10)]) == []


@pytest.mark.parametrize("source", [MKV, MP4])
def test_removed_stretch_is_left_out_and_the_rest_joined(tmp_path, source):
    info = engine.probe(source)
    start, end, removed = 3.0, 15.0, [(6.0, 9.0), (12.0, 13.5)]
    path = engine.cut(info, start, end, [0, 1], tmp_path / f"joined{source.suffix}",
                      removed=removed)
    kept = engine.segments(start, end, removed)
    for index in (0, 1):
        expected = [h for s, e in kept for _, h in packets(source, s, e)[index]]
        assert [h for _, h in packets(path)[index]] == expected
    with av.open(str(path)) as c:
        times = [float(f.pts * f.time_base) for f in c.decode(video=0)]
    assert len(times) == round(sum(e - s for s, e in kept) * 30)
    assert times == sorted(times) and times[0] < 0.05


def test_nothing_left(mkv, tmp_path):
    with pytest.raises(engine.SpliceError):
        engine.cut(mkv, 3, 6, [0], tmp_path / "x.mkv", removed=[(0, 10)])


def test_subtitles_across_a_join(mkv, tmp_path):
    path = engine.cut(mkv, 0, mkv.duration, [0, 4], tmp_path / "j.mkv",
                      removed=[(3.0, 10.5), (15.0, 19.5)])
    with av.open(str(path)) as c:
        lines = [(round(float(p.pts * p.time_base), 2), round(float(p.duration * p.time_base), 2),
                  bytes(p).decode()) for p in c.demux(c.streams.subtitles[0]) if p.size]
    # Kept 0-3, 10.5-15, 19.5-end: the first line stops at the join, the one on screen
    # at 10.5 carries on from it, and the lines in the left-out stretches are gone
    assert lines == [(1.0, 2.0, "Bonjour from part one"),
                     (3.0, 2.5, "This one spans the cut at 10s")]


# --- Cuts anywhere ------------------------------------------------------------------

def test_frames_and_snapping(mkv):
    assert len(mkv.frames) == 720 and mkv.frames[:3] == [0.0, 0.033, 0.067]
    assert engine.snap_frame(mkv, 2.21) == 2.2 and engine.snap_frame(mkv, 2.22) == 2.233
    assert engine.next_frame(mkv, 2.2) == 2.233 and engine.previous_frame(mkv, 2.2) == 2.167
    assert engine.is_keyframe(mkv, 3.0) and not engine.is_keyframe(mkv, 2.2)
    assert mkv.video_delay > 0  # B-frames


def test_reencoded_spans(mkv):
    # 2.2-11.7: picture re-encoded from 2.2 to the keyframe at 3.0, and from 10.5 to 11.7
    spans = engine.reencoded_spans(mkv, [(2.2, 11.7)])
    assert [(round(a, 3), round(b, 3)) for a, b in spans] == [(2.2, 3.0), (10.5, 11.7)]
    assert engine.reencoded_spans(mkv, [(3.0, 10.5)]) == []
    assert engine.reencoded_spans(mkv, [(4.6, 5.9)]) == [(4.6, 5.9)]  # no keyframe inside
    assert engine.can_reencode(mkv) == ""


def decoded_times(path):
    with av.open(str(path)) as c:  # raises on anything it can't decode
        return [float(f.pts * f.time_base) for f in c.decode(video=0)]


@pytest.mark.parametrize("source, start, end, removed", [
    (MKV, 2.2, 11.7, []),
    (MP4, 2.2, 11.7, []),
    (MKV, 0.7, 20.3, [(5.1, 8.4)]),
    (MKV, 4.6, 5.9, []),            # inside one keyframe's stretch: all re-encoded
    (MKV, 3.0, 10.5, []),           # on keyframes: nothing re-encoded
])
def test_cut_anywhere(tmp_path, source, start, end, removed):
    info = engine.probe(source)
    snap = lambda t: engine.snap_frame(info, t)  # noqa: E731
    s, e = snap(start), snap(end)
    rm = [(snap(a), snap(b)) for a, b in removed]
    kept = engine.segments(s, e, rm)
    path = engine.cut(info, s, e, [0, 1], tmp_path / f"exact{source.suffix}", removed=rm,
                      exact=True)
    times = decoded_times(path)
    expected = sum(1 for f in info.frames for a, b in kept if a - 0.0005 <= f < b - 0.0005)
    assert len(times) == expected
    assert times == sorted(times) and times[0] < 0.01
    # The sound is always a straight copy
    sound = [h for a, b in kept for _, h in packets(source, a, b)[1]]
    assert [h for _, h in packets(path)[1]] == sound


def test_cut_anywhere_copies_between_keyframes(tmp_path):
    info = engine.probe(MKV)
    path = engine.cut(info, 2.2, 11.7, [0], tmp_path / "exact.mkv", exact=True)
    copied = [h for _, h in packets(MKV, 3.0, 10.5)[0]]
    out = [h for _, h in packets(path)[0]]
    # The pictures from the keyframe at 3.0 to the one at 10.5 are the originals,
    # bar the format settings put back in front of the first of them
    assert out[25:24 + len(copied)] == copied[1:]


def test_keyframe_cut_off_keyframes_keeps_sound_with_picture(mkv, tmp_path):
    # 2.2 and 11.7 aren't keyframes: without re-encoding, the cut goes on the
    # nearest ones (1.5 and 12.0), sound and picture alike
    off = engine.cut(mkv, 2.2, 11.7, [0, 1], tmp_path / "off.mkv")
    on = engine.cut(mkv, 1.5, 12.0, [0, 1], tmp_path / "on.mkv")
    assert packets(off) == packets(on)
    # Sound alone can still be cut anywhere
    sound = engine.cut(mkv, 2.2, 11.7, [1], tmp_path / "sound.m4a")
    assert packets(sound)[0] == packets(MKV, 2.2, 11.7)[1]


def test_chapters_inside_a_part_are_kept(mkv, tmp_path):
    # 4.5-22.5 covers the end of the first chapter (to 10), the second (to 20) and
    # the start of the third; 13.5-15 is left out
    assert [(round(a, 3), round(b, 3), t) for a, b, t in
            engine.part_chapters(mkv, [(4.5, 13.5), (15.0, 22.5)])] == [
        (0.0, 5.5, 'Opening: The "Start"'), (5.5, 14.0, "Middle/Part?"), (14.0, 16.5, "Finale")]
    path = engine.cut(mkv, 4.5, 22.5, [0, 1], tmp_path / "p.mkv", removed=[(13.5, 15.0)])
    with av.open(str(path)) as c:
        assert [(float(ch["start"] * ch["time_base"]), ch["metadata"]["title"])
                for ch in c.chapters()] == [(0.0, 'Opening: The "Start"'),
                                            (5.5, "Middle/Part?"), (14.0, "Finale")]
    # One chapter's worth: no chapters written
    assert engine.part_chapters(mkv, [(10.5, 19.5)]) == []


def test_codec_names_are_the_codecs_not_the_decoders():
    info = engine.probe(FIXTURES / "tone.mp3")
    track = info.tracks[0]
    assert track.codec == "MP3"          # not "mp3float"
    assert engine.sound_ext(track) == ".mp3"


def remux(source: Path, dest: Path) -> Path:
    """The same packets in another kind of file."""
    with av.open(str(source)) as c, av.open(str(dest), "w") as o:
        streams = [s for s in c.streams if s.type in ("video", "audio")]
        mapping = {s.index: o.add_stream_from_template(s, opaque=True) for s in streams}
        for p in c.demux(*streams):
            if p.size:
                p.stream = mapping[p.stream.index]
                o.mux(p)
    return dest


@pytest.mark.parametrize("exact", [False, True])
def test_mpeg_ts(tmp_path, exact):
    # MPEG-TS seeks by decoding time, which can land past the keyframe wanted
    info = engine.probe(remux(MKV, tmp_path / "v.ts"))
    snap = engine.snap_frame if exact else engine.snap
    s, e = snap(info, 10.4), snap(info, 16.4)
    path = engine.cut(info, s, e, [0, 1], tmp_path / "part.ts", exact=exact)
    times = decoded_times(path)
    assert len(times) == round((e - s) * 30)
