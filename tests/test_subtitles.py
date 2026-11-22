"""Reading subtitle text for the player."""

from __future__ import annotations

from splicer.subtitles import Cue, active, packet_text, tidy


def test_srt_and_tags():
    assert packet_text("subrip", b"<i>Hello</i>\r\n<b>there</b>") == "Hello\nthere"


def test_ass_matroska_and_dialogue():
    assert packet_text("ass", rb"3,0,Default,,0,0,0,,{\an8}Top, with a comma\Nsecond") == \
        "Top, with a comma\nsecond"
    assert packet_text("ass", rb"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi") == "Hi"
    assert packet_text("ass", rb"1,0,Default,,0,0,0,,{\p1}m 0 0 l 10 10") == ""


def test_mov_text():
    assert packet_text("mov_text", b"\x00\x05Hello\x00\x00styles") == "Hello"


def test_tidy_and_active():
    cues = tidy([Cue(5, 0, "no end"), Cue(1, 3, "first"), Cue(2, 4, "overlap"), Cue(9, 9.5, "")])
    assert [(c.start, c.end, c.text) for c in cues] == [
        (1, 3, "first"), (2, 4, "overlap"), (5, 9.0, "no end")]
    starts = [c.start for c in cues]
    assert active(cues, starts, 2.5) == "first\noverlap"
    assert active(cues, starts, 4.5) == ""
    assert active(cues, starts, 8.9) == "no end"


def test_a_long_line_under_many_short_ones():
    cues = tidy([Cue(0.0, 100.0, "sign")] + [Cue(i, i + 0.5, f"line{i}") for i in range(1, 21)])
    starts = [c.start for c in cues]
    assert active(cues, starts, 18.2) == "sign\nline18"
