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
    # Message boxes are noted, never shown: nothing may pop up on the screen
    shown = []
    for name in ("showinfo", "showerror", "showwarning"):
        monkeypatch.setattr(appmod.messagebox, name,
                            lambda *args, n=name, **kw: shown.append((n, args)))
    for name in ("askyesno", "askokcancel", "askyesnocancel"):
        monkeypatch.setattr(appmod.messagebox, name,
                            lambda *args, n=name, **kw: shown.append((n, args)) or True)
    a = appmod.App()
    a.shown_messages = shown
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
    a.seek(18.0)
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


def click_checkbox(a, part, shift=False):
    x, y, w, h = a.tree.bbox(part.iid, "#1")
    state = 1 if shift else 0
    a.tree.event_generate("<Shift-Button-1>" if shift else "<Button-1>",
                          x=x + w // 2, y=y + h // 2, state=state)
    a.update()


def test_shift_click_checks_the_parts_in_between(window):
    a = window
    click_checkbox(a, a.parts[0])  # off
    assert [p.use for p in a.parts] == [False, True, True]
    click_checkbox(a, a.parts[2], shift=True)  # off, and everything up to it
    assert [p.use for p in a.parts] == [False, False, False]
    click_checkbox(a, a.parts[1])  # on
    click_checkbox(a, a.parts[0], shift=True)
    assert [p.use for p in a.parts] == [True, True, False]
    a.do_undo()  # one step for the whole range
    assert [p.use for p in a.parts] == [False, True, False]


def test_ctrl_a_selects_every_part(window):
    a = window
    a.tree.focus_set()
    a.tree.event_generate("<Control-a>")
    assert len(a._selected_parts()) == 3


def test_busy_buttons_only_while_busy(window, tmp_path):
    a = window
    assert not a.cancel_btn.winfo_ismapped() and not a.progress.winfo_ismapped()
    a.busy = "splicing"
    a._sync()
    a.update()
    assert a.cancel_btn.winfo_ismapped() and a.progress.winfo_ismapped()
    a.busy = ""
    a.last_output = tmp_path
    a._sync()
    a.update()
    assert not a.cancel_btn.winfo_ismapped() and a.open_btn.winfo_ismapped()


def test_the_file_name_follows_the_settings(window):
    a = window
    a._select(a.parts[1])
    assert "Saved as 02 - " in a.length_label.cget("text")
    a.numbered_var.set(False)
    a._refresh_parts()
    assert "Saved as 02 - " not in a.length_label.cget("text")
    click_checkbox(a, a.parts[1])
    assert "not saved" in a.length_label.cget("text")


def test_sound_only_from_a_video_with_cover_art(window, tmp_path):
    a = window
    a.open_video(FIXTURES / "withcover.mp4")
    wait(a, lambda: a.info is not None and not a.busy)
    a.save_as_var.set(appmod.SAVE_AS["sound"])
    a._save_as_changed()
    for p in a.parts:
        p.use = p is a.parts[0]
    a._refresh_parts()
    a.output_var.set(str(tmp_path / "out"))
    a._splice()
    wait(a, lambda: not a.busy, 60)
    files = list((tmp_path / "out").iterdir())
    assert len(files) == 1 and files[0].suffix == ".m4a"
    assert not a.shown_messages


def test_quick_clicks_end_where_the_last_one_points(window):
    # Frame back a bunch, faster than the player can find frames, then back 5 s
    a = window
    a.seek(10.0)
    pump(a, 0.3)
    for _ in range(8):
        a.step(-1)
    a.step(1)
    a.jump(-5)
    expected = 10.0 - 7 / 30 - 5
    assert a.position == pytest.approx(expected, abs=0.02)  # straight away
    wait(a, lambda: a.player.moves_done == a.player.moves_sent)
    pump(a, 0.3)
    assert a.position == pytest.approx(expected, abs=0.02)
    assert a.player.position == pytest.approx(expected, abs=0.02)
    a.step(1)
    pump(a, 0.3)
    assert a.player.position == pytest.approx(expected + 1 / 30, abs=0.02)


def test_buttons_leave_the_keyboard_to_the_player(window):
    # A clicked button kept the keyboard, so Space pressed it again instead of playing
    a = window
    for w in (a.play_btn, a.full_btn, a.frame_btn, a.numbered_check, a.splice_btn):
        assert str(w.cget("takefocus")) == "0"
    # Space plays from a pick-from-list box, but not from a box being typed in
    assert not a._typing(a.subtitle_box)
    assert a._typing(a.title_entry)


def test_a_click_leaves_the_box_being_typed_in(window, monkeypatch):
    a = window
    moved = []
    monkeypatch.setattr(a, "focus_get", lambda: a.title_entry)
    monkeypatch.setattr(a, "focus_set", lambda: moved.append(True))

    class Click:
        widget = a.play_btn
    a._clicked(Click)
    assert moved
    moved.clear()
    Click.widget = a.time_entries["start"] if hasattr(a, "time_entries") else a.output_entry
    a._clicked(Click)  # clicking another box to type in keeps typing
    assert not moved


def test_the_picture_is_left_to_the_player(window):
    # Tk painting the video black as the window changed size made it flash
    assert window.video.cget("bg") == ""


def test_shortcuts_window(window):
    a = window
    a.show_shortcuts()
    win = a.shortcuts_window
    win.attributes("-alpha", 0.0)
    a.show_shortcuts()  # a second click brings back the same one
    assert a.shortcuts_window is win
    win.destroy()


def test_clicking_the_empty_player_opens_a_video(window, monkeypatch):
    a = window
    opened, toggled = [], []
    monkeypatch.setattr(a, "_choose_file", lambda: opened.append(True))
    monkeypatch.setattr(a, "toggle_play", lambda: toggled.append(True))
    a._video_clicked()
    assert toggled and not opened  # a video is open: play or pause
    a.info = None
    a._video_clicked()
    assert opened


def test_cutting_out_marks_the_start_then_the_end(window):
    a = window
    a._select(a.parts[1])
    part = a._current()
    a.seek(15.0, pause=True)
    pump(a, 0.2)
    a.cut_out()
    assert part.removed == []  # only marked: nothing skipped, no jump
    assert a.position == 15.0
    assert a.cut_btn.cget("text") == "End the cut here"
    a.seek(18.0, pause=True)
    pump(a, 0.2)
    a.cut_out()
    assert part.removed == [[15.0, 18.0]]
    assert a.cut_btn.cget("text") == "Cut out a section"
    assert a.position == 18.0


def test_a_marked_cut_can_be_cancelled(window):
    a = window
    a._select(a.parts[1])
    a.seek(15.0, pause=True)
    pump(a, 0.2)
    a.cut_out()
    a.cancel_cut()
    assert a.cut_from is None and a._current().removed == []
    assert a.cut_btn.cget("text") == "Cut out a section"


def test_now_past_the_end_moves_the_whole_stretch(window):
    a = window
    a._select(a.parts[1])
    part = a._current()
    part.removed = [[12.0, 13.5]]  # keyframes are every 1.5 s
    a._show_selected()
    a.seek(16.5, pause=True)
    pump(a, 0.2)
    a.move_removed(part, 0, 0, a.snap(a.position), asked=a.position, carry=True)
    assert part.removed == [[16.5, 18.0]]
    a.seek(15.0, pause=True)
    pump(a, 0.2)
    a.move_removed(part, 0, 1, a.snap(a.position), asked=a.position, carry=True)
    assert part.removed == [[13.5, 15.0]]


def test_moving_a_cut_leaves_the_next_part_alone_by_default(window):
    a = window
    first, second = a.parts[0], a.parts[1]
    assert abs(first.end - second.start) < 1e-6  # they share a cut
    start = second.start
    a.move_cut(first, "end", a.step_point(first.end, -1))
    assert first.end < start and second.start == start


def test_moving_a_cut_can_move_the_next_part_too(window):
    a = window
    a.joined_var.set(True)
    first, second = a.parts[0], a.parts[1]
    a.move_cut(first, "end", a.step_point(first.end, -1))
    assert second.start == first.end
