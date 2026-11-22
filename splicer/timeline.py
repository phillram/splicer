"""The two bars that show the video along its length.

Overview: the whole video in one thin bar, under the picture. Click or drag to
move the playhead.

Timeline: the editing bar along the bottom of the window. It zooms (Ctrl+wheel,
or the buttons) from the whole video down to a few seconds, scrolls (wheel),
shows a time ruler, every part with its name, the chapters and, close up,
every keyframe. Drag a part's edge to move that cut; click or drag anywhere
else to move the playhead; double-click a part to select it.
"""

from __future__ import annotations

import bisect
import tkinter as tk
from tkinter import font
from typing import TYPE_CHECKING

from splicer import engine
from splicer.engine import EPS

if TYPE_CHECKING:
    from splicer.app import App

BG, PANEL, FIELD, HOVER = "#1e1f22", "#2b2d31", "#383a40", "#4e5058"
FG, MUTED, ACCENT, WARN = "#e6e6e6", "#9a9ca3", "#5865f2", "#faa61a"
SEGMENTS = ("#3c4270", "#4a5190")
OFF_SEGMENT = "#34363c"
EDGE_GRAB = 6  # pixels either side of a cut that pick it up

STEPS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)


def clock(t: float, step: float) -> str:
    """A ruler label: 4:38, 1:02:03, or 4:38.5 when the ticks are under a second."""
    whole = int(t + 1e-6)
    h, rest = divmod(whole, 3600)
    m, s = divmod(rest, 60)
    text = f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"
    if step < 1:
        text += f".{int(round((t - whole) * 10)) % 10}"
    return text


class Overview(tk.Canvas):
    """The whole video in a bar, with the part of it the timeline shows outlined."""

    def __init__(self, parent, app: "App", height: int = 22, bg: str = PANEL):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, cursor="hand2")
        self.app = app
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<ButtonPress-1>", lambda e: self._seek(e, False))
        self.bind("<B1-Motion>", lambda e: self._seek(e, False))
        self.bind("<ButtonRelease-1>", lambda e: self._seek(e, True))

    def _scale(self) -> float:
        info = self.app.info
        return (self.winfo_width() - 2) / info.duration if info and info.duration else 0

    def _seek(self, event, exact: bool):
        scale = self._scale()
        if scale:
            self.app.seek(min(max((event.x - 1) / scale, 0.0), self.app.info.duration), exact)

    def draw(self):
        self.delete("all")
        scale = self._scale()
        if not scale:
            return
        app, h = self.app, self.winfo_height()
        selected = {p.iid for p in app._selected_parts()}
        for i, part in enumerate(app.parts):
            x0, x1 = 1 + part.start * scale, 1 + part.end * scale
            color = ACCENT if part.iid in selected else SEGMENTS[i % 2] if part.use \
                else OFF_SEGMENT
            self.create_rectangle(x0, 5, max(x1, x0 + 1), h - 3, fill=color, width=0)
        for ch in app.info.chapters:
            x = 1 + ch.start * scale
            self.create_line(x, 0, x, 4, fill=WARN, width=2)
        view = getattr(app, "timeline", None)
        if view is not None and view.span < app.info.duration - EPS:
            x0 = 1 + view.start * scale
            x1 = 1 + (view.start + view.span) * scale
            self.create_rectangle(x0, 1, max(x1, x0 + 3), h - 1, outline=FG, width=1)
        self.draw_playhead()

    def draw_playhead(self):
        self.delete("head")
        scale = self._scale()
        if scale:
            x = 1 + self.app.position * scale
            self.create_line(x, 0, x, self.winfo_height(), fill="white", width=2, tags="head")


class Timeline(tk.Canvas):
    """The zoomable editing bar."""

    RULER, LANE_TOP, LANE_BOTTOM = 20, 26, 70

    def __init__(self, parent, app: "App"):
        super().__init__(parent, height=96, bg=PANEL, highlightthickness=0)
        self.app = app
        self.start = 0.0       # the time at the left edge
        self.span = 1.0        # seconds across the width
        self.fitted = True
        self.drag = None       # ("edge", part, side, original) or ("scrub",)
        self.ghost = None
        self.small = font.Font(family="Segoe UI", size=8)
        self.label = font.Font(family="Segoe UI", size=9)
        self.bind("<Configure>", lambda e: self._resized())
        self.bind("<Motion>", self._hover)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-1>", self._double)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Shift-MouseWheel>", lambda e: self._pan_wheel(e))
        self.bind("<Control-MouseWheel>", lambda e: self._zoom_wheel(e))

    # --- Where things are ---

    @property
    def duration(self) -> float:
        return self.app.info.duration if self.app.info else 0.0

    def x(self, t: float) -> float:
        width = max(self.winfo_width() - 2, 1)
        return 1 + (t - self.start) / self.span * width

    def t(self, x: float) -> float:
        width = max(self.winfo_width() - 2, 1)
        return min(max(self.start + (x - 1) / width * self.span, 0.0), self.duration)

    # --- Zooming and scrolling ---

    def fit(self):
        self.start, self.span, self.fitted = 0.0, max(self.duration, 0.001), True
        self.draw()

    def zoom(self, factor: float, around: float | None = None):
        if not self.duration:
            return
        around = self.app.position if around is None else around
        span = min(max(self.span * factor, 2.0), self.duration)
        share = (around - self.start) / self.span if self.span else 0.5
        self.span = span
        self.fitted = span >= self.duration - EPS
        self._place(around - share * span)

    def _place(self, start: float):
        self.start = min(max(start, 0.0), max(self.duration - self.span, 0.0))
        self.draw()
        self.app.overview_changed()

    def show(self, start: float, end: float):
        """Zoom to show start..end with a little room either side."""
        if not self.duration:
            return
        room = (end - start) * 0.08 + 0.5
        self.span = min(max(end - start + 2 * room, 2.0), self.duration)
        self.fitted = self.span >= self.duration - EPS
        self._place(start - room)

    def follow(self, t: float):
        """Keep the playhead in view while playing."""
        if self.fitted or not self.duration:
            return
        if not self.start <= t <= self.start + self.span * 0.95:
            self._place(t - self.span * 0.1)

    def _resized(self):
        if self.fitted:
            self.span = max(self.duration, 0.001)
        self.draw()

    def _wheel(self, event):
        self._pan_wheel(event)

    def _pan_wheel(self, event):
        if self.duration:
            self._place(self.start - event.delta / 120 * self.span * 0.1)

    def _zoom_wheel(self, event):
        if self.duration:
            self.zoom(0.8 if event.delta > 0 else 1.25, self.t(event.x))

    # --- Drawing ---

    def draw(self):
        self.delete("all")
        app, info = self.app, self.app.info
        width, height = self.winfo_width(), self.winfo_height()
        if not info or not self.duration or width < 10:
            self.create_text(width // 2, height // 2, text="The timeline shows here.",
                             fill=MUTED, font=self.label)
            return
        if self.fitted:
            self.span = self.duration
        self._ruler(width)
        lane_bottom = height - 16
        selected = {p.iid for p in app._selected_parts()}
        for i, part in enumerate(app.parts):
            x0, x1 = self.x(part.start), self.x(part.end)
            if x1 < 0 or x0 > width:
                continue
            on = part.iid in selected
            color = ACCENT if on else SEGMENTS[i % 2] if part.use else OFF_SEGMENT
            self.create_rectangle(x0, self.LANE_TOP, max(x1, x0 + 1), lane_bottom,
                                  fill=color, outline=FG if on else "", width=1)
            self.create_line(x0, self.LANE_TOP - 2, x0, lane_bottom + 2, fill=FG)
            self.create_line(x1, self.LANE_TOP - 2, x1, lane_bottom + 2, fill=FG)
            room = min(x1, width) - max(x0, 0) - 10
            if room > 24:
                text = self._fit_text(part.title, room)
                self.create_text(max(x0, 0) + 5, self.LANE_TOP + 4, text=text, anchor="nw",
                                 fill="white" if part.use else MUTED, font=self.label)
                if room > 90:
                    self.create_text(max(x0, 0) + 5, lane_bottom - 3, anchor="sw",
                                     text=clock(part.length, 1), fill=MUTED, font=self.small)
        for ch in info.chapters:
            x = self.x(ch.start)
            if 0 <= x <= width:
                self.create_line(x, self.RULER - 4, x, self.LANE_TOP, fill=WARN, width=2)
        self._keyframes(width, lane_bottom)
        self.draw_playhead()

    def _ruler(self, width: int):
        self.create_rectangle(0, 0, width, self.RULER, fill=FIELD, width=0)
        per_px = self.span / max(width, 1)
        step = next((s for s in STEPS if s / per_px >= 90), STEPS[-1])
        first = int(self.start / step) * step
        t = first
        while t <= self.start + self.span:
            x = self.x(t)
            if x >= 0:
                self.create_line(x, self.RULER - 6, x, self.RULER, fill=MUTED)
                self.create_text(x + 3, 2, text=clock(t, step), anchor="nw", fill=MUTED,
                                 font=self.small)
            minor = step / 5
            for k in range(1, 5):
                xm = self.x(t + k * minor)
                if 0 <= xm <= width:
                    self.create_line(xm, self.RULER - 3, xm, self.RULER, fill=HOVER)
            t += step

    def _keyframes(self, width: int, bottom: int):
        keys = self.app.info.keyframes
        if not keys:
            return
        lo = bisect.bisect_left(keys, self.start)
        hi = bisect.bisect_right(keys, self.start + self.span)
        if hi - lo > width / 4:  # too close together to be useful
            return
        for k in keys[lo:hi]:
            x = self.x(k)
            self.create_line(x, bottom + 3, x, bottom + 11, fill=MUTED)
        self.create_text(width - 4, bottom + 7, text="keyframes", anchor="e", fill=HOVER,
                         font=self.small)

    def _fit_text(self, text: str, room: float) -> str:
        if self.label.measure(text) <= room:
            return text
        while text and self.label.measure(text + "…") > room:
            text = text[:-1]
        return text + "…" if text else ""

    def draw_playhead(self):
        self.delete("head")
        if not self.duration:
            return
        x = self.x(self.app.position)
        if -2 <= x <= self.winfo_width() + 2:
            self.create_line(x, 0, x, self.winfo_height(), fill="white", width=2, tags="head")
            self.create_polygon(x - 6, 0, x + 6, 0, x, 8, fill="white", tags="head")

    # --- The mouse ---

    def _edge_at(self, x: float, y: float):
        """(part, "start" or "end") whose cut is under the pointer, if any."""
        if not self.LANE_TOP - 4 <= y <= self.winfo_height() - 12 or self.app.busy:
            return None
        best, gap = None, EDGE_GRAB + 1
        selected = {p.iid for p in self.app._selected_parts()}
        for part in self.app.parts:
            for side in ("start", "end"):
                d = abs(self.x(getattr(part, side)) - x)
                # A cut shared by two parts: the selected one's wins
                if d < gap or (d == gap and part.iid in selected):
                    best, gap = (part, side), d
        return best if gap <= EDGE_GRAB else None

    def _hover(self, event):
        if self.drag:
            return
        self.configure(cursor="sb_h_double_arrow" if self._edge_at(event.x, event.y)
                       else "hand2")

    def _press(self, event):
        if not self.app.info:
            return
        edge = self._edge_at(event.x, event.y)
        if edge:
            part, side = edge
            self.drag = ("edge", part, side, getattr(part, side))
            self.app._select(part, seek=False)
        else:
            self.drag = ("scrub",)
            self.app.seek(self.t(event.x), exact=False)

    def _move(self, event):
        if not self.drag:
            return
        if self.drag[0] == "scrub":
            self.app.seek(self.t(event.x), exact=False)
            return
        _, part, side, _ = self.drag
        t = engine.snap(self.app.info, self.t(event.x))
        self.delete("ghost")
        x = self.x(t)
        self.create_line(x, 0, x, self.winfo_height(), fill=WARN, width=2, tags="ghost")
        self.create_text(x + 4, self.RULER + 2, text=clock(t, 0.1), anchor="nw", fill=WARN,
                         font=self.small, tags="ghost")
        self.ghost = t
        self.app.seek(t, exact=False)

    def _release(self, event):
        drag, self.drag = self.drag, None
        self.delete("ghost")
        if not drag:
            return
        if drag[0] == "scrub":
            self.app.seek(self.t(event.x), exact=True)
            return
        _, part, side, original = drag
        if self.ghost is not None and abs(self.ghost - original) > EPS:
            self.app.move_cut(part, side, self.ghost)
        else:
            self.app.go_part_edge(side)
        self.ghost = None

    def _double(self, event):
        if not self.app.info:
            return
        t = self.t(event.x)
        hits = [p for p in self.app.parts if p.start - EPS <= t < p.end + EPS]
        if hits:
            self.app._select(hits[-1])
