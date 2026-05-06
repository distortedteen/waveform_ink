#!/usr/bin/env python3
"""
waveform-ink — scrolling ASCII waveform renderer

Usage:
  python waveform_ink.py                    # live mic
  python waveform_ink.py song.mp3           # audio file
  python waveform_ink.py song.wav --style=mirror
  python waveform_ink.py --list-devices     # show available mic devices
  python waveform_ink.py --device=2         # pick mic by index

Styles:       bar | mirror | center | dot
Glyph sets:   blocks | braille | sharp | retro
Controls:
  SPACE       freeze / unfreeze
  s           save frame as .txt
  g           cycle glyph set
  v           cycle style (bar / mirror / center / dot)
  c           cycle colormap
  +/-         raise / lower gain
  r           reset gain
  q / ESC     quit
"""

import curses
import sys
import os
import time
import argparse
import threading
import queue
import math
from datetime import datetime
from pathlib import Path
from collections import deque

import numpy as np

try:
    import sounddevice as sd
    HAS_SD = True
except OSError:
    HAS_SD = False

try:
    import soundfile as sf
    HAS_SF = True
except ImportError:
    HAS_SF = False


# ─── constants ────────────────────────────────────────────────────────────────

SAMPLE_RATE  = 44100
CHUNK_SIZE   = 1024        # ~23 ms per chunk
MAX_HISTORY  = 2000        # max columns of waveform history

GLYPH_SETS = {
    "blocks":  " ▁▂▃▄▅▆▇█",
    "braille": " ⣀⣤⣶⣿",
    "sharp":   " ░▒▓█",
    "retro":   " .:!|I#@",
}
GLYPH_KEYS = list(GLYPH_SETS.keys())

STYLES = ["bar", "mirror", "center", "dot"]

# color mode IDs (curses pair numbers)
PAIR_DEFAULT = 1   # white/green gradient (base)
PAIR_MID     = 2   # mid amplitude
PAIR_PEAK    = 3   # high amplitude
PAIR_DIM     = 4   # muted / trail
PAIR_STATUS  = 5   # status bar
PAIR_FROZEN  = 6   # frozen indicator
PAIR_LABEL   = 7   # axis labels

COLORMAPS = ["green", "amber", "cyan", "ghost", "heat"]


# ─── audio source abstraction ─────────────────────────────────────────────────

class AudioSource:
    """Base class — subclasses push RMS chunks into self.q."""

    def __init__(self):
        self.q:      queue.Queue[float] = queue.Queue(maxsize=MAX_HISTORY)
        self.running = False
        self._thread: threading.Thread | None = None

    def start(self): ...
    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)


class MicSource(AudioSource):
    def __init__(self, device: int | None = None):
        super().__init__()
        self.device = device

    def start(self):
        if not HAS_SD:
            raise RuntimeError("sounddevice not available — install it or provide an audio file")
        self.running = True

        def _callback(indata, frames, time_info, status):
            mono = indata[:, 0]
            rms  = float(np.sqrt(np.mean(mono ** 2)))
            try:
                self.q.put_nowait(rms)
            except queue.Full:
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                self.q.put_nowait(rms)

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=CHUNK_SIZE,
            callback=_callback,
            device=self.device,
            dtype='float32',
        )
        self._stream.start()

    def stop(self):
        self.running = False
        if hasattr(self, '_stream'):
            self._stream.stop()
            self._stream.close()


class FileSource(AudioSource):
    def __init__(self, path: str, speed: float = 1.0):
        super().__init__()
        self.path  = path
        self.speed = speed

    def start(self):
        if not HAS_SF:
            raise RuntimeError("soundfile not available — pip install soundfile")
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            data, sr = sf.read(self.path, dtype='float32', always_2d=True)
        except Exception as e:
            # push a sentinel error via a negative value
            self.q.put(-1.0)
            return

        mono      = data.mean(axis=1)
        chunk     = CHUNK_SIZE
        sleep_per = chunk / sr / self.speed
        i         = 0

        while self.running and i < len(mono):
            window = mono[i:i + chunk]
            if len(window) == 0:
                break
            rms = float(np.sqrt(np.mean(window ** 2)))
            try:
                self.q.put(rms, timeout=0.5)
            except queue.Full:
                pass
            time.sleep(sleep_per)
            i += chunk

        # loop file seamlessly
        if self.running:
            self._run()


# ─── waveform history ─────────────────────────────────────────────────────────

class WaveHistory:
    def __init__(self, maxlen: int = MAX_HISTORY):
        self._buf: deque[float] = deque(maxlen=maxlen)
        self.gain = 6.0       # multiplier so quiet sources look interesting
        self.peak = 0.0       # rolling peak for auto-scale guide

    def push(self, rms: float):
        self.peak = max(self.peak * 0.998, rms)   # slow decay
        self._buf.append(min(1.0, rms * self.gain))

    def column(self) -> list[float]:
        return list(self._buf)

    def latest(self, n: int) -> list[float]:
        buf = self._buf
        if len(buf) <= n:
            return list(buf)
        return list(buf)[-n:]


# ─── rendering helpers ────────────────────────────────────────────────────────

def rms_to_glyph(v: float, glyphs: str) -> str:
    """Map 0.0–1.0 to a glyph in the ramp."""
    idx = int(v * (len(glyphs) - 1))
    return glyphs[max(0, min(idx, len(glyphs) - 1))]


def rms_to_height(v: float, h: int) -> int:
    return int(v * h)


def rms_to_color_pair(v: float) -> int:
    if v < 0.35:
        return PAIR_DEFAULT
    if v < 0.70:
        return PAIR_MID
    return PAIR_PEAK


def render_bar(col: float, h: int, glyphs: str) -> list[tuple[str, int]]:
    """
    Single column, bottom-up bar.
    Returns list of (char, color_pair) from top (row 0) to bottom (row h-1).
    """
    filled = rms_to_height(col, h)
    cells  = []
    for row in range(h):
        dist_from_top = row
        dist_from_bot = h - 1 - row
        if dist_from_bot < filled:
            # filled cell: brightness increases toward bottom
            v_local = col * (dist_from_bot / max(1, filled))
            g       = rms_to_glyph(max(0.2, col - v_local * 0.4), glyphs)
            cp      = rms_to_color_pair(col)
        else:
            g  = " "
            cp = PAIR_DIM
        cells.append((g, cp))
    return cells


def render_mirror(col: float, h: int, glyphs: str) -> list[tuple[str, int]]:
    """Bar that grows from both top and bottom toward the center."""
    half    = h // 2
    filled  = rms_to_height(col, half)
    cells   = []
    for row in range(h):
        mid_dist = min(row, h - 1 - row)
        if mid_dist < filled:
            g  = rms_to_glyph(col, glyphs)
            cp = rms_to_color_pair(col)
        else:
            g  = " "
            cp = PAIR_DIM
        cells.append((g, cp))
    return cells


def render_center(col: float, h: int, glyphs: str) -> list[tuple[str, int]]:
    """Grows from center outward (classic oscilloscope look)."""
    center  = h // 2
    half_f  = rms_to_height(col, h // 2)
    cells   = []
    for row in range(h):
        dist = abs(row - center)
        if dist <= half_f:
            g  = rms_to_glyph(col, glyphs)
            cp = rms_to_color_pair(col)
        else:
            g  = " "
            cp = PAIR_DIM
        cells.append((g, cp))
    return cells


def render_dot(col: float, h: int, glyphs: str) -> list[tuple[str, int]]:
    """Single dot that moves up/down — oscilloscope trace."""
    pos   = h - 1 - rms_to_height(col, h - 1)
    cells = []
    for row in range(h):
        if row == pos:
            g  = rms_to_glyph(col, glyphs)
            cp = rms_to_color_pair(col)
        else:
            g  = "·" if row % 4 == 0 else " "
            cp = PAIR_DIM
        cells.append((g, cp))
    return cells


RENDERERS = {
    "bar":    render_bar,
    "mirror": render_mirror,
    "center": render_center,
    "dot":    render_dot,
}


# ─── export ───────────────────────────────────────────────────────────────────

def export_frame(cols: list[float], h: int, render_fn, glyphs: str,
                 metadata: dict) -> str:
    """Render a frame to a .txt art string."""
    lines    = [""] * h
    for col_v in cols:
        cells = render_fn(col_v, h, glyphs)
        for row, (ch, _) in enumerate(cells):
            lines[row] += ch

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname    = f"waveform_{ts}.txt"
    header   = (
        f"waveform-ink  {ts}\n"
        f"style={metadata.get('style')}  "
        f"glyphs={metadata.get('glyphs')}  "
        f"gain={metadata.get('gain'):.1f}x\n"
        + "─" * max(len(lines[0]), 20) + "\n"
    )
    footer   = "\n" + "─" * max(len(lines[0]), 20)
    content  = header + "\n".join(lines) + footer

    with open(fname, "w", encoding="utf-8") as f:
        f.write(content)
    return fname


# ─── color setup ──────────────────────────────────────────────────────────────

_COLORMAP_DEFS = {
    "green": [
        (curses.COLOR_GREEN,  -1),   # PAIR_DEFAULT — mid green
        (curses.COLOR_WHITE,  -1),   # PAIR_MID
        (curses.COLOR_WHITE,  -1),   # PAIR_PEAK  (bold applied separately)
        (curses.COLOR_GREEN,  -1),   # PAIR_DIM
    ],
    "amber": [
        (curses.COLOR_YELLOW, -1),
        (curses.COLOR_WHITE,  -1),
        (curses.COLOR_RED,    -1),
        (curses.COLOR_YELLOW, -1),
    ],
    "cyan": [
        (curses.COLOR_CYAN,   -1),
        (curses.COLOR_WHITE,  -1),
        (curses.COLOR_CYAN,   -1),
        (curses.COLOR_CYAN,   -1),
    ],
    "ghost": [
        (curses.COLOR_WHITE,  -1),
        (curses.COLOR_WHITE,  -1),
        (curses.COLOR_WHITE,  -1),
        (-1,                  -1),
    ],
    "heat": [
        (curses.COLOR_BLUE,   -1),
        (curses.COLOR_YELLOW, -1),
        (curses.COLOR_RED,    -1),
        (curses.COLOR_BLUE,   -1),
    ],
}

def apply_colormap(name: str):
    defs = _COLORMAP_DEFS.get(name, _COLORMAP_DEFS["green"])
    for i, (fg, bg) in enumerate(defs):
        curses.init_pair(PAIR_DEFAULT + i, fg, bg)
    curses.init_pair(PAIR_STATUS,  curses.COLOR_BLACK,  curses.COLOR_GREEN)
    curses.init_pair(PAIR_FROZEN,  curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(PAIR_LABEL,   curses.COLOR_GREEN,  -1)


# ─── main UI ──────────────────────────────────────────────────────────────────

def draw_hud(stdscr: curses.window, h: int, w: int, state: dict):
    """Status bar at the bottom."""
    frozen_tag = " ❄ FROZEN " if state["frozen"] else ""
    src_tag    = f"{'MIC' if state['mic'] else Path(state['file']).stem[:16]}"
    info       = (
        f"  {src_tag}  │  "
        f"{state['style']}  │  "
        f"{state['glyphs']}  │  "
        f"gain {state['gain']:.1f}x  │  "
        f"{state['colormap']}"
        f"{frozen_tag}"
    )
    keys = "  SPACE=freeze  s=save  g=glyph  v=style  c=color  +/-=gain  q=quit"
    bar  = (info + keys)[:w - 1].ljust(w - 1)

    attr = (curses.color_pair(PAIR_FROZEN)
            if state["frozen"]
            else curses.color_pair(PAIR_STATUS))
    try:
        stdscr.addstr(h - 1, 0, bar, attr)
    except curses.error:
        pass

    # flash message above status bar
    if state.get("flash") and time.time() < state.get("flash_until", 0):
        msg  = f"  {state['flash']}  "
        attr = curses.color_pair(PAIR_FROZEN) | curses.A_BOLD
        try:
            stdscr.addstr(h - 2, w - len(msg) - 2, msg, attr)
        except curses.error:
            pass


def draw_labels(stdscr: curses.window, h: int, render_h: int,
                style: str, gain: float):
    """Faint amplitude markers on the left edge."""
    if style == "bar":
        markers = [(0, "█"), (render_h // 4, "▅"),
                   (render_h // 2, "▃"), (3 * render_h // 4, "▁"), (render_h - 1, " ")]
    elif style in ("mirror", "center"):
        markers = [(0, "─"), (render_h // 2, "┼"), (render_h - 1, "─")]
    else:
        markers = [(render_h // 2, "─")]

    attr = curses.color_pair(PAIR_LABEL) | curses.A_DIM
    for row, ch in markers:
        try:
            stdscr.addch(row, 0, ch, attr)
        except curses.error:
            pass


def run(stdscr: curses.window, source: AudioSource,
        is_mic: bool, file_path: str):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    history     = WaveHistory()
    glyph_idx   = 0
    style_idx   = 0
    color_idx   = 0
    frozen      = False
    frozen_snap: list[float] = []

    apply_colormap(COLORMAPS[color_idx])
    source.start()

    state = {
        "frozen":   False,
        "mic":      is_mic,
        "file":     file_path or "",
        "style":    STYLES[style_idx],
        "glyphs":   GLYPH_KEYS[glyph_idx],
        "gain":     history.gain,
        "colormap": COLORMAPS[color_idx],
        "flash":    "",
        "flash_until": 0.0,
    }

    def flash(msg: str):
        state["flash"]       = msg
        state["flash_until"] = time.time() + 3.0

    while True:
        h, w = stdscr.getmaxyx()
        render_h = h - 1          # reserve last row for HUD
        render_w = w - 2          # reserve left edge for labels

        # ── drain audio queue into history ───────────────────────────────────
        if not frozen:
            try:
                while True:
                    val = source.q.get_nowait()
                    if val < 0:
                        flash("error reading audio file")
                        break
                    history.push(val)
            except queue.Empty:
                pass

        # ── render ───────────────────────────────────────────────────────────
        glyphs    = GLYPH_SETS[GLYPH_KEYS[glyph_idx]]
        render_fn = RENDERERS[STYLES[style_idx]]
        cols      = (frozen_snap if frozen
                     else history.latest(render_w))

        # pad left with zeros if not enough history yet
        if len(cols) < render_w:
            cols = [0.0] * (render_w - len(cols)) + cols

        stdscr.erase()

        for ci, col_v in enumerate(cols):
            x      = ci + 2          # leave col 0-1 for labels
            if x >= w:
                break
            cells  = render_fn(col_v, render_h, glyphs)
            for row, (ch, cp) in enumerate(cells):
                if row >= render_h:
                    break
                attr = curses.color_pair(cp)
                if cp == PAIR_PEAK:
                    attr |= curses.A_BOLD
                if cp == PAIR_DIM:
                    attr |= curses.A_DIM
                try:
                    stdscr.addch(row, x, ch, attr)
                except curses.error:
                    pass

        draw_labels(stdscr, h, render_h, STYLES[style_idx], history.gain)

        state["frozen"]   = frozen
        state["style"]    = STYLES[style_idx]
        state["glyphs"]   = GLYPH_KEYS[glyph_idx]
        state["gain"]     = history.gain
        state["colormap"] = COLORMAPS[color_idx]
        draw_hud(stdscr, h, w, state)
        stdscr.refresh()

        # ── input ─────────────────────────────────────────────────────────────
        stdscr.timeout(30)       # ~33fps
        key = stdscr.getch()

        if key == curses.ERR:
            continue
        elif key in (ord('q'), 27):
            break
        elif key == ord(' '):
            frozen = not frozen
            if frozen:
                frozen_snap = history.latest(render_w)
            flash("❄ frozen" if frozen else "live")
        elif key == ord('s'):
            snap  = frozen_snap if frozen else history.latest(render_w)
            if len(snap) < render_w:
                snap = [0.0] * (render_w - len(snap)) + snap
            fname = export_frame(snap, render_h, render_fn, glyphs, {
                "style":  STYLES[style_idx],
                "glyphs": GLYPH_KEYS[glyph_idx],
                "gain":   history.gain,
            })
            flash(f"saved → {fname}")
        elif key == ord('g'):
            glyph_idx = (glyph_idx + 1) % len(GLYPH_KEYS)
            flash(f"glyphs → {GLYPH_KEYS[glyph_idx]}")
        elif key == ord('v'):
            style_idx = (style_idx + 1) % len(STYLES)
            flash(f"style → {STYLES[style_idx]}")
        elif key == ord('c'):
            color_idx = (color_idx + 1) % len(COLORMAPS)
            apply_colormap(COLORMAPS[color_idx])
            flash(f"color → {COLORMAPS[color_idx]}")
        elif key in (ord('+'), ord('=')):
            history.gain = min(history.gain + 0.5, 20.0)
            flash(f"gain → {history.gain:.1f}x")
        elif key in (ord('-'), ord('_')):
            history.gain = max(history.gain - 0.5, 0.5)
            flash(f"gain → {history.gain:.1f}x")
        elif key == ord('r'):
            history.gain = 6.0
            flash("gain reset")
        elif key == curses.KEY_RESIZE:
            stdscr.clear()

    source.stop()


# ─── entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="waveform-ink — scrolling ASCII waveform renderer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("file",           nargs="?",   help="audio file (wav, flac, ogg, mp3…)")
    parser.add_argument("--style",        default="bar",
                        choices=STYLES,   help="render style (default: bar)")
    parser.add_argument("--glyphs",       default="blocks",
                        choices=GLYPH_KEYS, help="glyph set (default: blocks)")
    parser.add_argument("--color",        default="green",
                        choices=COLORMAPS, help="colormap (default: green)")
    parser.add_argument("--gain",         type=float,  default=6.0,
                        help="amplitude multiplier (default: 6.0)")
    parser.add_argument("--device",       type=int,    default=None,
                        help="mic device index (use --list-devices to find)")
    parser.add_argument("--speed",        type=float,  default=1.0,
                        help="file playback speed multiplier (default: 1.0)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list available audio input devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        if not HAS_SD:
            print("sounddevice not available — pip install sounddevice")
            sys.exit(1)
        print(sd.query_devices())
        sys.exit(0)

    # pick source
    if args.file:
        if not HAS_SF:
            print("soundfile not installed — pip install soundfile")
            sys.exit(1)
        if not Path(args.file).exists():
            print(f"file not found: {args.file}")
            sys.exit(1)
        source   = FileSource(args.file, speed=args.speed)
        is_mic   = False
        filepath = args.file
    else:
        if not HAS_SD:
            print("sounddevice not installed — pip install sounddevice")
            print("provide an audio file instead: python waveform_ink.py song.wav")
            sys.exit(1)
        source   = MicSource(device=args.device)
        is_mic   = True
        filepath = ""

    # apply CLI initial state
    glyph_start = GLYPH_KEYS.index(args.glyphs) if args.glyphs in GLYPH_KEYS else 0
    style_start = STYLES.index(args.style)       if args.style  in STYLES      else 0

    # patch initial values into run() via monkey-patch on history
    def _run(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.start_color()
        curses.use_default_colors()
        apply_colormap(args.color)

        history           = WaveHistory()
        history.gain      = args.gain
        glyph_idx_ref     = [glyph_start]
        style_idx_ref     = [style_start]
        color_idx_ref     = [COLORMAPS.index(args.color)]
        frozen            = [False]
        frozen_snap       = [[]]
        state             = {
            "frozen":      False,
            "mic":         is_mic,
            "file":        filepath,
            "style":       STYLES[style_idx_ref[0]],
            "glyphs":      GLYPH_KEYS[glyph_idx_ref[0]],
            "gain":        history.gain,
            "colormap":    args.color,
            "flash":       "",
            "flash_until": 0.0,
        }

        def flash(msg):
            state["flash"]       = msg
            state["flash_until"] = time.time() + 3.0

        source.start()

        while True:
            h, w     = stdscr.getmaxyx()
            render_h = h - 1
            render_w = w - 2

            if not frozen[0]:
                try:
                    while True:
                        val = source.q.get_nowait()
                        if val < 0:
                            flash("error reading file")
                            break
                        history.push(val)
                except queue.Empty:
                    pass

            glyphs    = GLYPH_SETS[GLYPH_KEYS[glyph_idx_ref[0]]]
            render_fn = RENDERERS[STYLES[style_idx_ref[0]]]
            cols      = (frozen_snap[0] if frozen[0]
                         else history.latest(render_w))
            if len(cols) < render_w:
                cols = [0.0] * (render_w - len(cols)) + cols

            stdscr.erase()

            for ci, col_v in enumerate(cols):
                x = ci + 2
                if x >= w:
                    break
                cells = render_fn(col_v, render_h, glyphs)
                for row, (ch, cp) in enumerate(cells):
                    if row >= render_h:
                        break
                    attr = curses.color_pair(cp)
                    if cp == PAIR_PEAK:
                        attr |= curses.A_BOLD
                    if cp == PAIR_DIM:
                        attr |= curses.A_DIM
                    try:
                        stdscr.addch(row, x, ch, attr)
                    except curses.error:
                        pass

            draw_labels(stdscr, h, render_h, STYLES[style_idx_ref[0]], history.gain)

            state["frozen"]   = frozen[0]
            state["style"]    = STYLES[style_idx_ref[0]]
            state["glyphs"]   = GLYPH_KEYS[glyph_idx_ref[0]]
            state["gain"]     = history.gain
            state["colormap"] = COLORMAPS[color_idx_ref[0]]
            draw_hud(stdscr, h, w, state)
            stdscr.refresh()

            stdscr.timeout(30)
            key = stdscr.getch()

            if key == curses.ERR:
                continue
            elif key in (ord('q'), 27):
                break
            elif key == ord(' '):
                frozen[0] = not frozen[0]
                if frozen[0]:
                    frozen_snap[0] = history.latest(render_w)
                flash("❄ frozen" if frozen[0] else "live")
            elif key == ord('s'):
                snap = frozen_snap[0] if frozen[0] else history.latest(render_w)
                if len(snap) < render_w:
                    snap = [0.0] * (render_w - len(snap)) + snap
                fname = export_frame(snap, render_h, render_fn, glyphs, {
                    "style":  STYLES[style_idx_ref[0]],
                    "glyphs": GLYPH_KEYS[glyph_idx_ref[0]],
                    "gain":   history.gain,
                })
                flash(f"saved → {fname}")
            elif key == ord('g'):
                glyph_idx_ref[0] = (glyph_idx_ref[0] + 1) % len(GLYPH_KEYS)
                flash(f"glyphs → {GLYPH_KEYS[glyph_idx_ref[0]]}")
            elif key == ord('v'):
                style_idx_ref[0] = (style_idx_ref[0] + 1) % len(STYLES)
                flash(f"style → {STYLES[style_idx_ref[0]]}")
            elif key == ord('c'):
                color_idx_ref[0] = (color_idx_ref[0] + 1) % len(COLORMAPS)
                apply_colormap(COLORMAPS[color_idx_ref[0]])
                flash(f"color → {COLORMAPS[color_idx_ref[0]]}")
            elif key in (ord('+'), ord('=')):
                history.gain = min(history.gain + 0.5, 20.0)
                flash(f"gain → {history.gain:.1f}x")
            elif key in (ord('-'), ord('_')):
                history.gain = max(history.gain - 0.5, 0.5)
                flash(f"gain → {history.gain:.1f}x")
            elif key == ord('r'):
                history.gain = 6.0
                flash("gain reset")
            elif key == curses.KEY_RESIZE:
                stdscr.clear()

        source.stop()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            curses.endwin()
        except Exception:
            pass


if __name__ == "__main__":
    main()
