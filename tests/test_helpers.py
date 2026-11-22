"""Times and file names as the window shows and takes them."""

from __future__ import annotations

import pytest

from splicer import engine
from splicer.app import Part, file_names, fmt_time, parse_time, parts_from_times


@pytest.mark.parametrize("text, seconds", [
    ("4:38", 278.0), ("4:38.245", 278.245), ("4:38,245", 278.245), ("278.245", 278.245),
    ("1:02:03.5", 3723.5), (" 0:05 ", 5.0), ("0", 0.0),
])
def test_parse_time(text, seconds):
    assert parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "4:75", "-3", "1:-2"])
def test_parse_time_rejects(text):
    assert parse_time(text) is None


def test_fmt_time():
    assert fmt_time(278.245) == "4:38.245"
    assert fmt_time(3723.5) == "1:02:03.500"
    assert fmt_time(0) == "0:00.000"
    assert parse_time(fmt_time(1915.101)) == pytest.approx(1915.101)


def test_file_names_numbered_and_unchecked_skipped():
    parts = [Part("Saria’s Song", 0, 1), Part("Skip me", 1, 2, use=False),
             Part("Song Of Time", 2, 3)]
    names = file_names(parts, ".mkv", numbered=True)
    assert [names.get(p) for p in parts] == [
        "01 - Saria’s Song.mkv", None, "02 - Song Of Time.mkv"]


def test_file_names_clash():
    parts = [Part("Intro", 0, 1), Part("intro", 1, 2), Part("Intro", 2, 3)]
    assert list(file_names(parts, ".mp4", numbered=False).values()) == [
        "Intro.mp4", "intro (2).mp4", "Intro (3).mp4"]


def test_file_names_width_grows():
    parts = [Part(f"P{n}", n, n + 1) for n in range(120)]
    assert file_names(parts, ".mkv", numbered=True)[parts[0]] == "001 - P0.mkv"


def test_parts_from_times():
    info = engine.probe(__import__("pathlib").Path(__file__).parent / "fixtures" / "chaptered.mkv")
    parts = parts_from_times(info, [(0, "A"), (10, ""), (20, "C"), (99, "Past the end")])
    assert [(p.title, p.start, p.end) for p in parts] == [
        ("A", 0.0, 10.5), ("Part 2", 10.5, 19.5), ("C", 19.5, info.duration)]
