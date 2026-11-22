"""The Splicer window: open a video, pick its tracks, watch it and tune the parts
(one per chapter to begin with), then write each part to its own file,
losslessly."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from splicer import __version__, engine
from splicer.engine import EPS, Info
from splicer.paths import DATA_DIR, ICON, SETTINGS_FILE
from splicer.player import Player

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    BaseTk = TkinterDnD.Tk
except Exception:  # drag and drop is a nicety; everything else works without it
    DND_FILES, BaseTk = None, tk.Tk

BG, PANEL, FIELD, HOVER = "#1e1f22", "#2b2d31", "#383a40", "#4e5058"
FG, MUTED, ACCENT = "#e6e6e6", "#9a9ca3", "#5865f2"
GOOD, WARN, BAD = "#3ba55d", "#faa61a", "#ed4245"
SEGMENTS = ("#3c4270", "#4a5190")   # parts on the timeline, alternating
OFF_SEGMENT = "#34363c"

CHECKED, UNCHECKED = "☑", "☐"
DEFAULT_NAME = re.compile(r"Part \d+")
SYMBOLS = ("Segoe UI Symbol", 11)

END_MODES = {"stop": "Stop", "loop": "Loop the part", "continue": "Keep playing"}

PART_COLUMNS = (  # id, heading, width, anchor, stretch
    ("use", "", 34, "center", False),
    ("n", "#", 36, "e", False),
    ("title", "Name", 220, "w", True),
    ("start", "Start", 88, "e", False),
    ("end", "End", 88, "e", False),
    ("length", "Length", 80, "e", False),
    ("file", "File name", 220, "w", True),
)

SHORTCUTS = """Space  play / pause
←  →  back / forward 5 seconds
,  .  one frame back / forward
Ctrl+←  Ctrl+→  keyframe before / after
Home  End  start / last frame of the part
I  O  set the part's start / end here
F  full screen (Esc to leave)
M  mute"""


@dataclass(eq=False)
class Part:
    title: str
    start: float
    end: float
    use: bool = True
    iid: str = ""

    @property
    def length(self) -> float:
        return max(self.end - self.start, 0.0)


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
                 wraplength=360, padx=8, pady=5, font=("Segoe UI", 9)
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


# --- The bar along the video -------------------------------------------------------

class Timeline(tk.Canvas):
    """The whole video as a bar: the parts on it, the chapters marked along the top,
    and the playhead. Click or drag to move the playhead; double-click a part to
    select it."""

    def __init__(self, parent, app: "App", height: int = 30, bg: str = PANEL):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, cursor="hand2")
        self.app = app
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-1>", self._double)

    def _scale(self) -> float:
        info = self.app.info
        return (self.winfo_width() - 2) / info.duration if info and info.duration else 0

    def _time(self, x: int) -> float:
        scale = self._scale()
        return min(max((x - 1) / scale, 0.0), self.app.info.duration) if scale else 0.0

    def draw(self):
        self.delete("all")
        app, info = self.app, self.app.info
        scale = self._scale()
        height = self.winfo_height()
        if not scale:
            return
        selected = {p.iid for p in app._selected_parts()}
        for i, part in enumerate(app.parts):
            x0, x1 = 1 + part.start * scale, 1 + part.end * scale
            color = SEGMENTS[i % 2] if part.use else OFF_SEGMENT
            if part.iid in selected:
                color = ACCENT
            self.create_rectangle(x0, 6, max(x1, x0 + 1), height - 4, fill=color, width=0)
            self.create_line(x0, 4, x0, height - 2, fill=MUTED)
        for ch in info.chapters:
            x = 1 + ch.start * scale
            self.create_line(x, 0, x, 5, fill=WARN, width=2)
        self.draw_playhead()

    def draw_playhead(self):
        self.delete("head")
        scale = self._scale()
        if not scale:
            return
        x = 1 + self.app.position * scale
        self.create_line(x, 0, x, self.winfo_height(), fill="white", width=2, tags="head")
        self.create_polygon(x - 5, 0, x + 5, 0, x, 6, fill="white", tags="head")

    def _press(self, event):
        if self.app.info:
            self.app.seek(self._time(event.x), exact=False)

    def _drag(self, event):
        if self.app.info:
            self.app.seek(self._time(event.x), exact=False)

    def _release(self, event):
        if self.app.info:
            self.app.seek(self._time(event.x), exact=True)

    def _double(self, event):
        if not self.app.info:
            return
        t = self._time(event.x)
        hits = [p for p in self.app.parts if p.start - EPS <= t < p.end + EPS]
        if hits:
            self.app._select(hits[-1])


# --- Full screen -------------------------------------------------------------------

class FullScreen(tk.Toplevel):
    """The video filling the screen, with a bar of controls that shows when the
    mouse moves and hides again when it rests."""

    def __init__(self, app: "App"):
        super().__init__(app, bg="black")
        self.app = app
        self.attributes("-fullscreen", True)
        self.video = tk.Frame(self, bg="black")
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
        self.timeline = Timeline(self.bar, app, height=26, bg=FIELD)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(0, 10))
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


# --- The window -------------------------------------------------------------------

class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Splicer")
        self.configure(bg=BG)
        self.minsize(1000, 640)
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
        self.muted = False
        self._filling = False

        self._style()
        self._build()
        self.player = Player(self.events.put)
        self.player.set_volume(self._volume())
        self.player.set_end_mode(self.end_mode)
        self._sync()
        geometry = self.settings.get("geometry", "")
        scale = self.ui_font.measure("0") / 7
        width = min(int(1440 * scale), int(self.winfo_screenwidth() * 0.92))
        height = min(int(900 * scale), int(self.winfo_screenheight() * 0.88))
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

    def _load_settings(self) -> dict:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        self.settings["geometry"] = self.geometry()
        self.settings["numbered"] = bool(self.numbered_var.get())
        self.settings["volume"] = int(self.volume_var.get())
        self.settings["end_mode"] = self.end_mode
        try:
            self.settings["sashes"] = [self.panes.sashpos(0), self.left.sashpos(0)]
        except tk.TclError:
            pass
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            write_json(SETTINGS_FILE, self.settings)
        except OSError:
            pass

    def _restore_sashes(self):
        self.update_idletasks()
        saved = self.settings.get("sashes")
        width, height = self.panes.winfo_width(), self.left.winfo_height()
        main = saved[0] if saved else int(width * 0.46)
        side = saved[1] if saved else int(self.ui_font.metrics("linespace") * 9)
        try:
            self.panes.sashpos(0, min(max(main, 300), width - 300))
            self.left.sashpos(0, min(max(side, 60), height - 200))
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
        st.configure("TEntry", padding=(4, 2))
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=7, gripcount=0, background=BG,
                     lightcolor=BG, darkcolor=BG, bordercolor=BG)
        st.map("Sash", background=[("active", HOVER)])
        self.ui_font = font.Font(family="Segoe UI", size=9)
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
        keys = ttk.Label(status, text="Keyboard shortcuts", style="Bar.TLabel", cursor="hand2")
        keys.pack(side="right")
        tip(keys, SHORTCUTS)

        # Three sections, each resizable by dragging the gaps between them
        self.panes = ttk.Panedwindow(self, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self.left = ttk.Panedwindow(self.panes, orient="vertical")
        self.panes.add(self.left, weight=1)
        tracks = ttk.Frame(self.left, padding=(2, 2, 6, 6))
        self.left.add(tracks, weight=0)
        parts = ttk.Frame(self.left, padding=(2, 6, 6, 2))
        self.left.add(parts, weight=1)
        player = ttk.Frame(self.panes, padding=(8, 2, 2, 2))
        self.panes.add(player, weight=2)
        self._build_tracks(tracks)
        self._build_parts(parts)
        self._build_player(player)

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
        self.progress = ttk.Progressbar(row, maximum=1.0, length=140)
        self.progress.pack(side="right", padx=(0, 10))

    def _build_tracks(self, box):
        ttk.Label(box, text="Tracks", style="Section.TLabel").pack(anchor="w")
        ttk.Label(box, text="Checked tracks go into every part, and the first checked "
                  "sound track is the one you hear. Click to check or uncheck.",
                  style="Note.TLabel", wraplength=520, justify="left").pack(anchor="w")
        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True, pady=(3, 0))
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
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Parts", style="Section.TLabel").pack(side="left")
        self.parts_note = ttk.Label(head, text="", style="Note.TLabel")
        self.parts_note.pack(side="left", padx=(8, 0))
        self.part_buttons = []
        for text, command, help_text in (
                ("Reset", self._reset_parts,
                 "Start over: one part per chapter, or the whole video if it has none."),
                ("Remove", self._remove_parts, "Remove the selected parts (Delete)."),
                ("Split", self._split_part,
                 "Cut the selected part in two: at the playhead if it's inside the part, "
                 "otherwise near its middle."),
                ("Add part", self._add_part,
                 "Add a part in the gap after the selected one."),
        ):
            b = tip(ttk.Button(head, text=text, command=command), help_text)
            b.pack(side="right", padx=(4, 0))
            self.part_buttons.append(b)

        # The selected part, under the list; the list gives way when space is short
        edit = ttk.Frame(box, padding=(0, 8, 0, 0))
        edit.pack(side="bottom", fill="x")
        holder = ttk.Frame(box)
        holder.pack(fill="both", expand=True, pady=(4, 0))
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
        self.tree.bind("<Double-1>", lambda e: (self.title_entry.focus_set(),
                                                self.title_entry.select_range(0, "end")))

        edit.columnconfigure(1, weight=1)
        ttk.Label(edit, text="Name").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(edit, textvariable=self.title_var)
        self.title_entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        self.title_var.trace_add("write", lambda *a: self._title_changed())
        self.edit_widgets = [self.title_entry]
        self.time_vars, self.side_notes = {}, {}
        for r, side in enumerate(("start", "end"), 1):
            ttk.Label(edit, text="Starts at" if side == "start" else "Ends at"
                      ).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            line = ttk.Frame(edit)
            line.grid(row=r, column=1, sticky="w", pady=2)
            back = tip(ttk.Button(line, text="◀", style="Step.TButton",
                                  command=lambda s=side: self._step_cut(s, -1)),
                       "Move to the keyframe before.")
            back.pack(side="left")
            var = tk.StringVar()
            entry = ttk.Entry(line, textvariable=var, width=13, justify="center")
            entry.pack(side="left", padx=3)
            tip(entry, "Type a time (1:02:03.5, 4:38 or 278.2) and press Enter. It moves "
                "to the nearest keyframe: a lossless cut can only happen there. "
                "Up and Down step between keyframes.")
            for key in ("<Return>", "<KP_Enter>", "<FocusOut>"):
                entry.bind(key, lambda e, s=side: self._commit_time(s))
            entry.bind("<Up>", lambda e, s=side: self._step_cut(s, 1))
            entry.bind("<Down>", lambda e, s=side: self._step_cut(s, -1))
            forward = tip(ttk.Button(line, text="▶", style="Step.TButton",
                                     command=lambda s=side: self._step_cut(s, 1)),
                          "Move to the keyframe after.")
            forward.pack(side="left")
            here = tip(ttk.Button(line, text="Set to playhead",
                                  command=lambda s=side: self._cut_here(s)),
                       ("Start the part at the frame in the player (I), or the keyframe "
                        "nearest it." if side == "start" else
                        "End the part after the frame in the player (O), or at the keyframe "
                        "nearest that."))
            here.pack(side="left", padx=(8, 0))
            note = ttk.Label(line, text="", style="Note.TLabel")
            note.pack(side="left", padx=(8, 0))
            self.time_vars[side], self.side_notes[side] = var, note
            self.edit_widgets += [back, entry, forward, here]
        self.length_label = ttk.Label(edit, text="", style="Muted.TLabel")
        self.length_label.grid(row=3, column=1, sticky="w", pady=(2, 0))

    def _build_player(self, box):
        head = ttk.Frame(box)
        head.pack(fill="x")
        ttk.Label(head, text="Player", style="Section.TLabel").pack(side="left")
        self.part_time = ttk.Label(head, text="", style="Note.TLabel")
        self.part_time.pack(side="right")

        controls = ttk.Frame(box, padding=(0, 6, 0, 0))
        controls.pack(side="bottom", fill="x")
        self.timeline = Timeline(box, self, height=30)
        self.timeline.pack(side="bottom", fill="x", pady=(6, 0))
        self.video = tk.Frame(box, bg="black", cursor="hand2")
        self.video.pack(fill="both", expand=True, pady=(4, 0))
        self.placeholder = tk.Label(self.video, text="Open a video to watch it here.",
                                    bg="black", fg=MUTED, font=("Segoe UI", 10))
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.video.bind("<Configure>", lambda e: self.redraw_soon())
        self.video.bind("<Expose>", lambda e: self.redraw_soon())
        self.video.bind("<ButtonRelease-1>", lambda e: self.toggle_play())
        self.video.bind("<Double-1>", lambda e: self.enter_full_screen())

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
        self.full_btn = tip(ttk.Button(row, text="⛶  Full screen",
                                       command=self.enter_full_screen),
                            "Watch full screen (F, or double-click the picture). Esc leaves.")
        self.full_btn.pack(side="right")

        row = ttk.Frame(controls)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="At the end of the part").pack(side="left")
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
        self.volume_var = tk.DoubleVar(value=self.settings.get("volume", 80))
        volume = ttk.Scale(row, from_=0, to=100, variable=self.volume_var, length=110,
                           command=lambda v: self.player.set_volume(self._volume()))
        volume.pack(side="right")
        self.mute_btn = tip(ttk.Button(row, text="🔊", style="Transport.TButton",
                                       command=self.toggle_mute), "Mute (M)")
        self.mute_btn.pack(side="right", padx=(0, 4))

    # --- Keys ---

    def _bind_keys(self):
        self.bind_all("<Control-o>", lambda e: self._choose_file())
        keys = {
            "<space>": self.toggle_play,
            "<Left>": lambda: self.jump(-5), "<Right>": lambda: self.jump(5),
            "<comma>": lambda: self.step(-1), "<period>": lambda: self.step(1),
            "<Control-Left>": lambda: self.jump_keyframe(-1),
            "<Control-Right>": lambda: self.jump_keyframe(1),
            "<Home>": lambda: self.go_part_edge("start"),
            "<End>": lambda: self.go_part_edge("end"),
            "<i>": lambda: self._cut_here("start"), "<I>": lambda: self._cut_here("start"),
            "<o>": lambda: self._cut_here("end"), "<O>": lambda: self._cut_here("end"),
            "<f>": self.toggle_full_screen, "<F>": self.toggle_full_screen,
            "<F11>": self.toggle_full_screen,
            "<Escape>": self.leave_full_screen,
            "<m>": self.toggle_mute, "<M>": self.toggle_mute,
        }
        for key, action in keys.items():
            self.bind_all(key, lambda e, a=action: self._key(e, a))
        # The parts list has its own meaning for these; the player's comes first
        for key in ("<space>", "<Left>", "<Right>", "<Home>", "<End>"):
            self.tree.bind(key, lambda e, k=key: self._key(e, keys[k]))

    def _key(self, event, action):
        if isinstance(event.widget, (ttk.Entry, tk.Entry, ttk.Combobox)) and \
                event.keysym != "Escape":
            return None  # typing
        if not self.info and event.keysym not in ("Escape",):
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

    def _drop(self, event):
        paths = [Path(p) for p in self.tk.splitlist(event.data)]
        files = [p for p in paths if p.is_file()]
        if files:
            self.open_video(files[0])

    def open_video(self, path: Path):
        if self.busy:
            self.bell()
            return
        self.settings["last_dir"] = str(path.parent)
        self.load_id += 1
        self.busy = "reading"
        self.cancel.clear()
        self.player.unload()
        self.info, self.parts, self.track_use = None, [], {}
        self.shown_part = None
        self.position = 0.0
        self.last_output = None
        self.file_label.configure(text=path.name)
        self.file_detail.configure(text="Reading…")
        self.title(f"Splicer: {path.name}")
        self.placeholder.configure(text="Reading…")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
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
        self.file_detail.configure(text="  ·  ".join(bits))
        self.output_var.set(str(info.path.parent / engine.safe_name(info.path.stem)))
        if info.has_video:
            self.placeholder.place_forget()
        else:
            self.placeholder.configure(text="No picture: sound only.")
        self.player.load(info.path, info.origin, info.duration, self._audio_track())
        self._reset_parts(ask=False)
        self.status.configure(text="Check the tracks and parts, then press Splice.")

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
            return
        for t in self.info.tracks:
            on = self.track_use.get(t.index, False)
            self.tracks.insert("", "end", iid=str(t.index),
                               values=(CHECKED if on else UNCHECKED, t.label),
                               tags=() if on else ("off",))

    def _toggle_track(self, event):
        iid = self.tracks.identify_row(event.y)
        if not iid or self.busy:
            return
        index = int(iid)
        self.track_use[index] = not self.track_use.get(index, False)
        on = self.track_use[index]
        self.tracks.item(iid, values=(CHECKED if on else UNCHECKED,
                                      self.tracks.item(iid, "values")[1]),
                         tags=() if on else ("off",))
        self.player.set_audio(self._audio_track())
        self._sync()

    # --- Parts ---

    def _reset_parts(self, ask: bool = True):
        info = self.info
        if not info or self.busy:
            return
        if ask and self.parts and not messagebox.askyesno(
                "Splicer", "Throw away your changes and start again from the chapters?",
                parent=self):
            return
        if info.chapters:
            self.parts = []
            for ch in info.chapters:
                start, end = engine.snap(info, ch.start), engine.snap(info, ch.end)
                self.parts.append(Part(ch.title, start, end, use=end - start > EPS))
        else:
            self.parts = [Part("Part 1", 0.0, info.duration)]
        self._sorted()
        self.shown_part = None
        self._refresh_parts()
        if self.parts:
            self._select(self.parts[0])

    def _sorted(self):
        self.parts.sort(key=lambda p: (p.start, p.end))
        # Names Splicer made up ("Part 3") follow the order; names you typed don't change
        for n, part in enumerate(self.parts, 1):
            if DEFAULT_NAME.fullmatch(part.title):
                part.title = f"Part {n}"

    def _refresh_parts(self):
        keep = self._selected_parts()
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
                part.title, fmt_time(part.start), fmt_time(part.end), fmt_time(part.length),
                names.get(part, "not saved")))
        if keep:
            self.tree.selection_set([p.iid for p in keep if p in self.parts])
        used = [p for p in self.parts if p.use]
        if self.info:
            total = sum(p.length for p in used)
            self.parts_note.configure(text=f"{len(used)} of {len(self.parts)} checked  ·  "
                                           f"{fmt_time(total).rsplit('.', 1)[0]} in all")
        else:
            self.parts_note.configure(text="")
        self._draw_timelines()
        self._sync()

    def _names(self) -> dict[Part, str]:
        ext = self.info.path.suffix.lower() if self.info else ""
        return file_names(self.parts, ext, bool(self.numbered_var.get()))

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

    def _select(self, part: Part):
        self.tree.selection_set(part.iid)
        self.tree.focus(part.iid)
        self.tree.see(part.iid)
        self._show_selected()

    def _tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if self.tree.identify_column(event.x) == "#1" and iid and not self.busy:
            part = next(p for p in self.parts if p.iid == iid)
            part.use = not part.use
            self._refresh_parts()
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
        part = Part("Part 0", start, end)  # numbered by _sorted
        self.parts.append(part)
        self._sorted()
        self._refresh_parts()
        self._select(part)
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")

    def _split_part(self):
        info, part = self.info, self._current()
        if not info or not part or self.busy:
            return
        inside = lambda t: part.start + EPS < t < part.end - EPS  # noqa: E731
        at = engine.snap(info, self.position)
        if not inside(at):
            at = engine.snap(info, (part.start + part.end) / 2)
        if not inside(at):
            keys = [k for k in info.keyframes if inside(k)]
            if not keys:
                self.status.configure(text="There's no keyframe inside that part to split at.")
                self.bell()
                return
            at = min(keys, key=lambda k: abs(k - (part.start + part.end) / 2))
        title = "Part 0" if DEFAULT_NAME.fullmatch(part.title) else f"{part.title} (2)"
        second = Part(title, at, part.end, part.use)
        part.end = at
        self.parts.append(second)
        self._sorted()
        self._refresh_parts()
        self._select(second)

    def _remove_parts(self):
        chosen = self._selected_parts()
        if not chosen or self.busy:
            return
        index = self.parts.index(chosen[0])
        self.parts = [p for p in self.parts if p not in chosen]
        self._sorted()
        self._refresh_parts()
        if self.parts:
            self._select(self.parts[min(index, len(self.parts) - 1)])
        else:
            self._show_selected()

    # --- The selected part ---

    def _show_selected(self):
        part = self._current() if self.info else None
        if not part:
            self.title_var.set("")
            for side in ("start", "end"):
                self.time_vars[side].set("")
                self.side_notes[side].configure(text="")
            self.length_label.configure(text="")
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
        self.length_label.configure(text=f"Length {fmt_time(part.length)}")
        self.side_notes["start"].configure(
            text="the start of the video" if part.start <= EPS else "")
        self.side_notes["end"].configure(
            text="the end of the video" if part.end >= self.info.duration - EPS else "")
        self.player.set_range(part.start, part.end)
        if part is not self.shown_part:
            self.shown_part = part
            self.player.seek(part.start)  # carries on playing if it was
        self._draw_timelines()
        self._update_time()
        self._sync()

    def _title_changed(self):
        if self._filling:
            return
        part = self._current()
        if not part or self.busy or self.title_var.get() == part.title:
            return
        part.title = self.title_var.get()
        names = self._names()
        for other in self.parts:  # a clash can change another part's file name too
            if self.tree.exists(other.iid):
                values = list(self.tree.item(other.iid, "values"))
                values[2], values[6] = other.title, names.get(other, "not saved")
                self.tree.item(other.iid, values=values)

    def _commit_time(self, side: str):
        part, info = self._current(), self.info
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
        self._move(part, side, engine.snap(info, min(t, info.duration)), asked=t)

    def _step_cut(self, side: str, direction: int):
        part, info = self._current(), self.info
        if not part or not info or self.busy:
            return "break"
        now = getattr(part, side)
        t = engine.next_keyframe(info, now) if direction > 0 \
            else engine.previous_keyframe(info, now)
        self._move(part, side, t)
        return "break"

    def _cut_here(self, side: str):
        """Start the part at the frame in the player, or end it after that frame."""
        part, info = self._current(), self.info
        if not part or not info or self.busy:
            return
        asked = self.position if side == "start" else \
            min(self.position + self.player.frame_duration, info.duration)
        self._move(part, side, engine.snap(info, asked), asked=asked)

    def _move(self, part: Part, side: str, t: float, asked: float | None = None):
        other = part.end if side == "start" else part.start
        if (side == "start" and t >= other - EPS) or (side == "end" and t <= other + EPS):
            self.status.configure(text="A part has to end after it starts.")
            self.time_vars[side].set(fmt_time(getattr(part, side)))
            self.bell()
            return
        setattr(part, side, t)
        self._sorted()
        self._refresh_parts()
        self._select(part)
        # Show the cut: the part's first frame, or its last
        if side == "start":
            self.player.seek(t, pause=True)
        else:
            self.player.seek(t - self.player.frame_duration, pause=True)
        if asked is not None and abs(asked - t) > EPS:
            self.status.configure(text=f"{fmt_time(asked)} isn't a keyframe: the cut is at the "
                                       f"nearest one, {fmt_time(t)}.")
        else:
            self.status.configure(text="")

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

    def toggle_play(self):
        if self.info:
            self.player.toggle()

    def stop(self):
        if self.info:
            self.player.stop()

    def seek(self, t: float, exact: bool = True):
        if not self.info:
            return
        self.position = max(0.0, min(t, self.info.duration))
        self.player.seek(self.position, exact=exact)
        self._update_time()

    def jump(self, seconds: float):
        self.seek(self.position + seconds)

    def jump_keyframe(self, direction: int):
        if self.info:
            self.seek(engine.next_keyframe(self.info, self.position) if direction > 0
                      else engine.previous_keyframe(self.info, self.position))

    def step(self, frames: int):
        if self.info:
            self.player.step(frames)

    def go_part_edge(self, side: str):
        part = self._current()
        if not part:
            return
        if side == "start":
            self.player.seek(part.start, pause=True)
        else:
            self.player.seek(part.end - self.player.frame_duration, pause=True)

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

    def _timelines(self) -> list[Timeline]:
        lines = [self.timeline]
        if self.full_screen:
            lines.append(self.full_screen.timeline)
        return lines

    def _draw_timelines(self):
        for line in self._timelines():
            line.draw()

    def _update_time(self):
        info = self.info
        text = f"{fmt_time(self.position)}  /  {fmt_time(info.duration)}" if info else ""
        self.time_label.configure(text=text)
        part = self._current() if info else None
        if part:
            into = self.position - part.start
            self.part_time.configure(
                text=f"{part.title}:  {'-' if into < 0 else ''}{fmt_time(abs(into))}  of  "
                     f"{fmt_time(part.length)}")
        else:
            self.part_time.configure(text="")
        if self.full_screen:
            self.full_screen.time_label.configure(text=text)
        for line in self._timelines():
            line.draw_playhead()

    def _update_transport(self):
        symbol = "⏸" if self.playing else "▶"
        self.play_btn.configure(text=symbol)
        if self.full_screen:
            self.full_screen.play_btn.configure(text=symbol)

    # --- Where the parts go ---

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
        self.focus_set()  # commits a time still being typed
        self.update_idletasks()
        tracks = [i for i, on in self.track_use.items() if on]
        kinds = {t.index: t.kind for t in info.tracks}
        if not any(kinds[i] in ("video", "audio", "subtitle") for i in tracks):
            messagebox.showinfo("Splicer", "Check at least one picture, sound or subtitle "
                                "track.", parent=self)
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
        if need > free:
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
        jobs = [(p.title, p.start, p.end, dest) for p, dest in jobs]

        def work():
            done_length, written, failed = 0.0, [], []
            for n, (title, start, end, dest) in enumerate(jobs, 1):
                length = end - start
                self.events.put(("part", n, len(jobs), dest.name))
                try:
                    engine.cut(info, start, end, tracks, dest, title,
                               lambda f, base=done_length, length=length: self.events.put(
                                   ("progress", (base + f * length) / max(total, 1e-9))),
                               self.cancel)
                    written.append(dest)
                except engine.Cancelled:
                    self.events.put(("spliced", written, failed, True))
                    return
                except Exception as e:
                    failed.append((dest.name, str(e) or repr(e)))
                done_length += length
            self.events.put(("spliced", written, failed, False))

        threading.Thread(target=work, daemon=True).start()

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
        for w in self.edit_widgets:
            state(w, idle and part is not None)
        for w in (self.output_entry, self.browse_btn, self.numbered_check):
            state(w, idle)
        for w in self.transport:
            state(w, loaded)
        state(self.full_btn, loaded and self.info.has_video)
        state(self.splice_btn, idle and loaded and any(p.use for p in self.parts))
        state(self.cancel_btn, bool(self.busy))
        state(self.open_btn, idle and self.last_output is not None
              and self.last_output.is_dir())

    def _pump(self):
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "pos":
                    self.position = event[1]
                    self._update_time()
                elif kind == "state":
                    self.playing = event[1]
                    self._update_transport()
                elif kind == "loaded":
                    self.redraw_soon()
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
                    else:
                        self._loaded(event[2])
                    self._refresh_all()
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
        except queue.Empty:
            pass
        self.after(15, self._pump)

    # --- Closing ---

    def _on_close(self):
        if self.busy == "splicing":
            if not messagebox.askyesno("Splicer", "Parts are still being written. Stop and "
                                       "close? The one being written is removed; the "
                                       "finished ones stay.", parent=self):
                return
        self.cancel.set()
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
    if paths := [Path(arg) for arg in sys.argv[1:] if Path(arg).is_file()]:
        app.after(200, lambda: app.open_video(paths[0]))
    app.mainloop()
    return 0
