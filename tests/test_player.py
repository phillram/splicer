"""The built-in player, without a window: where it stops, loops and steps."""

from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

from splicer import engine
from splicer.player import Player

MKV = Path(__file__).parent / "fixtures" / "chaptered.mkv"


@pytest.fixture
def player():
    info = engine.probe(MKV)
    events: queue.Queue = queue.Queue()
    p = Player(events.put)
    p.set_volume(0.0)
    p.load(info.path, info.origin, info.duration, 1)
    wait(p, events, lambda e: e[0] == "loaded")
    yield p, events
    p.close()


def wait(p, events, test, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            event = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if test(event):
            return event
    raise AssertionError("timed out")


def settle(p, events, seconds=0.3):
    time.sleep(seconds)
    while not events.empty():
        events.get()


def test_seek_is_exact(player):
    p, events = player
    p.seek(5.0)
    settle(p, events)
    assert p.position == pytest.approx(5.0, abs=0.02)
    p.step(1)
    settle(p, events)
    assert p.position == pytest.approx(5.0 + 1 / 30, abs=0.02)
    p.step(-1)
    settle(p, events)
    assert p.position == pytest.approx(5.0, abs=0.02)


def test_stops_on_the_parts_last_frame(player):
    p, events = player
    p.set_range(10.5, 12.0)
    p.set_end_mode("stop")
    p.seek(11.5)
    p.play()
    wait(p, events, lambda e: e == ("state", True))
    wait(p, events, lambda e: e == ("state", False), timeout=5)
    settle(p, events)
    assert p.position == pytest.approx(12.0 - 1 / 30, abs=0.01)
    # Playing again starts the part over
    p.play()
    wait(p, events, lambda e: e[0] == "pos" and e[1] < 11.0)
    p.pause()


def test_loops_the_part(player):
    p, events = player
    p.set_range(10.5, 12.0)
    p.set_end_mode("loop")
    p.seek(11.6)
    p.play()
    wait(p, events, lambda e: e[0] == "pos" and e[1] < 11.0, timeout=5)
    wait(p, events, lambda e: e[0] == "pos" and e[1] > 10.8, timeout=5)  # and on it goes
    assert p.playing
    p.pause()


def test_keeps_playing_past_the_end(player):
    p, events = player
    p.set_range(10.5, 12.0)
    p.set_end_mode("continue")
    p.seek(11.7)
    p.play()
    wait(p, events, lambda e: e[0] == "pos" and e[1] > 12.3, timeout=5)
    p.pause()


def test_a_part_at_the_end_of_the_video_loops(player):
    # Its last frame comes before the sound ends, so the picture runs out first
    p, events = player
    p.set_range(20.0, p.duration)
    p.set_end_mode("loop")
    p.seek(p.duration - 1.0)
    p.play()
    wait(p, events, lambda e: e[0] == "pos" and e[1] < 21.0, timeout=5)
    assert p.playing
    p.pause()


def test_play_again_after_stopping_at_the_end_of_the_video(player):
    p, events = player
    p.set_range(20.0, p.duration)
    p.set_end_mode("stop")
    p.seek(p.duration - 0.5)
    p.play()
    wait(p, events, lambda e: e == ("state", True))
    wait(p, events, lambda e: e == ("state", False), timeout=5)
    settle(p, events)
    p.play()
    wait(p, events, lambda e: e[0] == "pos" and e[1] < 21.0, timeout=5)
    p.pause()
