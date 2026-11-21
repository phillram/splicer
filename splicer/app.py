"""The Splicer window: open a video, pick its tracks, tune the parts (one per
chapter to begin with), and write each part to its own file, losslessly."""

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
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk

from splicer import __version__, engine
from splicer.engine import EPS, Info
from splicer.paths import DATA_DIR, ICON, SETTINGS_FILE

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

PREVIEW_W, PREVIEW_H = 320, 180
CHECKED, UNCHECKED = "☑", "☐"
DEFAULT_NAME = re.compile(r"Part \d+")

PART_COLUMNS = (  # id, heading, width, anchor, stretch
    ("use", "", 34, "center", False),
    ("n", "#", 36, "e", False),
    ("title", "Name", 300, "w", True),
    ("start", "Start", 92, "e", False),
    ("end", "End", 92, "e", False),
    ("length", "Length", 84, "e", False),
    ("file", "File name", 300, "w", True),
)


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
                 wraplength=340, padx=8, pady=5, font=("Segoe UI", 9)
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


# --- The window -------------------------------------------------------------------

class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Splicer")
        self.configure(bg=BG)
        self.minsize(900, 700)
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
        self.output_auto = True            # still the folder Splicer chose
        self.previews: dict[str, tk.PhotoImage | None] = {"start": None, "end": None}
        self.preview_cache: OrderedDict = OrderedDict()
        self.preview_wanted: dict[str, tuple | None] = {"start": None, "end": None}
        self.preview_wake = threading.Event()
        self.preview_lock = threading.Lock()
        threading.Thread(target=self._preview_worker, daemon=True).start()

        self._style()
        self._build()
        self._sync()
        geometry = self.settings.get("geometry", "")
        scale = self.ui_font.measure("0") / 7
        self.geometry(geometry if on_screen(geometry)
                      else f"{int(1180 * scale)}x{int(860 * scale)}")
        self.bind("<Map>", lambda e: e.widget is self and dark_title_bar(self), add=True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Control-o>", lambda e: self._choose_file())
        self.after(50, self._pump)

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
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            write_json(SETTINGS_FILE, self.settings)
        except OSError:
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
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Bar.TFrame", background=PANEL)
        st.configure("Bar.TLabel", background=PANEL, foreground=MUTED)
        st.configure("BarStrong.TLabel", background=PANEL, foreground=FG,
                     font=("Segoe UI", 10, "bold"))
        st.configure("Section.TLabel", foreground=FG, font=("Segoe UI", 9, "bold"))
        st.configure("Note.TLabel", foreground=MUTED)
        st.configure("Warn.TLabel", foreground=WARN)
        st.configure("TSeparator", background=HOVER)
        st.configure("TCheckbutton", background=BG, indicatorbackground=FIELD,
                     indicatorforeground="white", indicatormargin=(0, 0, 4, 0))
        st.map("TCheckbutton", background=[("active", BG)],
               indicatorbackground=[("selected", ACCENT), ("disabled", PANEL)])
        st.configure("TEntry", padding=(4, 2))
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                     bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
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
        # Top bar: the video
        top = ttk.Frame(self, style="Bar.TFrame", padding=(10, 8))
        top.pack(fill="x")
        tip(ttk.Button(top, text="Open video…", style="Accent.TButton",
                       command=self._choose_file),
            "Pick a video (Ctrl+O). You can also drop one onto the window.").pack(side="left")
        names = ttk.Frame(top, style="Bar.TFrame")
        names.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.file_label = ttk.Label(names, text="No video open", style="BarStrong.TLabel")
        self.file_label.pack(anchor="w")
        self.file_detail = ttk.Label(names, text="Drop a video here, or press Open video.",
                                     style="Bar.TLabel")
        self.file_detail.pack(anchor="w")

        body = ttk.Frame(self, padding=(10, 8, 10, 0))
        body.pack(fill="both", expand=True)

        # Tracks
        ttk.Label(body, text="Tracks", style="Section.TLabel").pack(anchor="w")
        ttk.Label(body, text="Checked tracks go into every part. Click one to check or "
                  "uncheck it.", style="Note.TLabel").pack(anchor="w")
        holder = ttk.Frame(body)
        holder.pack(fill="x", pady=(3, 8))
        self.tracks = ttk.Treeview(holder, columns=("use", "track"), show="", height=4,
                                   selectmode="none")
        self.tracks.column("use", width=34, stretch=False, anchor="center")
        self.tracks.column("track", width=600, stretch=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tracks.yview)
        self.tracks.configure(yscrollcommand=scroll.set)
        self.tracks.pack(side="left", fill="x", expand=True)
        scroll.pack(side="left", fill="y")
        self.tracks.tag_configure("off", foreground=MUTED)
        self.tracks.bind("<Button-1>", self._toggle_track)

        # Parts
        head = ttk.Frame(body)
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
                 "Cut the selected part in two at the keyframe nearest its middle."),
                ("Add part", self._add_part,
                 "Add a part after the selected one, running to the next part or the end."),
        ):
            b = tip(ttk.Button(head, text=text, command=command), help_text)
            b.pack(side="right", padx=(4, 0))
            self.part_buttons.append(b)

        self.timeline = tk.Canvas(body, height=26, bg=PANEL, highlightthickness=0,
                                  cursor="hand2")
        self.timeline.pack(fill="x", pady=(4, 3))
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())
        self.timeline.bind("<Button-1>", self._timeline_click)

        holder = ttk.Frame(body)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=[c[0] for c in PART_COLUMNS],
                                 show="headings", selectmode="extended", height=4)
        for cid, heading, width, anchor, stretch in PART_COLUMNS:
            self.tree.heading(cid, text=heading, anchor=anchor if anchor != "e" else "e")
            self.tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.tag_configure("off", foreground=MUTED)
        self.tree.tag_configure("empty", foreground=BAD)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())
        self.tree.bind("<Delete>", lambda e: self._remove_parts())
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        self.tree.bind("<Double-1>", lambda e: (self.title_entry.focus_set(),
                                                self.title_entry.select_range(0, "end")))

        # The selected part
        edit = ttk.Frame(body, padding=(0, 8, 0, 0))
        edit.pack(side="bottom", fill="x", before=holder)  # the list gives way, not this
        row = ttk.Frame(edit)
        row.pack(fill="x")
        ttk.Label(row, text="Name").pack(side="left")
        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(row, textvariable=self.title_var)
        self.title_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.title_var.trace_add("write", lambda *a: self._title_changed())

        sides = ttk.Frame(edit)
        sides.pack(fill="x", pady=(6, 0))
        self.time_vars, self.time_entries, self.preview_labels = {}, {}, {}
        self.side_notes, self.edit_widgets = {}, [self.title_entry]
        for column, side in enumerate(("start", "end")):
            box = ttk.Frame(sides)
            box.grid(row=0, column=column, sticky="nw", padx=(0, 16) if side == "start" else 0)
            line = ttk.Frame(box)
            line.pack(anchor="w")
            ttk.Label(line, text="Starts at" if side == "start" else "Ends at",
                      width=9).pack(side="left")
            back = tip(ttk.Button(line, text="◀", style="Step.TButton",
                                  command=lambda s=side: self._step(s, -1)),
                       "Move to the keyframe before.")
            back.pack(side="left")
            var = tk.StringVar()
            entry = ttk.Entry(line, textvariable=var, width=13, justify="center")
            entry.pack(side="left", padx=3)
            tip(entry, "Type a time (1:02:03.5, 4:38 or 278.2) and press Enter. It moves "
                "to the nearest keyframe: a lossless cut can only happen there.")
            entry.bind("<Return>", lambda e, s=side: self._commit_time(s))
            entry.bind("<KP_Enter>", lambda e, s=side: self._commit_time(s))
            entry.bind("<FocusOut>", lambda e, s=side: self._commit_time(s))
            entry.bind("<Up>", lambda e, s=side: self._step(s, 1))
            entry.bind("<Down>", lambda e, s=side: self._step(s, -1))
            forward = tip(ttk.Button(line, text="▶", style="Step.TButton",
                                     command=lambda s=side: self._step(s, 1)),
                          "Move to the keyframe after.")
            forward.pack(side="left")
            note = ttk.Label(line, text="", style="Note.TLabel")
            note.pack(side="left", padx=(8, 0))
            frame = tk.Frame(box, bg=PANEL, width=PREVIEW_W, height=PREVIEW_H)
            frame.pack(anchor="w", pady=(4, 0))
            frame.pack_propagate(False)  # a fixed size, image or not
            image = tk.Label(frame, bg=PANEL, fg=MUTED, text="", font=("Segoe UI", 9))
            image.pack(fill="both", expand=True)
            self.time_vars[side], self.time_entries[side] = var, entry
            self.preview_labels[side], self.side_notes[side] = image, note
            self.edit_widgets += [back, entry, forward]
        self.length_label = ttk.Label(sides, text="", style="Muted.TLabel")
        self.length_label.grid(row=0, column=2, sticky="nw", padx=(16, 0), pady=(3, 0))

        # Bottom: where the parts go, and go
        # Packed before the body, so a short window squeezes the list, not these
        bottom = ttk.Frame(self, padding=(10, 8))
        bottom.pack(side="bottom", fill="x", before=body)
        ttk.Separator(self).pack(side="bottom", fill="x", pady=(8, 0), before=body)
        row = ttk.Frame(bottom)
        row.pack(fill="x")
        ttk.Label(row, text="Save parts in").pack(side="left")
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.output_entry.bind("<Key>", lambda e: setattr(self, "output_auto", False),
                               add=True)
        self.browse_btn = ttk.Button(row, text="Browse…", command=self._browse_output)
        self.browse_btn.pack(side="left")
        self.numbered_var = tk.BooleanVar(value=self.settings.get("numbered", True))
        self.numbered_check = tip(
            ttk.Checkbutton(row, text="Number the files", variable=self.numbered_var,
                            command=self._refresh_parts),
            "Start each file name with its place in the video: 01 - Name, 02 - Name…")
        self.numbered_check.pack(side="left", padx=(12, 0))

        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(8, 0))
        self.splice_btn = tip(ttk.Button(row, text="Splice", style="Accent.TButton",
                                         command=self._splice),
                              "Write each checked part to its own file. Nothing is "
                              "re-encoded: the parts are exact copies.")
        self.splice_btn.pack(side="right")
        self.stop_btn = ttk.Button(row, text="Stop", command=self._stop)
        self.stop_btn.pack(side="right", padx=(0, 6))
        self.open_btn = tip(ttk.Button(row, text="Open folder", command=self._open_output),
                            "Show the folder the parts were saved in.")
        self.open_btn.pack(side="right", padx=(0, 6))
        self.progress = ttk.Progressbar(row, maximum=1.0, length=180)
        self.progress.pack(side="left")
        self.status = ttk.Label(row, text=f"Splicer {__version__}", style="Muted.TLabel")
        self.status.pack(side="left", padx=(10, 0), fill="x", expand=True)

        if DND_FILES:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._drop)
            except Exception:
                pass

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
        self.info, self.parts, self.track_use = None, [], {}
        self.last_output = None
        self.file_label.configure(text=path.name)
        self.file_detail.configure(text="Reading…")
        self.title(f"Splicer: {path.name}")
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
        self.output_auto = True
        self.output_var.set(str(info.path.parent / engine.safe_name(info.path.stem)))
        self._reset_parts(ask=False)
        self.status.configure(text="Check the tracks and parts, then press Splice.")

    # --- Tracks ---

    def _refresh_tracks(self):
        self.tracks.delete(*self.tracks.get_children())
        if not self.info:
            return
        for t in self.info.tracks:
            on = self.track_use.get(t.index, False)
            self.tracks.insert("", "end", iid=str(t.index),
                               values=(CHECKED if on else UNCHECKED, t.label),
                               tags=() if on else ("off",))
        self.tracks.configure(height=min(max(len(self.info.tracks), 2), 7))

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
        self._draw_timeline()
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

    def _toggle_selected(self):
        if self.busy:
            return
        chosen = self._selected_parts()
        if chosen:
            on = not all(p.use for p in chosen)
            for p in chosen:
                p.use = on
            self._refresh_parts()

    def _add_part(self):
        info = self.info
        if not info or self.busy:
            return
        current = self._current()
        # The gap after the selected part; or after the last one; or, with the whole
        # video already covered, a copy of the selected part to change
        start, end = None, None
        for after in ([current.end] if current else []) +                 [max((p.end for p in self.parts), default=0.0)]:
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
        middle = engine.snap(info, (part.start + part.end) / 2)
        if not part.start + EPS < middle < part.end - EPS:
            inside = [k for k in info.keyframes if part.start + EPS < k < part.end - EPS]
            if not inside:
                self.status.configure(text="There's no keyframe inside that part to split at.")
                self.bell()
                return
            middle = min(inside, key=lambda k: abs(k - (part.start + part.end) / 2))
        title = "Part 0" if DEFAULT_NAME.fullmatch(part.title) else f"{part.title} (2)"
        second = Part(title, middle, part.end, part.use)
        part.end = middle
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
        part = self._current()
        self._showing = part
        if not part or not self.info:
            self.title_var.set("")
            for side in ("start", "end"):
                self.time_vars[side].set("")
                self.side_notes[side].configure(text="")
                self._set_preview(side, None, "")
            self.length_label.configure(text="")
            self._sync()
            return
        if self.title_var.get() != part.title:
            self._filling = True
            self.title_var.set(part.title)
            self._filling = False
        for side in ("start", "end"):
            self.time_vars[side].set(fmt_time(getattr(part, side)))
        self._show_details(part)
        self._sync()

    def _show_details(self, part: Part):
        info = self.info
        self.length_label.configure(text=f"Length {fmt_time(part.length)}")
        for side in ("start", "end"):
            t = getattr(part, side)
            note = ""
            if side == "end" and t >= info.duration - EPS:
                note = "the end of the video"
                self._set_preview(side, None, "The end of the video")
                self.preview_wanted["end"] = None
            else:
                if side == "end":
                    note = "just before this frame"
                elif t <= EPS:
                    note = "the start of the video"
                self._request_preview(side, t)
            self.side_notes[side].configure(text=note)

    def _title_changed(self):
        if getattr(self, "_filling", False):
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
        t = parse_time(text)
        if t is None:
            self.status.configure(text=f"“{text}” isn't a time. Try 4:38, 4:38.25 or 1:02:03.")
            self.time_vars[side].set(fmt_time(getattr(part, side)))
            self.bell()
            return
        self._move(part, side, engine.snap(info, min(t, info.duration)), asked=t)

    def _step(self, side: str, direction: int):
        part, info = self._current(), self.info
        if not part or not info or self.busy:
            return "break"
        now = getattr(part, side)
        t = engine.next_keyframe(info, now) if direction > 0 \
            else engine.previous_keyframe(info, now)
        self._move(part, side, t)
        return "break"

    def _move(self, part: Part, side: str, t: float, asked: float | None = None):
        other = part.end if side == "start" else part.start
        if (side == "start" and t >= other - EPS) or (side == "end" and t <= other + EPS):
            self.status.configure(text="A part has to end after it starts.")
            self.time_vars[side].set(fmt_time(getattr(part, side)))
            self.bell()
            return
        if abs(getattr(part, side) - t) > EPS / 2:
            setattr(part, side, t)
            self._sorted()
            self._refresh_parts()
            self._select(part)
        else:
            self.time_vars[side].set(fmt_time(t))
        if asked is not None and abs(asked - t) > EPS:
            self.status.configure(text=f"{fmt_time(asked)} moved to the nearest keyframe, "
                                       f"{fmt_time(t)}.")

    # --- Previews ---

    def _request_preview(self, side: str, t: float):
        key = (str(self.info.path), round(t, 4))
        if key in self.preview_cache:
            self.preview_cache.move_to_end(key)
            self._set_preview(side, self.preview_cache[key], "")
            with self.preview_lock:
                self.preview_wanted[side] = None
            return
        self._set_preview(side, None, "…")
        with self.preview_lock:
            self.preview_wanted[side] = (key, self.info.path, t, self.info.origin)
        self.preview_wake.set()

    def _preview_worker(self):
        while True:
            self.preview_wake.wait()
            self.preview_wake.clear()
            while True:
                with self.preview_lock:
                    side = next((s for s in ("start", "end") if self.preview_wanted[s]), None)
                    if side is None:
                        break
                    job = self.preview_wanted[side]
                    self.preview_wanted[side] = None
                key, path, t, origin = job
                try:
                    data = engine.preview(path, t, origin, PREVIEW_W, PREVIEW_H)
                except Exception:
                    data = None
                self.events.put(("preview", side, key, data))

    def _preview_ready(self, side: str, key, data: bytes | None):
        self.preview_cache[key] = data
        while len(self.preview_cache) > 60:
            self.preview_cache.popitem(last=False)
        part = self._current()
        if not part or not self.info or str(self.info.path) != key[0]:
            return
        if round(getattr(part, side), 4) == key[1]:
            self._set_preview(side, data, "")

    def _set_preview(self, side: str, data: bytes | None, text: str):
        label = self.preview_labels[side]
        image = None
        if data:
            try:
                image = tk.PhotoImage(data=data, format="ppm")
            except tk.TclError:
                image = None
        if image is None and not text and self.info and not self.info.has_video:
            text = "No picture"
        self.previews[side] = image
        label.configure(image=image or "", text="" if image else text)

    # --- The timeline ---

    def _draw_timeline(self):
        c = self.timeline
        c.delete("all")
        info = self.info
        width, height = c.winfo_width(), c.winfo_height()
        if not info or width < 10 or info.duration <= 0:
            return
        scale = (width - 2) / info.duration
        selected = {p.iid for p in self._selected_parts()}
        for i, part in enumerate(self.parts):
            x0, x1 = 1 + part.start * scale, 1 + part.end * scale
            color = SEGMENTS[i % 2] if part.use else OFF_SEGMENT
            if part.iid in selected:
                color = ACCENT
            c.create_rectangle(x0, 4, max(x1, x0 + 1), height - 4, fill=color, width=0)
            c.create_line(x0, 2, x0, height - 2, fill=FG)
        for ch in info.chapters:
            x = 1 + ch.start * scale
            c.create_line(x, 0, x, 4, fill=WARN)

    def _timeline_click(self, event):
        info = self.info
        if not info or not self.parts:
            return
        t = (event.x - 1) / max(self.timeline.winfo_width() - 2, 1) * info.duration
        hits = [p for p in self.parts if p.start - EPS <= t < p.end + EPS]
        if hits:
            self._select(hits[-1])
            self._draw_timeline()

    # --- Where the parts go ---

    def _browse_output(self):
        folder = filedialog.askdirectory(parent=self, title="Save the parts in",
                                         initialdir=self.output_var.get() or None,
                                         mustexist=False)
        if folder:
            self.output_var.set(str(Path(folder)))
            self.output_auto = False

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

    def _stop(self):
        if self.busy:
            self.cancel.set()
            self.status.configure(text="Stopping…")

    def _spliced(self, written: list[Path], failed: list, stopped: bool):
        self.busy = ""
        self.progress.configure(value=0 if stopped else 1.0)
        parts = f"{len(written)} part{'s' * (len(written) != 1)}"
        if stopped:
            text = f"Stopped. {parts} written; the one being written was removed."
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
        state(self.splice_btn, idle and loaded and any(p.use for p in self.parts))
        state(self.stop_btn, bool(self.busy))
        state(self.open_btn, idle and self.last_output is not None
              and self.last_output.is_dir())

    def _pump(self):
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "reading" and event[1] == self.load_id:
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
                    self._refresh_all()
                    messagebox.showerror("Splicer", event[2], parent=self)
                elif kind == "preview":
                    self._preview_ready(event[1], event[2], event[3])
                elif kind == "part":
                    self.status.configure(text=f"Splicing {event[1]} of {event[2]}: {event[3]}")
                elif kind == "progress":
                    self.progress.configure(value=event[1])
                elif kind == "spliced":
                    self._spliced(event[1], event[2], event[3])
        except queue.Empty:
            pass
        self.after(50, self._pump)

    # --- Closing ---

    def _on_close(self):
        if self.busy == "splicing":
            if not messagebox.askyesno("Splicer", "Parts are still being written. Stop and "
                                       "close? The one being written is removed; the "
                                       "finished ones stay.", parent=self):
                return
        self.cancel.set()
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
