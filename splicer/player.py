"""Splicer's built-in player: plays the video inside the window, with sound.

Decoding is PyAV (FFmpeg's libraries, already inside Splicer). The picture is
drawn straight onto a window with Windows' own GDI, and the sound goes out
through Windows' waveOut, so there's nothing else to install. Picture follows
the sound's clock, and frames that come too late are skipped rather than
shown, so the sound never stutters to wait for the picture.

All the work happens on one thread of its own. The window talks to it through
the methods at the top of Player, which only queue a command; the player
reports back through the notify function it was given: ("pos", seconds),
("state", playing), ("loaded", has_picture), ("error", message).
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

RATE = 48_000                  # what all sound is turned into: 48 kHz, 16-bit stereo
BYTES_PER_SAMPLE = 4
EPS = 0.0005


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


# --- Picture out: GDI --------------------------------------------------------------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class Screen:
    """Draws frames, letterboxed, onto a window."""

    def __init__(self):
        self.user32, self.gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        self.user32.GetDC.restype = ctypes.c_void_p
        self.user32.GetDC.argtypes = [ctypes.c_void_p]
        self.user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
        self.user32.FillRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT),
                                         ctypes.c_void_p]
        self.gdi32.GetStockObject.restype = ctypes.c_void_p
        self.gdi32.SetStretchBltMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.gdi32.SetBrushOrgEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p]
        self.gdi32.StretchDIBits.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 8 + [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD]
        self.black = self.gdi32.GetStockObject(4)  # BLACK_BRUSH
        self.hwnd = None
        self.last_rect = None

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

    def draw(self, image: tuple[int, int, bytes], aspect: float, force_clear: bool = False):
        """Draw a bottom-to-top BGR image (w, h, data), fitted to the window."""
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
            self.gdi32.SetStretchBltMode(dc, 4)  # HALFTONE: smooth if it must stretch
            self.gdi32.SetBrushOrgEx(dc, 0, 0, None)
            self.gdi32.StretchDIBits(dc, dx, dy, dw, dh, 0, 0, w, h, data,
                                     ctypes.byref(header), 0, 0x00CC0020)  # SRCCOPY
        finally:
            self.user32.ReleaseDC(self.hwnd, dc)


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
        self.commands.put(("stop",))

    def seek(self, t: float, exact: bool = True, pause: bool = False):
        """Show the frame at t. Exact: that very frame; otherwise the keyframe
        before it, which is much quicker (for dragging along the bar)."""
        self.commands.put(("seek", t, exact, pause))

    def step(self, frames: int):
        """One frame forward (1) or back (-1), paused."""
        self.commands.put(("step", frames))

    def set_range(self, start: float | None, end: float | None):
        self.commands.put(("range", start, end))

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

    def close(self):
        self.commands.put(("quit",))

    # --- The player thread ---

    def _run(self):
        self.screen = Screen()
        try:
            self.wave = WaveOut()
        except (OSError, AttributeError):
            self.wave = None
        self.volume = 1.0
        self.container = None
        self.path = None
        self.origin = self.duration = 0.0
        self.audio_index = None
        self.range = (None, None)
        self.mode = "stop"
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
                # Only the last of a run of seeks matters (dragging along the bar)
                if command[0] == "seek" and any(c[0] == "seek" for c in batch[i + 1:]):
                    continue
                try:
                    if self._command(command) is False:
                        self._close_file()
                        if self.wave:
                            self.wave.close()
                        return
                except Exception as e:  # an odd file shouldn't take the player down
                    self._set_playing(False)
                    self.notify(("error", str(e) or repr(e)))
            if self.playing:
                try:
                    self._tick()
                except Exception as e:
                    self._set_playing(False)
                    self.notify(("error", str(e) or repr(e)))

    def _reset_state(self):
        self.video_queue: collections.deque = collections.deque()  # (t, image)
        self.pending_audio: list[tuple[float, bytes]] = []
        self.frames_iter = None
        self.eof = False
        self.primed = False        # sound queued and the clock running from position
        self.audio_on = False      # the clock comes from the sound device
        self.audio_next = 0.0      # the time of the next sample to hand over
        self.t0 = 0.0
        self.wall0 = 0.0
        self.paused_at = 0.0
        self.underrun_since = None
        self.last_frame = None     # the decoded frame on screen
        self.last_image = None
        self.last_report = 0.0

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
            self.volume = command[1]
            if self.wave:
                self.wave.set_volume(self.volume)
        elif kind == "mode":
            self.mode = command[1]
        elif kind == "range":
            self.range = (command[1], command[2])
        elif not self.container:
            return None
        elif kind == "audio":
            if command[1] != self.audio_index:
                was = self.playing
                self._open(self.path, self.origin, self.duration, command[1])
                self._seek(self.position, exact=True)
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
            self._pause()
            start = self.range[0] if self.range[0] is not None else 0.0
            self._seek(start, exact=True)
        elif kind == "seek":
            _, t, exact, pause = command
            was = self.playing and not pause
            if self.playing:
                self._pause()
            self._seek(t, exact)
            if was:
                self._play()
        elif kind == "step":
            if self.playing:
                self._pause()
            if command[1] < 0:
                self._seek(self.position - self.frame_duration, exact=True)
            else:
                self._step_forward()
        return None

    # --- Opening ---

    def _open(self, path: Path, origin: float, duration: float, audio: int | None):
        self._close_file()
        self.path, self.origin, self.duration = path, origin, duration
        self.container = av.open(str(path))
        videos = [s for s in self.container.streams.video
                  if not s.disposition & av.stream.Disposition.attached_pic]
        self.video = videos[0] if videos else None
        self.has_picture = self.video is not None
        if self.video is not None:
            self.video.thread_type = "AUTO"
            rate = self.video.average_rate or self.video.guessed_rate
            self.frame_duration = float(1 / rate) if rate else 1 / 30
            ctx = self.video.codec_context
            sar = self.video.sample_aspect_ratio
            self.aspect = float(sar) if sar and sar.numerator else 1.0
            self.aspect *= ctx.width / ctx.height if ctx.height else 16 / 9
        self.audio = None
        self.audio_index = audio
        if audio is not None and self.wave:
            for s in self.container.streams.audio:
                if s.index == audio:
                    self.audio = s
        self.streams = [s for s in (self.video, self.audio) if s is not None]

    def _close_file(self):
        self._set_playing(False)
        if self.wave:
            self.wave.reset()
        if self.container:
            try:
                self.container.close()
            except Exception:
                pass
        self.container = None
        self._reset_state()

    def _frames(self):
        """("video" or "audio", seconds, frame) in the order the file holds them."""
        for packet in self.container.demux(*self.streams):
            try:
                frames = packet.decode()
            except av.FFmpegError:
                continue
            kind = packet.stream.type
            for frame in frames:
                if frame.pts is None:
                    continue
                yield kind, float(frame.pts * frame.time_base) - self.origin, frame

    def _next(self):
        try:
            return next(self.frames_iter)
        except (StopIteration, av.FFmpegError):
            self.eof = True
            return None

    # --- Moving around ---

    def _seek(self, t: float, exact: bool):
        t = max(0.0, min(t, self.duration))
        if self.wave:
            self.wave.reset()
        self.video_queue.clear()
        self.pending_audio = []
        self.primed = self.audio_on = False
        self.eof = False
        self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
        self.container.seek(int(max(t + self.origin, 0) * av.time_base), backward=True)
        self.frames_iter = self._frames()
        if not self.video:
            self.position = t
            self.audio_next = t
            self._report(force=True)
            return
        target, last = None, None
        while True:
            item = self._next()
            if item is None:
                break
            kind, ft, frame = item
            if kind == "audio":
                self._take_audio(ft, frame)
                continue
            last = (ft, frame)
            if not exact or ft >= t - self.frame_duration / 2:
                target = last
                break
        target = target or last
        if target:
            self._show(*target)
        else:
            self.position = t
        # Sound from before the frame shown is never needed
        self.pending_audio = [(s, d) for s, d in self.pending_audio
                              if s + len(d) / BYTES_PER_SAMPLE / RATE > self.position]
        self._report(force=True)

    def _step_forward(self):
        if not self.video or not self.frames_iter:
            return
        if self.primed:  # sound already handed to the device: start clean from here
            self._seek(self.position + self.frame_duration, exact=True)
            return
        while True:
            item = self._next()
            if item is None:
                return
            kind, ft, frame = item
            if kind == "audio":
                self._take_audio(ft, frame)
            elif ft > self.position + EPS:
                self._show(ft, frame)
                self._report(force=True)
                return

    def _show(self, t: float, frame):
        self.last_frame = frame
        self.last_image = self._image(frame)
        self.screen.draw(self.last_image, self.aspect)
        self.position = t

    def _redraw(self):
        if self.last_frame is not None:
            self.last_image = self._image(self.last_frame)
            self.screen.draw(self.last_image, self.aspect, force_clear=True)
        else:
            self.screen.clear()

    def _image(self, frame) -> tuple[int, int, bytes] | None:
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

    # --- Playing ---

    def _set_playing(self, on: bool):
        if on != self.playing:
            self.playing = on
            self.notify(("state", on))

    def _clock(self) -> float:
        if not self.playing:
            return self.paused_at
        now = time.perf_counter()
        if self.audio_on:
            played = self.wave.played()
            t = self.t0 + played / RATE
            if played >= self.wave.written - 1:
                # The sound has run dry (its track ended, or decoding fell behind):
                # carry on by the wall clock rather than freeze
                if self.underrun_since is None:
                    self.underrun_since = now
                elif now - self.underrun_since > 0.3:
                    self.audio_on = False
                    self.t0, self.wall0 = t, now
            else:
                self.underrun_since = None
            return t
        return self.t0 + (now - self.wall0)

    def _play(self):
        if not self.container:
            return
        start, end = self.range
        if self.mode != "continue" and start is not None and end is not None and \
                not (start - EPS <= self.position < end - self.frame_duration * 1.5):
            self._seek(start, exact=True)
        elif self.position >= self.duration - self.frame_duration:
            self._seek(start or 0.0, exact=True)
        if self.primed:
            # Carry on from a pause: everything is still queued
            if self.audio_on:
                self.wave.resume()
            else:
                self.wall0 = time.perf_counter() - (self.paused_at - self.t0)
            self._set_playing(True)
            return
        self.t0 = self.audio_next = self.position
        self.underrun_since = None
        self.audio_on = self.audio is not None and self.wave is not None
        if self.audio_on:
            self.wave.reset()
            self.wave.set_volume(self.volume)
            self.wave.pause()
            pending, self.pending_audio = self.pending_audio, []
            for s, data in pending:
                self._write_audio(s, data)
            self._set_playing(True)  # so decoding hands sound straight over
            while not self.eof and self.wave.written / RATE < 0.3 and \
                    len(self.video_queue) < 30:
                self._decode_one()
            self.wave.resume()
        else:
            self.wall0 = time.perf_counter()
            self._set_playing(True)
        self.primed = True

    def _pause(self):
        if not self.playing:
            return
        self.paused_at = self._clock()
        if self.audio_on:
            self.wave.pause()
        self._set_playing(False)
        self._report(force=True)

    def _tick(self):
        now = self._clock()
        start, end = self.range
        if self.mode != "continue" and end is not None and start is not None and \
                now >= end - EPS:
            if self.mode == "loop":  # straight back to the start, still playing
                self._seek(start, exact=True)
                self._play()
            else:  # stop on the part's last frame
                self._pause()
                self._seek(end - self.frame_duration, exact=True)
            return
        shown = None
        while self.video_queue and self.video_queue[0][0] <= now:
            shown = self.video_queue.popleft()  # anything overtaken is skipped
        if shown:
            t, image = shown
            self.last_image = image
            self.screen.draw(image, self.aspect)
            self.position = t
            self._report()
        elif not self.video:
            self.position = now
            self._report()

        ahead = (self.wave.written - self.wave.played()) / RATE if self.audio_on else 1.0
        if not self.eof and len(self.video_queue) < 60 and ahead < 3.0 and \
                (len(self.video_queue) < 4 or ahead < 0.5):
            self._decode_one()
            return
        if self.eof and not self.video_queue and (not self.audio_on or ahead <= 0.01):
            self._pause()
            self.position = min(self.position, self.duration)
            self._report(force=True)
            return
        wait = self.video_queue[0][0] - now if self.video_queue else 0.005
        time.sleep(max(0.001, min(wait, 0.005)))

    def _decode_one(self):
        item = self._next()
        if item is None:
            return
        kind, t, frame = item
        if kind == "audio":
            self._take_audio(t, frame)
        elif t >= self._clock() - self.frame_duration:  # too late to show: skip it
            self.last_frame = frame
            self.video_queue.append((t, self._image(frame)))

    def _take_audio(self, t: float, frame):
        if self.audio is None:
            return
        for out in self.resampler.resample(frame):
            data = bytes(out.planes[0])[:out.samples * BYTES_PER_SAMPLE]
            if self.playing:
                self._write_audio(t, data)
            else:
                self.pending_audio.append((t, data))
            t += out.samples / RATE

    def _write_audio(self, start: float, data: bytes):
        """Hand sound over, lined up with the clock: a gap is filled with silence,
        and anything from before the clock's start is cut off."""
        gap = start - self.audio_next
        if gap > 0.002:
            data = b"\0" * (int(round(min(gap, 1.0) * RATE)) * BYTES_PER_SAMPLE) + data
        elif gap < -0.002:
            cut = int(round(-gap * RATE)) * BYTES_PER_SAMPLE
            if cut >= len(data):
                return
            data = data[cut:]
        self.wave.write(data)
        self.audio_next += len(data) / BYTES_PER_SAMPLE / RATE

    def _report(self, force: bool = False):
        now = time.perf_counter()
        if force or now - self.last_report > 0.04:
            self.last_report = now
            self.notify(("pos", self.position))
