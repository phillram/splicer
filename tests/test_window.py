"""The window itself, driven without being seen: flows that once went wrong."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from splicer import app as appmod

FIXTURES = Path(__file__).parent / "fixtures"
MKV = FIXTURES / "chaptered.mkv"


def pump(window, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        window.update()
        time.sleep(0.01)


def wait(window, test, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        window.update()
        if test():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def window(tmp_path, monkeypatch):
    # Its own empty settings, and nothing saved anywhere
    monkeypatch.setattr(appmod, "SETTINGS_FILE", tmp_path / "settings.json")
    a = appmod.App()
    a.attributes("-alpha", 0.0)  # never shows on screen
    a.lower()
    a._save = lambda: None
    a._save_project_now = lambda: None
    a._restore_project = lambda: False
    a.open_video(MKV)
    wait(a, lambda: a.info is not None and not a.busy)
    pump(a, 0.3)
    yield a
    a.player.close()
    a.destroy()


def test_undo_brings_back_the_cuts_setting(window):
    a = window
    a.cut_mode_var.set(appmod.CUT_MODES["anywhere"])
    a._cut_mode_changed()
    a.joined_var.set(False)
    part = a.parts[0]
    a.move_cut(part, "end", a.snap(9.47))
    assert part.end == pytest.approx(9.467, abs=0.001)
    a.cut_mode_var.set(appmod.CUT_MODES["keyframes"])
    a._cut_mode_changed()
    assert a.parts[0].end == 9.0
    a.do_undo()
    # Back to how it was: off a keyframe, and cuts that can go anywhere
    assert a.cut_mode == "anywhere" and a.anywhere
    assert a.parts[0].end == pytest.approx(9.467, abs=0.001)


def test_naming_after_a_cuts_change_is_its_own_undo(window):
    a = window
    a.cut_mode_var.set(appmod.CUT_MODES["anywhere"])
    a._cut_mode_changed()
    a.joined_var.set(False)
    a.move_cut(a.parts[0], "end", a.snap(9.47))
    a.title_var.set("X")
    a.cut_mode_var.set(appmod.CUT_MODES["keyframes"])
    a._cut_mode_changed()
    a.title_var.set("XY")
    a.do_undo()
    # Only the typing is undone; the Cuts change (and the cut it moved) stays
    assert a.parts[0].title == "X" and a.cut_mode == "keyframes"
    assert a.parts[0].end == 9.0


def test_joining_empty_parts(window):
    a = window
    a.parts[0].end = a.parts[0].start
    a.parts[1].end = a.parts[1].start
    a._refresh_parts()
    a.tree.selection_set([a.parts[0].iid, a.parts[1].iid])
    a._join_parts()  # says so, rather than failing
    assert len(a.parts) == 3


def test_timestamps_window_belongs_to_its_video(window):
    a = window
    a.clipboard_clear()
    a.clipboard_append("0:00 A\n0:05 B")
    a.open_import()
    assert a.import_dialog is not None
    a.busy = "splicing"
    a.import_times([(0.0, "A"), (5.0, "B")])  # nothing while splicing
    assert len(a.parts) == 3
    a.busy = ""
    a.open_video(MKV)
    assert a.import_dialog is None  # closed with the old video
    a.import_times([(0.0, "A"), (5.0, "B")])  # nothing while reading
    wait(a, lambda: a.info is not None and not a.busy)


def test_ctrl_arrows_on_the_parts_list(window):
    a = window
    a.seek(10.0)
    pump(a, 0.3)
    a.tree.focus_set()
    a.tree.event_generate("<Control-Left>")
    assert a.position == 9.0  # the keyframe before, not 5 seconds back


def test_refreshing_keeps_the_part_you_are_on(window):
    a = window
    a.tree.selection_set([a.parts[0].iid, a.parts[2].iid])
    a.tree.focus(a.parts[2].iid)
    a._show_selected()
    a.numbered_var.set(False)
    a._refresh_parts()
    assert a._current() is a.parts[2]


def test_left_out_stretches_are_locked_while_splicing(window):
    a = window
    a._select(a.parts[1])
    a.seek(15.0)
    pump(a, 0.2)
    a.cut_out()
    part = a._current()
    assert part.removed
    a.busy = "splicing"
    a._sync()
    assert all(w.instate(["disabled"]) for w in a.removed_widgets)
    a.put_back(part, 0)
    assert part.removed  # unchanged
    a.busy = ""


def test_splice_takes_a_time_still_being_typed(window, tmp_path):
    a = window
    for p in a.parts:
        p.use = p is a.parts[0]
    a._refresh_parts()
    a._select(a.parts[0])
    a.output_var.set(str(tmp_path / "out"))
    a.time_vars["end"].set("0:06")  # typed, but Enter never pressed
    a._splice()
    wait(a, lambda: not a.busy, 60)
    assert a.parts[0].end == 6.0
    files = list((tmp_path / "out").iterdir())
    assert len(files) == 1
