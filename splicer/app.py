"""The Splicer window: open a video, pick its tracks, watch it and tune the parts
(one per chapter to begin with), then write each part to its own file,
losslessly."""

from __future__ import annotations

import bisect
import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from splicer import __version__, engine, projects, timestamps
from splicer.engine import EPS, Info
from splicer.paths import DATA_DIR, ICON, SETTINGS_FILE
from splicer.player import Player
from splicer.timeline import Overview, Timeline

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
except Exception:  # drag and drop is a nicety; everything else works without it
    DND_FILES, BaseTk = None, tk.Tk

BG, PANEL, FIELD, HOVER = "#1e1f22", "#2b2d31", "#383a40", "#4e5058"
FG, MUTED, ACCENT = "#e6e6e6", "#9a9ca3", "#5865f2"
GOOD, WARN, BAD = "#3ba55d", "#faa61a", "#ed4245"

CHECKED, UNCHECKED = "☑", "☐"
DEFAULT_NAME = re.compile(r"Part \d+")
SYMBOLS = ("Segoe UI Symbol", 11)
UNDO_LIMIT = 200
RECENT_LIMIT = 12

END_MODES = {"stop": "Stop", "loop": "Loop the part", "continue": "Keep playing"}
CUT_MODES = {"keyframes": "On keyframes (lossless)", "anywhere": "Anywhere (re-encodes near cuts)"}
SAVE_AS = {"video": "Video", "sound": "Sound only"}

PART_COLUMNS = (  # id, heading, width, anchor, stretch
    ("use", "", 34, "center", False),
    ("n", "#", 36, "e", False),
    ("title", "Name", 220, "w", True),
    ("start", "Start", 88, "e", False),
    ("end", "End", 88, "e", False),
    ("length", "Length", 80, "e", False),
)

SHORTCUTS = (  # keys, what they do; then what the mouse does
    ("Space", "Play / pause"),
    ("← →", "Back / forward 5 seconds"),
    (", .", "One frame back / forward"),
    ("Ctrl+← Ctrl+→", "Keyframe before / after"),
    ("Home End", "Start / last frame of the part"),
    ("Page Up Page Down", "Previous / next part"),
    ("I O", "Start / end the part at the current time"),
    ("Ctrl+Z Ctrl+Y", "Undo / redo"),
    ("Ctrl+A", "Select every part"),
    ("+ −", "Zoom the timeline in / out"),
    ("S", "Save the frame as a picture"),
    ("F", "Full screen (Esc leaves)"),
    ("M", "Mute"),
)
MOUSE = ("On the timeline, drag a cut to move it and click to jump there. The wheel "
         "scrolls; Ctrl+wheel zooms. Shift+click a part's checkbox to check or uncheck "
         "every part up to the last one you clicked.")


@dataclass(eq=False)
class Part:
    title: str
    start: float
    end: float
    use: bool = True
    removed: list = field(default_factory=list)  # [[from, to], …] left out of the middle
    iid: str = ""

    @property
    def kept(self) -> list[tuple[float, float]]:
        return engine.segments(self.start, self.end, self.removed)

    @property
    def length(self) -> float:
        """How long the part's file will be: removed stretches don't count."""
        return sum(e - s for s, e in self.kept)

    def into(self, t: float) -> float:
        """How far t is into the part's file."""
        return sum(min(max(t - s, 0.0), e - s) for s, e in self.kept) + min(t - self.start, 0)


def tidy_removed(part: Part) -> None:
    """Removed stretches inside the part, in order, not overlapping. One that reaches
    an end of the part just moves that end in."""
    stretches = sorted([max(r0, part.start), min(r1, part.end)] for r0, r1 in part.removed)
    merged: list[list[float]] = []
    for r0, r1 in stretches:
        if r1 - r0 <= EPS:
            continue
        if merged and r0 <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], r1)
        else:
            merged.append([r0, r1])
    while merged and merged[0][0] <= part.start + EPS and merged[0][1] < part.end - EPS:
        part.start = merged.pop(0)[1]
    while merged and merged[-1][1] >= part.end - EPS and merged[-1][0] > part.start + EPS:
        part.end = merged.pop()[0]
    part.removed = merged


# --- Small helpers, as in Leveler --------------------------------------------------

class Tooltip:
    """A short explanation that appears when the pointer rests on a widget."""

    def __init__(self, widget: tk.Misc, text: str):
        self.widget, self.text, self.tip, self.pending = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress>", self._hide, add=True)

    def _schedule(self, _):
        self.pending = self.widget.after(500, self._show)

    def _show(self):
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget, bg=HOVER)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#111214", fg=FG, justify="left",
                 wraplength=380, padx=8, pady=5, font=("Segoe UI", 9)
                 ).pack(padx=1, pady=1)

    def _hide(self, _=None):
        if self.pending:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def tip(widget, text):
    Tooltip(widget, text)
    return widget


def dark_title_bar(window: tk.Misc) -> None:
    """Ask Windows 10/11 for a dark title bar. Harmless anywhere else."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-20H1 number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
        flags = 0x27  # SWP_NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED | NOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)
    except Exception:
        pass


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def on_screen(geometry: str) -> bool:
    """Is a saved "WxH+X+Y" still on a connected monitor?"""
    m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geometry or "")
    if not m:
        return False
    x, y = int(m[3]), int(m[4])
    try:
        metric = ctypes.windll.user32.GetSystemMetrics
        left, top, width, height = (metric(i) for i in (76, 77, 78, 79))
    except Exception:
        return True
    return left <= x + 100 < left + width and top <= y < top + height - 40


def fmt_time(t: float) -> str:
    """1:02:03.456, or 4:38.245 under an hour."""
    ms = int(round(max(t, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02}:{s:02}.{ms:03}" if h else f"{m}:{s:02}.{ms:03}"


def parse_time(text: str) -> float | None:
    """Seconds from "1:02:03.5", "4:38", "4:38,245" or "278.245"; None if it isn't a time."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    pieces = text.split(":")
    if len(pieces) > 3:
        return None
    try:
        values = [float(p) if i == len(pieces) - 1 else int(p) for i, p in enumerate(pieces)]
    except ValueError:
        return None
    if any(v < 0 for v in values) or any(v >= 60 for v in values[1:]):
        return None
    seconds = 0.0
    for v in values:
        seconds = seconds * 60 + v
    return seconds


def fmt_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def file_names(parts: list[Part], ext: str, numbered: bool) -> dict[Part, str]:
    """The file each checked part becomes: "01 - Title.mkv", or "Title.mkv".
    Names that would clash get " (2)", " (3)"…"""
    used = [p for p in parts if p.use]
    width = max(2, len(str(len(used))))
    names, taken = {}, set()
    for n, part in enumerate(used, 1):
        stem = engine.safe_name(part.title)
        if numbered:
            stem = f"{n:0{width}} - {stem}"
        name, k = stem, 2
        while name.lower() in taken:
            name, k = f"{stem} ({k})", k + 1
        taken.add(name.lower())
        names[part] = name + ext
    return names


def parts_from_times(info: Info, found: list[tuple[float, str]], snap=None) -> list[Part]:
    """Parts from a timestamp list: each runs to the next time, the last to the end."""
    snap = snap or (lambda t: engine.snap(info, t))
    parts = []
    found = [(t, title) for t, title in found if t < info.duration - EPS]
    for i, (t, title) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else info.duration
        start, end = snap(t), snap(end)
        parts.append(Part(title or f"Part {i + 1}", start, end, use=end - start > EPS))
    return parts


# --- Full screen -------------------------------------------------------------------

class FullScreen(tk.Toplevel):
    """The video filling the screen, with a bar of controls that shows when the
    mouse moves and hides again when it rests."""

    def __init__(self, app: "App"):
        super().__init__(app, bg="black")
        self.app = app
        self.attributes("-fullscreen", True)
        self.video = tk.Frame(self, bg="")  # the player paints it all: no flashing
        self.video.pack(fill="both", expand=True)
        self.bar = ttk.Frame(self, style="Bar.TFrame", padding=(12, 6))
        self.play_btn = ttk.Button(self.bar, text="▶", style="Transport.TButton",
                                   command=app.toggle_play)
        self.play_btn.pack(side="left")
        ttk.Button(self.bar, text="⏹", style="Transport.TButton",
                   command=app.stop).pack(side="left", padx=(4, 0))
        self.time_label = ttk.Label(self.bar, text="", style="BarTime.TLabel")
        self.time_label.pack(side="left", padx=10)
        ttk.Button(self.bar, text="Leave full screen", command=app.leave_full_screen
                   ).pack(side="right")
        self.overview = Overview(self.bar, app, height=24, bg=FIELD)
        self.overview.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.hide_job = None
        self.bind("<Motion>", self._woken)
        self.video.bind("<Double-1>", lambda e: app.leave_full_screen())
        self.video.bind("<ButtonRelease-1>", lambda e: app.toggle_play())
        self.video.bind("<Configure>", lambda e: app.redraw_soon())
        self.video.bind("<Expose>", lambda e: app.redraw_soon())
        self.protocol("WM_DELETE_WINDOW", app.leave_full_screen)
        self._woken()

    def _woken(self, _=None):
        if not self.bar.winfo_ismapped():
            self.bar.pack(side="bottom", fill="x", before=self.video)
        self.video.configure(cursor="")
        if self.hide_job:
            self.after_cancel(self.hide_job)
        self.hide_job = self.after(2500, self._rest)

    def _rest(self):
        self.hide_job = None
        x, y = self.winfo_pointerxy()
        over = self.winfo_containing(x, y)
        if over is not None and str(over).startswith(str(self.bar)):
            self.hide_job = self.after(2500, self._rest)  # still using the controls
            return
        self.bar.pack_forget()
        self.video.configure(cursor="none")


# --- Importing timestamps ----------------------------------------------------------

class ImportDialog(tk.Toplevel):
    """Paste (or check) a timestamp list and turn it into parts."""

    def __init__(self, app: "App", text: str, source: str):
        super().__init__(app, bg=BG)
        self.app = app
        self.title("Parts from timestamps")
        self.transient(app)
        self.geometry(f"{int(640 * app.scale)}x{int(520 * app.scale)}")
        self.bind("<Map>", lambda e: e.widget is self and dark_title_bar(self), add=True)
        box = ttk.Frame(self, padding=12)
        box.pack(fill="both", expand=True)
        words = ttk.Label(box, text=source, style="Note.TLabel", justify="left")
        words.pack(anchor="w", fill="x")
        words.bind("<Configure>", lambda e: words.configure(wraplength=max(e.width - 4, 100)))
        row = ttk.Frame(box)
        row.pack(side="bottom", fill="x")  # before the text box, so it never pushes them off
        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True, pady=8)
        self.text = tk.Text(holder, bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                            wrap="word", font=("Segoe UI", 9), padx=6, pady=4,
                            selectbackground=ACCENT, undo=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.text.insert("1.0", text)
        self.text.bind("<<Modified>>", self._changed)
        self.found_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.found_label.pack(side="left")
        ttk.Button(row, text="Make parts", style="Accent.TButton",
                   command=self._make).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))
        self._changed()
        self.text.focus_set()

    def _found(self):
        return timestamps.parse(self.text.get("1.0", "end"))

    def _changed(self, _=None):
        self.text.edit_modified(False)
        found = self._found()
        if found:
            first = ", ".join(f"{timestamps.stamp(t)} {title}".strip()
                              for t, title in found[:3])
            more = "…" if len(found) > 3 else ""
            self.found_label.configure(
                text=f"{len(found)} timestamp{'s' * (len(found) != 1)}: {first}{more}")
        else:
            self.found_label.configure(text="No timestamps yet: lines like “4:38 Title”.")

    def _make(self):
        found = self._found()
        if not found:
            self.bell()
            return
        self.app.import_times(found)
        self.destroy()


# --- The window -------------------------------------------------------------------

class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Splicer")
        self.configure(bg=BG)
        self.minsize(1000, 680)
        if ICON.exists():
            try:
                self.iconbitmap(default=str(ICON))
            except tk.TclError:
                pass

        self.settings = self._load_settings()
        self.info: Info | None = None
        self.parts: list[Part] = []
        self.track_use: dict[int, bool] = {}
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = ""                     # "", "reading" or "splicing"
        self.load_id = 0                   # which load an event belongs to
        self.last_output: Path | None = None
        self.position = 0.0                # the player's playhead
        self.playing = False
        self.shown_part: Part | None = None
        self.full_screen: FullScreen | None = None
        self.redraw_job = None
        self.save_job = None
        self.muted = False
        self._filling = False
        self.undo: list = []
        self.redo: list = []
        self.last_edit = None              # consecutive edits of one kind undo together
        self.subtitle_choices: dict[str, int | None] = {"Off": None}
        self.cant_reencode = ""            # why this video's cuts must stay on keyframes
        self.wave: list[float] | None = None  # the sound's shape, for the timeline
        self.wave_for = None               # (load, sound track) it belongs to

        self._style()
        self._build()
        self.player = Player(self.events.put)
        self.player.set_volume(self._volume())
        self.player.set_end_mode(self.end_mode)
        self._sync()
        geometry = self.settings.get("geometry", "")
        width = min(int(1500 * self.scale), int(self.winfo_screenwidth() * 0.94))
        height = min(int(940 * self.scale), int(self.winfo_screenheight() * 0.9))
        self.geometry(geometry if on_screen(geometry) else f"{width}x{height}")
        self.bind("<Map>", self._mapped, add=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._bind_keys()
        self.after(50, self._pump)

    def _mapped(self, event):
        if event.widget is not self:
            return
        dark_title_bar(self)
        if not getattr(self, "_sashes_set", False):
            self._sashes_set = True
            self.after(150, self._restore_sashes)
        self.player.set_target(self.video.winfo_id())

    # --- Settings ---

    SETTING_TYPES = {"geometry": str, "numbered": bool, "move_neighbor": bool, "volume": (int, float),
                     "end_mode": str, "save_as": str, "cut_mode": str, "last_dir": str,
                     "recent": list, "panes": list}

    def _load_settings(self) -> dict:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Anything of the wrong kind (a hand-edited or damaged file) is left out
        return {k: v for k, v in data.items()
                if k not in self.SETTING_TYPES or isinstance(v, self.SETTING_TYPES[k])}

    def _save(self):
        self.settings["geometry"] = self.geometry()
        self.settings["numbered"] = bool(self.numbered_var.get())
        self.settings["move_neighbor"] = bool(self.joined_var.get())
        self.settings.pop("joined", None)  # was on by default; editing one part moved another
        self.settings["volume"] = int(self.volume_var.get())
        self.settings["end_mode"] = self.end_mode
        self.settings["save_as"] = self.save_as
        self.settings["cut_mode"] = self.cut_mode
        try:
            self.settings["panes"] = [self.upper.sashpos(0), self.outer.sashpos(0)]
            self.settings.pop("sashes", None)  # 1.1's layout
        except tk.TclError:
            pass
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            write_json(SETTINGS_FILE, self.settings)
        except OSError:
            pass

    def _restore_sashes(self):
        self.update_idletasks()
        saved = self.settings.get("panes")
        ok = isinstance(saved, list) and len(saved) == 2 and \
            all(isinstance(v, int) for v in saved)
        width, height = self.upper.winfo_width(), self.outer.winfo_height()
        main = saved[0] if ok else int(width * 0.42)
        lower = saved[1] if ok else height - int(165 * self.scale)
        try:
            self.upper.sashpos(0, min(max(main, 320), width - 360))
            self.outer.sashpos(0, min(max(lower, int(height * 0.55)),
                                      height - int(110 * self.scale)))
        except tk.TclError:
            pass

    # --- Building ---

    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                     bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD,
                     troughcolor=PANEL, arrowcolor=FG, selectbackground=ACCENT,
                     selectforeground="white", insertcolor=FG, font=("Segoe UI", 9))
        st.map(".", foreground=[("disabled", "#62646b")])
        st.configure("TButton", background=FIELD, padding=(8, 2), borderwidth=0, width=-4)
        st.map("TButton", background=[("pressed", ACCENT), ("active", HOVER)])
        st.configure("Accent.TButton", background=ACCENT, foreground="white")
        st.map("Accent.TButton", background=[("active", "#4752c4"), ("disabled", FIELD)])
        st.configure("Step.TButton", padding=(4, 1), width=-2)
        st.configure("Transport.TButton", font=SYMBOLS, padding=(6, 0), width=-2)
        st.configure("TMenubutton", background=FIELD, foreground=FG, padding=(8, 2),
                     arrowcolor=FG, borderwidth=0)
        st.map("TMenubutton", background=[("active", HOVER)])
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Time.TLabel", font=("Segoe UI", 10))
        st.configure("Bar.TFrame", background=PANEL)
        st.configure("Bar.TLabel", background=PANEL, foreground=MUTED)
        st.configure("BarTime.TLabel", background=PANEL, foreground=FG, font=("Segoe UI", 10))
        st.configure("BarStrong.TLabel", background=PANEL, foreground=FG,
                     font=("Segoe UI", 10, "bold"))
        st.configure("Section.TLabel", foreground=FG, font=("Segoe UI", 9, "bold"))
        st.configure("Note.TLabel", foreground=MUTED)
        st.configure("TSeparator", background=HOVER)
        for style, bg in (("TCheckbutton", BG), ("Bar.TCheckbutton", PANEL)):
            st.configure(style, background=bg, indicatorbackground=FIELD,
                         indicatorforeground="white", indicatormargin=(0, 0, 4, 0))
            st.map(style, background=[("active", bg)],
                   indicatorbackground=[("selected", ACCENT), ("disabled", PANEL)])
        st.configure("TCombobox", padding=(4, 1), arrowsize=12)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", FG)], background=[("active", HOVER)],
               selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")
        # Buttons, checkboxes and sliders don't keep the keyboard after a click: with
        # it, Space pressed the last button clicked instead of playing or pausing
        for kind in ("TButton", "TCheckbutton", "TMenubutton", "TScale"):
            self.option_add(f"*{kind}.takeFocus", 0)
        self.option_add("*Menu.background", PANEL)
        self.option_add("*Menu.foreground", FG)
        self.option_add("*Menu.activeBackground", ACCENT)
        self.option_add("*Menu.activeForeground", "white")
        self.option_add("*Menu.disabledForeground", "#62646b")
        self.option_add("*Menu.relief", "flat")
        st.configure("TEntry", padding=(4, 2))
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=7, gripcount=0, background=BG,
                     lightcolor=BG, darkcolor=BG, bordercolor=BG)
        st.map("Sash", background=[("active", HOVER)])
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=FIELD, foreground=MUTED, padding=(14, 4),
                     borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", PANEL)],
               foreground=[("selected", FG)], expand=[("selected", (0, 0, 0, 0))])
        st.configure("Tab.TFrame", background=PANEL)
        st.configure("Tab.TLabel", background=PANEL)
        st.configure("TabNote.TLabel", background=PANEL, foreground=MUTED)
        st.configure("TabWarn.TLabel", background=PANEL, foreground=WARN)
        st.configure("Tab.TCheckbutton", background=PANEL, indicatorbackground=FIELD,
                     indicatorforeground="white", indicatormargin=(0, 0, 4, 0))
        st.map("Tab.TCheckbutton", background=[("active", PANEL)],
               indicatorbackground=[("selected", ACCENT), ("disabled", PANEL)])
        self.ui_font = font.Font(family="Segoe UI", size=9)
        self.scale = self.ui_font.measure("0") / 7
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=self.ui_font.metrics("linespace") + 6, borderwidth=0)
        st.map("Treeview", background=[("selected", ACCENT)],
               foreground=[("selected", "white")])
        st.configure("Treeview.Heading", background=FIELD, foreground=MUTED,
                     relief="flat", padding=(4, 2))
        st.map("Treeview.Heading", background=[("active", HOVER)])
        for orient in ("Vertical", "Horizontal"):
            st.configure(f"{orient}.TScrollbar", background=FIELD, troughcolor=PANEL,
                         bordercolor=PANEL, lightcolor=FIELD, darkcolor=FIELD,
                         arrowcolor=MUTED, gripcount=0, arrowsize=12)
            st.map(f"{orient}.TScrollbar", background=[("active", HOVER)])

    def _build(self):
        self._build_top()
        status = ttk.Frame(self, style="Bar.TFrame", padding=(10, 3))
        status.pack(side="bottom", fill="x")
        self.status = ttk.Label(status, text=f"Splicer {__version__}", style="Bar.TLabel")
        self.status.pack(side="left", fill="x", expand=True)

        # Parts and Tracks | Player, over the timeline: drag the gaps to resize
        self.outer = ttk.Panedwindow(self, orient="vertical")
        self.outer.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self.upper = ttk.Panedwindow(self.outer, orient="horizontal")
        self.outer.add(self.upper, weight=1)
        self.tabs = ttk.Notebook(self.upper)
        self.upper.add(self.tabs, weight=1)
        parts = ttk.Frame(self.tabs, style="Tab.TFrame", padding=8)
        tracks = ttk.Frame(self.tabs, style="Tab.TFrame", padding=8)
        self.tabs.add(parts, text="Parts")
        self.tabs.add(tracks, text="Tracks")
        player = ttk.Frame(self.upper, padding=(8, 0, 0, 0))
        self.upper.add(player, weight=2)
        lower = ttk.Frame(self.outer, padding=(0, 6, 0, 0))
        self.outer.add(lower, weight=0)
        self._build_parts(parts)
        self._build_tracks(tracks)
        self._build_player(player)
        self._build_timeline(lower)

        if DND_FILES:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._drop)
            except Exception:
                pass

    def _build_top(self):
        top = ttk.Frame(self, style="Bar.TFrame", padding=(10, 8, 10, 6))
        top.pack(fill="x")
        row = ttk.Frame(top, style="Bar.TFrame")
        row.pack(fill="x")
        tip(ttk.Button(row, text="Open video…", style="Accent.TButton",
                       command=self._choose_file),
            "Pick a video (Ctrl+O). You can also drop one onto the window.").pack(side="left")
        self.recent_menu = tk.Menu(self, tearoff=False, postcommand=self._fill_recent)
        recent = ttk.Menubutton(row, text="Recent", menu=self.recent_menu)
        recent.pack(side="left", padx=(6, 0))
        tip(recent, "Videos you opened lately.")
        tip(ttk.Button(row, text="Keyboard shortcuts", command=self.show_shortcuts),
            "The keys, and what the mouse does on the timeline.").pack(side="right")
        names = ttk.Frame(row, style="Bar.TFrame")
        names.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.file_label = ttk.Label(names, text="No video open", style="BarStrong.TLabel")
        self.file_label.pack(anchor="w")
        self.file_detail = ttk.Label(names, text="Drop a video here, or press Open video.",
                                     style="Bar.TLabel")
        self.file_detail.pack(anchor="w")

        row = ttk.Frame(top, style="Bar.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Save parts in", style="Bar.TLabel").pack(side="left")
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.browse_btn = ttk.Button(row, text="Browse…", command=self._browse_output)
        self.browse_btn.pack(side="left")
        ttk.Label(row, text="Cuts", style="Bar.TLabel").pack(side="left", padx=(12, 4))
        self.cut_mode = self.settings.get("cut_mode", "keyframes")
        if self.cut_mode not in CUT_MODES:
            self.cut_mode = "keyframes"
        self.cut_mode_var = tk.StringVar(value=CUT_MODES[self.cut_mode])
        self.cut_mode_box = ttk.Combobox(row, textvariable=self.cut_mode_var, state="readonly",
                                         width=29, values=list(CUT_MODES.values()))
        self.cut_mode_box.pack(side="left")
        self.cut_mode_box.bind("<<ComboboxSelected>>", lambda e: self._cut_mode_changed())
        tip(self.cut_mode_box,
            "On keyframes: every part is an exact copy of the original, but cuts can only "
            "go on keyframes, every few seconds.\n"
            "Anywhere: cuts go on any frame. Between each cut and the keyframe beside it the "
            "picture is re-encoded, in the same format at very high quality (it looks the "
            "same; that's usually a second or two); everything else, and all of the sound, "
            "is still an exact copy. The timeline marks the re-encoded bits in orange.")
        ttk.Label(row, text="as", style="Bar.TLabel").pack(side="left", padx=(12, 4))
        self.save_as = self.settings.get("save_as", "video")
        if self.save_as not in SAVE_AS:
            self.save_as = "video"
        self.save_as_var = tk.StringVar(value=SAVE_AS[self.save_as])
        self.save_as_box = ttk.Combobox(row, textvariable=self.save_as_var, state="readonly",
                                        width=11, values=list(SAVE_AS.values()))
        self.save_as_box.pack(side="left")
        self.save_as_box.bind("<<ComboboxSelected>>", lambda e: self._save_as_changed())
        tip(self.save_as_box, "Video: every checked track, in a file like the original.\n"
            "Sound only: just the first checked sound track, in its own kind of file "
            "(Opus as .opus, AAC as .m4a, MP3 as .mp3, FLAC as .flac, anything else as "
            ".mka), numbered as tracks of an album named after the video.")
        self.numbered_var = tk.BooleanVar(value=self.settings.get("numbered", True))
        self.numbered_check = tip(
            ttk.Checkbutton(row, text="Number the files", variable=self.numbered_var,
                            style="Bar.TCheckbutton", command=self._refresh_parts),
            "Start each file name with its place in the video: 01 - Name, 02 - Name…")
        self.numbered_check.pack(side="left", padx=(12, 16))
        self.splice_btn = tip(ttk.Button(row, text="Splice", style="Accent.TButton",
                                         command=self._splice),
                              "Write each checked part to its own file. Nothing is "
                              "re-encoded: the parts are exact copies.")
        self.splice_btn.pack(side="right")
        self.cancel_btn = tip(ttk.Button(row, text="Cancel", command=self._cancel),
                              "Stop splicing. The part being written is removed; "
                              "finished ones stay.")
        self.cancel_btn.pack(side="right", padx=(0, 6))
        self.open_btn = tip(ttk.Button(row, text="Open folder", command=self._open_output),
                            "Show the folder the parts were saved in.")
        self.open_btn.pack(side="right", padx=(0, 6))
        self.progress = ttk.Progressbar(row, maximum=1.0, length=120)
        self.cancel_btn.pack_forget()
        self.open_btn.pack_forget()
        self.shown_actions = []

    def _build_tracks(self, box):
        note = ttk.Label(box, text="Checked tracks go into every part, and the first checked "
                         "sound track is the one you hear. Click a track to check or uncheck "
                         "it.", style="TabNote.TLabel", justify="left")
        note.pack(anchor="w", fill="x")
        note.bind("<Configure>", lambda e: note.configure(wraplength=max(e.width - 4, 100)))
        holder = ttk.Frame(box, style="Tab.TFrame")
        holder.pack(fill="both", expand=True, pady=(6, 0))
        self.tracks = ttk.Treeview(holder, columns=("use", "track"), show="", height=3,
                                   selectmode="none")
        self.tracks.column("use", width=34, stretch=False, anchor="center")
        self.tracks.column("track", width=400, stretch=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tracks.yview)
        self.tracks.configure(yscrollcommand=scroll.set)
        self.tracks.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tracks.tag_configure("off", foreground=MUTED)
        self.tracks.bind("<Button-1>", self._toggle_track)

    def _build_parts(self, box):
        head = ttk.Frame(box, style="Tab.TFrame")
        head.pack(fill="x")
        self.parts_note = ttk.Label(head, text="", style="TabNote.TLabel")  # not shown: the
        # count and total are in the tab's name, which leaves the buttons their room
        self.part_buttons = []
        self.more_menu = tk.Menu(self, tearoff=False)
        self.more_menu.add_command(label="Parts from timestamps…", command=self.open_import)
        self.more_menu.add_command(label="Copy the parts as timestamps",
                                   command=self.copy_times)
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Start again from the chapters",
                                   command=self._reset_parts)
        more = ttk.Menubutton(head, text="More", menu=self.more_menu, width=5)
        more.pack(side="right", padx=(4, 0))
        self.part_buttons.append(more)
        self.redo_btn = tip(ttk.Button(head, text="Redo",
                                       command=self.do_redo), "Redo (Ctrl+Y)")
        self.redo_btn.pack(side="right", padx=(4, 0))
        self.undo_btn = tip(ttk.Button(head, text="Undo",
                                       command=self.do_undo), "Undo (Ctrl+Z)")
        self.undo_btn.pack(side="right", padx=(12, 0))
        for text, command, help_text in (
                ("Remove", self._remove_parts, "Remove the selected parts (Delete)."),
                ("Split", self._split_part,
                 "Cut the selected part in two at the current time (or near its middle, "
                 "if the current time isn't inside it)."),
                ("Join", self._join_parts,
                 "Join the selected parts (Ctrl+click or Shift+click to pick them) into one "
                 "part, saved as one file. Whatever lies between them is left out."),
                ("Add part", self._add_part,
                 "Add a part in the gap after the selected one."),
        ):
            b = tip(ttk.Button(head, text=text, command=command), help_text)
            b.pack(side="right", padx=(4, 0))
            self.part_buttons.append(b)
            if text == "Join":
                self.join_btn = b

        # The selected part, under the list; the list gives way when space is short
        edit = ttk.Frame(box, style="Tab.TFrame", padding=(0, 8, 0, 0))
        edit.pack(side="bottom", fill="x")
        holder = ttk.Frame(box, style="Tab.TFrame")
        holder.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(holder, columns=[c[0] for c in PART_COLUMNS],
                                 show="headings", selectmode="extended", height=4)
        for cid, heading, width, anchor, stretch in PART_COLUMNS:
            self.tree.heading(cid, text=heading, anchor=anchor)
            self.tree.column(cid, width=width, minwidth=30, anchor=anchor, stretch=stretch)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.tag_configure("off", foreground=MUTED)
        self.tree.tag_configure("empty", foreground=BAD)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
        self.tree.bind("<Delete>", lambda e: self._remove_parts())
        self.tree.bind("<Control-a>", lambda e: (
            self.tree.selection_set(self.tree.get_children()), "break")[1])
        self.tree.bind("<Double-1>", lambda e: (self.title_entry.focus_set(),
                                                self.title_entry.select_range(0, "end")))

        edit.columnconfigure(1, weight=1)
        ttk.Label(edit, text="Name", style="Tab.TLabel").grid(row=0, column=0, sticky="w",
                                                             pady=(0, 6))
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(edit, textvariable=self.title_var)
        self.title_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.title_var.trace_add("write", lambda *a: self._title_changed())
        self.edit_widgets = [self.title_entry]
        self.time_vars, self.side_notes = {}, {}
        for r, side in enumerate(("start", "end"), 1):
            ttk.Label(edit, text="Starts at" if side == "start" else "Ends at",
                      style="Tab.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            line = ttk.Frame(edit, style="Tab.TFrame")
            line.grid(row=r, column=1, sticky="w", pady=2)
            back = tip(ttk.Button(line, text="◀", style="Step.TButton",
                                  command=lambda s=side: self._step_cut(s, -1)),
                       "Move back one cut point: a keyframe, or a frame when cuts can go "
                       "anywhere.")
            back.pack(side="left")
            var = tk.StringVar()
            entry = ttk.Entry(line, textvariable=var, width=13, justify="center")
            entry.pack(side="left", padx=3)
            tip(entry, "Type a time (1:02:03.5, 4:38 or 278.2) and press Enter. It moves "
                "to the nearest keyframe (or, when cuts can go anywhere, the nearest frame). "
                "Up and Down step from one to the next.")
            for key in ("<Return>", "<KP_Enter>", "<FocusOut>"):
                entry.bind(key, lambda e, s=side: self._commit_time(s))
            entry.bind("<Up>", lambda e, s=side: self._step_cut(s, 1))
            entry.bind("<Down>", lambda e, s=side: self._step_cut(s, -1))
            forward = tip(ttk.Button(line, text="▶", style="Step.TButton",
                                     command=lambda s=side: self._step_cut(s, 1)),
                          "Move on one cut point: a keyframe, or a frame when cuts can go "
                          "anywhere.")
            forward.pack(side="left")
            here = tip(ttk.Button(line, text="Use current time",
                                  command=lambda s=side: self._cut_here(s)),
                       ("Start the part at the picture showing in the player (I), or at the "
                        "keyframe nearest it." if side == "start" else
                        "End the part just after the picture showing in the player (O), or "
                        "at the keyframe nearest that."))
            here.pack(side="left", padx=(8, 0))
            note = ttk.Label(line, text="", style="TabNote.TLabel")
            note.pack(side="left", padx=(8, 0))
            self.time_vars[side], self.side_notes[side] = var, note
            self.edit_widgets += [back, entry, forward, here]
        self.length_label = ttk.Label(edit, text="", style="TabNote.TLabel")
        self.length_label.grid(row=3, column=1, sticky="ew", pady=(2, 0))
        self.length_label.bind("<Configure>", lambda e: self.length_label.configure(
            wraplength=max(e.width - 4, 100)))
        cut_row = ttk.Frame(edit, style="Tab.TFrame")
        cut_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.cut_btn = tip(ttk.Button(cut_row, text="Cut out a section", command=self.cut_out),
                           "Leave a stretch out of the middle of this part; what's either "
                           "side of it is joined into one file, still losslessly. Press it "
                           "where the stretch starts, play on, and press it again where it "
                           "ends. Change its times below, or drag its edges on the timeline.")
        self.cut_btn.pack(side="left")
        self.cut_note = ttk.Label(cut_row, text="", style="TabNote.TLabel")
        self.cut_note.pack(side="left", padx=(8, 0))
        self.cut_cancel_btn = tip(ttk.Button(cut_row, text="Cancel", command=self.cancel_cut),
                                  "Forget the start that's marked; nothing is left out.")
        self.edit_widgets.append(self.cut_btn)
        self.cut_from: tuple | None = None  # (part, time) while a cut's start is marked
        self.removed_box = ttk.Frame(edit, style="Tab.TFrame")
        self.removed_box.grid(row=5, column=0, columnspan=2, sticky="ew")
        # Off by default: editing one part leaves every other part alone
        self.joined_var = tk.BooleanVar(value=self.settings.get("move_neighbor", False))
        tip(ttk.Checkbutton(edit, text="Moving a cut moves the part next to it", variable=self.joined_var,
                            style="Tab.TCheckbutton"),
            "When a part ends where the next one starts, moving that cut moves both, so "
            "nothing falls between them. Unchecked, only the part you're editing "
            "changes.").grid(row=6, column=0, columnspan=2, sticky="w",
                                                pady=(8, 0))

    def _build_player(self, box):
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Player", style="Section.TLabel").pack(side="left")
        self.part_time = ttk.Label(head, text="", style="Note.TLabel")
        self.part_time.pack(side="right")

        controls = ttk.Frame(box, padding=(0, 6, 0, 0))
        controls.pack(side="bottom", fill="x")
        self.overview = Overview(box, self, height=20)
        self.overview.pack(side="bottom", fill="x", pady=(6, 0))
        self.video = tk.Frame(box, bg="black", cursor="hand2")
        self.video.pack(fill="both", expand=True, pady=(4, 0))
        self.placeholder = tk.Label(self.video, text="Click here to open a video, or drop one "
                                    "on the window.", bg="black", fg=MUTED,
                                    font=("Segoe UI", 10), cursor="hand2")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.placeholder.bind("<ButtonRelease-1>", lambda e: self._video_clicked())
        self.video.bind("<Configure>", lambda e: self.redraw_soon())
        self.video.bind("<Expose>", lambda e: self.redraw_soon())
        self.video.bind("<ButtonRelease-1>", lambda e: self._video_clicked())
        self.video.bind("<Double-1>", lambda e: self.info and self.enter_full_screen())

        row = ttk.Frame(controls)
        row.pack(fill="x")
        self.transport = []
        for text, command, help_text in (
                ("⏮", lambda: self.go_part_edge("start"), "Start of the part (Home)"),
                ("⏪", lambda: self.jump(-5), "Back 5 seconds (←)"),
                ("▶", self.toggle_play, "Play / pause (Space)"),
                ("⏹", self.stop, "Stop, back at the start of the part"),
                ("⏩", lambda: self.jump(5), "Forward 5 seconds (→)"),
                ("⏭", lambda: self.go_part_edge("end"), "Last frame of the part (End)"),
        ):
            b = tip(ttk.Button(row, text=text, style="Transport.TButton", command=command),
                    help_text)
            b.pack(side="left", padx=(0, 3))
            self.transport.append(b)
        self.play_btn = self.transport[2]
        for text, frames, help_text in (("‹ Frame", -1, "One frame back (,)"),
                                        ("Frame ›", 1, "One frame forward (.)")):
            b = tip(ttk.Button(row, text=text, command=lambda f=frames: self.step(f)),
                    help_text)
            b.pack(side="left", padx=(6 if frames < 0 else 3, 0))
            self.transport.append(b)
        self.time_label = ttk.Label(row, text="", style="Time.TLabel")
        self.time_label.pack(side="left", padx=(12, 0))
        self.full_btn = tip(ttk.Button(row, text="⛶", style="Transport.TButton",
                                       command=self.enter_full_screen),
                            "Full screen (F, or double-click the picture). Esc leaves.")
        self.full_btn.pack(side="right")
        self.frame_btn = tip(ttk.Button(row, text="Save frame", command=self.save_frame),
                             "Save the picture in the player, at full size, as a PNG in the "
                             "folder the parts go to (S).")
        self.frame_btn.pack(side="right", padx=(0, 4))
        row = ttk.Frame(controls)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="At the end").pack(side="left")
        self.end_mode = self.settings.get("end_mode", "stop")
        if self.end_mode not in END_MODES:
            self.end_mode = "stop"
        self.end_mode_var = tk.StringVar(value=END_MODES[self.end_mode])
        mode = ttk.Combobox(row, textvariable=self.end_mode_var, state="readonly", width=13,
                            values=list(END_MODES.values()))
        mode.pack(side="left", padx=(6, 0))
        mode.bind("<<ComboboxSelected>>", lambda e: self._end_mode_changed())
        tip(mode, "Stop: stop on the part's last frame. Loop the part: go back to its "
            "start and carry on. Keep playing: play on past the end.")
        ttk.Label(row, text="Subtitles").pack(side="left", padx=(14, 0))
        self.subtitle_var = tk.StringVar(value="Off")
        self.subtitle_box = ttk.Combobox(row, textvariable=self.subtitle_var, state="readonly",
                                         width=16, values=["Off"])
        self.subtitle_box.pack(side="left", padx=(6, 0))
        self.subtitle_box.bind("<<ComboboxSelected>>", lambda e: self._subtitles_changed())
        tip(self.subtitle_box, "Subtitles to show in the player. Only text subtitles can be "
            "shown here; every checked subtitle track goes into the parts either way.")
        self.volume_var = tk.DoubleVar(value=self.settings.get("volume", 80))
        volume = ttk.Scale(row, from_=0, to=100, variable=self.volume_var, length=90,
                           command=lambda v: self.player.set_volume(self._volume()))
        volume.pack(side="right")
        self.mute_btn = tip(ttk.Button(row, text="🔊", style="Transport.TButton",
                                       command=self.toggle_mute), "Mute (M)")
        self.mute_btn.pack(side="right", padx=(0, 4))

    def _build_timeline(self, box):
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Timeline", style="Section.TLabel").pack(side="left")
        self.zoom_buttons = []
        for text, command, help_text in (
                ("Whole video", lambda: self.timeline.fit(), "Show the whole video."),
                ("Part", self.zoom_to_part, "Zoom to the selected part."),
                ("+", lambda: self.timeline.zoom(0.5), "Zoom in (+)."),
                ("−", lambda: self.timeline.zoom(2.0), "Zoom out (−)."),
        ):
            b = tip(ttk.Button(head, text=text, command=command,
                               style="Step.TButton" if len(text) == 1 else "TButton"),
                    help_text)
            b.pack(side="right", padx=(4, 0))
            self.zoom_buttons.append(b)
        self.timeline = Timeline(box, self)
        self.timeline.pack(fill="both", expand=True, pady=(4, 0))

    # --- Keys ---

    def _bind_keys(self):
        self.bind_all("<Control-o>", lambda e: self._choose_file())
        for key in ("<Control-z>", "<Control-Z>"):
            self.bind_all(key, lambda e: self._undo_key(e, self.do_undo))
        for key in ("<Control-y>", "<Control-Y>", "<Control-Shift-z>", "<Control-Shift-Z>"):
            self.bind_all(key, lambda e: self._undo_key(e, self.do_redo))
        keys = {
            "<space>": self.toggle_play,
            "<Left>": lambda: self.jump(-5), "<Right>": lambda: self.jump(5),
            "<comma>": lambda: self.step(-1), "<period>": lambda: self.step(1),
            "<Control-Left>": lambda: self.jump_keyframe(-1),
            "<Control-Right>": lambda: self.jump_keyframe(1),
            "<Home>": lambda: self.go_part_edge("start"),
            "<End>": lambda: self.go_part_edge("end"),
            "<Prior>": lambda: self.select_next(-1), "<Next>": lambda: self.select_next(1),
            "<i>": lambda: self._cut_here("start"), "<I>": lambda: self._cut_here("start"),
            "<o>": lambda: self._cut_here("end"), "<O>": lambda: self._cut_here("end"),
            "<s>": self.save_frame, "<S>": self.save_frame,
            "<plus>": lambda: self.timeline.zoom(0.5),
            "<equal>": lambda: self.timeline.zoom(0.5),
            "<KP_Add>": lambda: self.timeline.zoom(0.5),
            "<minus>": lambda: self.timeline.zoom(2.0),
            "<KP_Subtract>": lambda: self.timeline.zoom(2.0),
            "<f>": self.toggle_full_screen, "<F>": self.toggle_full_screen,
            "<F11>": self.toggle_full_screen,
            "<Escape>": self.leave_full_screen,
            "<m>": self.toggle_mute, "<M>": self.toggle_mute,
        }
        for key, action in keys.items():
            self.bind_all(key, lambda e, a=action: self._key(e, a))
        self.bind_all("<ButtonPress-1>", self._clicked, add=True)
        # The parts list has its own meaning for these; the player's comes first
        for key in ("<space>", "<Left>", "<Right>", "<Control-Left>", "<Control-Right>",
                    "<Home>", "<End>", "<Prior>", "<Next>"):
            self.tree.bind(key, lambda e, k=key: self._key(e, keys[k]))

    def _typing(self, widget) -> bool:
        if isinstance(widget, ttk.Combobox):  # a pick-from-list box takes no typing
            return "readonly" not in widget.state()
        return isinstance(widget, (ttk.Entry, tk.Entry, tk.Text))

    def _clicked(self, event):
        """A click anywhere but a box to type in, or the parts list, leaves the box
        being typed in, so the keys work the player again."""
        widget = event.widget
        if not isinstance(widget, tk.Misc) or \
                isinstance(widget, (ttk.Entry, tk.Entry, tk.Text, ttk.Treeview, tk.Listbox)):
            return
        try:
            if self._typing(self.focus_get()):
                widget.winfo_toplevel().focus_set()
        except (KeyError, tk.TclError):  # focus in a menu or a closed window
            pass

    def _key(self, event, action):
        if self._typing(event.widget) and event.keysym != "Escape":
            return None
        if not self.info and event.keysym != "Escape":
            return None
        action()
        return "break"

    def _undo_key(self, event, action):
        if isinstance(event.widget, tk.Text):  # the timestamps box has its own undo
            return None
        action()
        return "break"

    # --- Opening a video ---

    def _choose_file(self):
        if self.busy:
            return
        exts = " ".join(f"*{e}" for e in engine.VIDEO_EXTS)
        path = filedialog.askopenfilename(
            parent=self, title="Open a video", initialdir=self.settings.get("last_dir") or None,
            filetypes=[("Videos", exts), ("All files", "*.*")])
        if path:
            self.open_video(Path(path))

    def _fill_recent(self):
        menu = self.recent_menu
        menu.delete(0, "end")
        recent = [p for p in self.settings.get("recent", []) if isinstance(p, str)]
        if not recent:
            menu.add_command(label="Nothing yet", state="disabled")
            return
        for path in recent:
            exists = Path(path).is_file()
            menu.add_command(label=path if exists else f"{path}  (missing)",
                             state="normal" if exists else "disabled",
                             command=lambda p=path: self.open_video(Path(p)))
        menu.add_separator()
        menu.add_command(label="Clear this list", command=self._clear_recent)

    def _clear_recent(self):
        self.settings["recent"] = []

    def _remember_recent(self, path: Path):
        recent = [p for p in self.settings.get("recent", []) if isinstance(p, str)
                  and Path(p) != path]
        self.settings["recent"] = [str(path)] + recent[:RECENT_LIMIT - 1]

    def _drop(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data)]
        files = [p for p in paths if p.is_file()]
        if files:
            self.open_video(files[0])

    def open_video(self, path: Path):
        if self.busy:
            self.bell()
            return
        self._save_project_now()
        self._close_import()  # its times were for the last video
        self.settings["last_dir"] = str(path.parent)
        self.load_id += 1
        self.busy = "reading"
        self.cancel.clear()
        self.player.unload()
        self.info, self.parts, self.track_use = None, [], {}
        self.undo, self.redo, self.last_edit = [], [], None
        self.shown_part = None
        self.position = 0.0
        self.last_output = None
        self.file_label.configure(text=path.name)
        self.file_detail.configure(text="Reading…")
        self.title(f"Splicer: {path.name}")
        self.placeholder.configure(text="Reading…")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.video.configure(bg="black")
        self._refresh_all()
        load_id = self.load_id

        def work():
            try:
                info = engine.probe(path, lambda f: self.events.put(("reading", load_id, f)),
                                    self.cancel)
                self.events.put(("read", load_id, info))
            except engine.Cancelled:
                self.events.put(("read", load_id, None))
            except (engine.SpliceError, OSError) as e:
                self.events.put(("read_failed", load_id, f"{path.name}: {e}"))
            except Exception as e:  # anything FFmpeg throws at an odd file
                self.events.put(("read_failed", load_id, f"{path.name}: {e!r}"))

        threading.Thread(target=work, daemon=True).start()
        self._sync()

    def _loaded(self, info: Info):
        self.info = info
        self.track_use = {t.index: t.chosen for t in info.tracks}
        size = info.path.stat().st_size
        bits = [fmt_time(info.duration).rsplit(".", 1)[0], fmt_size(size)]
        bits.append(f"{len(info.chapters)} chapter{'s' * (len(info.chapters) != 1)}"
                    if info.chapters else "no chapters")
        if info.has_video:
            bits.append(f"{len(info.keyframes)} keyframes")
        else:
            bits.append("no picture: cuts can go anywhere")
        if timestamps.best_source(info.tags):
            bits.append("timestamps in its description (More ▸ Parts from timestamps)")
        self.file_detail.configure(text="  ·  ".join(bits))
        self.output_var.set(str(info.path.parent / engine.safe_name(info.path.stem)))
        if info.has_video:
            self.placeholder.place_forget()
            # The player paints all of it: Tk painting it black too, as the window
            # changes size, made the picture flash
            self.video.configure(bg="")
        else:
            self.placeholder.configure(text="No picture: sound only.")
        self._remember_recent(info.path)
        self.cant_reencode = engine.can_reencode(info)
        self.player.load(info.path, info.origin, info.duration, self._audio_track())
        restored = self._restore_project()
        if not restored:
            self._reset_parts(ask=False)
        self.player.set_audio(self._audio_track())  # the restored tracks may differ
        self._refresh_tracks()
        self._fill_subtitles()
        self._measure_wave()
        self.timeline.fit()
        if restored:
            self.status.configure(text="Picked up where you left off with this video. "
                                       "More ▸ Start again from the chapters to undo that.")
        else:
            self.status.configure(text="Check the tracks and parts, then press Splice.")

    # --- Remembering each video's parts ---

    def _restore_project(self) -> bool:
        data = projects.load(self.info.path)
        if not data:
            return False
        parts = []
        try:
            for title, start, end, use, *more in data["parts"]:
                start, end = float(start), float(end)
                if not (0 <= start < end <= self.info.duration + 0.01):
                    return False
                removed = [[float(a), float(b)] for a, b in (more[0] if more else [])]
                parts.append(Part(str(title), start, min(end, self.info.duration), bool(use),
                                  removed))
            for index, on in (data.get("tracks") or {}).items():
                if int(index) in self.track_use:
                    self.track_use[int(index)] = bool(on)
        except (TypeError, ValueError, AttributeError, KeyError):
            return False
        if not parts:
            return False
        if not self.anywhere:
            # Saved while cuts could go anywhere: they go on keyframes now
            for p in parts:
                p.start, p.end = self.snap(p.start), self.snap(p.end)
                p.removed = [[self.snap(a), self.snap(b)] for a, b in p.removed]
                if p.end - p.start <= EPS:
                    p.use = False
        self.parts = parts
        self._sorted()
        self._refresh_all()
        self._select(self.parts[0])
        return True

    def _project_changed(self):
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(800, self._save_project_now)

    def _save_project_now(self):
        if self.save_job:
            self.after_cancel(self.save_job)
            self.save_job = None
        if not self.info:
            return
        projects.save(self.info.path, {
            "parts": [[p.title, p.start, p.end, p.use, p.removed] for p in self.parts],
            "tracks": {str(k): v for k, v in self.track_use.items()},
        })

    # --- Tracks ---

    def _audio_track(self) -> int | None:
        """The sound the player plays: the first checked sound track."""
        if not self.info:
            return None
        for t in self.info.tracks:
            if t.kind == "audio" and self.track_use.get(t.index):
                return t.index
        return None

    def _refresh_tracks(self):
        self.tracks.delete(*self.tracks.get_children())
        if not self.info:
            self.tabs.tab(1, text="Tracks")
            return
        for t in self.info.tracks:
            on = self.track_use.get(t.index, False)
            label = t.label + ("" if t.kind != "subtitle" or t.text
                               else "  (can't be shown in the player)")
            self.tracks.insert("", "end", iid=str(t.index),
                               values=(CHECKED if on else UNCHECKED, label),
                               tags=() if on else ("off",))
        used = sum(self.track_use.values())
        self.tabs.tab(1, text=f"Tracks  {used} of {len(self.info.tracks)}")

    def _toggle_track(self, event):
        iid = self.tracks.identify_row(event.y)
        if not iid or self.busy:
            return
        index = int(iid)
        self.track_use[index] = not self.track_use.get(index, False)
        self._refresh_tracks()
        self.player.set_audio(self._audio_track())
        self._measure_wave()
        self._refresh_parts()  # sound only: the file names follow the sound track
        self._project_changed()

    def _measure_wave(self):
        """Work out the shape of the sound you hear, in the background, for the timeline."""
        info, index = self.info, self._audio_track()
        key = (self.load_id, index)
        if not info or key == self.wave_for:
            return
        self.wave_for, self.wave = key, None
        self.timeline.draw()
        if index is None:
            return
        if getattr(self, "wave_stop", None):
            self.wave_stop.set()  # the one for the last video or sound track
        cancel = self.wave_stop = threading.Event()

        def work():
            try:
                peaks = engine.waveform(info.path, index, info.origin, info.duration, cancel)
            except Exception:  # the timeline just goes without it
                peaks = None
            self.events.put(("wave", key, peaks))

        threading.Thread(target=work, daemon=True).start()

    def _fill_subtitles(self):
        self.subtitle_choices = {"Off": None}
        chosen = "Off"
        if self.info:
            for t in self.info.tracks:
                if t.kind == "subtitle" and t.text and self.info.subtitles.get(t.index):
                    label = " · ".join(x for x in (engine.language_name(t.language)
                                                   if t.language not in ("", "und") else "",
                                                   t.title) if x) or f"Track {t.index}"
                    while label in self.subtitle_choices:
                        label += " "
                    self.subtitle_choices[label] = t.index
                    if chosen == "Off" and self.track_use.get(t.index) and (t.default or t.forced):
                        chosen = label
        self.subtitle_box.configure(values=list(self.subtitle_choices))
        self.subtitle_var.set(chosen)
        self._subtitles_changed()

    def _subtitles_changed(self):
        index = self.subtitle_choices.get(self.subtitle_var.get())
        cues = self.info.subtitles.get(index) if self.info and index is not None else None
        self.player.set_subtitles(cues)

    # --- Where cuts can go ---

    @property
    def anywhere(self) -> bool:
        """Can cuts go on any frame (re-encoding near them) in this video?"""
        return self.cut_mode == "anywhere" and self.info is not None and not self.cant_reencode

    def snap(self, t: float) -> float:
        """Where a cut asked for at t really goes: the nearest frame, or keyframe."""
        return engine.snap_frame(self.info, t) if self.anywhere else engine.snap(self.info, t)

    def step_point(self, t: float, direction: int) -> float:
        """The cut point before or after t: a frame, or a keyframe."""
        if self.anywhere:
            return engine.next_frame(self.info, t) if direction > 0 \
                else engine.previous_frame(self.info, t)
        return engine.next_keyframe(self.info, t) if direction > 0 \
            else engine.previous_keyframe(self.info, t)

    def _chapter_parts(self, snap) -> list[Part]:
        return [Part(ch.title, snap(ch.start), snap(ch.end),
                     use=snap(ch.end) - snap(ch.start) > EPS) for ch in self.info.chapters]

    def _cut_mode_changed(self):
        label = self.cut_mode_var.get()
        mode = next(k for k, v in CUT_MODES.items() if v == label)
        if mode == self.cut_mode:
            return
        if mode == "anywhere" and self.cant_reencode:
            self.cut_mode = mode
            self.status.configure(text=self.cant_reencode)
            self._refresh_parts()
            return
        before = self._snapshot()
        untouched = self.info is not None and self.info.chapters and \
            [(p.title, p.start, p.end) for p in self.parts] == \
            [(p.title, p.start, p.end) for p in self._chapter_parts(
                lambda t: engine.snap(self.info, t))]
        self.cut_mode = mode
        if not self.info or not self.parts:
            self._refresh_parts()
            return
        if mode == "anywhere" and untouched:
            self.undo.append(before)
            self.redo.clear()
            self.last_edit = None
            self.parts = self._chapter_parts(self.snap)
            self.shown_part = None
            self._changed(select=self.parts[0])
            self.status.configure(text="Cuts can go anywhere now, and the parts start and end "
                                       "exactly at the chapters.")
            return
        if mode == "keyframes":
            moved = 0
            for p in self.parts:
                for attr in ("start", "end"):
                    t = engine.snap(self.info, getattr(p, attr))
                    moved += abs(t - getattr(p, attr)) > EPS
                    setattr(p, attr, t)
                for r in p.removed:
                    for k in (0, 1):
                        t = engine.snap(self.info, r[k])
                        moved += abs(t - r[k]) > EPS
                        r[k] = t
                if p.end - p.start <= EPS:
                    p.use = False
            if moved:
                self.undo.append(before)
                self.redo.clear()
                self.last_edit = None
            self._changed(select=self._current())
            self.status.configure(
                text=f"Cuts stay on keyframes now: {moved} moved to the nearest one. Ctrl+Z "
                     "undoes that." if moved else "Cuts stay on keyframes now.")
            return
        self._refresh_parts()
        self._show_selected()
        self.status.configure(text="Cuts can go on any frame now. The bits re-encoded show in "
                                   "orange on the timeline.")

    # --- Undo ---

    def _snapshot(self):
        """The parts, and the Cuts setting they were made under, for undo."""
        return (self.cut_mode, [(p.title, p.start, p.end, p.use, [list(r) for r in p.removed])
                                for p in self.parts])

    def _remember(self, kind=None):
        """Call before changing the parts."""
        if kind is not None and kind == self.last_edit:
            return  # typing a name: one undo for the lot
        self.undo.append(self._snapshot())
        del self.undo[:-UNDO_LIMIT]
        self.redo.clear()
        self.last_edit = kind

    def _back_to(self, snapshot):
        current = self._current()
        index = self.parts.index(current) if current in self.parts else 0
        mode, parts = snapshot
        if mode != self.cut_mode:
            self.cut_mode = mode
            self.cut_mode_var.set(CUT_MODES[mode])
        self.parts = [Part(t, s, e, u, [list(r) for r in rm]) for t, s, e, u, rm in parts]
        self.last_edit = None
        self.shown_part = None
        self._refresh_parts()
        if self.parts:
            part = self.parts[min(index, len(self.parts) - 1)]
            self.shown_part = part  # leave the player where it is
            self._select(part, seek=False)
        else:
            self._show_selected()
        self._project_changed()

    def do_undo(self):
        if not self.undo or self.busy or not self.info:
            return
        self.redo.append(self._snapshot())
        self._back_to(self.undo.pop())
        self.status.configure(text="Undone.")

    def do_redo(self):
        if not self.redo or self.busy or not self.info:
            return
        self.undo.append(self._snapshot())
        self._back_to(self.redo.pop())
        self.status.configure(text="Redone.")

    # --- Parts ---

    def _reset_parts(self, ask: bool = True):
        info = self.info
        if not info or self.busy:
            return
        if ask:
            self._remember()
        if info.chapters:
            self.parts = []
            for ch in info.chapters:
                start, end = self.snap(ch.start), self.snap(ch.end)
                self.parts.append(Part(ch.title, start, end, use=end - start > EPS))
        else:
            self.parts = [Part("Part 1", 0.0, info.duration)]
        self._sorted()
        self.shown_part = None
        self._refresh_parts()
        if self.parts:
            self._select(self.parts[0])
        if ask:
            self.status.configure(text="Back to the chapters. Ctrl+Z undoes that.")
            self._project_changed()

    def _sorted(self):
        for part in self.parts:
            tidy_removed(part)
        self.parts.sort(key=lambda p: (p.start, p.end))
        # Names Splicer made up ("Part 3") follow the order; names you typed don't change
        for n, part in enumerate(self.parts, 1):
            if DEFAULT_NAME.fullmatch(part.title):
                part.title = f"Part {n}"

    def _changed(self, select: Part | None = None):
        """After any change to the parts: tidy, show, remember."""
        self._sorted()
        self._refresh_parts()
        if select is not None and select in self.parts:
            self._select(select)
        self._project_changed()

    def _refresh_parts(self):
        keep = self._selected_parts()
        focused = self._current()
        self.tree.delete(*self.tree.get_children())
        names = self._names()
        width = max(2, len(str(sum(p.use for p in self.parts))))
        n = 0
        for i, part in enumerate(self.parts):
            part.iid = f"p{i}"
            if part.use:
                n += 1
            tags = () if part.use else ("off",)
            if part.length <= EPS:
                tags = ("empty",)
            self.tree.insert("", "end", iid=part.iid, tags=tags, values=(
                CHECKED if part.use else UNCHECKED,
                f"{n:0{width}}" if part.use else "",
                part.title, fmt_time(part.start), fmt_time(part.end), fmt_time(part.length)))
        if keep:
            self.tree.selection_set([p.iid for p in keep if p in self.parts])
        if focused in self.parts:
            self.tree.focus(focused.iid)  # emptied by the rebuild
        if self.info and getattr(self, "editing", None) in self.parts:
            self._describe(self.editing)  # its file name may have changed
        used = [p for p in self.parts if p.use]
        if self.info:
            total = sum(p.length for p in used)
            self.parts_note.configure(text=f"{len(used)} of {len(self.parts)} checked  ·  "
                                           f"{fmt_time(total).rsplit('.', 1)[0]} in all")
            self.tabs.tab(0, text=f"Parts  {len(used)} of {len(self.parts)}  ·  "
                                  f"{fmt_time(total).rsplit('.', 1)[0]}")
        else:
            self.parts_note.configure(text="")
            self.tabs.tab(0, text="Parts")
        self._draw_timelines()
        self._sync()

    def _sound_track(self):
        """The track saved when saving sound only."""
        index = self._audio_track()
        return next((t for t in self.info.tracks if t.index == index), None) \
            if self.info and index is not None else None

    def _ext(self) -> str:
        if not self.info:
            return ""
        if self.save_as == "sound":
            track = self._sound_track()
            return engine.sound_ext(track) if track else ".mka"
        return self.info.path.suffix.lower()

    def _names(self) -> dict[Part, str]:
        return file_names(self.parts, self._ext(), bool(self.numbered_var.get()))

    def _selected_parts(self) -> list[Part]:
        wanted = set(self.tree.selection())
        return [p for p in self.parts if p.iid in wanted]

    def _current(self) -> Part | None:
        focus = self.tree.focus()
        selected = self._selected_parts()
        for p in selected:
            if p.iid == focus:
                return p
        return selected[0] if selected else None

    def _select(self, part: Part, seek: bool = True):
        if not seek:
            self.shown_part = part
        self.tree.selection_set(part.iid)
        self.tree.focus(part.iid)
        self.tree.see(part.iid)
        self._show_selected()

    def select_next(self, direction: int):
        if not self.parts:
            return
        current = self._current()
        index = self.parts.index(current) + direction if current in self.parts else 0
        self._select(self.parts[min(max(index, 0), len(self.parts) - 1)])

    def _tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if self.tree.identify_column(event.x) == "#1" and iid and not self.busy:
            part = next(p for p in self.parts if p.iid == iid)
            use = not part.use
            group = [part]
            anchor = getattr(self, "check_anchor", None)
            if event.state & 0x0001 and anchor in self.parts:
                # Shift+click: every part from the last checkbox clicked to this one
                a, b = sorted((self.parts.index(anchor), self.parts.index(part)))
                group = self.parts[a:b + 1]
            self._remember()
            for p in group:
                p.use = use
            self.check_anchor = part
            self._changed()
            return "break"
        return None

    def _add_part(self):
        info = self.info
        if not info or self.busy:
            return
        current = self._current()
        # The gap after the selected part; or after the last one; or, with the whole
        # video already covered, a copy of the selected part to change
        start, end = None, None
        for after in ([current.end] if current else []) + \
                [max((p.end for p in self.parts), default=0.0)]:
            later = [p.start for p in self.parts if p.start > after - EPS]
            stop = min(later) if later else info.duration
            if stop - after > EPS:
                start, end = after, stop
                break
        if start is None:
            start, end = (current.start, current.end) if current else (0.0, info.duration)
        self._remember()
        part = Part("Part 0", start, end)  # numbered by _sorted
        self.parts.append(part)
        self._changed(select=part)
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")

    def _split_part(self):
        info, part = self.info, self._current()
        if not info or not part or self.busy:
            return
        inside = lambda t: part.start + EPS < t < part.end - EPS  # noqa: E731
        at = self.snap(self.position)
        if not inside(at):
            at = self.snap((part.start + part.end) / 2)
        if not inside(at):
            keys = [k for k in (info.frames if self.anywhere else info.keyframes) if inside(k)]
            if not keys:
                self.status.configure(text="There's no keyframe inside that part to split at.")
                self.bell()
                return
            at = min(keys, key=lambda k: abs(k - (part.start + part.end) / 2))
        for r0, r1 in part.removed:  # never split inside a stretch that's left out
            if r0 - EPS < at < r1 + EPS:
                at = r1 if r1 < part.end - EPS else r0
        if not inside(at):
            self.bell()
            return
        self._remember()
        title = "Part 0" if DEFAULT_NAME.fullmatch(part.title) else f"{part.title} (2)"
        second = Part(title, at, part.end, part.use,
                      [list(r) for r in part.removed if r[0] >= at - EPS])
        part.removed = [r for r in part.removed if r[1] <= at + EPS]
        part.end = at
        self.parts.append(second)
        self._changed(select=second)

    def _join_parts(self):
        chosen = self._selected_parts()
        if self.busy or not self.info:
            return
        if len(chosen) < 2:
            self.status.configure(text="Pick two or more parts to join: Ctrl+click or "
                                       "Shift+click them in the list.")
            self.bell()
            return
        spans = sorted(seg for p in chosen for seg in p.kept)
        merged: list[list[float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + EPS:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        if not merged:
            self.status.configure(text="Those parts are empty: there's nothing to join.")
            self.bell()
            return
        self._remember()
        joined = Part(chosen[0].title, merged[0][0], merged[-1][1], True,
                      [[a[1], b[0]] for a, b in zip(merged, merged[1:])])
        self.parts = [p for p in self.parts if p not in chosen] + [joined]
        self.shown_part = None
        self._changed(select=joined)
        gaps = len(joined.removed)
        self.status.configure(
            text=f"Joined {len(chosen)} parts into one, {fmt_time(joined.length)} long"
                 + (f"; {gaps} stretch{'es' * (gaps != 1)} between them left out."
                    if gaps else ".") + " Ctrl+Z undoes that.")

    # --- Stretches left out of a part ---

    def cut_out(self):
        info, part = self.info, self._current()
        if not info or not part or self.busy:
            return
        # Two presses: the first marks where the stretch starts and changes nothing else (no
        # jump, nothing skipped while playing on); the second, where it ends.
        mark = self.cut_from
        if mark is None or mark[0] is not part:
            at = self.snap(self.position)
            if not part.start - EPS <= at < part.end - EPS:
                self.status.configure(text="Go to where the stretch to leave out starts, inside "
                                           "this part, then press Cut out a section.")
                self.bell()
                return
            self.cut_from = (part, self.position)
            self._show_cut_mark()
            self.status.configure(
                text=f"Cutting from {fmt_time(at)}. Play on to where the stretch ends and "
                     "press End the cut here.")
            return
        a = min(max(self.snap(mark[1]), part.start), part.end)
        b = min(max(self.snap(self.position), part.start), part.end)
        a, b = min(a, b), max(a, b)
        if b - a <= EPS:
            near = "" if self.anywhere else " (or the same keyframe)"
            self.status.configure(
                text=f"That's where the cut starts{near}, so nothing would be left out. Play "
                     "on to where the stretch ends, then press End the cut here.")
            self.bell()
            return
        self._remember()
        self.cut_from = None
        part.removed.append([a, b])
        self.shown_part = part
        self._changed(select=part)
        self.status.configure(
            text=f"Left out {fmt_time(a)} to {fmt_time(b)} ({b - a:.2f} s). Change its "
                 "times below or drag its edges on the timeline; play across it to see the "
                 "join.")

    def cancel_cut(self):
        if self.cut_from is not None:
            self.cut_from = None
            self._show_cut_mark()
            self.status.configure(text="Cancelled: nothing is left out.")

    def _show_cut_mark(self):
        """The Cut out button says what pressing it does next."""
        part = self._current() if self.info else None
        if self.cut_from is not None and self.cut_from[0] is not part:
            self.cut_from = None  # a start marked in another part (or video) is forgotten
        if self.cut_from is None:
            self.cut_btn.configure(text="Cut out a section")
            self.cut_note.configure(text="")
            self.cut_cancel_btn.pack_forget()
        else:
            self.cut_btn.configure(text="End the cut here")
            self.cut_note.configure(text=f"from {fmt_time(self.snap(self.cut_from[1]))}")
            self.cut_cancel_btn.pack(side="left", padx=(8, 0))

    def move_removed(self, part: Part, i: int, edge: int, t: float,
                     asked: float | None = None, carry: bool = False):
        """Move one edge (0: from, 1: to) of a stretch left out of a part. t is on a
        keyframe; asked is the time typed (or showing), if it was something else. With
        carry, an edge taken past the other one takes the whole stretch along, keeping
        its length, rather than refusing."""
        if part not in self.parts or i >= len(part.removed) or self.busy:
            return
        stretch = list(part.removed[i])
        stretch[edge] = min(max(t, part.start), part.end)
        carried = False
        if carry and stretch[1] - stretch[0] <= EPS:
            length = part.removed[i][1] - part.removed[i][0]
            if edge == 0:
                other = self.snap(min(stretch[0] + length, part.end))
                if other <= stretch[0] + EPS:
                    other = min(self.step_point(stretch[0], 1), part.end)
                stretch[1] = other
            else:
                other = self.snap(max(stretch[1] - length, part.start))
                if other >= stretch[1] - EPS:
                    other = max(self.step_point(stretch[1], -1), part.start)
                stretch[0] = other
            carried = stretch[1] - stretch[0] > EPS
        if stretch[1] - stretch[0] <= EPS:
            self._say_removed(part, self._too_short(part, i, edge, t, asked), error=True)
            self.bell()
            self._fill_removed(part)
            return
        if abs(stretch[edge] - part.removed[i][edge]) <= EPS:
            if asked is not None and abs(asked - t) > EPS and not self.anywhere:
                self._say_removed(part, f"{fmt_time(asked)} isn't a keyframe, and the nearest "
                                        f"one is {fmt_time(t)}, where it already is.")
            self._fill_removed(part)
            return
        self._remember()
        part.removed[i] = stretch
        self.shown_part = part
        if asked is not None and abs(asked - t) > EPS and not self.anywhere:
            self._say_removed(part, f"{fmt_time(asked)} isn't a keyframe: it's at the nearest "
                                    f"one, {fmt_time(t)}.")
        else:
            self._say_removed(part, "")
            moved = "Moved the whole stretch: it now leaves" if carried else "Leaves"
            self.status.configure(text=f"{moved} out {fmt_time(stretch[0])} to "
                                       f"{fmt_time(stretch[1])} ({stretch[1] - stretch[0]:.2f} s).")
        self._changed(select=part)
        # Show the join: the last picture before the stretch, or the first after it
        if edge == 0:
            self.seek(stretch[0] - self.player.frame_duration, pause=True)
        else:
            self.seek(stretch[1], pause=True)

    def put_back(self, part: Part, i: int):
        if part in self.parts and i < len(part.removed) and not self.busy:
            self._remember()
            del part.removed[i]
            self.shown_part = part
            self._changed(select=part)

    def _too_short(self, part: Part, i: int, edge: int, t: float, asked) -> str:
        """Why a stretch can't be made that short, and what it can be."""
        r0, r1 = part.removed[i]
        if self.anywhere:
            return "That would leave nothing out: a stretch left out has to end after it starts."
        wanted = fmt_time(asked) if asked is not None else fmt_time(t)
        other = "starts" if edge == 1 else "ends"
        between = [k for k in self.info.keyframes if r0 + EPS < k < r1 - EPS]
        text = (f"{wanted} isn't a keyframe, and the nearest one is {fmt_time(t)}, where this "
                f"stretch {other}, so nothing would be left out. ")
        if not between:
            return text + (f"There's no keyframe between {fmt_time(r0)} and {fmt_time(r1)}, so "
                           f"{r1 - r0:.2f} s is as short as a stretch here can be without "
                           "re-encoding. To cut anywhere, set Cuts (at the top) to Anywhere.")
        return text + "It can end at " + ", ".join(fmt_time(k) for k in between[:4]) + "."

    def _say_removed(self, part: Part, text: str, error: bool = False):
        self.removed_message = (part, text, error)
        if text:
            self.status.configure(text=text)

    def _fill_removed(self, part: Part | None):
        box = self.removed_box
        for child in box.winfo_children():
            child.destroy()
        self.removed_widgets: list = []   # locked while splicing, like the rest
        self.removed_commits: list = []   # (entry, commit) for times still being typed
        if not part:
            return
        if part.removed and not self.anywhere:
            keys = self.info.keyframes
            lo = min(r[0] for r in part.removed) - 8
            hi = max(r[1] for r in part.removed) + 8
            near = [fmt_time(k) for k in keys if max(lo, part.start) <= k <= min(hi, part.end)]
            if near:
                hint = ttk.Label(box, style="TabNote.TLabel", justify="left",
                                 text="A stretch left out starts and ends on keyframes. Near here "
                                      "they're at " + ", ".join(near[:10])
                                      + ("…" if len(near) > 10 else "") + ".")
                hint.pack(anchor="w", fill="x", pady=(4, 0), side="bottom")
                hint.bind("<Configure>",
                          lambda e, w=hint: w.configure(wraplength=max(e.width - 4, 100)))
        part_said, text, error = getattr(self, "removed_message", (None, "", False))
        if part_said is part and text:
            said = ttk.Label(box, text=text, style="TabWarn.TLabel" if error else "TabNote.TLabel",
                             justify="left")
            said.pack(anchor="w", fill="x", pady=(4, 0), side="bottom")
            said.bind("<Configure>",
                      lambda e, w=said: w.configure(wraplength=max(e.width - 4, 100)))
        for i, (r0, r1) in enumerate(part.removed):
            row = ttk.Frame(box, style="Tab.TFrame")
            row.pack(anchor="w", pady=(4, 0))
            ttk.Label(row, text="Leaves out", style="Tab.TLabel").pack(side="left")
            for edge, value in ((0, r0), (1, r1)):
                if edge:
                    ttk.Label(row, text="to", style="Tab.TLabel").pack(side="left", padx=4)
                var = tk.StringVar(value=fmt_time(value))
                entry = ttk.Entry(row, textvariable=var, width=11, justify="center")
                entry.pack(side="left", padx=(6 if not edge else 0, 2))
                commit = lambda e, i=i, edge=edge, var=var: self._commit_removed(  # noqa: E731
                    part, i, edge, var)
                for key in ("<Return>", "<KP_Enter>", "<FocusOut>"):
                    entry.bind(key, commit)
                self.removed_widgets.append(entry)
                self.removed_commits.append((entry, commit))
                entry.bind("<Up>", lambda e, i=i, edge=edge: self._step_removed(part, i, edge, 1))
                entry.bind("<Down>",
                           lambda e, i=i, edge=edge: self._step_removed(part, i, edge, -1))
                tip(entry, "Type a time and press Enter; Up and Down step from one cut point "
                    "to the next (keyframes, or frames when cuts can go anywhere).")
                now = tip(ttk.Button(row, text="Now", style="Step.TButton",
                                     command=lambda i=i, edge=edge: self.move_removed(
                                         part, i, edge, self.snap(self.position),
                                         asked=self.position, carry=True)),
                          "Use the current time in the player.")
                now.pack(side="left")
                self.removed_widgets.append(now)
            ttk.Label(row, text=f"({r1 - r0:.2f} s)", style="TabNote.TLabel"
                      ).pack(side="left", padx=6)
            back = tip(ttk.Button(row, text="Put back",
                                  command=lambda i=i: self.put_back(part, i)),
                       "Keep this stretch in the part after all.")
            back.pack(side="left")
            self.removed_widgets.append(back)

    def _commit_removed(self, part: Part, i: int, edge: int, var: tk.StringVar):
        if part not in self.parts or i >= len(part.removed) or self.busy:
            return
        text = var.get()
        if text == fmt_time(part.removed[i][edge]):
            return
        t = parse_time(text)
        if t is None:
            self.status.configure(text=f"“{text}” isn't a time. Try 4:38, 4:38.25 or 1:02:03.")
            var.set(fmt_time(part.removed[i][edge]))
            self.bell()
            return
        self.move_removed(part, i, edge, self.snap(t), asked=t, carry=True)

    def _step_removed(self, part: Part, i: int, edge: int, direction: int):
        if part in self.parts and i < len(part.removed) and not self.busy:
            now = part.removed[i][edge]
            self.move_removed(part, i, edge, self.step_point(now, direction))
        return "break"

    def _remove_parts(self):
        chosen = self._selected_parts()
        if not chosen or self.busy:
            return
        self._remember()
        index = self.parts.index(chosen[0])
        self.parts = [p for p in self.parts if p not in chosen]
        self._changed()
        if self.parts:
            self._select(self.parts[min(index, len(self.parts) - 1)])
        else:
            self._show_selected()

    # --- Timestamps ---

    def open_import(self):
        if not self.info or self.busy:
            return
        text = timestamps.best_source(self.info.tags)
        if text:
            source = ("These timestamps were in the video's description. Change them if you "
                      "like, then press Make parts. Your current parts are replaced "
                      "(Ctrl+Z brings them back).")
        else:
            try:
                clip = self.clipboard_get()
            except tk.TclError:
                clip = ""
            text = clip if len(timestamps.parse(clip)) >= 2 else ""
            source = ("Paste a list of times, one per line, like “4:38 Second song” (as "
                      "in video descriptions), then press Make parts. Your current parts are "
                      "replaced (Ctrl+Z brings them back).")
        self._close_import()
        self.import_dialog = ImportDialog(self, text, source)

    def _close_import(self):
        dialog = getattr(self, "import_dialog", None)
        if dialog is not None:
            try:
                dialog.destroy()
            except tk.TclError:
                pass
        self.import_dialog = None

    def import_times(self, found: list[tuple[float, str]]):
        if not self.info or self.busy:
            self.bell()
            return
        parts = parts_from_times(self.info, found, self.snap)
        if not parts:
            self.status.configure(text="None of those times are inside the video.")
            return
        self._remember()
        self.parts = parts
        self.shown_part = None
        self._changed(select=parts[0])
        self.status.configure(text=f"Made {len(parts)} parts from the timestamps. "
                                   "Ctrl+Z undoes that.")

    def copy_times(self):
        used = [p for p in self.parts if p.use]
        if not used:
            return
        text = "\n".join(f"{timestamps.stamp(p.start)} {p.title}" for p in used)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text=f"Copied {len(used)} timestamps, ready to paste.")

    # --- The selected part ---

    def _describe(self, part: Part):
        """The note under the part's times: its length, what's re-encoded, its file."""
        gaps = len(part.removed)
        text = f"Length {fmt_time(part.length)}" + (
            f"  ({gaps} stretch{'es' * (gaps != 1)} left out)" if gaps else "")
        if self.anywhere:
            redo = engine.reencoded(self.info, part.kept)
            if redo > EPS:
                text += f"\nRe-encodes {redo:.1f} s of picture near its cuts"
        name = self._names().get(part)
        text += f"\nSaved as {name}" if name else "\nUnchecked, so not saved"
        self.length_label.configure(text=text)

    def _show_selected(self):
        part = self._current() if self.info else None
        self._show_cut_mark()
        if not part:
            self.title_var.set("")
            for side in ("start", "end"):
                self.time_vars[side].set("")
                self.side_notes[side].configure(text="")
            self.length_label.configure(text="")
            self._fill_removed(None)
            self.player.set_range(None, None)
            self.shown_part = None
            self._draw_timelines()
            self._sync()
            return
        if self.title_var.get() != part.title:
            self._filling = True
            self.title_var.set(part.title)
            self._filling = False
        for side in ("start", "end"):
            self.time_vars[side].set(fmt_time(getattr(part, side)))
        self.editing = part
        self._describe(part)
        self._fill_removed(part)
        self.side_notes["start"].configure(
            text="the start of the video" if part.start <= EPS else "")
        self.side_notes["end"].configure(
            text="the end of the video" if part.end >= self.info.duration - EPS else "")
        self.player.set_range(part.start, part.end, [tuple(r) for r in part.removed])
        if part is not self.shown_part:
            self.shown_part = part
            self.seek(part.start)  # carries on playing if it was
            self.timeline.follow(part.start)
        self._draw_timelines()
        self._update_time()
        self._sync()

    def _title_changed(self):
        if self._filling:
            return
        part = self._current()
        if not part or self.busy or self.title_var.get() == part.title:
            return
        self._remember(("title", id(part)))
        part.title = self.title_var.get()
        names = self._names()
        for other in self.parts:  # a clash can change another part's file name too
            if self.tree.exists(other.iid):
                values = list(self.tree.item(other.iid, "values"))
                values[2], values[6] = other.title, names.get(other, "not saved")
                self.tree.item(other.iid, values=values)
        self.timeline.draw()
        self._project_changed()

    def _commit_time(self, side: str):
        # Clicking another part moves the selection before the box loses focus, so
        # the time typed belongs to the part the box was showing
        part, info = getattr(self, "editing", None), self.info
        if part not in self.parts:
            part = self._current()
        if not part or not info or self.busy:
            return
        text = self.time_vars[side].get()
        if text == fmt_time(getattr(part, side)):
            return
        t = parse_time(text)
        if t is None:
            self.status.configure(text=f"“{text}” isn't a time. Try 4:38, 4:38.25 or 1:02:03.")
            self.time_vars[side].set(fmt_time(getattr(part, side)))
            self.bell()
            return
        self.move_cut(part, side, self.snap(min(t, info.duration)), asked=t)

    def _step_cut(self, side: str, direction: int):
        part, info = self._current(), self.info
        if not part or not info or self.busy:
            return "break"
        now = getattr(part, side)
        self.move_cut(part, side, self.step_point(now, direction))
        return "break"

    def _cut_here(self, side: str):
        """Start the part at the frame in the player, or end it after that frame."""
        part, info = self._current(), self.info
        if not part or not info or self.busy:
            return
        asked = self.position if side == "start" else \
            min(self.position + self.player.frame_duration, info.duration)
        self.move_cut(part, side, self.snap(asked), asked=asked)

    def move_cut(self, part: Part, side: str, t: float, asked: float | None = None):
        """Move one end of a part (t already on a keyframe), and with "Moving a cut
        moves the part next to it" checked, the neighbour that shares that cut."""
        if part not in self.parts:
            return
        old = getattr(part, side)
        other = part.end if side == "start" else part.start
        if (side == "start" and t >= other - EPS) or (side == "end" and t <= other + EPS):
            self.status.configure(text="A part has to end after it starts.")
            self.time_vars[side].set(fmt_time(old))
            self.bell()
            return
        if abs(t - old) <= EPS:
            self.time_vars[side].set(fmt_time(old))
            return
        self._remember()
        setattr(part, side, t)
        moved = 0
        if self.joined_var.get():
            for neighbour in self.parts:
                if neighbour is part:
                    continue
                if side == "end" and abs(neighbour.start - old) <= EPS \
                        and t < neighbour.end - EPS:
                    neighbour.start = t
                    moved += 1
                elif side == "start" and abs(neighbour.end - old) <= EPS \
                        and t > neighbour.start + EPS:
                    neighbour.end = t
                    moved += 1
        self.shown_part = part
        self._changed(select=part)
        # Show the cut: the part's first frame, or its last
        if side == "start":
            self.seek(t, pause=True)
        else:
            self.seek(t - self.player.frame_duration, pause=True)
        notes = []
        if asked is not None and abs(asked - t) > EPS and not self.anywhere:
            notes.append(f"{fmt_time(asked)} isn't a keyframe: the cut is at the nearest one, "
                         f"{fmt_time(t)}.")
        if moved:
            notes.append("The part next to it moved too.")
        self.status.configure(text="  ".join(notes))

    # --- The player ---

    def _volume(self) -> float:
        return 0.0 if self.muted else self.volume_var.get() / 100

    def toggle_mute(self):
        self.muted = not self.muted
        self.mute_btn.configure(text="🔇" if self.muted else "🔊")
        self.player.set_volume(self._volume())

    def _end_mode_changed(self):
        label = self.end_mode_var.get()
        self.end_mode = next(k for k, v in END_MODES.items() if v == label)
        self.player.set_end_mode(self.end_mode)

    def _video_clicked(self):
        """A click on the picture plays or pauses; with no video yet, opens one."""
        if self.info:
            self.toggle_play()
        elif not self.busy:
            self._choose_file()

    def show_shortcuts(self):
        if getattr(self, "shortcuts_window", None) and self.shortcuts_window.winfo_exists():
            self.shortcuts_window.lift()
            return
        win = self.shortcuts_window = tk.Toplevel(self, bg=PANEL)
        win.title("Keyboard shortcuts")
        win.transient(self)
        win.resizable(False, False)
        dark_title_bar(win)
        box = ttk.Frame(win, style="Tab.TFrame", padding=16)
        box.pack(fill="both", expand=True)
        for row, (keys, what) in enumerate(SHORTCUTS):
            ttk.Label(box, text=keys, style="BarStrong.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 18), pady=1)
            ttk.Label(box, text=what, style="Tab.TLabel").grid(row=row, column=1, sticky="w")
        ttk.Label(box, text=MOUSE, style="TabNote.TLabel", wraplength=380, justify="left"
                  ).grid(row=len(SHORTCUTS), column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(box, text="Close", command=win.destroy).grid(
            row=len(SHORTCUTS) + 1, column=1, sticky="e", pady=(12, 0))
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        win.geometry(f"+{self.winfo_rootx() + 80}+{self.winfo_rooty() + 80}")

    def toggle_play(self):
        if self.info:
            self.player.toggle()

    # Every button works from where the last one was going, not from where the player
    # has got to: it may still be finding an older frame, and the newest click wins.

    def _landing(self, t: float) -> float:
        """The frame the player shows when asked for t: the first from half a frame
        before it."""
        t = max(0.0, min(t, self.info.duration))
        frames = self.info.frames
        if not frames:
            return t
        i = bisect.bisect_left(frames, t - self.player.frame_duration / 2)
        return frames[i] if i < len(frames) else frames[-1]

    def stop(self):
        if self.info:
            part = self._current()
            self.position = self._landing(part.start if part else 0.0)
            self.player.stop()
            self._update_time()

    def seek(self, t: float, exact: bool = True, pause: bool = False):
        if not self.info:
            return
        t = max(0.0, min(t, self.info.duration))
        self.position = self._landing(t) if exact else t
        self.player.seek(t, exact=exact, pause=pause)
        self._update_time()

    def jump(self, seconds: float):
        self.seek(self.position + seconds)

    def jump_keyframe(self, direction: int):
        if self.info:
            self.seek(engine.next_keyframe(self.info, self.position) if direction > 0
                      else engine.previous_keyframe(self.info, self.position))

    def step(self, frames: int):
        """Frames forward (or back), paused."""
        if not self.info:
            return
        t = self.position
        for _ in range(abs(frames)):
            t = engine.next_frame(self.info, t) if frames > 0 \
                else engine.previous_frame(self.info, t)
        if self.info.frames:
            t = min(t, self.info.frames[-1])  # the end of the video has no frame of its own
        self.seek(t, pause=True)

    def go_part_edge(self, side: str):
        part = self._current()
        if not part:
            return
        self.seek(part.start if side == "start" else part.end - self.player.frame_duration,
                  pause=True)

    def zoom_to_part(self):
        part = self._current()
        if part:
            self.timeline.show(part.start, part.end)

    def save_frame(self):
        if not self.info or not self.info.has_video:
            return
        folder = Path(self.output_var.get().strip() or self.info.path.parent)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.status.configure(text=f"Can't make {folder}: {e}")
            return
        stamp = fmt_time(self.position).replace(":", ".")
        self.player.save_frame(folder / f"{engine.safe_name(self.info.path.stem)} {stamp}.png")

    def redraw_soon(self):
        """Repaint the picture once the window has settled (Tk paints over it)."""
        if self.redraw_job:
            self.after_cancel(self.redraw_job)
        self.redraw_job = self.after(30, self._redraw)

    def _redraw(self):
        self.redraw_job = None
        self.player.redraw()

    def toggle_full_screen(self):
        if self.full_screen:
            self.leave_full_screen()
        else:
            self.enter_full_screen()

    def enter_full_screen(self):
        if self.full_screen or not self.info or not self.info.has_video:
            return
        self.full_screen = FullScreen(self)
        self.full_screen.update_idletasks()
        self.full_screen.focus_force()
        self.player.set_target(self.full_screen.video.winfo_id())
        self._update_transport()
        self._update_time()

    def leave_full_screen(self):
        if not self.full_screen:
            return
        self.player.set_target(self.video.winfo_id())
        self.full_screen.destroy()
        self.full_screen = None
        self.focus_force()
        self.redraw_soon()

    def _overviews(self) -> list[Overview]:
        bars = [self.overview]
        if self.full_screen:
            bars.append(self.full_screen.overview)
        return bars

    def overview_changed(self):
        for bar in self._overviews():
            bar.draw()

    def _draw_timelines(self):
        for bar in self._overviews():
            bar.draw()
        self.timeline.draw()

    def _update_time(self):
        info = self.info
        text = f"{fmt_time(self.position)}  /  {fmt_time(info.duration)}" if info else ""
        self.time_label.configure(text=text)
        part = self._current() if info else None
        if part:
            into = part.into(self.position)
            self.part_time.configure(
                text=f"{part.title}:  {'-' if into < 0 else ''}{fmt_time(abs(into))}  of  "
                     f"{fmt_time(part.length)}")
        else:
            self.part_time.configure(text="")
        if self.full_screen:
            self.full_screen.time_label.configure(text=text)
        for bar in self._overviews():
            bar.draw_playhead()
        if self.playing:
            self.timeline.follow(self.position)
        self.timeline.draw_playhead()

    def _update_transport(self):
        symbol = "⏸" if self.playing else "▶"
        self.play_btn.configure(text=symbol)
        if self.full_screen:
            self.full_screen.play_btn.configure(text=symbol)

    # --- Where the parts go ---

    def _save_as_changed(self):
        label = self.save_as_var.get()
        self.save_as = next(k for k, v in SAVE_AS.items() if v == label)
        self._refresh_parts()

    def _browse_output(self):
        folder = filedialog.askdirectory(parent=self, title="Save the parts in",
                                         initialdir=self.output_var.get() or None,
                                         mustexist=False)
        if folder:
            self.output_var.set(str(Path(folder)))

    def _open_output(self):
        folder = self.last_output or Path(self.output_var.get())
        if folder and folder.is_dir():
            os.startfile(folder)

    # --- Splicing ---

    def _splice(self):
        info = self.info
        if not info or self.busy:
            return
        # A time still being typed counts: commit it now, as leaving the box would
        typing = self.focus_get()
        for side in ("start", "end"):
            self._commit_time(side)
        for entry, commit in list(getattr(self, "removed_commits", [])):
            if entry is typing and entry.winfo_exists():
                commit(None)
        self.focus_set()
        self.update_idletasks()
        kinds = {t.index: t.kind for t in info.tracks}
        if self.save_as == "sound":
            sound = self._sound_track()
            if not sound:
                messagebox.showinfo("Splicer", "Check a sound track to save (Tracks tab).",
                                    parent=self)
                return
            ext = engine.sound_ext(sound)
            tracks = [sound.index]
            if ext == ".mka":  # carries the cover art too (FFmpeg can't put it in .m4a)
                tracks += [i for i, on in self.track_use.items() if on and kinds[i] == "cover"]
        else:
            tracks = [i for i, on in self.track_use.items() if on]
            if not any(kinds[i] in ("video", "audio", "subtitle") for i in tracks):
                messagebox.showinfo("Splicer", "Check at least one picture, sound or subtitle "
                                    "track (Tracks tab).", parent=self)
                return
        used = [p for p in self.parts if p.use]
        if not used:
            messagebox.showinfo("Splicer", "Check at least one part.", parent=self)
            return
        empty = [p for p in used if p.length <= EPS]
        if empty:
            messagebox.showinfo("Splicer", f"“{empty[0].title}” is empty: it starts and ends "
                                "on the same keyframe. Change it or uncheck it.", parent=self)
            return
        text = self.output_var.get().strip()
        if not text:
            messagebox.showinfo("Splicer", "Choose a folder to save the parts in.",
                                parent=self)
            return
        folder = Path(text)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Splicer", f"Can't make the folder {folder}:\n{e}",
                                 parent=self)
            return
        names = self._names()
        jobs = [(p, folder / names[p]) for p in used]
        if any(dest.resolve() == info.path.resolve() for _, dest in jobs):
            messagebox.showerror("Splicer", "A part would replace the video itself. "
                                 "Choose another folder or name.", parent=self)
            return
        existing = [dest for _, dest in jobs if dest.exists()]
        if existing and not messagebox.askyesno(
                "Splicer", f"{len(existing)} of these files already exist in {folder}, "
                f"like {existing[0].name}.\n\nReplace them?", parent=self):
            return
        # Roughly what the parts will take: the video's size, by length
        need = sum(p.length for p in used) / max(info.duration, 1e-9) * info.path.stat().st_size
        free = shutil.disk_usage(folder).free + sum(d.stat().st_size for d in existing)
        if need > free and self.save_as == "video":
            messagebox.showerror("Splicer", f"The parts need about {fmt_size(need)}, but "
                                 f"there's only {fmt_size(free)} free there.", parent=self)
            return

        self.busy = "splicing"
        self.cancel.clear()
        self.last_output = folder
        self.progress.configure(value=0)
        total = sum(p.length for p, _ in jobs)
        self.status.configure(text=f"Splicing 1 of {len(jobs)}…")
        self._sync()
        album = info.path.stem
        jobs = [(p.title, p.start, p.end, dest,
                 {"track": f"{n}/{len(jobs)}", "album": album}
                 if self.save_as == "sound" else {}, [tuple(r) for r in p.removed], p.length)
                for n, (p, dest) in enumerate(jobs, 1)]

        exact = self.anywhere and self.save_as == "video"

        def work():
            done_length, written, failed = 0.0, [], []
            for n, (title, start, end, dest, tags, removed, length) in enumerate(jobs, 1):
                self.events.put(("part", n, len(jobs), dest.name))
                try:
                    engine.cut(info, start, end, tracks, dest, title,
                               lambda f, base=done_length, length=length: self.events.put(
                                   ("progress", (base + f * length) / max(total, 1e-9))),
                               self.cancel, tags=tags, removed=removed,
                               exact=exact, note=lambda text, n=n, name=dest.name:
                               self.events.put(("part", n, len(jobs), f"{name} ({text})")))
                    written.append(dest)
                except engine.Cancelled:
                    self.events.put(("spliced", written, failed, True))
                    return
                except Exception as e:
                    failed.append((dest.name, str(e) or repr(e)))
                done_length += length
            self.events.put(("spliced", written, failed, False))

        self.splicer_thread = threading.Thread(target=work, daemon=True)
        self.splicer_thread.start()

    def _cancel(self):
        if self.busy:
            self.cancel.set()
            self.status.configure(text="Stopping…")

    def _spliced(self, written: list[Path], failed: list, stopped: bool):
        self.busy = ""
        self.progress.configure(value=0 if stopped else 1.0)
        parts = f"{len(written)} part{'s' * (len(written) != 1)}"
        if stopped:
            text = f"Cancelled. {parts} written; the one being written was removed."
        elif failed:
            text = f"Wrote {parts}; {len(failed)} failed."
        else:
            text = f"Done: wrote {parts} to {self.last_output}."
        self.status.configure(text=text)
        self._sync()
        if failed:
            lines = "\n".join(f"• {name}: {error}" for name, error in failed[:12])
            messagebox.showerror("Splicer", f"These parts couldn't be written:\n\n{lines}",
                                 parent=self)

    # --- Keeping the window in step ---

    def _refresh_all(self):
        self._refresh_tracks()
        self._refresh_parts()
        self._show_selected()

    def _sync(self):
        """Enable what can be used right now."""
        idle = not self.busy
        loaded = self.info is not None
        part = self._current() if loaded else None

        def state(widget, on):
            widget.state(["!disabled"] if on else ["disabled"])

        for b in self.part_buttons:
            state(b, idle and loaded)
        state(self.join_btn, idle and loaded and len(self._selected_parts()) > 1)
        state(self.undo_btn, idle and loaded and bool(self.undo))
        state(self.redo_btn, idle and loaded and bool(self.redo))
        for w in self.edit_widgets + getattr(self, "removed_widgets", []):
            if w.winfo_exists():
                state(w, idle and part is not None)
        for w in (self.output_entry, self.browse_btn, self.numbered_check):
            state(w, idle)
        self.save_as_box.state(["readonly", "!disabled"] if idle else ["disabled"])
        self.cut_mode_box.state(["readonly", "!disabled"] if idle else ["disabled"])
        for w in self.transport + self.zoom_buttons:
            state(w, loaded)
        has_picture = loaded and self.info.has_video
        state(self.full_btn, has_picture)
        state(self.frame_btn, has_picture)
        state(self.splice_btn, idle and loaded and any(p.use for p in self.parts))
        # Cancel and the progress bar only while busy; Open folder once there's a folder
        open_folder = idle and self.last_output is not None and self.last_output.is_dir()
        shown = [w for w, on in ((self.cancel_btn, not idle), (self.open_btn, open_folder),
                                 (self.progress, not idle)) if on]
        if shown != self.shown_actions:
            for w in self.shown_actions:
                w.pack_forget()
            beside = self.splice_btn
            for w in shown:
                w.pack(side="right", padx=(0, 10 if w is self.progress else 6), after=beside)
                beside = w
            self.shown_actions = shown

    def _pump(self):
        try:
            for _ in range(200):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(event)
                except Exception as e:  # keep listening, whatever happened
                    self.busy = ""
                    self.status.configure(text=f"Something went wrong: {e}")
                    self._sync()
        finally:
            self.after(15, self._pump)

    def _handle(self, event):
        """One message from a worker thread or the player."""
        kind = event[0]
        if kind == "pos":
            if event[2] < self.player.moves_sent:
                return  # from a move overtaken by a newer click: not where it's going
            self.position = event[1]
            self._update_time()
        elif kind == "state":
            self.playing = event[1]
            self._update_transport()
        elif kind == "loaded":
            self.redraw_soon()
        elif kind == "wave":
            if event[1] == self.wave_for:
                self.wave = event[2]
                if event[2] is None:
                    self.wave_for = None  # stopped or failed: try again next time
                self.timeline.draw()
        elif kind == "saved":
            self.last_output = event[1].parent
            self.status.configure(text=f"Saved the frame as {event[1]}")
            self._sync()
        elif kind == "error":
            self.status.configure(text=f"Player: {event[1]}")
        elif kind == "reading" and event[1] == self.load_id:
            self.progress.configure(value=event[2])
            self.file_detail.configure(text=f"Finding keyframes… {event[2]:.0%}")
        elif kind == "read" and event[1] == self.load_id:
            self.busy = ""
            self.progress.configure(value=0)
            if event[2] is None:
                self.file_detail.configure(text="Stopped.")
                self._refresh_all()
            else:
                self._loaded(event[2])
                self._sync()
        elif kind == "read_failed" and event[1] == self.load_id:
            self.busy = ""
            self.progress.configure(value=0)
            self.file_detail.configure(text="Couldn't read this file.")
            self.placeholder.configure(text="Couldn't read this file.")
            self._refresh_all()
            messagebox.showerror("Splicer", event[2], parent=self)
        elif kind == "part":
            self.status.configure(text=f"Splicing {event[1]} of {event[2]}: {event[3]}")
        elif kind == "progress":
            self.progress.configure(value=event[1])
        elif kind == "spliced":
            self._spliced(event[1], event[2], event[3])

    # --- Closing ---

    def _on_close(self):
        if self.busy == "splicing":
            if not messagebox.askyesno("Splicer", "Parts are still being written. Stop and "
                                       "close? The one being written is removed; the "
                                       "finished ones stay.", parent=self):
                return
        self.cancel.set()
        # Let the part being written stop and remove itself before the process ends
        worker = getattr(self, "splicer_thread", None)
        if worker is not None and worker.is_alive():
            self.status.configure(text="Stopping…")
            self.update_idletasks()
            worker.join(timeout=30)
        self._save_project_now()
        self.player.close()
        self._save()
        self.destroy()


def main() -> int:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except Exception:
        pass
    app = App()
    # A video dropped on Splicer.exe, or opened with it, opens straight away
    if paths := [Path(arg).resolve() for arg in sys.argv[1:] if Path(arg).is_file()]:
        app.after(200, lambda: app.open_video(paths[0]))
    app.mainloop()
    return 0
