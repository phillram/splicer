"""Timestamp lists, as video descriptions write them."""

from __future__ import annotations

import pytest

from splicer import timestamps


def test_description_style():
    text = """Great video!

    00:00 Saria's Song
    04:38 Zelda's Lullaby
    1:02:03 The Long One
    Not a time: 12.5 or 4 PM
    """
    assert timestamps.parse(text) == [
        (0.0, "Saria's Song"), (278.0, "Zelda's Lullaby"), (3723.0, "The Long One")]


@pytest.mark.parametrize("line, t, title", [
    ("4:38 - Zelda's Lullaby", 278, "Zelda's Lullaby"),
    ("[04:38] Zelda's Lullaby", 278, "Zelda's Lullaby"),
    ("Zelda's Lullaby (4:38)", 278, "Zelda's Lullaby"),
    ("Zelda's Lullaby - 4:38", 278, "Zelda's Lullaby"),
    ("3. 4:38 Zelda's Lullaby", 278, "Zelda's Lullaby"),
    ("4:38 – 6:44 Zelda's Lullaby", 278, "Zelda's Lullaby"),
    ("4:38.5 Half", 278.5, "Half"),
    ("4:38", 278, ""),
])
def test_line_shapes(line, t, title):
    assert timestamps.parse(line) == [(pytest.approx(t), title)]


def test_bad_times_and_repeats():
    assert timestamps.parse("4:75 Nope\n1:00 A\n1:00 B\n0:30 C") == [(30.0, "C"), (60.0, "A")]


def test_best_source():
    tags = {"title": "Video 1:00", "DESCRIPTION": "Intro\n0:00 One\n1:00 Two\n2:00 Three",
            "comment": "0:00 Only one"}
    assert timestamps.best_source(tags).startswith("Intro")
    assert timestamps.best_source({"comment": "0:00 Only one"}) == ""


def test_stamp():
    assert [timestamps.stamp(t) for t in (0, 278.9, 3723.2)] == ["0:00", "4:38", "1:02:03"]
