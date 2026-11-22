"""Splicer's built-in player: plays the video inside the window, with sound and
subtitles.

Decoding is PyAV (FFmpeg's libraries, already inside Splicer). The picture is
drawn straight onto a window with Windows' own GDI, and the sound goes out
through Windows' waveOut, so there's nothing else to install.

Three threads share the work, so a heavy picture can never starve the sound:
- the sound thread reads its own copy of the file and keeps the sound card
  most of a second ahead; its clock is the clock everything follows
- the picture thread decodes frames a few ahead
- the player thread shows each frame when its time comes, skipping any that
  are already late (without spending time converting them), and handles the
  window's commands

The window talks to it through the methods at the top of Player, which only
queue a command; the player reports back through the notify function it was
given: ("pos", seconds, moves done), ("state", playing), ("loaded", has_picture),
("saved", path), ("error", message).
"""

from __future__ import annotations

import collections
import ctypes
import queue
import threading
import time
from ctypes import wintypes
from pathlib import Path

import av

from splicer.engine import seek_before
from splicer.subtitles import Cue, active, longest

MOVES = ("seek", "stop", "step")  # commands that move the picture, numbered

RATE = 48_000                  # what all sound is turned into: 48 kHz, 16-bit stereo
BYTES_PER_SAMPLE = 4
EPS = 0.0005
SOUND_AHEAD = 0.8              # seconds of sound kept queued
SOUND_CHUNK = 2400             # samples per buffer handed over (50 ms)
FRAMES_AHEAD = 8               # decoded frames kept ready


# --- Sound out: Windows waveOut ----------------------------------------------------

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class MMTIME(ctypes.Structure):
    _fields_ = [("wType", wintypes.UINT), ("u", wintypes.DWORD), ("pad", wintypes.DWORD)]


WHDR_DONE = 0x1
TIME_SAMPLES, TIME_BYTES = 0x2, 0x4
WAVE_MAPPER = 0xFFFFFFFF


class WaveOut:
    """A 48 kHz stereo sound output. Raises OSError if there's no sound device."""

    def __init__(self):
        self.winmm = winmm = ctypes.windll.winmm
        winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT,
                                      ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t,
                                      ctypes.c_size_t, wintypes.DWORD]
        for name in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            getattr(winmm, name).argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR),
                                             wintypes.UINT]
        winmm.waveOutGetPosition.argtypes = [ctypes.c_void_p, ctypes.POINTER(MMTIME),
                                             wintypes.UINT]
        for name in ("waveOutReset", "waveOutPause", "waveOutRestart", "waveOutClose"):
            getattr(winmm, name).argtypes = [ctypes.c_void_p]
        winmm.waveOutSetVolume.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        fmt = WAVEFORMATEX(1, 2, RATE, RATE * BYTES_PER_SAMPLE, BYTES_PER_SAMPLE, 16, 0)
        self.handle = ctypes.c_void_p()
        if winmm.waveOutOpen(ctypes.byref(self.handle), WAVE_MAPPER, ctypes.byref(fmt),
                             0, 0, 0) != 0:
            raise OSError("no sound device")
        self.buffers: collections.deque = collections.deque()
        self.written = 0           # samples handed over since the last reset

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._reap()
        buffer = ctypes.create_string_buffer(data, len(data))
        header = WAVEHDR(ctypes.cast(buffer, ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
        size = ctypes.sizeof(WAVEHDR)
        self.winmm.waveOutPrepareHeader(self.handle, ctypes.byref(header), size)
        self.winmm.waveOutWrite(self.handle, ctypes.byref(header), size)
        self.buffers.append((header, buffer))
        self.written += len(data) // BYTES_PER_SAMPLE

    def played(self) -> int:
        """Samples played since the last reset."""
        t = MMTIME(TIME_SAMPLES, 0, 0)
        self.winmm.waveOutGetPosition(self.handle, ctypes.byref(t), ctypes.sizeof(MMTIME))
        if t.wType == TIME_BYTES:
            return t.u // BYTES_PER_SAMPLE
        return t.u

    def _reap(self):
        size = ctypes.sizeof(WAVEHDR)
        while self.buffers and self.buffers[0][0].dwFlags & WHDR_DONE:
            header, _ = self.buffers.popleft()
            self.winmm.waveOutUnprepareHeader(self.handle, ctypes.byref(header), size)

    def reset(self):
        """Stop and throw away everything queued; the count starts again at 0."""
        self.winmm.waveOutReset(self.handle)
        self._reap()
        self.written = 0

    def pause(self):
        self.winmm.waveOutPause(self.handle)

    def resume(self):
        self.winmm.waveOutRestart(self.handle)

    def set_volume(self, volume: float):
        level = max(0, min(int(volume * 0xFFFF), 0xFFFF))
        self.winmm.waveOutSetVolume(self.handle, level | (level << 16))

    def close(self):
        self.reset()
        self.winmm.waveOutClose(self.handle)


class Sound:
    """One sound track, played from its own copy of the file on its own thread."""

    def __init__(self):
        try:
            self.wave = WaveOut()
        except (OSError, AttributeError):
            self.wave = None
        self.lock = threading.RLock()
        self.container = None
        self.stream = None
        self.frames = None
        self.origin = 0.0
        self.running = self.paused = False
        self.eof = True
        self.t0 = 0.0
        self.next_t = 0.0
        self.volume = 1.0
        self.pending = bytearray()
        self.wake = threading.Event()
        self.quit = False
        threading.Thread(target=self._run, daemon=True, name="sound").start()

    def open(self, path: Path | None, index: int | None, origin: float):
        with self.lock:
            self.stop()
            if self.container:
                self.container.close()
            self.container = self.stream = None
            self.origin = origin
            if path is None or index is None or self.wave is None:
                return
            container = av.open(str(path))
            for s in container.streams.audio:
                if s.index == index:
                    self.container, self.stream = container, s
                    return
            container.close()

    def start(self, t: float) -> bool:
        """Play from t. False if there's no sound to play."""
        with self.lock:
            if not self.container:
                return False
            self.wave.reset()
            self.wave.pause()
            self.wave.set_volume(self.volume)
            tb = self.stream.time_base
            self.container.seek(int(max(t + self.origin, 0) / tb), stream=self.stream,
                                backward=True)
            self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
            self.frames = self._frames()
            self.t0 = self.next_t = t
            self.pending = bytearray()
            self.eof = False
            while not self.eof and self.wave.written < RATE * 0.25:
                self._decode_some()
            self._flush()
            self.wave.resume()
            self.running, self.paused = True, False
        self.wake.set()
        return True

    def pause(self):
        with self.lock:
            if self.running and not self.paused:
                self.wave.pause()
                self.paused = True

    def resume(self):
        with self.lock:
            if self.running and self.paused:
                self.wave.resume()
                self.paused = False
        self.wake.set()

    def stop(self):
        with self.lock:
            self.running = False
            if self.wave:
                self.wave.reset()

    def set_volume(self, volume: float):
        self.volume = volume
        if self.wave:
            self.wave.set_volume(volume)

    def clock(self) -> tuple[float, bool]:
        """(the time being heard, whether the sound has run dry)."""
        played = self.wave.played()
        return self.t0 + played / RATE, played >= self.wave.written - 1

    def close(self):
        self.quit = True
        self.wake.set()
        with self.lock:
            self.stop()
            if self.container:
                self.container.close()
                self.container = None
            if self.wave:
                self.wave.close()
                self.wave = None

    def _frames(self):
        for packet in self.container.demux(self.stream):
            try:
                frames = packet.decode()
            except av.FFmpegError:
                continue
            for frame in frames:
                if frame.pts is not None:
                    yield float(frame.pts * frame.time_base) - self.origin, frame

    def _decode_some(self):
        """Decode one packet's worth, handing it over in chunks."""
        try:
            t, frame = next(self.frames)
        except (StopIteration, av.FFmpegError, ValueError):
            self.eof = True
            self._flush()
            return
        for out in self.resampler.resample(frame):
            self._add(t, bytes(out.planes[0])[:out.samples * BYTES_PER_SAMPLE])
            t += out.samples / RATE
        if len(self.pending) >= SOUND_CHUNK * BYTES_PER_SAMPLE:
            self._flush()

    def _add(self, start: float, data: bytes):
        """Line sound up with the clock: a gap is filled with silence, and anything
        from before the start is cut off."""
        gap = start - self.next_t
        if gap > 0.002:
            data = b"\0" * (int(round(min(gap, 1.0) * RATE)) * BYTES_PER_SAMPLE) + data
        elif gap < -0.002:
            cut = int(round(-gap * RATE)) * BYTES_PER_SAMPLE
            if cut >= len(data):
                return
            data = data[cut:]
        self.pending += data
        self.next_t += len(data) / BYTES_PER_SAMPLE / RATE

    def _flush(self):
        if self.pending:
            self.wave.write(bytes(self.pending))
            self.pending = bytearray()

    def _run(self):
        while not self.quit:
            busy = False
            with self.lock:
                if self.running and not self.paused and not self.eof and self.wave:
                    ahead = (self.wave.written - self.wave.played()) / RATE
                    if ahead < SOUND_AHEAD:
                        self._decode_some()
                        busy = True
            if not busy:
                self.wake.wait(0.02)
                self.wake.clear()


# --- Picture out: GDI --------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


DT_CENTER, DT_WORDBREAK, DT_NOPREFIX, DT_CALCRECT = 0x1, 0x10, 0x800, 0x400


class Screen:
    """Draws frames, letterboxed, onto a window, with subtitles over them."""

    def __init__(self):
        self.user32, self.gdi32 = u, g = ctypes.windll.user32, ctypes.windll.gdi32
        u.GetDC.restype = ctypes.c_void_p
        u.GetDC.argtypes = [ctypes.c_void_p]
        u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        u.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p]
        u.DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                                ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        g.GetStockObject.restype = ctypes.c_void_p
        g.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.SetBrushOrgEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p]
        g.StretchDIBits.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 8 + [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD]
        g.CreateFontW.restype = ctypes.c_void_p
        g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [ctypes.c_wchar_p]
        g.SelectObject.restype = ctypes.c_void_p
        g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        g.SetBkMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        g.SetTextColor.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        g.DeleteObject.argtypes = [ctypes.c_void_p]
        self.black = g.GetStockObject(4)  # BLACK_BRUSH
        self.hwnd = None
        self.last_rect = None
        self.fonts: dict[int, int] = {}

    def size(self) -> tuple[int, int]:
        if not self.hwnd:
            return 0, 0
        r = wintypes.RECT()
        self.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return r.right - r.left, r.bottom - r.top

    def clear(self):
        if not self.hwnd:
            return
        r = wintypes.RECT()
        self.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        dc = self.user32.GetDC(self.hwnd)
        if dc:
            self.user32.FillRect(dc, ctypes.byref(r), self.black)
            self.user32.ReleaseDC(self.hwnd, dc)
        self.last_rect = None

    def draw(self, image, aspect: float, text: str = "", force_clear: bool = False):
        """Draw a top-to-bottom BGR image (w, h, data), fitted to the window."""
        if not self.hwnd or not image:
            return
        w, h, data = image
        cw, ch = self.size()
        if cw < 2 or ch < 2:
            return
        scale = min(cw / (h * aspect), ch / h)
        dw, dh = max(int(h * aspect * scale), 1), max(int(h * scale), 1)
        dx, dy = (cw - dw) // 2, (ch - dh) // 2
        rect = (dx, dy, dw, dh)
        dc = self.user32.GetDC(self.hwnd)
        if not dc:
            return
        try:
            if force_clear or rect != self.last_rect:
                full = wintypes.RECT(0, 0, cw, ch)
                self.user32.FillRect(dc, ctypes.byref(full), self.black)
                self.last_rect = rect
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 24, 0,
                                      0, 0, 0, 0, 0)
            # Same size: a straight copy. Otherwise HALFTONE, smooth.
            self.gdi32.SetStretchBltMode(dc, 3 if abs(w - dw) <= 4 and h == dh else 4)
            self.gdi32.SetBrushOrgEx(dc, 0, 0, None)
            self.gdi32.StretchDIBits(dc, dx, dy, dw, dh, 0, 0, w, h, data,
                                     ctypes.byref(header), 0, 0x00CC0020)  # SRCCOPY
            if text:
                self._subtitle(dc, dx, dy, dw, dh, text)
        finally:
            self.user32.ReleaseDC(self.hwnd, dc)

    def _font(self, height: int):
        if height not in self.fonts:
            if len(self.fonts) > 8:
                for old in self.fonts.values():
                    self.gdi32.DeleteObject(old)
                self.fonts.clear()
            # height, width, escapement, orientation, weight, italic, underline, strikeout,
            # charset, precision, clipping, quality (ANTIALIASED), pitch, face
            self.fonts[height] = self.gdi32.CreateFontW(-height, 0, 0, 0, 600, 0, 0, 0, 1, 0,
                                                        0, 4, 0, "Segoe UI")
        return self.fonts[height]

    def _subtitle(self, dc, dx, dy, dw, dh, text):
        height = max(14, dh // 17)
        old = self.gdi32.SelectObject(dc, self._font(height))
        self.gdi32.SetBkMode(dc, 1)  # TRANSPARENT
        margin = dw // 16
        flags = DT_CENTER | DT_WORDBREAK | DT_NOPREFIX
        calc = wintypes.RECT(dx + margin, 0, dx + dw - margin, 0)
        self.user32.DrawTextW(dc, text, -1, ctypes.byref(calc), flags | DT_CALCRECT)
        bottom = dy + dh - dh // 16
        top = bottom - (calc.bottom - calc.top)
        edge = max(2, height // 12)
        self.gdi32.SetTextColor(dc, 0)
        for ox, oy in ((-edge, 0), (edge, 0), (0, -edge), (0, edge),
                       (-edge, -edge), (edge, edge), (-edge, edge), (edge, -edge)):
            r = wintypes.RECT(dx + margin + ox, top + oy, dx + dw - margin + ox, bottom + oy)
            self.user32.DrawTextW(dc, text, -1, ctypes.byref(r), flags)
        self.gdi32.SetTextColor(dc, 0xFFFFFF)
        r = wintypes.RECT(dx + margin, top, dx + dw - margin, bottom)
        self.user32.DrawTextW(dc, text, -1, ctypes.byref(r), flags)
        self.gdi32.SelectObject(dc, old)


# --- The picture thread ------------------------------------------------------------

class Feeder(threading.Thread):
    """Decodes frames ahead while playing, into the player's queue."""

    def __init__(self, player: "Player"):
        super().__init__(daemon=True, name="picture")
        self.player = player
        self.stopping = False

    def run(self):
        p = self.player
        while not self.stopping:
            with p.ready:
                while len(p.frames_ready) >= FRAMES_AHEAD and not self.stopping:
                    p.ready.wait(0.05)
            if self.stopping:
                return
            item = p._next_frame()
            if item is None:
                return
            with p.ready:
                p.frames_ready.append(item)
                p.ready.notify_all()

    def finish(self):
        self.stopping = True
        with self.player.ready:
            self.player.ready.notify_all()
        self.join()


# --- The player --------------------------------------------------------------------

class Player:
    def __init__(self, notify):
        self.notify = notify
        self.commands: queue.Queue = queue.Queue()
        # Read by the window; written only by the player thread
        self.frame_duration = 1 / 30
        self.has_picture = False
        self.position = 0.0
        self.playing = False
        self.moves_sent = 0   # numbered moves asked for
        self.moves_done = 0   # and the last one done (or skipped for a newer one)
        threading.Thread(target=self._run, daemon=True, name="player").start()

    # --- What the window calls (any thread) ---

    def load(self, path: Path, origin: float, duration: float, audio: int | None):
        self.commands.put(("load", Path(path), origin, duration, audio))

    def unload(self):
        self.commands.put(("unload",))

    def play(self):
        self.commands.put(("play",))

    def pause(self):
        self.commands.put(("pause",))

    def toggle(self):
        self.commands.put(("toggle",))

    def stop(self):
        """Pause, back at the start of the part (or the video)."""
        self.commands.put(("stop", self._move()))

    def seek(self, t: float, exact: bool = True, pause: bool = False):
        """Show the frame at t. Exact: that very frame; otherwise the keyframe
        before it, which is much quicker (for dragging along the bar)."""
        self.commands.put(("seek", t, exact, pause, self._move()))

    def step(self, frames: int):
        """One frame forward (1) or back (-1), paused."""
        self.commands.put(("step", frames, self._move()))

    def _move(self) -> int:
        """Number a command that moves the picture. Positions are reported with the
        number of the last one done, so the window can tell an old one from the
        newest (called from the window's thread only)."""
        self.moves_sent += 1
        return self.moves_sent

    def set_range(self, start: float | None, end: float | None, removed=()):
        """The selected part: where it starts and ends, and the stretches left out
        of it, which playing jumps over."""
        self.commands.put(("range", start, end, tuple(removed)))

    def set_end_mode(self, mode: str):
        """What happens at the end of the part: "stop", "loop" or "continue"."""
        self.commands.put(("mode", mode))

    def set_target(self, hwnd: int | None):
        self.commands.put(("target", hwnd))

    def redraw(self):
        self.commands.put(("redraw",))

    def set_volume(self, volume: float):
        self.commands.put(("volume", volume))

    def set_audio(self, index: int | None):
        self.commands.put(("audio", index))

    def set_subtitles(self, cues: list[Cue] | None):
        self.commands.put(("subtitles", cues or []))

    def save_frame(self, path: Path):
        """Write the frame on screen, at full size, as a PNG."""
        self.commands.put(("save", Path(path)))

    def close(self):
        self.commands.put(("quit",))

    # --- The player thread ---

    def _run(self):
        self.screen = Screen()
        self.sound = Sound()
        self.container = None
        self.video = None
        self.path = None
        self.origin = self.duration = 0.0
        self.audio_index = None
        self.range = (None, None)
        self.removed: tuple = ()
        self.mode = "stop"
        self.cues: list[Cue] = []
        self.cue_starts: list[float] = []
        self.ready = threading.Condition()
        self.frames_ready: collections.deque = collections.deque()
        self.feeder: Feeder | None = None
        self.aspect = 16 / 9
        self._reset_state()
        while True:
            try:  # idle: wait for something to do; playing: just look
                batch = [self.commands.get_nowait() if self.playing else self.commands.get()]
            except queue.Empty:
                batch = []
            while True:
                try:
                    batch.append(self.commands.get_nowait())
                except queue.Empty:
                    break
            for i, command in enumerate(batch):
                # A newer move overrides an older one still waiting: dragging along
                # the bar, or clicking faster than frames can be found
                if command[0] in MOVES and any(c[0] in ("seek", "stop") for c in batch[i + 1:]):
                    self.moves_done = command[-1]
                    continue
                try:
                    if command[0] in MOVES:
                        self.moves_done = command[-1]
                    if self._command(command) is False:
                        self._close_file()
                        self.sound.close()
                        return
                except Exception as e:  # an odd file shouldn't take the player down
                    self._halt()
                    self.notify(("error", str(e) or repr(e)))
            if self.playing:
                try:
                    self._tick()
                except Exception as e:
                    self._halt()
                    self.notify(("error", str(e) or repr(e)))

    def _reset_state(self):
        self.frames_iter = None
        self.eof = False
        self.session = False       # sound and picture set going from position (maybe paused)
        self.sound_clock = False   # the clock is the sound's
        self.t0 = self.wall0 = 0.0
        self.dry_since = None
        self.last_frame = None     # the decoded frame on screen
        self.last_report = 0.0
        with self.ready:
            self.frames_ready.clear()

    def _command(self, command):
        kind = command[0]
        if kind == "quit":
            return False
        if kind == "load":
            _, path, origin, duration, audio = command
            self._open(path, origin, duration, audio)
            self._seek(0.0, exact=True)
            self.notify(("loaded", self.has_picture))
        elif kind == "unload":
            self._close_file()
            self.screen.clear()
        elif kind == "target":
            self.screen.hwnd = command[1]
            self.screen.last_rect = None
            self._redraw()
        elif kind == "redraw":
            self._redraw()
        elif kind == "volume":
            self.sound.set_volume(command[1])
        elif kind == "mode":
            self.mode = command[1]
        elif kind == "range":
            self.range = (command[1], command[2])
            self.removed = command[3]
        elif kind == "subtitles":
            self.cues = command[1]
            self.cue_starts = [c.start for c in self.cues]
            self.cue_longest = longest(self.cues)
            if not self.playing:
                self._redraw()
        elif self.path is None:
            return None
        elif kind == "audio":
            if command[1] != self.audio_index:
                was = self.playing
                self._halt()
                self.audio_index = command[1]
                self.sound.open(self.path, command[1], self.origin)
                if was:
                    self._play()
        elif kind == "play":
            self._play()
        elif kind == "pause":
            self._pause()
        elif kind == "toggle":
            if self.playing:
                self._pause()
            else:
                self._play()
        elif kind == "stop":
            self._halt()
            start = self.range[0] if self.range[0] is not None else 0.0
            self._seek(start, exact=True)
        elif kind == "seek":
            _, t, exact, pause, _ = command
            was = self.playing and not pause
            self._halt()
            if exact and self.video and self.frames_iter is not None and not self.eof and \
                    EPS < t - self.position <= self.frame_duration * 1.5:
                self._step_forward()  # the next frame: just decode on, no need to seek
            else:
                self._seek(t, exact)
            if was:
                self._play()
        elif kind == "step":
            self._halt()
            if command[1] < 0:
                self._seek(self.position - self.frame_duration, exact=True)
            else:
                self._step_forward()
        elif kind == "save":
            self._save_frame(command[1])
        return None

    # --- Opening ---

    def _open(self, path: Path, origin: float, duration: float, audio: int | None):
        self._close_file()
        self.path, self.origin, self.duration = path, origin, duration
        container = av.open(str(path))
        videos = [s for s in container.streams.video
                  if not s.disposition & av.stream.Disposition.attached_pic]
        self.video = videos[0] if videos else None
        self.has_picture = self.video is not None
        if self.video is not None:
            self.container = container
            self.video.thread_type = "AUTO"
            rate = self.video.average_rate or self.video.guessed_rate
            self.frame_duration = float(1 / rate) if rate else 1 / 30
            ctx = self.video.codec_context
            sar = self.video.sample_aspect_ratio
            self.aspect = float(sar) if sar and sar.numerator else 1.0
            self.aspect *= ctx.width / ctx.height if ctx.height else 16 / 9
        else:
            container.close()
            self.frame_duration = 0.02
        self.audio_index = audio
        self.sound.open(path, audio, origin)

    def _close_file(self):
        self._halt()
        self.sound.open(None, None, 0.0)
        if self.container:
            try:
                self.container.close()
            except Exception:
                pass
        self.container = None
        self.video = None
        self.path = None
        self._reset_state()

    def _frames(self):
        """(seconds, frame) for each picture, from where the file was last sought."""
        for packet in self.container.demux(self.video):
            try:
                frames = packet.decode()
            except av.FFmpegError:
                continue
            for frame in frames:
                if frame.pts is not None:
                    yield float(frame.pts * frame.time_base) - self.origin, frame

    def _next_frame(self):
        if self.frames_iter is None:
            return None
        try:
            return next(self.frames_iter)
        except (StopIteration, av.FFmpegError, ValueError):
            self.eof = True
            return None

    # --- Moving around ---

    def _seek(self, t: float, exact: bool):
        t = max(0.0, min(t, self.duration))
        self._end_session()
        self.eof = False
        with self.ready:
            self.frames_ready.clear()
        if not self.video:
            self.position = t
            self._report(force=True)
            return
        seek_before(self.container, self.video, t + self.origin)
        self.frames_iter = self._frames()
        target, last = None, None
        while True:
            item = self._next_frame()
            if item is None:
                break
            last = item
            if not exact or item[0] >= t - self.frame_duration / 2:
                target = item
                break
        target = target or last
        if target:
            self._show(*target)
        else:
            self.position = t
        self._report(force=True)

    def _step_forward(self):
        if not self.video:
            return
        with self.ready:
            item = self.frames_ready.popleft() if self.frames_ready else None
        while item is None or item[0] <= self.position + EPS:
            item = self._next_frame()
            if item is None:
                return
        self._show(*item)
        self._report(force=True)

    def _show(self, t: float, frame):
        self.last_frame = frame
        self.screen.draw(self._image(frame), self.aspect, self._text(t))
        self.position = t

    def _text(self, t: float) -> str:
        return active(self.cues, self.cue_starts, t, self.cue_longest) if self.cues else ""

    def _redraw(self):
        if self.last_frame is not None:
            self.screen.draw(self._image(self.last_frame), self.aspect,
                             self._text(self.position), force_clear=True)
        else:
            self.screen.clear()

    def _image(self, frame):
        """The frame shrunk (or grown) to fit the window, as GDI wants it."""
        cw, ch = self.screen.size()
        if cw < 4 or ch < 4:
            return None
        scale = min(cw / (frame.height * self.aspect), ch / frame.height)
        w = max(int(frame.height * self.aspect * scale) // 4 * 4, 4)  # rows of whole words
        h = max(int(frame.height * scale), 2)
        rgb = frame.reformat(width=w, height=h, format="bgr24", interpolation="BILINEAR")
        plane = rgb.planes[0]
        data = bytes(plane)
        row = w * 3
        if plane.line_size != row:
            data = b"".join(data[y * plane.line_size:y * plane.line_size + row]
                            for y in range(h))
        return w, h, data

    def _save_frame(self, path: Path):
        if self.last_frame is None:
            self.notify(("error", "There's no picture to save."))
            return
        rgb = self.last_frame.reformat(format="rgb24")
        codec = av.CodecContext.create("png", "w")
        codec.width, codec.height, codec.pix_fmt = rgb.width, rgb.height, "rgb24"
        data = b"".join(bytes(p) for p in codec.encode(rgb) + codec.encode(None))
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        self.notify(("saved", path))

    # --- Playing ---

    def _set_playing(self, on: bool):
        if on != self.playing:
            self.playing = on
            self.notify(("state", on))

    def _clock(self) -> float:
        if not self.playing:
            return self.position
        now = time.perf_counter()
        if self.sound_clock:
            t, dry = self.sound.clock()
            if dry:
                # The sound has run out (its track ended, or it fell behind): carry on
                # by the wall clock rather than freeze
                if self.dry_since is None:
                    self.dry_since = now
                elif now - self.dry_since > 0.3 or self.sound.eof:
                    self.sound_clock = False
                    self.t0, self.wall0 = t, now
            else:
                self.dry_since = None
            return t
        return self.t0 + (now - self.wall0)

    def _play(self):
        if self.path is None or (not self.video and not self.sound.container):
            return
        start, end = self.range
        if self.mode != "continue" and start is not None and end is not None and \
                not (start - EPS <= self.position < end - self.frame_duration * 1.5):
            self._seek(start, exact=True)
        elif self.position >= self.duration - self.frame_duration or                 (self.video and self.eof and not self.frames_ready):
            # At the end of the video (its last frame can come before the sound ends)
            self._seek(start or 0.0, exact=True)
        if self.session:
            # Carry on from a pause: everything is still where it was
            if self.sound_clock:
                self.sound.resume()
            else:
                self.t0, self.wall0 = self.position, time.perf_counter()
        else:
            self.dry_since = None
            self.sound_clock = self.sound.start(self.position)
            self.t0, self.wall0 = self.position, time.perf_counter()
            self.session = True
        if self.video and not self.eof:
            self.feeder = Feeder(self)
            self.feeder.start()
        self._set_playing(True)

    def _stop_feeder(self):
        if self.feeder:
            self.feeder.finish()
            self.feeder = None

    def _pause(self):
        if not self.playing:
            return
        if not self.video:
            self.position = min(self._clock(), self.duration)
        if self.sound_clock:
            self.sound.pause()
        self._stop_feeder()
        self._set_playing(False)
        self._report(force=True)

    def _end_session(self):
        """Sound and picture no longer line up with a pause: start afresh next time."""
        self._stop_feeder()
        self.sound.stop()
        self.session = False

    def _halt(self):
        self._pause()
        self._end_session()

    def _tick(self):
        now = self._clock()
        start, end = self.range
        if self.mode != "continue" and end is not None and start is not None and \
                now >= end - EPS:
            if self.mode == "loop":  # straight back to the start, still playing
                self._end_session()
                self._seek(start, exact=True)
                self._play()
            else:  # stop on the part's last frame
                self._halt()
                self._seek(end - self.frame_duration, exact=True)
            return
        if start is not None:
            for r0, r1 in self.removed:  # jump over a stretch left out of the part
                if r0 - EPS <= now < r1 - EPS:
                    self._end_session()
                    self._seek(r1, exact=True)
                    self._play()
                    return
        due = None
        with self.ready:
            while self.frames_ready and self.frames_ready[0][0] <= now:
                due = self.frames_ready.popleft()  # anything overtaken is skipped
            if due:
                self.ready.notify_all()
            upcoming = self.frames_ready[0][0] if self.frames_ready else None
        if due:
            self._show(*due)
            self._report()
        elif not self.video:
            self.position = min(now, self.duration)
            self._report()
        if self.video:
            finished = self.eof and upcoming is None and \
                (self.feeder is None or not self.feeder.is_alive())
        else:
            finished = now >= self.duration - EPS or \
                (self.sound_clock and self.sound.eof and self.sound.clock()[1])
        if finished:
            if self.mode == "loop" and start is not None and end is not None:
                # A part that runs to the end of the video loops too
                self._end_session()
                self._seek(start, exact=True)
                self._play()
                return
            self._halt()
            self._report(force=True)
            return
        wait = (upcoming - now) if upcoming is not None else 0.004
        time.sleep(max(0.001, min(wait, 0.004)))

    def _report(self, force: bool = False):
        now = time.perf_counter()
        if force or now - self.last_report > 0.04:
            self.last_report = now
            self.notify(("pos", self.position, self.moves_done))
