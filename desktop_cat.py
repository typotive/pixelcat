#!/usr/bin/env python3
"""
desktop_cat.py - a tiny pixel cat that wanders your screen, reminds you to take breaks,
fetches files for you and clears out junk in Cobra mode.

What it does
------------
* A borderless, transparent, always-on-top window holds a pixel-art cat that idles,
  walks left/right along the bottom of your screen and naps now and then.
* An activity tracker watches mouse/keyboard input. After 5 minutes with no input
  you count as "away" and the continuous-session counter resets (or pauses, see
  idle_policy in Config).
* After 40 minutes of sitting at the screen without a break, the cat stops, hops in
  place and tells you to get up and walk. Any break of 5+ minutes resets the count.
* Daily routine: a sip of water every 30 minutes of screen time, lunch at 1:00 pm and
  "let's go home" at 5:45 pm (Config.water_every_s / lunch_time / home_time, or
  --water-minutes / --lunch / --home). Lunch and home show once a day. If you're away
  at that moment they show when you return (within 90 min). Each has a "Later" button.
* Click the cat or the walk bubble to acknowledge: the 40-minute timer resets and the cat
  goes back to wandering. The bubble's "x" only snoozes the reminder for a few
  minutes without resetting the timer.
* Double-click the cat and it asks what to do:
  - Fetch a file: type part of a name and results appear instantly from an in-memory
    index of every file name on your fixed drives.
  - Cobra mode: shows how much is in the Recycle Bin and in temp/cache folders, and on
    "Strike!" empties the bin and deletes cache files untouched for 24 h+. In-use and
    locked files are skipped. Nothing is deleted without that confirmation.

Running it
----------
    pip install PyQt6            # or: pip install PySide6
    python desktop_cat.py        # normal mode (40 min sitting limit, 5 min idle)
    python desktop_cat.py --demo # fast timers: alert after 30 s, idle after 20 s
    python desktop_cat.py --install-shortcut   # Desktop + Start menu shortcuts with a cat icon

To pin to the taskbar, right-click the "Desktop Cat" shortcut > Pin to taskbar (Windows
does not allow apps to pin themselves). Launching it again while it runs makes the
existing cat say hello instead of starting a second cat.

Other options: --alert-minutes N, --idle-minutes N, --idle-policy reset|pause, --scale N,
--index PATH (repeatable; index these folders instead of all fixed drives).
Right-click the cat (or the tray icon) for status, "Fetch a file", "Cobra mode",
"Test alert now", "Reset timer", "Rebuild file index" and "Quit". Drag the cat with the
left mouse button to move it. Ctrl+C in the terminal also quits.

Fetching files (how the index works)
------------------------------------
Index file *names* only, not contents, and search them as you type.
* Index: every name sits in one packed lowercase byte blob, with arrays of offsets and
  parent-folder numbers. There are no per-file Python objects, so a million files take
  tens of MB, and a search is a few C-speed bytes.find() calls over the blob.
* Database: the index is saved to %LOCALAPPDATA%\\DesktopCat\\file_index.bin, so
  startup is instant. It is rebuilt in the background when older than
  index_max_age_min.
* Real-time updates: ReadDirectoryChangesW watches each drive root recursively
  (no admin needed). Creates, deletes and renames are applied live.
* Search syntax:  words = AND (all must match),  !word = NOT,  "exact phrase",
  ext:pdf;docx,  file: / folder:,  wild*cards?,  terms with \\ match the full path.
* In the popup: Enter / double-click = go to the location (a file is selected in its
  folder, never opened; a folder is opened), Shift+Enter = copy path, Ctrl+Enter = open
  the file itself, Up/Down = select, Esc = close. You can also drag a result into any app.
* By default Windows, ProgramData, AppData, recycle bins, node_modules, .git and
  similar noisy folders are skipped (see Config.index_exclude_*).

Typeface
--------
Text uses the first available typeface in FONT_CHOICES (installed, or as a .ttf/.otf in a
"fonts" folder next to this script), falling back to system sans-serif fonts.

Liquid-glass bubbles
--------------------
Glass windows sample the screen behind themselves, blur and slightly magnify it, then
add a tint, sheen and bright rim. On Windows 10 2004+ they exclude themselves from
screen capture so they can keep re-sampling live while visible (side effect: bubbles do
not appear in screenshots or screen shares). Elsewhere they sample once when shown.

Idle detection uses the Win32 GetLastInputInfo API on Windows (mouse + keyboard).
On macOS/Linux it falls back to polling the mouse cursor position (mouse only).
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import fnmatch
import json
import math
import os
import random
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# --- Qt binding shim: PyQt6 preferred, PySide6 as fallback -----------------------
try:
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

    Signal = QtCore.pyqtSignal
except ImportError:  # pragma: no cover - depends on what is installed
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

    Signal = QtCore.Signal

Qt = QtCore.Qt


SEP = os.sep  # "\\" on Windows, "/" on macOS


def _default_exclude_paths() -> tuple[str, ...]:
    if sys.platform == "darwin":
        paths = [str(Path.home() / "Library"), str(Path.home() / ".Trash")]
    else:
        paths = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramData", r"C:\ProgramData")]
    return tuple(p.lower().rstrip(SEP) for p in paths if p)


# =================================================================================
# Configuration
# =================================================================================
@dataclass
class Config:
    # --- Health timers (seconds) ---
    alert_after_s: int = 40 * 60       # max continuous sitting (screen time) before "get up and walk"
    idle_threshold_s: int = 5 * 60     # no input for this long = user is away
    idle_policy: str = "reset"         # "reset": away resets session; "pause": away only pauses it
    snooze_s: int = 5 * 60             # "x" on the alert bubble hides it for this long
    alert_rotate_s: int = 45           # pick a new reminder text this often while alerting

    # --- Daily routine ---
    water_every_s: int = 30 * 60       # "sip of water" after this much screen time (pauses while away)
    water_later_s: int = 10 * 60       # "Later" on the water bubble asks again after this
    lunch_time: str = "13:00"          # 24 h clock
    home_time: str = "17:45"
    clock_later_s: int = 10 * 60       # "10 more min" on the lunch / home bubbles
    clock_window_min: int = 90         # if you're away at that time, it still shows when you return within this
    routine_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # Mon=0 ... Sun=6; drop days you don't work

    # --- Timer intervals (milliseconds) ---
    tracker_interval_ms: int = 1000    # how often activity is polled
    move_interval_ms: int = 40         # movement tick (~25 FPS)
    frame_interval_ms: int = 180       # sprite animation frame step
    glass_refresh_ms: int = 150        # how often glass windows re-sample the screen
    search_debounce_ms: int = 90       # pause after a keystroke before searching

    # --- Pet behaviour ---
    behaviour_min_s: float = 3.0       # min time before picking a new wander behaviour
    behaviour_max_s: float = 8.0       # max time before picking a new wander behaviour
    chatter_min_s: int = 4 * 60        # random small talk interval (min)
    chatter_max_s: int = 9 * 60        # random small talk interval (max)
    walk_speed_px: int = 2             # pixels per movement tick
    pixel_scale: int = 5               # size of one sprite "pixel" on screen

    # --- File fetch (name index) ---
    index_roots: tuple[str, ...] = ()  # empty = every fixed drive
    index_exclude_names: tuple[str, ...] = (
        "$recycle.bin", "system volume information", "appdata", "node_modules", ".git",
        "__pycache__", ".venv", "venv", "$windows.~bt", "$windows.~ws", "$winreagent",
    )
    index_exclude_paths: tuple[str, ...] = field(default_factory=_default_exclude_paths)
    index_max_age_min: int = 60        # cached index older than this is rebuilt in the background
    fetch_max_results: int = 7         # rows shown in the fetch popup
    apps_refresh_min: int = 30         # re-read the Start menu app list this often
    search_candidates: int = 3000      # name matches examined per search before ranking

    # --- Cobra mode (Recycle Bin + temp/cache cleanup) ---
    cobra_min_age_h: int = 24          # only delete temp items untouched for this long (in-use files are newer)
    cobra_extra_dirs: tuple[str, ...] = ()  # more cache folders to clear, beyond the defaults
    cobra_fight_min_s: float = 2.5     # the cat-vs-cobra fight lasts at least this long, even if cleanup is instant

    # --- Fetch trip (cat runs off-screen and brings the item back before opening it) ---
    fetch_trip: bool = True            # False = open/go to the location instantly
    fetch_trip_leg_s: float = 1.0      # each way (out to the screen edge, and back)
    fetch_trip_pause_s: float = 0.5    # time spent "finding" it out of sight

    @classmethod
    def demo(cls) -> "Config":
        """Fast timers so the whole flow can be seen in under a minute."""
        return cls(alert_after_s=30, idle_threshold_s=20, snooze_s=15, alert_rotate_s=10,
                   chatter_min_s=20, chatter_max_s=40, water_every_s=45, water_later_s=20)


# Shown (rotating) when continuous screen time hits alert_after_s.
WALK_REMINDERS = [
    "🚶 Stand up and take a short walk. A few minutes is enough.",
    "🚶 Your back called: it wants a walk. Get off the chair for 5 minutes.",
    "🚶 Walk break: around the office, to the window, anywhere but this chair.",
    "🚶 Stretch your legs. The screen will still be here in five minutes.",
]

# Daily routine bubbles: title, body ({time} = scheduled time), buttons (key, label).
ROUTINE = {
    "water": ("💧 Sip of water", "Another {every} minutes on screen. Have a few sips.",
              [("water_ok", "Done 💧"), ("water_later", "Later")]),
    "lunch": ("🍱 Lunch break!", "It's {time}. Step away from the screen and eat something proper.",
              [("lunch_ok", "Going 🍱"), ("lunch_later", "10 more min")]),
    "home": ("🏠 Let's go home!", "It's {time}. Save your work and head out.",
             [("home_ok", "Heading out 👋"), ("home_later", "10 more min")]),
}
ROUTINE_REPLIES = {
    "water_ok": "Good human. 💧",
    "lunch_ok": "Enjoy your lunch! 🍱 I'll guard the desk.",
    "home_ok": "See you tomorrow! 👋🐾",
}

CHATTER = [
    "Mrrp.",
    "*stretches*",
    "Purr... 💤",
    "I'm keeping an eye on your screen time. 👀",
    "Don't forget to blink!",
    "Double-click me and I'll fetch a file for you. 🐾",
]

# Preferred UI typefaces as (family, italic), first match wins.
FONT_CHOICES = [("Redaction 50", True),
                ("Segoe UI Variable Text", False), ("Segoe UI", False), ("Helvetica Neue", False), ("Arial", False)]


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_span(seconds: float) -> str:
    """Round, friendly spans for settings: 2400 -> '40 min', 7200 -> '2 h', 5400 -> '1 h 30 min'."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return f"{int(seconds)} s"
    h, m = divmod(minutes, 60)
    return " ".join(part for part in (f"{h} h" if h else "", f"{m} min" if m else "") if part)


def fmt_clock(hhmm: str) -> str:
    """'13:00' -> '1:00 pm'."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").lower()


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    return f"{n:,}"


@dataclass
class UIFont:
    """The app typeface. make() builds sized fonts with the right italic setting."""
    family: str
    italic: bool = False

    def make(self, size: float, weight=QtGui.QFont.Weight.Normal) -> QtGui.QFont:
        font = QtGui.QFont(self.family)
        font.setPointSizeF(size)
        font.setWeight(weight)
        font.setItalic(self.italic)
        return font


def load_ui_font() -> UIFont:
    """Register any fonts bundled in ./fonts, then return the first preferred family available."""
    font_dir = Path(__file__).resolve().parent / "fonts"
    if font_dir.is_dir():
        for path in sorted(font_dir.glob("*.[ot]tf")):
            QtGui.QFontDatabase.addApplicationFont(str(path))
    available = set(QtGui.QFontDatabase.families())
    for family, italic in FONT_CHOICES:
        if family in available:
            return UIFont(family, italic)
    return UIFont(QtWidgets.QApplication.font().family())


# =================================================================================
# Activity tracking
# =================================================================================
class IdleDetector:
    """Reports seconds since the last mouse/keyboard input."""

    def __init__(self) -> None:
        self._use_win32 = sys.platform == "win32"
        if self._use_win32:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]

            self._lii = LASTINPUTINFO()
            self._lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
            self._kernel32.GetTickCount.restype = ctypes.c_uint32
        self._quartz_idle = self._load_quartz_idle()
        # Fallback state: cursor polling
        self._last_pos = None
        self._last_move = time.monotonic()

    @staticmethod
    def _load_quartz_idle():
        """macOS: seconds since any mouse/keyboard event (needs no permissions)."""
        if sys.platform != "darwin":
            return None
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            fn = lib.CGEventSourceSecondsSinceLastEventType
            fn.restype = ctypes.c_double
            fn.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            return fn
        except (OSError, AttributeError):
            return None

    def idle_seconds(self) -> float:
        if self._use_win32 and self._user32.GetLastInputInfo(ctypes.byref(self._lii)):
            # Both values are 32-bit millisecond tick counts; mask handles the 49-day wrap.
            elapsed_ms = (self._kernel32.GetTickCount() - self._lii.dwTime) & 0xFFFFFFFF
            return elapsed_ms / 1000.0

        if self._quartz_idle is not None:
            return float(self._quartz_idle(1, 0xFFFFFFFF))  # combined session state, any input event

        # Cross-platform fallback: only mouse movement counts as activity.
        pos = QtGui.QCursor.pos()
        now = time.monotonic()
        if pos != self._last_pos:
            self._last_pos = pos
            self._last_move = now
        return now - self._last_move


class ActivityTracker(QtCore.QObject):
    """Counts continuous active time and raises the break alert."""

    ticked = Signal(float, float)        # (session_seconds, idle_seconds)
    alert_triggered = Signal()
    user_away = Signal()
    user_returned = Signal()

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.detector = IdleDetector()
        self.session_s = 0.0             # continuous active time
        self.water_s = 0.0               # active time since the last water reminder (not reset by breaks)
        self.away = False
        self.alert_active = False
        self._last_poll = time.monotonic()

        # Poll timer: every tracker_interval_ms we read idle time and update the session.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(cfg.tracker_interval_ms)
        self._timer.timeout.connect(self.poll)

    def start(self) -> None:
        self._last_poll = time.monotonic()
        self._timer.start()

    def poll(self) -> None:
        now = time.monotonic()
        dt = now - self._last_poll       # real elapsed time, not the nominal interval
        self._last_poll = now
        idle = self.detector.idle_seconds()

        # A huge dt means the machine slept / the app was frozen: treat it as away too.
        if idle >= self.cfg.idle_threshold_s or dt >= self.cfg.idle_threshold_s:
            if not self.away:
                self.away = True
                if self.cfg.idle_policy == "pause":
                    # The idle stretch was counted as active while it was happening; undo it.
                    self.session_s = max(0.0, self.session_s - idle)
                else:
                    self.session_s = 0.0
                # Being away for 5+ minutes *is* a break, so any pending alert is cleared.
                self.alert_active = False
                self.user_away.emit()
        else:
            if self.away:
                self.away = False
                self.user_returned.emit()
            self.session_s += dt
            self.water_s += dt
            if not self.alert_active and self.session_s >= self.cfg.alert_after_s:
                self.alert_active = True
                self.alert_triggered.emit()

        self.ticked.emit(self.session_s, idle)

    def acknowledge(self) -> None:
        """User confirmed the break reminder: start a fresh sitting window."""
        self.session_s = 0.0
        self.alert_active = False

    def force_alert(self) -> None:
        """Debug helper: jump the session to the alert threshold."""
        self.session_s = max(self.session_s, float(self.cfg.alert_after_s))
        self.poll()


# =================================================================================
# File index (names only, packed in memory, updated live)
# =================================================================================
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Junctions and symlinks are not followed (they create loops and duplicates). Other
# reparse points ARE walked: cloud-sync folders are reparse points too.
SKIP_REPARSE_TAGS = (0xA0000003, 0xA000000C)  # IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK
CACHE_MAGIC = b"DCATIDX2"
# Results inside these rank first...
USER_CONTENT = tuple(s.replace("\\", SEP) for s in (
    "\\desktop\\", "\\documents\\", "\\downloads\\", "\\onedrive", "\\pictures\\", "\\videos\\", "\\music\\"))
# ...and paths containing these rank last.
NOISE_SEGMENTS = tuple(s.replace("\\", SEP) for s in (
    "\\appdata\\", "\\program files", "\\windows\\", "\\$recycle", "\\node_modules\\",
    "\\site-packages\\", "\\temp\\", "\\cache", "\\vendor\\", "\\registry\\", "packagecache",
    "pip_prebundle", "extscache", "\\dist\\", "\\build\\", "\\target\\", "\\."))


@dataclass
class Hit:
    path: str          # for apps: the .lnk path or "shell:AppsFolder\<AppID>"
    name: str
    is_dir: bool
    kind: str = "file"  # "file" (files and folders) or "app"


class NameTable:
    """Compact name table. Every name lives in one lowercase blob (searched) and one
    original-case blob (displayed), separated by '\\n'. Parallel arrays hold each
    entry's offsets, its parent entry and a directory flag. Full paths are rebuilt
    on demand by walking parents."""

    def __init__(self) -> None:
        self.lower = bytearray(b"\n")
        self.orig = bytearray(b"\n")
        self.lower_off = array("I")
        self.orig_off = array("I")
        self.parent = array("i")
        self.is_dir = bytearray()

    def __len__(self) -> int:
        return len(self.parent)

    def add(self, name: str, parent: int, is_dir: bool) -> int:
        self.lower_off.append(len(self.lower))
        self.lower += name.lower().encode("utf-8", "surrogatepass") + b"\n"
        self.orig_off.append(len(self.orig))
        self.orig += name.encode("utf-8", "surrogatepass") + b"\n"
        self.parent.append(parent)
        self.is_dir.append(1 if is_dir else 0)
        return len(self.parent) - 1

    def name(self, i: int) -> str:
        start = self.orig_off[i]
        return self.orig[start:self.orig.index(b"\n", start)].decode("utf-8", "surrogatepass")

    def lname(self, i: int) -> str:
        start = self.lower_off[i]
        return self.lower[start:self.lower.index(b"\n", start)].decode("utf-8", "surrogatepass")

    def path(self, i: int, removed=(), memo: dict | None = None) -> str | None:
        """Full path of entry i, or None if it or any parent was deleted (tombstoned)."""
        memo = {} if memo is None else memo
        chain = []
        base = None
        j = i
        while j >= 0:
            if j in removed:
                return None
            if j in memo:
                base = memo[j]
                break
            chain.append(j)
            j = self.parent[j]
        for k in reversed(chain):
            part = self.name(k)
            base = part if base is None else base + SEP + part
            memo[k] = base
        return base

    def find(self, needle: bytes):
        """Yield entries whose lowercase name contains needle (C-speed scan of the blob)."""
        blob, offsets, pos = self.lower, self.lower_off, 1
        while True:
            pos = blob.find(needle, pos)
            if pos < 0:
                return
            yield bisect.bisect_right(offsets, pos) - 1
            pos = blob.find(b"\n", pos) + 1  # skip to the next name

    def lookup(self, path_l: str) -> int | None:
        """Entry index for an exact lowercase full path, if present."""
        name = path_l.rstrip(SEP).rsplit(SEP, 1)[-1].encode("utf-8", "surrogatepass")
        blob, offsets, pos = self.lower, self.lower_off, 0
        needle = b"\n" + name + b"\n"
        while True:
            pos = blob.find(needle, pos)
            if pos < 0:
                return None
            i = bisect.bisect_right(offsets, pos + 1) - 1
            p = self.path(i)
            if p is not None and p.lower() == path_l:
                return i
            pos += 1

    # --- on-disk database ----------------------------------------------------------
    def save(self, path: Path, meta: dict) -> None:
        parts = [self.lower, self.orig, self.lower_off.tobytes(), self.orig_off.tobytes(),
                 self.parent.tobytes(), self.is_dir]
        header = json.dumps({**meta, "sizes": [len(p) for p in parts]}).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(CACHE_MAGIC + struct.pack("<I", len(header)) + header)
            for part in parts:
                f.write(part)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> tuple["NameTable", dict] | None:
        try:
            with open(path, "rb") as f:
                if f.read(len(CACHE_MAGIC)) != CACHE_MAGIC:
                    return None
                (hlen,) = struct.unpack("<I", f.read(4))
                meta = json.loads(f.read(hlen))
                raw = [f.read(n) for n in meta["sizes"]]
        except (OSError, ValueError, KeyError):
            return None
        table = cls()
        table.lower, table.orig = bytearray(raw[0]), bytearray(raw[1])
        for arr, data in ((table.lower_off, raw[2]), (table.orig_off, raw[3]), (table.parent, raw[4])):
            arr.frombytes(data)
        table.is_dir = bytearray(raw[5])
        return table, meta


class Query:
    """Parses search text: AND terms, !NOT, "phrases", ext:, file:/folder:,
    wildcards, and path terms containing a backslash."""

    def __init__(self, text: str) -> None:
        self.terms: list[str] = []
        self.path_terms: list[str] = []
        self.negs: list[str] = []
        self.exts: tuple[str, ...] = ()
        self.kind = None
        self.patterns: list[re.Pattern] = []
        fragments = []
        for tok in re.findall(r'!?"[^"]*"|\S+', text):
            neg = tok.startswith("!")
            tok = (tok[1:] if neg else tok).strip('"').lower().replace("/", SEP).replace("\\", SEP)
            if not tok:
                continue
            if tok.startswith("ext:"):
                self.exts += tuple("." + e.lstrip(".") for e in tok[4:].split(";") if e)
            elif tok in ("file:", "folder:", "app:"):
                self.kind = tok[:-1]
            elif neg:
                self.negs.append(tok)
            elif "*" in tok or "?" in tok:
                self.patterns.append(re.compile(fnmatch.translate(tok)))
                fragments.append(max(re.split(r"[*?]", tok), key=len))
            elif SEP in tok:
                self.path_terms.append(tok)
                fragments.append(tok.rstrip(SEP).rsplit(SEP, 1)[-1])
            else:
                self.terms.append(tok)
        # The "driver" is the longest literal that must appear in the name; it is what we
        # scan the blob for. Every other condition is checked on the candidates.
        candidates = [c for c in self.terms + fragments if c]
        if candidates:
            self.driver = max(candidates, key=len)
        elif self.exts:
            self.driver = max(self.exts, key=len)
        else:
            self.driver = ""

    def name_ok(self, name_l: str, is_dir: bool) -> bool:
        if self.kind == "file" and is_dir or self.kind == "folder" and not is_dir:
            return False
        if self.driver not in name_l:
            return False
        if self.exts and not name_l.endswith(self.exts):
            return False
        return all(p.match(name_l) for p in self.patterns)

    def path_ok(self, path_l: str) -> bool:
        return (all(t in path_l for t in self.terms) and all(t in path_l for t in self.path_terms)
                and not any(t in path_l for t in self.negs))

    def rank(self, name_l: str, path_l: str) -> tuple:
        d = self.driver
        stem = name_l.rsplit(".", 1)[0]
        if name_l == d or stem == d:
            quality = 0
        elif name_l.startswith(d):
            quality = 1
        elif re.search(r"(^|[^a-z0-9])" + re.escape(d), name_l):
            quality = 2
        else:
            quality = 3
        noise = sum(s in path_l for s in NOISE_SEGMENTS)  # "\\." = any dot-folder (.git, .cargo...)
        not_user_content = not any(s in path_l for s in USER_CONTENT)
        return (noise, quality, not_user_content, path_l.count(SEP), len(name_l))


class _DirWatcher(threading.Thread):
    """Blocking ReadDirectoryChangesW loop on one drive root (recursive, no admin needed)."""

    def __init__(self, root: str, on_event, on_overflow) -> None:
        super().__init__(daemon=True, name=f"watch {root}")
        self.root, self.on_event, self.on_overflow = root, on_event, on_overflow

    def run(self) -> None:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                              wintypes.LPVOID, wintypes.LPVOID]
        FILE_LIST_DIRECTORY, SHARE_ALL, OPEN_EXISTING, BACKUP_SEMANTICS = 0x1, 0x7, 3, 0x02000000
        NOTIFY_NAMES = 0x1 | 0x2  # FILE_NAME | DIR_NAME
        handle = k32.CreateFileW(self.root, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                                 BACKUP_SEMANTICS, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        buf = ctypes.create_string_buffer(256 * 1024)
        got = wintypes.DWORD()
        while k32.ReadDirectoryChangesW(handle, buf, len(buf), True, NOTIFY_NAMES, ctypes.byref(got), None, None):
            if got.value == 0:
                self.on_overflow()  # too many changes at once: the buffer was dropped
                continue
            data, off = buf.raw[:got.value], 0
            while True:
                nxt, action, length = struct.unpack_from("<III", data, off)
                name = data[off + 12:off + 12 + length].decode("utf-16-le", "surrogatepass")
                try:
                    self.on_event(action, os.path.join(self.root, name))
                except Exception:  # never let one bad event kill the watcher
                    pass
                if not nxt:
                    break
                off += nxt


class background_io:
    """Run the current thread at background CPU *and disk* priority (Windows), so big
    scans and deletes always yield to File Explorer and other apps."""

    BEGIN, END = 0x00010000, 0x00020000  # THREAD_MODE_BACKGROUND_BEGIN / _END

    def __enter__(self):
        self._on = False
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._on = bool(k32.SetThreadPriority(k32.GetCurrentThread(), self.BEGIN))
        return self

    def __exit__(self, *exc) -> None:
        if self._on:
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), self.END)


def app_data_dir() -> Path:
    """Where the file index and reminder state live."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DesktopCat"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "DesktopCat"


def default_index_roots() -> list[str]:
    if sys.platform != "win32":
        return [str(Path.home())]
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    DRIVE_FIXED = 3
    return [f"{chr(65 + i)}:\\" for i in range(26)
            if mask & (1 << i) and k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == DRIVE_FIXED]


class FileIndex(QtCore.QObject):
    """Background-built, live-updated index of file names on the configured roots."""

    progress = Signal(int)   # entries scanned so far
    changed = Signal()       # a new index is ready

    ACTION_ADDED, ACTION_REMOVED, ACTION_RENAMED_OLD, ACTION_RENAMED_NEW = 1, 2, 4, 5

    def __init__(self, cfg: Config, cache_path: Path | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.roots = list(cfg.index_roots) or default_index_roots()
        self.cache_path = cache_path or app_data_dir() / "file_index.bin"
        self.table: NameTable | None = None
        self.scanning = False
        self.scanned = 0
        self.updated_at = 0.0
        self._lock = threading.Lock()          # guards the live overlay below
        self._scan_lock = threading.Lock()     # one scan at a time
        self._added: dict[str, tuple[str, bool]] = {}  # lower path -> (path, is_dir)
        self._removed_idx: set[int] = set()    # tombstoned table entries
        self._removed_paths: set[str] = set()  # same, by path (re-applied after a rescan)
        self._exclude_names = set(cfg.index_exclude_names)
        self._exclude_paths = tuple(p.lower().rstrip(SEP) for p in cfg.index_exclude_paths)

    # --- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._bootstrap, daemon=True, name="file index").start()

    def rescan(self) -> None:
        threading.Thread(target=self._scan_and_swap, daemon=True, name="file rescan").start()

    def _bootstrap(self) -> None:
        loaded = NameTable.load(self.cache_path)
        if loaded and loaded[1].get("roots") == self.roots:
            self.table, meta = loaded
            self.updated_at = meta.get("time", 0.0)
            self.changed.emit()
        if sys.platform == "win32":
            for root in self.roots:
                _DirWatcher(root, self._on_fs_event, self.rescan).start()
        if self.table is None or time.time() - self.updated_at > self.cfg.index_max_age_min * 60:
            self._scan_and_swap()

    # --- full scan -----------------------------------------------------------------
    def _excluded(self, path_l: str, name_l: str) -> bool:
        return name_l in self._exclude_names or path_l in self._exclude_paths

    def _scan_and_swap(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            with background_io():
                self._scan()
        finally:
            self.scanning = False
            self._scan_lock.release()

    def _scan(self) -> None:
        self.scanning = True
        table = NameTable()
        next_report = 0
        if self.table is None:
            self.table = table  # first run: let searches see partial results while scanning
        for root in self.roots:
            stack = [(root, table.add(root.rstrip(SEP), -1, True))]
            while stack:
                folder, parent = stack.pop()
                try:
                    it = os.scandir(folder)
                except OSError:
                    continue  # access denied etc.
                with it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)  # free on Windows (from the dir listing)
                            attrs = getattr(st, "st_file_attributes", None)
                            if attrs is None:  # non-Windows
                                is_dir = entry.is_dir(follow_symlinks=False)
                                walk = is_dir
                            else:
                                is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
                                walk = is_dir and not (attrs & FILE_ATTRIBUTE_REPARSE_POINT
                                                       and st.st_reparse_tag in SKIP_REPARSE_TAGS)
                        except OSError:
                            continue
                        idx = table.add(entry.name, parent, is_dir)
                        if walk and not self._excluded(entry.path.lower(), entry.name.lower()):
                            stack.append((entry.path, idx))
                self.scanned = len(table)
                if self.scanned >= next_report:
                    next_report = self.scanned + 20000
                    self.progress.emit(self.scanned)
        with self._lock:
            self.table = table
            found = {p: table.lookup(p) for p in self._removed_paths}
            self._removed_paths = {p for p, i in found.items() if i is not None}
            self._removed_idx = {i for i in found.values() if i is not None}
            self._added = {k: v for k, v in self._added.items() if table.lookup(k) is None}
        self.updated_at = time.time()
        self.scanning = False
        self.changed.emit()
        try:
            table.save(self.cache_path, {"roots": self.roots, "time": self.updated_at})
        except OSError:
            pass

    # --- live updates (watcher threads) --------------------------------------------
    def _path_excluded(self, path_l: str) -> bool:
        if path_l.startswith(self._exclude_paths) or path_l.startswith(str(self.cache_path.parent).lower()):
            return True
        return any(part in self._exclude_names for part in path_l.split(SEP)[:-1])

    def _on_fs_event(self, action: int, path: str) -> None:
        path_l = path.lower()
        if self._path_excluded(path_l):
            return
        if action in (self.ACTION_ADDED, self.ACTION_RENAMED_NEW):
            self._add_live(path)
        elif action in (self.ACTION_REMOVED, self.ACTION_RENAMED_OLD):
            self._remove_live(path_l)

    def _add_live(self, path: str) -> None:
        is_dir = os.path.isdir(path)
        with self._lock:
            self._added[path.lower()] = (path, is_dir)
            self._removed_paths.discard(path.lower())
        if is_dir:  # a folder moved in brings its contents; the watcher only reports the folder
            count = 0
            for folder, dirs, files in os.walk(path):
                with self._lock:
                    for n in dirs:
                        self._added[os.path.join(folder, n).lower()] = (os.path.join(folder, n), True)
                    for n in files:
                        self._added[os.path.join(folder, n).lower()] = (os.path.join(folder, n), False)
                count += len(dirs) + len(files)
                if count > 50000:
                    break

    def _remove_live(self, path_l: str) -> None:
        with self._lock:
            if self._added.pop(path_l, None) is not None:
                prefix = path_l + SEP
                for key in [k for k in self._added if k.startswith(prefix)]:
                    del self._added[key]
                return
            self._removed_paths.add(path_l)
        table = self.table
        idx = table.lookup(path_l) if table is not None else None
        if idx is not None:
            with self._lock:
                self._removed_idx.add(idx)  # children disappear too: path() checks ancestors

    # --- search --------------------------------------------------------------------
    def count(self) -> int:
        table = self.table
        return (len(table) if table is not None else 0) + len(self._added)

    def search(self, text: str, limit: int) -> list[Hit]:
        q = Query(text)
        if not q.driver or q.kind == "app" or limit <= 0:
            return []
        scored: dict[str, tuple] = {}
        table = self.table
        with self._lock:
            removed = set(self._removed_idx)
            overlay = list(self._added.values())
        if table is not None:
            memo: dict[int, str] = {}
            examined = 0
            for i in table.find(q.driver.encode("utf-8", "surrogatepass")):
                is_dir = bool(table.is_dir[i])
                if not q.name_ok(table.lname(i), is_dir):
                    continue
                examined += 1
                if examined > self.cfg.search_candidates:
                    break
                path = table.path(i, removed, memo)
                if path is None:
                    continue
                path_l = path.lower()
                if q.path_ok(path_l):
                    scored[path_l] = (q.rank(path_l.rsplit(SEP, 1)[-1], path_l), path, is_dir)
        for path, is_dir in overlay:
            path_l = path.lower()
            name_l = path_l.rsplit(SEP, 1)[-1]
            if q.name_ok(name_l, is_dir) and q.path_ok(path_l):
                scored[path_l] = (q.rank(name_l, path_l), path, is_dir)
        best = sorted(scored.values(), key=lambda s: s[0])[:limit]
        return [Hit(path, path.rsplit(SEP, 1)[-1] or path, is_dir) for _, path, is_dir in best]


# --- Installed apps ------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000  # keep helper PowerShell windows from flashing under pythonw


def start_menu_dirs() -> list[str]:
    env = os.environ
    rel = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")
    return [os.path.join(env[v], rel) for v in ("ProgramData", "APPDATA") if env.get(v)]


def mac_app_bundles() -> list[str]:
    """.app bundles in the Applications folders, one subfolder deep (e.g. Utilities)."""
    if sys.platform != "darwin":
        return []
    bundles = []
    for root in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
        for folder in (root, *(e.path for e in _scandir_safe(root) if e.is_dir() and not e.name.endswith(".app"))):
            bundles += [e.path for e in _scandir_safe(folder) if e.name.endswith(".app")]
    return bundles


def _scandir_safe(folder: str) -> list[os.DirEntry]:
    try:
        return list(os.scandir(folder))
    except OSError:
        return []


def _start_apps() -> list[tuple[str, str]]:
    """(name, AppID) for everything the Start menu lists, including Store apps."""
    if sys.platform != "win32":
        return []
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW).stdout
        data = json.loads(out.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]


class AppIndex(QtCore.QObject):
    """Installed apps as the Start menu lists them. Start-menu shortcuts come first
    (they carry real icons); Get-StartApps adds the rest, including Store apps, which
    launch through shell:AppsFolder. Refreshed in the background."""

    changed = Signal()

    def __init__(self, refresh_min: int) -> None:
        super().__init__()
        self.apps: list[Hit] = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(refresh_min * 60 * 1000)
        self._timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.refresh()
        self._timer.start()

    def refresh(self) -> None:
        threading.Thread(target=self._load, daemon=True, name="app list").start()

    def _load(self) -> None:
        found: dict[str, Hit] = {}
        for folder in start_menu_dirs():
            for root, _, files in os.walk(folder):
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() in (".lnk", ".appref-ms") and "uninstall" not in stem.lower():
                        found.setdefault(stem.lower(), Hit(os.path.join(root, f), stem, False, "app"))
        for path in mac_app_bundles():
            stem = os.path.splitext(os.path.basename(path))[0]
            found.setdefault(stem.lower(), Hit(path, stem, False, "app"))
        for name, app_id in _start_apps():
            if "uninstall" not in name.lower():
                found.setdefault(name.lower(), Hit("shell:AppsFolder\\" + app_id, name, False, "app"))
        self.apps = sorted(found.values(), key=lambda h: h.name.lower())
        self.changed.emit()

    def search(self, text: str, limit: int) -> list[Hit]:
        q = Query(text)
        if not q.driver or q.exts or q.path_terms or q.kind in ("file", "folder"):
            return []
        scored = []
        for app in self.apps:
            name_l = app.name.lower()
            if q.name_ok(name_l, False) and q.path_ok(name_l):
                quality = q.rank(name_l, name_l)[1]
                if quality <= 2:  # whole word / prefix matches only: "word" ≠ "password"
                    scored.append((quality, len(name_l), app))
        scored.sort(key=lambda s: s[:2])
        return [app for *_, app in scored[:limit]]


def launch_app(target: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif target.startswith("shell:AppsFolder\\"):
        subprocess.Popen(["explorer.exe", target], creationflags=CREATE_NO_WINDOW)
    else:
        os.startfile(target)


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path)):
        raise OSError(f"cannot open {path}")


def force_foreground(widget: QtWidgets.QWidget) -> None:
    """Bring a window to the foreground so it receives typing. Windows normally refuses
    this for background apps, and the cat itself never takes focus, so we briefly attach
    to the current foreground thread's input queue, which Windows allows."""
    widget.activateWindow()
    if sys.platform != "win32":
        return
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    hwnd = int(widget.winId())
    fg_thread = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    me = k32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and u32.AttachThreadInput(fg_thread, me, True))
    u32.SetForegroundWindow(hwnd)
    if attached:
        u32.AttachThreadInput(fg_thread, me, False)


def reveal_path(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])  # Finder, with the item selected
    else:
        open_path(os.path.dirname(path))


# =================================================================================
# Cobra mode - empties the Recycle Bin and clears old temp/cache files
# =================================================================================
def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def recycle_bin_info() -> tuple[int, int]:
    """(bytes, items) in the Recycle Bins of all drives."""
    if sys.platform != "win32":
        return 0, 0
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def empty_recycle_bin() -> None:
    SHERB_SILENT = 0x1 | 0x2 | 0x4  # no confirmation, no progress UI, no sound
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_SILENT)


def _is_link(st: os.stat_result) -> bool:
    """Symlinks and junctions: removed as links, never followed into their target."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _tree_stats(path: str) -> tuple[float, int]:
    """(newest modification time, total bytes) of a file or folder tree."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return time.time(), 0  # unreadable: treat as "in use"
    if not stat.S_ISDIR(st.st_mode) or _is_link(st):
        return st.st_mtime, st.st_size if not stat.S_ISDIR(st.st_mode) else 0
    newest, size = st.st_mtime, 0
    try:
        children = os.listdir(path)
    except OSError:
        return time.time(), 0
    for child in children:
        child_newest, child_size = _tree_stats(os.path.join(path, child))
        newest, size = max(newest, child_newest), size + child_size
    return newest, size


def _remove_tree(path: str) -> tuple[int, int]:
    """Delete a file or folder tree; returns (bytes freed, items that could not be deleted)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0, 0  # already gone
    if stat.S_ISDIR(st.st_mode) and not _is_link(st):
        freed = failed = 0
        try:
            children = os.listdir(path)
        except OSError:
            return 0, 1
        for child in children:
            f, x = _remove_tree(os.path.join(path, child))
            freed, failed = freed + f, failed + x
        try:
            os.rmdir(path)
        except OSError:
            failed += 0 if failed else 1
        return freed, failed
    try:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(path)  # junction / directory symlink: removes the link only
            return 0, 0
        if getattr(st, "st_file_attributes", 0) & 0x1:  # FILE_ATTRIBUTE_READONLY
            os.chmod(path, stat.S_IWRITE)
        os.remove(path)
        return st.st_size, 0
    except OSError:
        return 0, 1  # in use or protected: left alone


@dataclass
class CleanupPlan:
    recycle_bytes: int
    recycle_items: int
    temp_items: list[tuple[str, int]]  # (path, bytes) of top-level temp/cache entries to delete

    @property
    def temp_bytes(self) -> int:
        return sum(size for _, size in self.temp_items)

    @property
    def empty(self) -> bool:
        return not self.recycle_items and not self.temp_items


class CobraCleaner(QtCore.QObject):
    """Cobra mode: empties the Recycle Bin and deletes old temp/cache files.

    Only top-level temp entries whose whole tree is older than cobra_min_age_h are
    removed: files that running programs and installers are using are newer, so they
    are left alone, and anything locked is skipped. Ages are re-checked right before
    deleting, and links/junctions are removed as links, never followed.
    """

    planned = Signal(object)    # CleanupPlan
    finished = Signal(int, int)  # (bytes freed, items skipped)

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.busy = False

    def cache_dirs(self) -> list[str]:
        env = os.environ
        candidates = list(self.cfg.cobra_extra_dirs)
        local = env.get("LOCALAPPDATA")
        if sys.platform == "win32" and local:  # no default cleanup targets on other systems
            candidates += [env.get("TEMP"), env.get("TMP"), os.path.join(local, "Temp"),
                           os.path.join(env.get("SystemRoot", r"C:\Windows"), "Temp"),
                           os.path.join(local, "Microsoft", "Windows", "INetCache"),
                           os.path.join(local, "CrashDumps")]
        seen, dirs = set(), []
        for d in candidates:
            if d and os.path.isdir(d):
                key = os.path.normcase(os.path.realpath(d))
                if key not in seen:
                    seen.add(key)
                    dirs.append(d)
        return dirs

    def _cutoff(self) -> float:
        return time.time() - self.cfg.cobra_min_age_h * 3600

    def build_plan(self) -> CleanupPlan:
        rb_bytes, rb_items = recycle_bin_info()
        cutoff, items = self._cutoff(), []
        for folder in self.cache_dirs():
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                path = os.path.join(folder, name)
                newest, size = _tree_stats(path)
                if newest < cutoff:
                    items.append((path, size))
        return CleanupPlan(rb_bytes, rb_items, items)

    def execute(self, plan: CleanupPlan) -> tuple[int, int]:
        freed = skipped = 0
        if plan.recycle_items:
            before = recycle_bin_info()[0]
            empty_recycle_bin()
            freed += max(0, before - recycle_bin_info()[0])
        cutoff = self._cutoff()
        for path, _ in plan.temp_items:
            if _tree_stats(path)[0] >= cutoff:
                continue  # touched since the plan was made: now in use, leave it
            f, x = _remove_tree(path)
            freed, skipped = freed + f, skipped + x
        return freed, skipped

    # Threaded wrappers for the UI.
    def plan_async(self) -> None:
        self.busy = True
        threading.Thread(target=self._plan_thread, daemon=True).start()

    def _plan_thread(self) -> None:
        with background_io():
            plan = self.build_plan()
        self._finish_plan(plan)

    def _finish_plan(self, plan: CleanupPlan) -> None:
        self.busy = False
        self.planned.emit(plan)

    def execute_async(self, plan: CleanupPlan) -> None:
        self.busy = True
        threading.Thread(target=self._execute_thread, args=(plan,), daemon=True).start()

    def _execute_thread(self, plan: CleanupPlan) -> None:
        result = (0, 0)
        try:
            with background_io():  # deleting gigabytes must not starve File Explorer of disk time
                result = self.execute(plan)
        finally:
            self._finish_execute(*result)  # always report back, so the cat never stays stuck

    def _finish_execute(self, freed: int, skipped: int) -> None:
        self.busy = False
        self.finished.emit(freed, skipped)


# =================================================================================
# Sprites (placeholder pixel art drawn from strings - swap for PNG frames later)
# =================================================================================
PALETTE = {
    "K": "#3b2a20",  # outline (warm dark brown reads softer than black)
    "O": "#f6a55b",  # orange fur
    "D": "#d9773a",  # tabby stripes
    "W": "#fffaf3",  # white fur (muzzle, chest, socks)
    "P": "#ff8fa3",  # nose / inner ears
    "E": "#1e2630",  # eyes
    "H": "#ffffff",  # eye highlight
}

# 20 x 16 tabby, facing right. Rows 0-12 are head/body/tail; legs are generated.
_BODY = [
    "............K....K..",
    "...........KPK..KPK.",
    "..KK.......KOOKKKOOK",
    ".KOK......KOOOOOOOOK",
    ".KOK......KOEHOOEHOK",
    ".KOK......KOEEOOEEOK",
    "..KOK.....KWOOPPOOWK",
    "...KOK....KWWWKKWWWK",
    "....KOKKKKKKKWWWWWK.",
    "....KOODOODOOOWWWWK.",
    "....KOODOODOOOOWWWK.",
    "....KOODOODOOOOOWWK.",
    "....KOOOOOOOOOOOOOK.",
]


def _legs(starts: tuple[int, ...], width: int = 20) -> list[str]:
    """Three leg rows (fur, white sock, outline) with legs starting at the given columns."""
    rows = []
    for part in ("KOK", "KWK", "KKK"):
        row = ["."] * width
        for s in starts:
            row[s:s + 3] = part
        rows.append("".join(row))
    return rows


def _replace(rows: list[str], changes: dict[int, str]) -> list[str]:
    return [changes.get(i, r) for i, r in enumerate(rows)]


_STAND = _legs((5, 8, 12, 15))
SPRITE_ART = {
    "stand": _BODY + _STAND,
    "blink": _replace(_BODY, {4: ".KOK......KOOOOOOOOK", 5: ".KOK......KOKKOOKKOK"}) + _STAND,
    "walk_a": _BODY + _legs((4, 8, 11, 16)),   # legs spread
    "walk_b": _BODY + _legs((6, 7, 13, 14)),   # legs passing
    "alert": _replace(_BODY, {3: ".KOK......KOEHOOEHOK", 4: ".KOK......KOEEOOEEOK"}) + _STAND,
    "sleep": ["." * 20] * 7 + [
        "..............K...K.",
        ".............KPK.KPK",
        "....KKKKKKKKKKOOKOOK",
        "...KOODOODOOKOOOOOOK",
        "..KOODOODOOKOKKOKKOK",
        "..KOODOODOOKOOOPPOOK",
        ".KOOOOOOOOOOKWWWWWK.",
        ".KOWWWOOOOOOOOOOOK..",
        "..KKKKKKKKKKKKKKK...",
    ],
}
# Two spare columns on the right (in front of the face) give the smashing paw room.
SPRITE_ART = {name: [row + ".." for row in rows] for name, rows in SPRITE_ART.items()}
CAT_ART_W = len(SPRITE_ART["stand"][0])


def _patch(rows: list[str], patches: dict[int, tuple[int, str]]) -> list[str]:
    """Overwrite pixels: {row: (start column, text)}; spaces in text keep the original."""
    out = list(rows)
    for r, (c, text) in patches.items():
        row = list(out[r])
        for i, ch in enumerate(text):
            if ch != " ":
                row[c + i] = ch
        out[r] = "".join(row)
    return out


_THREE_LEGS = [row + ".." for row in _BODY + _legs((5, 8, 12))]  # front leg is busy smashing
SPRITE_ART["smash_up"] = _patch(_THREE_LEGS, {  # paw raised high in front of the face
    0: (19, "KKK"), 1: (19, "KWK"), 2: (20, "WK"), 3: (20, "OK"), 4: (20, "OK"), 5: (20, "OK"), 6: (20, "KK"),
})
SPRITE_ART["smash_down"] = _patch(_THREE_LEGS, {  # paw slammed onto the ground ahead
    11: (19, "KK"), 12: (19, "OOK"), 13: (18, "KOOK"), 14: (18, "KWWK"), 15: (18, "KKKK"),
})

# Animation cycles per pet state (frame names from SPRITE_ART).
ANIMATIONS = {
    "idle": ["stand", "stand", "stand", "stand", "stand", "stand", "blink"],
    "walk": ["walk_a", "stand", "walk_b", "stand"],
    "sleep": ["sleep"],
    "alert": ["alert"],
    "smash": ["smash_up", "smash_up", "smash_down", "smash_down"],
}

# --- Cobra (Cobra mode opponent) ---------------------------------------------------
COBRA_PALETTE = {
    "G": "#5f9e4c",  # scales
    "L": "#c9df8a",  # belly / hood markings
    "Y": "#ffd23f",  # eyes
    "R": "#e5484d",  # tongue
}
_COBRA = [  # 10 x 13: slim hood, thin neck, small coil; ~80% of the cat's height
    "...KKKK...",
    "..KGGGGK..",
    ".KGYGGYGK.",
    ".KGGGGGGK.",
    ".KGLGGLGK.",
    "..KGGGGK..",
    "...KLLK...",
    "...KGLK...",
    "...KGLK...",
    "..KGGLGK..",
    ".KGGKKGGK.",
    "KGGLLLLGGK",
    ".KKKKKKKK.",
]


def _shift(rows: list[str], upto: int, dx: int) -> list[str]:
    """Slide rows[:upto] sideways by one pixel (dx = -1 or 1) to sway the hood."""
    moved = [("." + r[:-1]) if dx > 0 else (r[1:] + ".") for r in rows[:upto]]
    return moved + rows[upto:]


_COBRA_SHUT = _patch(_COBRA, {2: (3, "K  K")})  # eyes squeezed shut
COBRA_ART = {
    "rear": _COBRA,
    "sway": _patch(_shift(_COBRA, 9, 1), {5: (5, "RR")}),  # head leans, tongue flicks out
    "hit": ["." * 10] + _COBRA_SHUT[:3] + _COBRA_SHUT[4:],  # head squashed down a pixel, eyes shut
    "dead": ["." * 13] * 7 + [  # flat on its back, head on the left (X eyes are drawn on top)
        "..KKK........",
        ".KGGGK.......",
        "KGGGGGKKKKK..",
        "KGGGGGGLLLGKK",
        ".KRKKKKKKKKK.",
        "..R..........",
    ],
}
COBRA_DEAD_EYES = ((2.0, 8.9), (4.2, 8.9))  # art cell centres of the eyes on the "dead" frame


def render_art(rows: list[str], unit: int, palette: dict[str, str]) -> QtGui.QImage:
    """String art -> image, each art pixel a unit x unit block, with one empty cell of
    padding on every side."""
    width, height = len(rows[0]), len(rows)
    assert all(len(r) == width for r in rows), "sprite has ragged rows"
    img = QtGui.QImage((width + 2) * unit, (height + 2) * unit, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in palette:
                p.fillRect((x + 1) * unit, (y + 1) * unit, unit, unit, QtGui.QColor(palette[ch]))
    p.end()
    return img


class SpriteSheet:
    """Renders the string art into crisp pixmaps, plus mirrored copies for facing left.

    Each art pixel is drawn as a whole number of *device* pixels (scale x screen DPI
    ratio, rounded), so the cat stays sharp on 125%/150% Windows scaling instead of
    getting uneven, smeared pixels.
    """

    def __init__(self, scale: int, dpr: float) -> None:
        self.dpr = dpr
        self.unit = max(1, round(scale * dpr))
        self.frames: dict[str, dict[int, QtGui.QPixmap]] = {}
        for name, rows in SPRITE_ART.items():
            img = self._render(name, rows)
            mirrored = img.transformed(QtGui.QTransform().scale(-1, 1))
            self.frames[name] = {1: self._to_pixmap(img), -1: self._to_pixmap(mirrored)}
        size = self.frames["stand"][1].deviceIndependentSize()
        self.size = QtCore.QSize(math.ceil(size.width()), math.ceil(size.height()))

    def _to_pixmap(self, img: QtGui.QImage) -> QtGui.QPixmap:
        pix = QtGui.QPixmap.fromImage(img)
        pix.setDevicePixelRatio(self.dpr)
        return pix

    def _render(self, name: str, rows: list[str]) -> QtGui.QImage:
        return render_art(rows, self.unit, PALETTE)

    def frame_name(self, animation: str, index: int) -> str:
        cycle = ANIMATIONS[animation]
        return cycle[index % len(cycle)]

    def frame(self, animation: str, facing: int, index: int) -> QtGui.QPixmap:
        return self.frames[self.frame_name(animation, index)][facing]

    def cobra_frames(self) -> dict[str, QtGui.QPixmap]:
        """Cobra pixmaps at the cat's pixel size; "dead_r" is the dead frame mirrored."""
        palette = {**PALETTE, **COBRA_PALETTE}
        frames = {}
        for name, rows in COBRA_ART.items():
            img = render_art(rows, self.unit, palette)
            frames[name] = self._to_pixmap(img)
            if name == "dead":
                frames["dead_r"] = self._to_pixmap(img.transformed(QtGui.QTransform().scale(-1, 1)))
        return frames


# =================================================================================
# Cat window
# =================================================================================
class PetState:
    IDLE = "idle"
    WALK_LEFT = "walk_left"
    WALK_RIGHT = "walk_right"
    SLEEP = "sleep"
    ALERT = "alert"
    SMASH = "smash"
    RUN = "run"          # fetch trip: fast walk cycle


PET_WINDOW_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool                       # no taskbar button
    | Qt.WindowType.WindowDoesNotAcceptFocus   # never steal keyboard focus
)


class CatWindow(QtWidgets.QWidget):
    clicked = Signal()
    double_clicked = Signal()
    moved = Signal(QtCore.QRect)
    context_menu_requested = Signal(QtCore.QPoint)
    paw_down = Signal()  # the smashing paw just hit the ground (Cobra mode)
    trip_done = Signal()  # back home from a fetch trip, carrying the item

    TOP_MARGIN = 40     # room above the sprite for "!" and "z" overlays
    GROUND = 6          # room below the sprite for its soft ground shadow

    def __init__(self, sprites: SpriteSheet, cfg: Config, ui_font: UIFont) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.sprites = sprites
        self.cfg = cfg
        self.ui_font = ui_font
        self.setFixedSize(sprites.size.width() + 16, sprites.size.height() + self.TOP_MARGIN + self.GROUND)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.state = PetState.IDLE
        self.facing = 1          # 1 = right, -1 = left
        self.mode = None         # None = free wandering; "alert", "away", "fetch", "busy", "smash" = locked
        self._frame = 0
        self._press_global = None
        self._press_pos = None
        self._dragging = False
        self._swallow_release = False

        # Fetch trip state (see start_trip).
        self._trip_phase = None            # "out", "away", "back" or None
        self._trip_target = 0
        self._trip_home = 0
        self._trip_speed = 0.0
        self._trip_x = 0.0
        self._trip_item: QtGui.QPixmap | None = None
        self.carrying: QtGui.QPixmap | None = None  # drawn in the cat's mouth

        # Movement tick: moves the window while walking.
        self._move_timer = QtCore.QTimer(self)
        self._move_timer.setInterval(cfg.move_interval_ms)
        self._move_timer.timeout.connect(self._on_move_tick)

        # Animation tick: advances the sprite frame.
        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setInterval(cfg.frame_interval_ms)
        self._frame_timer.timeout.connect(self._on_frame_tick)

        # Behaviour timer: single-shot, re-armed with a random delay each time.
        self._behaviour_timer = QtCore.QTimer(self)
        self._behaviour_timer.setSingleShot(True)
        self._behaviour_timer.timeout.connect(self._choose_behaviour)

        # Single clicks wait one double-click interval, so a double-click doesn't also
        # fire the single-click action.
        self._click_timer = QtCore.QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QtWidgets.QApplication.doubleClickInterval())
        self._click_timer.timeout.connect(self.clicked.emit)

    # --- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        x = random.randint(avail.left(), max(avail.left(), avail.right() - self.width()))
        self.move(x, avail.bottom() - self.height() + 1)  # sit on top of the taskbar
        self.show()
        self._move_timer.start()
        self._frame_timer.start()
        self._schedule_behaviour()

    # --- state machine -------------------------------------------------------------
    def set_mode(self, mode: str | None) -> None:
        """Lock the cat into alert/away/fetch behaviour, or release it (None) to wander."""
        self.mode = mode
        self._behaviour_timer.stop()
        if mode == "alert":
            self._set_state(PetState.ALERT)
        elif mode == "away":
            self._set_state(PetState.SLEEP)
        elif mode in ("fetch", "busy"):
            self._set_state(PetState.IDLE)
        elif mode == "smash":
            self._set_state(PetState.SMASH)
        elif mode == "trip":
            self._set_state(PetState.RUN)
        else:
            self._set_state(PetState.IDLE)
            self._schedule_behaviour()

    @property
    def busy(self) -> bool:
        """Doing a task for the user (fetching, Cobra mode)."""
        return self.mode in ("fetch", "busy", "smash", "trip")

    # --- fetch trip --------------------------------------------------------------------
    def start_trip(self, item: QtGui.QPixmap | None, leg_s: float, pause_s: float) -> None:
        """Dash off the nearest screen edge, wait pause_s out of sight, run back home
        carrying `item` in the mouth, then emit trip_done. Each leg takes ~leg_s."""
        avail = self._screen_geometry()
        go_right = self.geometry().center().x() > avail.center().x()
        self.facing = 1 if go_right else -1
        self._trip_home = self.x()
        self._trip_target = avail.right() + 20 if go_right else avail.left() - self.width() - 20
        ticks = max(1.0, leg_s * 1000 / self.cfg.move_interval_ms)
        self._trip_speed = max(4.0, abs(self._trip_target - self.x()) / ticks)
        self._trip_x = float(self.x())
        self._trip_item, self._trip_pause_s = item, pause_s
        self.carrying = None
        self._trip_phase = "out"
        self.set_mode("trip")
        self._frame_timer.setInterval(max(60, self.cfg.frame_interval_ms // 2))  # legs go faster

    def _trip_return(self) -> None:
        self._trip_phase = "back"
        self.facing = -self.facing
        self._trip_target = self._trip_home
        self.carrying = self._trip_item
        self.update()

    def drop_item(self) -> None:
        self.carrying = None
        self.update()

    def wake_up(self) -> None:
        if self.mode is None and self.state == PetState.SLEEP:
            self._set_state(PetState.IDLE)
            self._schedule_behaviour()

    def _set_state(self, state: str) -> None:
        self.state = state
        self._frame = 0
        if state == PetState.WALK_LEFT:
            self.facing = -1
        elif state == PetState.WALK_RIGHT:
            self.facing = 1
        self.update()

    def _schedule_behaviour(self, factor: float = 1.0) -> None:
        delay = random.uniform(self.cfg.behaviour_min_s, self.cfg.behaviour_max_s) * factor
        self._behaviour_timer.start(int(delay * 1000))

    def _choose_behaviour(self) -> None:
        if self.mode is not None:
            return
        state = random.choices(
            [PetState.IDLE, PetState.WALK_LEFT, PetState.WALK_RIGHT, PetState.SLEEP],
            weights=[35, 25, 25, 15],
        )[0]
        self._set_state(state)
        # Naps last longer than the other behaviours.
        self._schedule_behaviour(factor=3.0 if state == PetState.SLEEP else 1.0)

    # --- timers --------------------------------------------------------------------
    def _on_move_tick(self) -> None:
        if self._trip_phase in ("out", "back") and not self._dragging:
            step = self._trip_speed * (1 if self._trip_target > self._trip_x else -1)
            if abs(self._trip_target - self._trip_x) <= self._trip_speed:
                self._trip_x = float(self._trip_target)
            else:
                self._trip_x += step
            self.move(round(self._trip_x), self.y())
            self.moved.emit(self.geometry())
            if self._trip_x == self._trip_target:
                if self._trip_phase == "out":
                    self._trip_phase = "away"  # out of sight, "finding" the item
                    QtCore.QTimer.singleShot(int(self._trip_pause_s * 1000), self._trip_return)
                else:
                    self._trip_phase = None
                    self._frame_timer.setInterval(self.cfg.frame_interval_ms)
                    self._set_state(PetState.IDLE)
                    self.trip_done.emit()
            return
        if self._dragging or self.state not in (PetState.WALK_LEFT, PetState.WALK_RIGHT):
            return
        avail = self._screen_geometry()
        x = self.x() + self.facing * self.cfg.walk_speed_px
        # Turn around at the screen edges.
        if x <= avail.left():
            x = avail.left()
            self._set_state(PetState.WALK_RIGHT)
        elif x + self.width() >= avail.right():
            x = avail.right() - self.width()
            self._set_state(PetState.WALK_LEFT)
        self.move(x, self.y())
        self.moved.emit(self.geometry())

    def _on_frame_tick(self) -> None:
        self._frame += 1
        if self.state == PetState.SMASH:
            name, prev = (self.sprites.frame_name("smash", i) for i in (self._frame, self._frame - 1))
            if name == "smash_down" and prev != "smash_down":
                self.paw_down.emit()
        self.update()

    def _screen_geometry(self) -> QtCore.QRect:
        screen = QtGui.QGuiApplication.screenAt(self.geometry().center())
        return (screen or QtGui.QGuiApplication.primaryScreen()).availableGeometry()

    # --- painting ------------------------------------------------------------------
    def _snap(self, value: float) -> float:
        """Align a logical coordinate to the device pixel grid so sprite pixels stay crisp."""
        dpr = self.sprites.dpr
        return round(value * dpr) / dpr

    def paintEvent(self, event) -> None:
        animation = {
            PetState.WALK_LEFT: "walk",
            PetState.WALK_RIGHT: "walk",
            PetState.SLEEP: "sleep",
            PetState.ALERT: "alert",
            PetState.SMASH: "smash",
            PetState.RUN: "walk",
        }.get(self.state, "idle")
        pix = self.sprites.frame(animation, self.facing, self._frame)
        w, h = self.sprites.size.width(), self.sprites.size.height()
        bounce = 6 if (self.state == PetState.ALERT and self._frame % 2) else 0
        x = self._snap((self.width() - w) / 2)
        y = self._snap(self.height() - self.GROUND - h - bounce)

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Soft ground shadow (shrinks while hopping).
        shadow_w = w * (0.55 if bounce else 0.65)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 55))
        p.drawEllipse(QtCore.QPointF(x + w / 2, self.height() - self.GROUND - 2), shadow_w / 2, 4)

        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        p.drawPixmap(QtCore.QPointF(x, y), pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Head centre sits near art column 15.5 (+1 padding cell); mirror when facing left.
        cells = CAT_ART_W + 2
        head_x = x + w * ((16.5 if self.facing == 1 else cells - 16.5) / cells)
        if self.carrying is not None:
            # Held in the mouth: just in front of the muzzle (art column ~19.5, row ~7.5).
            size = w * 0.2
            mouth_x = x + w * ((20.5 if self.facing == 1 else cells - 20.5) / cells)
            mouth_y = y + h * (8.5 / (len(SPRITE_ART["stand"]) + 2))
            p.drawPixmap(QtCore.QRectF(mouth_x - size / 2, mouth_y - size / 3, size, size),
                         self.carrying, QtCore.QRectF(self.carrying.rect()))
        if self.state == PetState.ALERT:
            p.setFont(self.ui_font.make(18, QtGui.QFont.Weight.Black))
            p.setPen(QtGui.QColor("#e5484d"))
            p.drawText(QtCore.QRectF(head_x - 10, y - 30, 20, 28), Qt.AlignmentFlag.AlignCenter, "!")
        elif self.state == PetState.SLEEP:
            # Three drifting z's, phase shifted by the frame counter.
            p.setPen(QtGui.QColor("#6c7a89"))
            base_y = y + h * 0.4
            for i in range(3):
                phase = (self._frame + i * 3) % 9
                p.setFont(self.ui_font.make(8 + phase, QtGui.QFont.Weight.Bold))
                p.drawText(QtCore.QPointF(head_x + phase * 2 - 4, base_y - phase * 4), "z")
        p.end()

    # --- mouse ---------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_pos = self.pos()
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if not self._dragging and delta.manhattanLength() > 4:
            self._dragging = True  # past the threshold this is a drag, not a click
        if self._dragging:
            self.move(self._press_pos + delta)
            self.moved.emit(self.geometry())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._swallow_release:
                self._swallow_release = False  # second release of a double-click
            elif not self._dragging:
                self._click_timer.start()
            self._press_global = None
            self._dragging = False

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._swallow_release = True
            self._press_global = None
            self.double_clicked.emit()

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(event.globalPos())


# =================================================================================
# Cobra (the Cobra-mode fight)
# =================================================================================
class CobraWindow(QtWidgets.QWidget):
    """The Cobra-mode opponent. Rises out of the ground in front of the cat, sways,
    flinches with a "POW!" on every paw strike, then keels over away from the cat with
    X eyes when the cleanup finishes, lingers, fades out and reports `finished`."""

    finished = Signal()

    TOP = 32                     # room above the cobra for the impact burst
    GROUND = CatWindow.GROUND    # same ground line as the cat
    RISE_S, FALL_S, LINGER_S, FADE_S = 0.5, 0.45, 2.5, 0.7
    HIT_S = 0.16                 # how long the flinch + burst shows after a strike
    SWAY_S = 0.36                # hood sway period while fighting
    WORDS = ("POW!", "BAM!", "WHAM!")

    def __init__(self, sprites: SpriteSheet, ui_font: UIFont) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # clicks pass through
        self.frames = sprites.cobra_frames()
        self.dpr = sprites.dpr
        self.cell = sprites.unit / sprites.dpr  # logical size of one art pixel
        size = self.frames["rear"].deviceIndependentSize()
        self._rw, self._rh = size.width(), size.height()
        # Wide enough for the cobra lying flat on either side of where it stood.
        half = self._rh + self._rw / 2 + 2 * self.cell
        self.setFixedSize(math.ceil(2 * half), math.ceil(self._rh + self.TOP + self.GROUND))
        self._font = ui_font.make(13, QtGui.QFont.Weight.Black)
        self._away = 1        # +1: the cat is on our left, so we fall to the right
        self._phase = "rise"  # rise -> fight -> fall -> dead -> fade
        self._t0 = time.monotonic()
        self._hit_at = -10.0
        self._hits = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS while on screen
        self._timer.timeout.connect(self._tick)

    # --- choreography ----------------------------------------------------------------
    def appear(self, cat_geometry: QtCore.QRect, facing: int, cat_sprite_w: float) -> None:
        """Stand just in front of the cat's face, on the same ground line."""
        self._away = facing
        cx = cat_geometry.center().x() + facing * (cat_sprite_w / 2 + self._rw / 2 - 3 * self.cell)
        self.move(round(cx - self.width() / 2), cat_geometry.bottom() - self.height() + 1)
        self._phase, self._t0 = "rise", time.monotonic()
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start()

    def hit(self) -> None:
        if self._phase in ("rise", "fight"):
            self._phase = "fight"
            self._hit_at = time.monotonic()
            self._hits += 1

    def defeat(self) -> None:
        if self._phase in ("rise", "fight"):
            self._phase, self._t0 = "fall", time.monotonic()

    def _tick(self) -> None:
        e = time.monotonic() - self._t0
        if self._phase == "rise" and e >= self.RISE_S:
            self._phase, self._t0 = "fight", time.monotonic()
        elif self._phase == "fall" and e >= self.FALL_S:
            self._phase, self._t0 = "dead", time.monotonic()
        elif self._phase == "dead" and e >= self.LINGER_S:
            self._phase, self._t0 = "fade", time.monotonic()
        elif self._phase == "fade":
            if e >= self.FADE_S:
                self._timer.stop()
                self.hide()
                self.finished.emit()
                return
            self.setWindowOpacity(1.0 - e / self.FADE_S)
        self.update()

    # --- painting --------------------------------------------------------------------
    def _snap(self, v: float) -> float:
        return round(v * self.dpr) / self.dpr

    def paintEvent(self, event) -> None:
        e = time.monotonic() - self._t0
        bottom = self.height() - self.GROUND
        cx = self.width() / 2
        standing = self._phase in ("rise", "fight")
        pivot = cx + self._away * self._rw / 2  # base corner it tips over on

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 55))
        # The shadow slides from under the standing cobra to under the fallen one as it topples.
        fallen = 0.0 if standing else 1.0
        if self._phase == "fall":
            fallen = min(1.0, e / self.FALL_S) ** 2
        shadow_cx = cx + (pivot + self._away * self._rh / 2 - cx) * fallen
        shadow_w = self._rw * 0.75 + (self._rh * 0.85 - self._rw * 0.75) * fallen
        p.drawEllipse(QtCore.QPointF(shadow_cx, bottom - 2), shadow_w / 2, 4)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)

        if self._phase == "rise":
            f = min(1.0, e / self.RISE_S)
            p.setClipRect(QtCore.QRectF(0, 0, self.width(), bottom))  # emerges from the ground
            p.drawPixmap(QtCore.QPointF(self._snap(cx - self._rw / 2), self._snap(bottom - self._rh * f)),
                         self.frames["rear"])
            p.setClipping(False)
        elif self._phase == "fight":
            hit = time.monotonic() - self._hit_at < self.HIT_S
            name = "hit" if hit else ("rear", "sway")[int(e / self.SWAY_S) % 2]
            p.drawPixmap(QtCore.QPointF(self._snap(cx - self._rw / 2), self._snap(bottom - self._rh)),
                         self.frames[name])
            if hit:
                self._paint_burst(p, cx, bottom)
        elif self._phase == "fall":
            f = min(1.0, e / self.FALL_S)
            p.save()
            p.translate(pivot, bottom)
            p.rotate(90 * f * f * self._away)  # accelerating topple away from the cat
            left = -self._rw if self._away > 0 else 0
            p.drawPixmap(QtCore.QPointF(left, -self._rh), self.frames["rear"])
            p.restore()
        else:  # dead / fade: flat on the ground where it fell, X eyes
            pix = self.frames["dead_r" if self._away > 0 else "dead"]
            dsize = pix.deviceIndependentSize()
            left = pivot if self._away > 0 else pivot - dsize.width()
            top = bottom - dsize.height()
            p.drawPixmap(QtCore.QPointF(self._snap(left), self._snap(top)), pix)
            art_w = len(COBRA_ART["dead"][0])
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(QtGui.QPen(QtGui.QColor("#2b1a14"), max(1.5, self.cell * 0.45),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            s = self.cell * 0.55
            for col, row in COBRA_DEAD_EYES:
                col = art_w - col if self._away > 0 else col  # mirrored frame
                ex, ey = left + (1 + col) * self.cell, top + (1 + row) * self.cell
                p.drawLine(QtCore.QPointF(ex - s, ey - s), QtCore.QPointF(ex + s, ey + s))
                p.drawLine(QtCore.QPointF(ex - s, ey + s), QtCore.QPointF(ex + s, ey - s))
        p.end()

    def _paint_burst(self, p: QtGui.QPainter, cx: float, bottom: float) -> None:
        """Impact star on the hood (cat's side) plus a comic-book word above it."""
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ix = cx - self._away * self._rw * 0.3   # the hood's edge facing the cat
        iy = bottom - self._rh * 0.72
        r1, r2 = self.cell * 1.3, self.cell * 2.8
        p.setPen(QtGui.QPen(QtGui.QColor("#ffd23f"), max(1.5, self.cell * 0.45),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for k in range(8):
            a = k * math.pi / 4 + 0.35
            p.drawLine(QtCore.QPointF(ix + math.cos(a) * r1, iy + math.sin(a) * r1),
                       QtCore.QPointF(ix + math.cos(a) * r2, iy + math.sin(a) * r2))
        word = self.WORDS[self._hits % len(self.WORDS)]
        width = QtGui.QFontMetricsF(self._font).horizontalAdvance(word)
        path = QtGui.QPainterPath()
        path.addText(QtCore.QPointF(ix - width / 2, bottom - self._rh - 6), self._font, word)
        p.strokePath(path, QtGui.QPen(QtGui.QColor("#1c1c1e"), 4, Qt.PenStyle.SolidLine,
                                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, QtGui.QColor("#ffffff"))


# =================================================================================
# Liquid glass base window
# =================================================================================
@dataclass(frozen=True)
class GlassStyle:
    tint: tuple[int, int, int, int]   # glass body colour over the blurred backdrop
    rim: tuple[int, int, int, int]    # inner edge highlight
    text: str
    accent: str                       # primary button fill
    accent_text: str                  # primary button label
    sheen: int = 34                   # strength of the top highlight (alpha)

    def text_color(self, alpha: int = 255) -> QtGui.QColor:
        c = QtGui.QColor(self.text)
        c.setAlpha(alpha)
        return c


class GlassWindow(QtWidgets.QWidget):
    """Frameless, always-on-top window painted as liquid glass with a tail pointing
    down at an anchor (the cat). Subclasses draw their content after paint_glass()."""

    TAIL_H = 10
    RADIUS = 18
    SHADOW = 12            # transparent margin around the glass for the drop shadow
    OVERLAP = 6            # how far the tail dips into the cat window's empty top margin
    BLUR_DOWNSCALE = 6     # backdrop is shrunk by this factor and smoothly re-enlarged = blur
    LENS = 0.04            # backdrop magnification inside the glass (fake refraction)

    STYLES = {
        # Dark grey smoked glass, white text.
        "info": GlassStyle(tint=(30, 30, 32, 222), rim=(255, 255, 255, 110), text="#f5f5f7",
                           accent="#f5f5f7", accent_text="#1c1c1e"),
        # Same glass warmed with red for break reminders and Cobra mode.
        "alert": GlassStyle(tint=(46, 24, 24, 228), rim=(255, 105, 95, 190), text="#fff3f0",
                            accent="#e5484d", accent_text="#ffffff"),
    }

    def __init__(self, flags, refresh_ms: int, activates: bool) -> None:
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activates)
        self._style = "info"
        self._tail_x = 0.0
        self._backdrop: QtGui.QImage | None = None

        # If we can hide from screen capture we can re-sample live without seeing ourselves.
        self._live_backdrop = self._exclude_from_capture()

        # Glass refresh: re-sample what's behind the window while it is visible.
        self._glass_timer = QtCore.QTimer(self)
        self._glass_timer.setInterval(refresh_ms)
        self._glass_timer.timeout.connect(self._refresh_backdrop)

    # --- backdrop ------------------------------------------------------------------
    def _exclude_from_capture(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), WDA_EXCLUDEFROMCAPTURE))
        except (AttributeError, OSError):
            return False

    def _refresh_backdrop(self) -> None:
        """Grab the screen area behind the window and keep a small (= blurred) copy."""
        if sys.platform == "darwin":
            return  # would trigger the Screen Recording permission prompt; plain tint instead
        if self.isVisible() and not self._live_backdrop:
            return  # would capture ourselves
        screen = QtGui.QGuiApplication.screenAt(self.geometry().center()) or QtGui.QGuiApplication.primaryScreen()
        origin = screen.geometry().topLeft()
        shot = screen.grabWindow(0, self.x() - origin.x(), self.y() - origin.y(), self.width(), self.height())
        if shot.isNull():
            self._backdrop = None
            return
        img = shot.toImage()
        self._backdrop = img.scaled(max(1, img.width() // self.BLUR_DOWNSCALE),
                                    max(1, img.height() // self.BLUR_DOWNSCALE),
                                    Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        self.update()

    def show_glass(self) -> None:
        if not self.isVisible():
            self._refresh_backdrop()  # sample before showing, so the first frame is already glass
        self.show()
        self.raise_()

    def showEvent(self, event) -> None:
        if self._live_backdrop:
            self._glass_timer.start()

    def hideEvent(self, event) -> None:
        self._glass_timer.stop()

    # --- geometry ------------------------------------------------------------------
    def follow(self, anchor: QtCore.QRect) -> None:
        """Position the window centred above the anchor (the cat), clamped to the screen."""
        screen = QtGui.QGuiApplication.screenAt(anchor.center()) or QtGui.QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        s = self.SHADOW
        cx = anchor.center().x()
        x = min(max(cx - self.width() // 2, avail.left() - s), avail.right() - self.width() + s + 1)
        y = max(anchor.top() + self.OVERLAP - (self.height() - s), avail.top() - s)
        self._tail_x = min(max(cx - x, s + self.RADIUS + 10), self.width() - s - self.RADIUS - 10)
        if self.isVisible() and not self._live_backdrop and self.pos() != QtCore.QPoint(x, y):
            self._backdrop = None  # stale snapshot would no longer line up; fall back to frost
        self.move(x, y)
        self.update()

    def _glass_rect(self) -> QtCore.QRectF:
        s = self.SHADOW
        return QtCore.QRectF(s, s, self.width() - 2 * s, self.height() - 2 * s - self.TAIL_H)

    def _shape(self) -> QtGui.QPainterPath:
        body = self._glass_rect()
        path = QtGui.QPainterPath()
        path.addRoundedRect(body, self.RADIUS, self.RADIUS)
        tail = QtGui.QPainterPath()
        tail.moveTo(self._tail_x - 9, body.bottom() - 2)
        tail.quadTo(self._tail_x - 2, body.bottom() + 2, self._tail_x, body.bottom() + self.TAIL_H)
        tail.quadTo(self._tail_x + 2, body.bottom() + 2, self._tail_x + 9, body.bottom() - 2)
        tail.closeSubpath()
        return path.united(tail)

    # --- painting ------------------------------------------------------------------
    def paint_glass(self, p: QtGui.QPainter) -> None:
        style = self.STYLES[self._style]
        rim_c = QtGui.QColor(*style.rim)
        shape = self._shape()
        body = self._glass_rect()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)

        # 1. Soft drop shadow, painted only outside the glass.
        outside = QtGui.QPainterPath()
        outside.addRect(QtCore.QRectF(self.rect()))
        p.setClipPath(outside.subtracted(shape))
        stroker = QtGui.QPainterPathStroker()
        for i in range(1, self.SHADOW // 2 + 1):
            stroker.setWidth(i * 4)
            p.fillPath(stroker.createStroke(shape).translated(0, 3), QtGui.QColor(0, 0, 0, 8))

        # 2. Blurred, slightly magnified backdrop = the "liquid" lens.
        p.setClipPath(shape)
        if self._backdrop is not None:
            mx, my = self.width() * self.LENS, self.height() * self.LENS
            p.drawImage(QtCore.QRectF(self.rect()).adjusted(-mx, -my, mx, my), self._backdrop)

        # 3. Tint, top sheen and a soft caustic glow near the bottom edge.
        p.fillPath(shape, QtGui.QColor(*style.tint))
        sheen = QtGui.QLinearGradient(0, body.top(), 0, body.bottom())
        sheen.setColorAt(0.0, QtGui.QColor(255, 255, 255, style.sheen))
        sheen.setColorAt(0.45, QtGui.QColor(255, 255, 255, style.sheen // 5))
        sheen.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.fillPath(shape, sheen)
        glow = QtGui.QRadialGradient(body.right() - body.width() * 0.25, body.bottom(), body.width() * 0.45)
        glow.setColorAt(0.0, QtGui.QColor(255, 255, 255, style.sheen // 2))
        glow.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.fillPath(shape, glow)

        # 4. Inner rim: brightest top-left, fading, catching light again bottom-right.
        rim_grad = QtGui.QLinearGradient(body.topLeft(), body.bottomRight())
        rim_grad.setColorAt(0.0, rim_c)
        rim_grad.setColorAt(0.5, QtGui.QColor(rim_c.red(), rim_c.green(), rim_c.blue(), 20))
        rim_grad.setColorAt(1.0, QtGui.QColor(rim_c.red(), rim_c.green(), rim_c.blue(), rim_c.alpha() // 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QBrush(rim_grad), 3))  # clipped: only the inner 1.5px shows
        p.drawPath(shape)
        p.setClipping(False)

        # 5. Hairline outer edge so the glass separates from light backgrounds.
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 40), 1))
        p.drawPath(shape)


# =================================================================================
# Speech bubble
# =================================================================================
class SpeechBubble(GlassWindow):
    clicked = Signal()      # body clicked
    dismissed = Signal()    # "x" clicked
    chosen = Signal(str)    # a choice button was clicked (its key)

    PAD = 14
    CLOSE = 16
    MAX_TEXT_W = 250
    BUTTON_H = 32
    BUTTON_GAP = 8

    def __init__(self, ui_font: UIFont, refresh_ms: int) -> None:
        super().__init__(PET_WINDOW_FLAGS, refresh_ms, activates=False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._title = ""
        self._body = ""
        self._buttons: list[tuple[str, str]] = []   # (key, label); the first one is primary
        self._button_widths: list[int] = []
        self._hover = -1
        self._title_font = ui_font.make(12, QtGui.QFont.Weight.Bold)
        self._body_font = ui_font.make(11)
        self._button_font = ui_font.make(11, QtGui.QFont.Weight.DemiBold)
        self._text_w = self._title_h = self._body_h = 0

        # Auto-hide timer for transient messages (not used for alerts).
        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    @property
    def has_buttons(self) -> bool:
        return self.isVisible() and bool(self._buttons)

    @property
    def button_keys(self) -> list[str]:
        """Keys of the last message's buttons (still set after the bubble hides)."""
        return [key for key, _ in self._buttons]

    def show_message(self, body: str, anchor: QtCore.QRect, title: str = "",
                     style: str = "info", timeout_ms: int = 0,
                     buttons: list[tuple[str, str]] | tuple = ()) -> None:
        self._title, self._body, self._style = title, body, style
        self._buttons = list(buttons)
        self._hover = -1
        self._relayout()
        self.follow(anchor)
        self.show_glass()
        if timeout_ms:
            self._hide_timer.start(timeout_ms)
        else:
            self._hide_timer.stop()

    def _relayout(self) -> None:
        flags = int(Qt.TextFlag.TextWordWrap.value)
        box = QtCore.QRect(0, 0, self.MAX_TEXT_W, 2000)
        title_r = (QtGui.QFontMetrics(self._title_font).boundingRect(box, flags, self._title)
                   if self._title else QtCore.QRect())
        body_r = (QtGui.QFontMetrics(self._body_font).boundingRect(box, flags, self._body)
                  if self._body else QtCore.QRect())
        button_fm = QtGui.QFontMetrics(self._button_font)
        self._button_widths = [button_fm.horizontalAdvance(label) + 30 for _, label in self._buttons]
        buttons_w = sum(self._button_widths) + self.BUTTON_GAP * max(0, len(self._buttons) - 1)
        self._text_w = max(title_r.width(), body_r.width(), buttons_w, 60)
        self._title_h = title_r.height() + (4 if self._title else 0)
        self._body_h = body_r.height()
        buttons_h = self.BUTTON_H + 12 if self._buttons else 0
        glass_w = self.PAD * 2 + self._text_w + self.CLOSE + 4
        glass_h = self.PAD * 2 + self._title_h + self._body_h + buttons_h + self.TAIL_H
        self.setFixedSize(glass_w + 2 * self.SHADOW, glass_h + 2 * self.SHADOW)

    def _button_rects(self) -> list[QtCore.QRectF]:
        body = self._glass_rect()
        x = body.left() + self.PAD
        y = body.top() + self.PAD + self._title_h + self._body_h + 12
        rects = []
        for w in self._button_widths:
            rects.append(QtCore.QRectF(x, y, w, self.BUTTON_H))
            x += w + self.BUTTON_GAP
        return rects

    def _close_rect(self) -> QtCore.QRectF:
        body = self._glass_rect()
        return QtCore.QRectF(body.right() - self.PAD - self.CLOSE + 6, body.top() + 8, self.CLOSE, self.CLOSE)

    def paintEvent(self, event) -> None:
        style = self.STYLES[self._style]
        body = self._glass_rect()
        p = QtGui.QPainter(self)
        self.paint_glass(p)

        # Dismiss "x" on a small glass chip.
        cr = self._close_rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 38))
        p.drawEllipse(cr)
        x_rect = cr.adjusted(5, 5, -5, -5)
        p.setPen(QtGui.QPen(style.text_color(220), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(x_rect.topLeft(), x_rect.bottomRight())
        p.drawLine(x_rect.topRight(), x_rect.bottomLeft())

        # Text.
        flags = int(Qt.TextFlag.TextWordWrap.value)
        left, top = int(body.left()) + self.PAD, int(body.top()) + self.PAD
        p.setPen(style.text_color())
        if self._title:
            p.setFont(self._title_font)
            p.drawText(QtCore.QRect(left, top, self._text_w, self._title_h), flags, self._title)
        p.setFont(self._body_font)
        p.drawText(QtCore.QRect(left, top + self._title_h, self._text_w, self._body_h), flags, self._body)

        # Choice buttons: the first is a solid primary pill, the rest are glass pills.
        p.setFont(self._button_font)
        for i, (rect, (_, label)) in enumerate(zip(self._button_rects(), self._buttons)):
            primary = i == 0
            fill = QtGui.QColor(style.accent) if primary else QtGui.QColor(255, 255, 255, 28)
            if i == self._hover:
                fill = fill.darker(112) if primary else QtGui.QColor(255, 255, 255, 60)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 0 if primary else 70), 1))
            p.setBrush(fill)
            p.drawRoundedRect(rect, self.BUTTON_H / 2, self.BUTTON_H / 2)
            p.setPen(QtGui.QColor(style.accent_text) if primary else style.text_color())
            p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
        p.end()

    def _button_at(self, pos: QtCore.QPointF) -> int:
        for i, rect in enumerate(self._button_rects()):
            if rect.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        hover = self._button_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        if not self._shape().contains(pos):
            return  # click landed in the transparent shadow margin
        button = self._button_at(pos)
        if button >= 0:
            self.hide()
            self.chosen.emit(self._buttons[button][0])
            return
        if self._close_rect().adjusted(-4, -4, 4, 4).contains(pos):
            self.hide()
            self.dismissed.emit()
            return
        if self._buttons:
            return  # a question is open: only its buttons or "x" answer it
        self.hide()
        self.clicked.emit()


# =================================================================================
# Fetch popup (search box + live results)
# =================================================================================
class FetchPopup(GlassWindow):
    fetched = Signal(str, str)   # (action, path); action is open / reveal / copy / drag
    launched = Signal(str, str)  # (app target, app name): an app result was activated
    closed = Signal()

    GLASS_W = 470
    PAD = 16
    INPUT_H = 38
    ROW_H = 46
    FOOTER_H = 28
    HINT = "↵ go to / launch app  ·  Shift+↵ copy path  ·  Ctrl+↵ open file  ·  drag out"
    APPS_ON_TOP = 3      # app matches shown above file results (all of them with "app:")

    def __init__(self, index: FileIndex, apps: AppIndex, cfg: Config, ui_font: UIFont) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        super().__init__(flags, cfg.glass_refresh_ms, activates=True)
        self.index = index
        self.apps = apps
        self._tile_font = ui_font.make(28, QtGui.QFont.Weight.Bold)
        self.cfg = cfg
        self._anchor = QtCore.QRect()
        self._results: list[Hit] = []
        self._sel = 0
        self._press_row = -1
        self._press_pos = None
        self._was_active = False
        self._name_font = ui_font.make(11, QtGui.QFont.Weight.DemiBold)
        self._path_font = ui_font.make(9.5)
        self._hint_font = ui_font.make(8.5)
        self._icon_provider = QtWidgets.QFileIconProvider()
        self._icon_cache: dict[str, QtGui.QIcon] = {}

        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setFont(ui_font.make(14))
        self.edit.setPlaceholderText("What should I fetch?  e.g. budget xlsx · ext:pdf invoice")
        self.edit.setFrame(False)
        style = self.STYLES["info"]
        self.edit.setStyleSheet(f"QLineEdit {{ background: transparent; border: none; color: {style.text}; }}")
        palette = self.edit.palette()
        palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, style.text_color(120))
        self.edit.setPalette(palette)
        self.edit.installEventFilter(self)

        # Debounce: search a moment after typing pauses, not on every keystroke.
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(cfg.search_debounce_ms)
        self._debounce.timeout.connect(self._run_search)
        self.edit.textChanged.connect(self._debounce.start)
        index.progress.connect(self._on_index_progress)
        index.changed.connect(self._on_index_progress)
        apps.changed.connect(self._on_index_progress)

    # --- open / close ----------------------------------------------------------------
    def open_at(self, anchor: QtCore.QRect) -> None:
        self._anchor = anchor
        self.edit.clear()
        self._results = []
        self._relayout()
        self.show_glass()
        force_foreground(self)
        self.edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def close_popup(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def changeEvent(self, event) -> None:
        # Clicking anywhere else closes the popup, like a menu.
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and self.isVisible():
                self._was_active = False
                self.close_popup()
        super().changeEvent(event)

    # --- search ----------------------------------------------------------------------
    def _run_search(self) -> None:
        text = self.edit.text().strip()
        limit = self.cfg.fetch_max_results
        apps = self.apps.search(text, limit if "app:" in text.lower() else self.APPS_ON_TOP) if text else []
        files = self.index.search(text, limit - len(apps)) if text else []
        self._results = apps + files
        self._sel = 0
        self._relayout()

    def _on_index_progress(self, *_) -> None:
        if self.isVisible():
            if self.edit.text().strip() and not self._results:
                self._run_search()
            self.update()

    def _status_line(self) -> str:
        n = self.index.count()
        if self.index.scanning:
            return f"Sniffing around your drives… {fmt_count(n)} items so far 🐾"
        if not self.edit.text().strip():
            return f"Type a name. I know {fmt_count(n)} files and folders and {len(self.apps.apps)} apps."
        return "Nothing found. 😿 Try fewer words, or ext:pdf, folder:, app:, !exclude"

    # --- layout ----------------------------------------------------------------------
    def _rows_top(self) -> float:
        return self._glass_rect().top() + self.PAD + self.INPUT_H + 8

    def _relayout(self) -> None:
        rows = max(1, len(self._results))
        glass_h = self.PAD + self.INPUT_H + 8 + rows * self.ROW_H + self.FOOTER_H + 6
        self.setFixedSize(self.GLASS_W + 2 * self.SHADOW, glass_h + self.TAIL_H + 2 * self.SHADOW)
        g = self._glass_rect()
        self.edit.setGeometry(int(g.left()) + self.PAD, int(g.top()) + self.PAD - 4,
                              int(g.width()) - 2 * self.PAD, self.INPUT_H)
        self.follow(self._anchor)

    def _icon_for(self, hit: Hit) -> QtGui.QIcon:
        """The real shell icon. Cached per extension, except types whose icon is per file."""
        if hit.kind == "app":
            key = "app:" + hit.path.lower()
            if key not in self._icon_cache:
                self._icon_cache[key] = (self._icon_provider.icon(QtCore.QFileInfo(hit.path))
                                         if not hit.path.startswith("shell:") else self._letter_tile(hit.name))
            return self._icon_cache[key]
        ext = os.path.splitext(hit.name)[1].lower()
        key = "<dir>" if hit.is_dir else (hit.path.lower() if ext in (".exe", ".lnk", ".ico", ".url") else ext)
        if key not in self._icon_cache:
            self._icon_cache[key] = self._icon_provider.icon(QtCore.QFileInfo(hit.path))
        return self._icon_cache[key]

    def _letter_tile(self, name: str) -> QtGui.QIcon:
        """Icon for Store apps without a shortcut: a coloured rounded tile with the first letter."""
        pix = QtGui.QPixmap(64, 64)
        pix.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor.fromHsl(sum(map(ord, name)) * 37 % 360, 140, 110))
        p.drawRoundedRect(QtCore.QRectF(4, 4, 56, 56), 14, 14)
        p.setPen(QtGui.QColor("#ffffff"))
        p.setFont(self._tile_font)
        p.drawText(QtCore.QRectF(4, 4, 56, 56), int(Qt.AlignmentFlag.AlignCenter), name[:1].upper())
        p.end()
        return QtGui.QIcon(pix)

    def _row_rect(self, r: int) -> QtCore.QRectF:
        g = self._glass_rect()
        return QtCore.QRectF(g.left() + 8, self._rows_top() + r * self.ROW_H, g.width() - 16, self.ROW_H - 4)

    def _row_at(self, pos: QtCore.QPointF) -> int:
        for r in range(len(self._results)):
            if self._row_rect(r).contains(pos):
                return r
        return -1

    # --- painting --------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        style = self.STYLES[self._style]
        fg, dim = style.text_color(), style.text_color(140)
        g = self._glass_rect()
        p = QtGui.QPainter(self)
        self.paint_glass(p)

        # Divider under the input.
        y = self._rows_top() - 5
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 70), 1))
        p.drawLine(QtCore.QPointF(g.left() + self.PAD, y), QtCore.QPointF(g.right() - self.PAD, y))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1))
        p.drawLine(QtCore.QPointF(g.left() + self.PAD, y + 1), QtCore.QPointF(g.right() - self.PAD, y + 1))

        if not self._results:
            p.setFont(self._path_font)
            p.setPen(dim)
            p.drawText(self._row_rect(0).adjusted(10, 0, -10, 0),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._status_line())

        name_fm = QtGui.QFontMetrics(self._name_font)
        path_fm = QtGui.QFontMetrics(self._path_font)
        for r, hit in enumerate(self._results):
            row = self._row_rect(r)
            if r == self._sel:
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 55), 1))
                p.setBrush(QtGui.QColor(255, 255, 255, 30))
                p.drawRoundedRect(row, 11, 11)
            self._icon_for(hit).paint(p, QtCore.QRect(int(row.left()) + 9, int(row.top()) + 8, 26, 26))
            p.setPen(fg)
            text_x = row.left() + 42
            text_w = int(row.width() - 52)
            p.setFont(self._name_font)
            p.drawText(QtCore.QRectF(text_x, row.top() + 4, text_w, 20), int(Qt.AlignmentFlag.AlignVCenter),
                       name_fm.elidedText(hit.name, Qt.TextElideMode.ElideRight, text_w))
            p.setFont(self._path_font)
            p.setPen(dim)
            if hit.kind == "app":
                folder = "Application"
            else:
                folder = hit.path.rsplit(SEP, 1)[0] if SEP in hit.path else hit.path
            p.drawText(QtCore.QRectF(text_x, row.top() + 22, text_w, 18), int(Qt.AlignmentFlag.AlignVCenter),
                       path_fm.elidedText(folder, Qt.TextElideMode.ElideMiddle, text_w))

        # Footer: key hints + index size.
        foot = QtCore.QRectF(g.left() + self.PAD, g.bottom() - self.FOOTER_H - 2,
                             g.width() - 2 * self.PAD, self.FOOTER_H)
        p.setFont(self._hint_font)
        p.setPen(dim)
        p.drawText(foot, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self.HINT)
        if self._results:
            p.drawText(foot, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       f"{fmt_count(self.index.count())} indexed")
        p.end()

    # --- keyboard / mouse ------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QtCore.QEvent.Type.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._results:
                step = 1 if key == Qt.Key.Key_Down else -1
                self._sel = (self._sel + step) % len(self._results)
                self.update()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._debounce.isActive():  # typed fast and hit Enter: search first
                    self._debounce.stop()
                    self._run_search()
                if mods & Qt.KeyboardModifier.ControlModifier:
                    self._activate("open")
                elif mods & Qt.KeyboardModifier.ShiftModifier:
                    self._activate("copy")
                else:
                    self._activate("reveal")  # default: go to the location, don't open the file
                return True
            if key == Qt.Key.Key_Escape:
                self.close_popup()
                return True
        return super().eventFilter(obj, event)

    def _activate(self, action: str, row: int | None = None) -> None:
        row = self._sel if row is None else row
        if not 0 <= row < len(self._results):
            return
        hit = self._results[row]
        self.hide()
        if hit.kind == "app" and action != "copy":
            self.launched.emit(hit.path, hit.name)  # apps launch on any activation
        else:
            self.fetched.emit(action, hit.path)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_row = self._row_at(event.position())
            self._press_pos = event.position()
            if self._press_row >= 0:
                self._sel = self._press_row
                self.update()

    def mouseMoveEvent(self, event) -> None:
        # Drag a result out of the popup = drop the real file into Explorer, mail, chat...
        if self._press_row < 0 or self._press_pos is None:
            return
        if (event.position() - self._press_pos).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return
        path = self._results[self._press_row].path
        self._press_row = -1
        if path.startswith("shell:"):
            return  # Store apps have no file to drag
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(path)])
        mime.setText(path)
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        self._was_active = False  # the drag takes focus away; don't treat it as "clicked elsewhere"
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.LinkAction)
        self.hide()
        self.fetched.emit("drag", path)

    def mouseReleaseEvent(self, event) -> None:
        self._press_row = -1
        self._press_pos = None

    def mouseDoubleClickEvent(self, event) -> None:
        row = self._row_at(event.position())
        if row >= 0:
            self._activate("reveal", row)


# =================================================================================
# App controller - wires tracker, cat, bubble and fetch together
# =================================================================================
class DesktopPet(QtCore.QObject):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.ui_font = ui_font = load_ui_font()
        dpr = QtGui.QGuiApplication.primaryScreen().devicePixelRatio()
        self.sprites = SpriteSheet(cfg.pixel_scale, dpr)
        self.cat = CatWindow(self.sprites, cfg, ui_font)
        self.bubble = SpeechBubble(ui_font, cfg.glass_refresh_ms)
        self.tracker = ActivityTracker(cfg)
        self.index = FileIndex(cfg)
        self.apps = AppIndex(cfg.apps_refresh_min)
        self.fetch = FetchPopup(self.index, self.apps, cfg, ui_font)
        self.cobra = CobraCleaner(cfg)
        self._cobra_plan: CleanupPlan | None = None   # plan waiting for the user's "Strike!"
        self._arena: CobraWindow | None = None        # the cobra on screen during a fight
        self._fight_t0 = 0.0
        self._last_reminder = None

        # Daily routine (water / lunch / home).
        self._now = datetime.now                       # swappable in tests
        self._later: dict[str, datetime] = {}          # routine -> when its "Later" is up
        self._state_path = self.index.cache_path.parent / "state.json"
        self._state = self._load_state()               # e.g. {"lunch": "2026-11-04"} = already shown today

        # While alerting, swap in a fresh reminder every alert_rotate_s.
        self._rotate_timer = QtCore.QTimer(self)
        self._rotate_timer.setInterval(cfg.alert_rotate_s * 1000)
        self._rotate_timer.timeout.connect(self._show_alert_bubble)

        # After "x" on an alert, bring the bubble back after snooze_s.
        self._snooze_timer = QtCore.QTimer(self)
        self._snooze_timer.setSingleShot(True)
        self._snooze_timer.timeout.connect(self._resume_alert)

        # Occasional small talk while wandering.
        self._chatter_timer = QtCore.QTimer(self)
        self._chatter_timer.setSingleShot(True)
        self._chatter_timer.timeout.connect(self._chatter)

        self.menu = self._build_menu()
        self.tray = self._build_tray()

        self.tracker.alert_triggered.connect(self._enter_alert)
        self.tracker.user_away.connect(self._on_away)
        self.tracker.user_returned.connect(self._on_returned)
        self.tracker.ticked.connect(self._on_tick)
        self.cat.clicked.connect(self._on_cat_clicked)
        self.cat.double_clicked.connect(self._ask_action)
        self.cat.moved.connect(self._on_cat_moved)
        self.cat.context_menu_requested.connect(self.menu.popup)
        self.bubble.clicked.connect(self._on_bubble_clicked)
        self.bubble.dismissed.connect(self._on_bubble_dismissed)
        self.bubble.chosen.connect(self._on_choice)
        self.fetch.fetched.connect(self._on_fetched)
        self.fetch.launched.connect(self._on_launched)
        self.fetch.closed.connect(self._release_cat)
        self.cobra.planned.connect(self._on_cobra_planned)
        self.cobra.finished.connect(self._on_cobra_finished)
        self.cat.paw_down.connect(lambda: self._arena and self._arena.hit())
        self.cat.trip_done.connect(self._on_trip_done)
        self._trip_delivery: tuple[str, str] | None = None  # (action, path) waiting for the cat

    def start(self) -> None:
        self.cat.start()
        self.tracker.start()
        self.index.start()
        self.apps.start()
        self._schedule_chatter()
        cfg = self.cfg
        self._say(f"💧 water every {cfg.water_every_s // 60} min\n"
                  f"🚶 get up and walk: no sitting over {fmt_span(cfg.alert_after_s)}\n"
                  f"🍱 lunch at {fmt_clock(cfg.lunch_time)}\n"
                  f"🏠 home at {fmt_clock(cfg.home_time)}\n\nDouble-click me for tricks.",
                  8000, title="Hi! I'll nudge you: 🐾")

    def wake_from_launch(self) -> None:
        """Another launch (e.g. the taskbar pin) while running: just say hello."""
        if not self.cat.busy:
            self._say("I'm already here! 🐾\nDouble-click me for tricks.", 3500)

    # --- menu / tray ---------------------------------------------------------------
    def _build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu()
        self._status_action = menu.addAction("Session: 0s")
        self._status_action.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Fetch a file…", self._open_fetch)
        menu.addAction("Cobra mode…", self._start_cobra)
        menu.addAction("Reset break timer", self._reset_timer)
        preview = menu.addMenu("Preview reminders")
        preview.addAction(f"🚶 Walk (after {fmt_span(self.cfg.alert_after_s)} sitting)", self.tracker.force_alert)
        preview.addAction("💧 Water", lambda: self._show_routine("water"))
        preview.addAction("🍱 Lunch", lambda: self._show_routine("lunch"))
        preview.addAction("🏠 Home", lambda: self._show_routine("home"))
        preview.addAction("🐍 Cobra fight (practice, deletes nothing)", self._practice_fight)
        menu.addAction("Rebuild file + app index", lambda: (self.index.rescan(), self.apps.refresh()))
        menu.addSeparator()
        menu.addAction("Quit", QtWidgets.QApplication.quit)
        return menu

    def _build_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(self.sprites.frames["stand"][1]), self)
        tray.setToolTip("Desktop Cat")
        tray.setContextMenu(self.menu)
        tray.show()
        return tray

    # --- helpers -------------------------------------------------------------------
    def _say(self, text: str, timeout_ms: int = 4000, **kwargs) -> None:
        self.bubble.show_message(text, self.cat.geometry(), timeout_ms=timeout_ms, **kwargs)

    def _schedule_chatter(self) -> None:
        delay = random.randint(self.cfg.chatter_min_s, self.cfg.chatter_max_s)
        self._chatter_timer.start(delay * 1000)

    def _chatter(self) -> None:
        if self.cat.mode is None and not self.bubble.isVisible():
            line = random.choice(CHATTER + [f"You've been at it for {fmt_duration(self.tracker.session_s)}."])
            self._say(line, 3500)
        self._schedule_chatter()

    def _status_text(self) -> str:
        left = max(0, self.cfg.alert_after_s - self.tracker.session_s)
        return (f"Active for {fmt_duration(self.tracker.session_s)}.\n"
                f"Time to get up in {fmt_duration(left)}.")

    def _occupy_cat(self, mode: str) -> None:
        """Hold the cat still for a task ("fetch" or "busy") and pause alert reminders."""
        self._rotate_timer.stop()  # don't pop reminders over the task
        self.cat.set_mode(mode)

    def _release_cat(self) -> None:
        """Task over: back to the pending alert, or to wandering."""
        if not self.cat.busy:
            return
        if self.tracker.alert_active:
            self.cat.set_mode("alert")
            self._rotate_timer.start()
        else:
            self.cat.set_mode(None)

    # --- double-click: choose a trick -------------------------------------------------
    def _ask_action(self) -> None:
        if self.cobra.busy:
            self._say("Still hissing at your caches… 🐍", 2500)
            return
        self._say("", 9000, title="Meow? What should I do? 🐾",
                  buttons=[("fetch", "🔎 Fetch a file"), ("cobra", "🐍 Cobra mode")])

    def _on_choice(self, key: str) -> None:
        if key == "fetch":
            self._open_fetch()
        elif key == "cobra":
            self._start_cobra()
        elif key == "cobra_go":
            self._strike()
        elif key == "cobra_cancel":
            self._cancel_cobra()
        elif key in ROUTINE_REPLIES or key.endswith("_later"):
            self._routine_answer(key)

    # --- daily routine: water, lunch, home ---------------------------------------
    def _check_routine(self) -> None:
        """Runs every tracker tick. Waits while you're away, the cat is mid-task or a
        question bubble is open, so reminders never stack on top of each other."""
        if self.tracker.away or self.cat.busy or self.bubble.has_buttons:
            return
        now = self._now()
        for key, due in list(self._later.items()):
            if now >= due:
                del self._later[key]
                self._show_routine(key)
                return
        if now.weekday() in self.cfg.routine_days:
            today = now.date().isoformat()
            for key, hhmm in (("lunch", self.cfg.lunch_time), ("home", self.cfg.home_time)):
                h, m = (int(x) for x in hhmm.split(":"))
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if (self._state.get(key) != today and key not in self._later
                        and at <= now < at + timedelta(minutes=self.cfg.clock_window_min)):
                    self._state[key] = today  # once a day, even across restarts
                    self._save_state()
                    self._show_routine(key)
                    return
        if (self.tracker.water_s >= self.cfg.water_every_s and "water" not in self._later
                and not self.tracker.alert_active):  # the walk alert comes first
            self.tracker.water_s = 0.0
            self._show_routine("water")

    def _show_routine(self, key: str) -> None:
        title, body, buttons = ROUTINE[key]
        hhmm = {"lunch": self.cfg.lunch_time, "home": self.cfg.home_time}.get(key)
        if key != "water":
            buttons = [(k, f"{self.cfg.clock_later_s // 60} more min" if k.endswith("_later") else label)
                       for k, label in buttons]
        text = body.format(time=fmt_clock(hhmm) if hhmm else "", every=self.cfg.water_every_s // 60)
        self.bubble.show_message(text, self.cat.geometry(),
                                 title=title, buttons=buttons,
                                 timeout_ms=60_000 if key == "water" else 0)  # lunch/home wait for an answer

    def _routine_answer(self, key: str) -> None:
        if key.endswith("_later"):
            routine = key[:-len("_later")]
            delay = self.cfg.water_later_s if routine == "water" else self.cfg.clock_later_s
            self._later[routine] = self._now() + timedelta(seconds=delay)
            when = f"{delay // 60} min" if delay >= 60 else f"{delay} s"
            self._say(f"Okay, I'll ask again in {when}. 🐾", 2500)
        else:
            self._say(ROUTINE_REPLIES[key], 3000)

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            pass

    # --- fetch flow ----------------------------------------------------------------
    def _open_fetch(self) -> None:
        self.bubble.hide()
        self._occupy_cat("fetch")
        self.fetch.open_at(self.cat.geometry())

    def _on_fetched(self, action: str, path: str) -> None:
        if action in ("reveal", "open") and self.cfg.fetch_trip:
            # The cat runs off-screen and comes back with the item before it opens.
            name = path.rstrip(SEP).rsplit(SEP, 1)[-1] or path
            icon = self.fetch._icon_for(Hit(path, name, os.path.isdir(path))).pixmap(32, 32)
            self._trip_delivery = (action, path)
            self.bubble.hide()
            self.cat.start_trip(icon, self.cfg.fetch_trip_leg_s, self.cfg.fetch_trip_pause_s)
            return
        self._deliver(action, path)

    def _on_launched(self, target: str, name: str) -> None:
        """App results launch straight away (no fetch trip)."""
        try:
            launch_app(target)
            msg = f"Launching {name}… 🚀"
        except OSError:
            msg = f"I couldn't start {name}. 😿"
        self._release_cat()
        self._say(msg, 3000)

    def _on_trip_done(self) -> None:
        if self._trip_delivery is None:
            return
        action, path = self._trip_delivery
        self._trip_delivery = None
        self._deliver(action, path)
        QtCore.QTimer.singleShot(1500, self.cat.drop_item)  # hold it up for a moment, then drop

    def _deliver(self, action: str, path: str) -> None:
        name = path.rstrip(SEP).rsplit(SEP, 1)[-1] or path
        wrapped = path.replace(SEP, SEP + "\u200b")  # zero-width spaces let long paths wrap
        try:
            if action == "open":
                open_path(path)
                msg = f"Here you go! 🐾\nOpened {name}"
            elif action == "reveal":
                if os.path.isdir(path):
                    open_path(path)  # a folder: go inside it
                    msg = f"Found it! 🐾\nHere's the {name} folder."
                else:
                    reveal_path(path)  # a file: its folder, with the file selected (not opened)
                    msg = f"Found it! 🐾\n{name} is selected in its folder."
            elif action == "copy":
                QtGui.QGuiApplication.clipboard().setText(path)
                msg = f"Path copied! 🐾\n{wrapped}"
            else:
                msg = f"Delivered {name}! 🐾"
        except OSError:
            msg = f"I couldn't open {name}. 😿"
        self._release_cat()
        self._say(msg, 4500)

    # --- cobra flow: size up -> confirm -> strike -----------------------------------
    def _start_cobra(self) -> None:
        if self.cobra.busy:
            return
        self._occupy_cat("busy")
        self._say("Sizing up your Recycle Bin and caches… 🐍", 0)
        self.cobra.plan_async()

    def _on_cobra_planned(self, plan: CleanupPlan) -> None:
        if plan.empty:
            self._release_cat()
            self._say("Nothing to strike: your Recycle Bin and caches are already clean. 🐍✨", 4000)
            return
        self._cobra_plan = plan
        lines = []
        if plan.recycle_items:
            lines.append(f"• Recycle Bin: {fmt_bytes(plan.recycle_bytes)} ({plan.recycle_items:,} items)")
        if plan.temp_items:
            lines.append(f"• Temp & cache files: {fmt_bytes(plan.temp_bytes)} ({len(plan.temp_items):,} items)")
        lines.append(f"\nOnly cache files untouched for {self.cfg.cobra_min_age_h} h+ are removed.\n"
                     "This deletes them for good.")
        self._say("\n".join(lines), 0, title="🐍 Cobra mode", style="alert",
                  buttons=[("cobra_go", "🐍 Strike!"), ("cobra_cancel", "Not now")])

    def _strike(self) -> None:
        plan, self._cobra_plan = self._cobra_plan, None
        if plan is None:
            return
        self._start_fight()
        self.cobra.execute_async(plan)

    def _cancel_cobra(self) -> None:
        self._cobra_plan = None
        self._release_cat()
        self._say("Okay, no striking today. 🐍", 2500)

    def _on_cobra_finished(self, freed: int, skipped: int) -> None:
        msg = f"Strike complete! 🐍\nFreed {fmt_bytes(freed)}."
        if skipped:
            msg += f"\n{skipped:,} items were in use, so I left them alone."
        self._finish_fight(msg)

    # --- the fight: cobra appears, cat smashes it, cobra keels over ------------------
    def _start_fight(self) -> None:
        if self._arena is not None:
            return
        self.bubble.hide()
        avail = self.cat._screen_geometry()
        room_right = avail.right() - self.cat.geometry().right()
        facing = 1 if room_right > self.sprites.size.width() * 1.5 else -1  # face where there's room
        self.cat.facing = facing
        self.cat.set_mode("smash")
        self._arena = CobraWindow(self.sprites, self.ui_font)
        self._arena.finished.connect(self._arena_gone)
        self._arena.appear(self.cat.geometry(), facing, self.sprites.size.width())
        self._fight_t0 = time.monotonic()

    def _finish_fight(self, message: str) -> None:
        wait = self.cfg.cobra_fight_min_s - (time.monotonic() - self._fight_t0)
        if wait > 0:  # quick cleanups still get a proper fight
            QtCore.QTimer.singleShot(int(wait * 1000), lambda: self._finish_fight(message))
            return
        if self._arena is not None:
            self._arena.defeat()
        self._release_cat()
        self._say(message, 6000)

    def _arena_gone(self) -> None:
        self._arena = None

    def _practice_fight(self) -> None:
        if self._arena is not None or self.cat.busy:
            return
        self._occupy_cat("busy")
        self._start_fight()
        self._finish_fight("Practice round! 🐍\nNothing was deleted.")

    # --- alert flow ----------------------------------------------------------------
    def _enter_alert(self) -> None:
        if self.cat.busy:
            return  # finishing the task; _release_cat switches to the alert afterwards
        self.cat.set_mode("alert")
        self._show_alert_bubble()
        self._rotate_timer.start()

    def _show_alert_bubble(self) -> None:
        if not self.tracker.alert_active or self.bubble.has_buttons:
            return  # don't replace an open question
        choices = [r for r in WALK_REMINDERS if r != self._last_reminder]
        self._last_reminder = random.choice(choices)
        self.bubble.show_message(
            self._last_reminder + "\n\n(Click me when you're back.)",
            self.cat.geometry(),
            title=f"🪑 Sitting for {fmt_span(self.tracker.session_s)} straight!",
            style="alert",
        )

    def _resume_alert(self) -> None:
        self._show_alert_bubble()
        self._rotate_timer.start()

    def _acknowledge(self) -> None:
        self.tracker.acknowledge()
        self._rotate_timer.stop()
        self._snooze_timer.stop()
        self.cat.set_mode(None)
        self._say(f"Thanks! Next walk in {fmt_span(self.cfg.alert_after_s)}. 😺", 3500)

    def _reset_timer(self) -> None:
        if self.tracker.alert_active:
            self._acknowledge()
        else:
            self.tracker.acknowledge()
            self._say("Break timer reset.", 2500)

    # --- signal handlers -----------------------------------------------------------
    def _on_tick(self, session_s: float, idle_s: float) -> None:
        state = "away" if self.tracker.away else f"active {fmt_duration(session_s)}"
        water_left = max(0.0, self.cfg.water_every_s - self.tracker.water_s)
        self._status_action.setText(f"Session: {state}  ·  water in {fmt_duration(water_left)}  ·  "
                                    f"{fmt_count(self.index.count())} files indexed")
        if self.tray:
            self.tray.setToolTip(f"Desktop Cat - {state}")
        self._check_routine()

    def _on_cat_moved(self, geometry: QtCore.QRect) -> None:
        if self.bubble.isVisible():
            self.bubble.follow(geometry)

    def _on_cat_clicked(self) -> None:
        if self.cat.busy:
            return
        if self.tracker.alert_active:
            self._acknowledge()
        else:
            self.cat.wake_up()
            self._say("Purr ♥\n" + self._status_text(), 4000)

    def _on_bubble_clicked(self) -> None:
        if self.tracker.alert_active and not self.cat.busy:
            self._acknowledge()

    def _on_bubble_dismissed(self) -> None:
        if self._cobra_plan is not None:
            self._cancel_cobra()  # "x" on the confirmation = not now
        elif self.bubble.button_keys:
            pass  # "x" on any other question (trick menu, water/lunch/home) just closes it
        elif self.tracker.alert_active and not self.cat.busy:
            # "x" snoozes the reminder; the timer is NOT reset and the cat keeps waiting.
            self._rotate_timer.stop()
            self._snooze_timer.start(self.cfg.snooze_s * 1000)

    def _on_away(self) -> None:
        # The tracker already cleared any alert: time away counts as the break.
        self._rotate_timer.stop()
        self._snooze_timer.stop()
        if self.cat.busy:
            return
        self.bubble.hide()
        self.cat.set_mode("away")

    def _on_returned(self) -> None:
        if self.cat.busy:
            return
        self.cat.set_mode(None)
        if self.cfg.idle_policy == "pause":
            self._say(f"Welcome back! Resuming at {fmt_duration(self.tracker.session_s)}.", 3500)
        else:
            self._say("Welcome back! Fresh session started. 🐾", 3500)


# =================================================================================
# Single instance + shortcut install
# =================================================================================
INSTANCE_KEY = f"DesktopCat-{os.environ.get('USERNAME') or os.environ.get('USER', 'user')}"


def notify_running_instance() -> bool:
    """If the cat is already running, poke it and return True."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"wake")
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def listen_for_launches(pet: DesktopPet) -> QtNetwork.QLocalServer:
    server = QtNetwork.QLocalServer(pet)
    QtNetwork.QLocalServer.removeServer(INSTANCE_KEY)  # clear a stale socket after a crash
    server.listen(INSTANCE_KEY)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), pet.wake_from_launch()))
    return server


def app_icon_image(size: int) -> QtGui.QImage:
    """The dedicated icon: the standing cat, centred on a transparent square."""
    unit = size // (CAT_ART_W + 2)
    art = render_art(SPRITE_ART["stand"], unit, PALETTE)
    canvas = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(canvas)
    # +unit: the two spare paw columns on the right would otherwise push the cat off-centre.
    p.drawImage((size - art.width()) // 2 + unit, (size - art.height()) // 2, art)
    p.end()
    return canvas


def write_icns(path: Path) -> None:
    """macOS icon file: a header, then one (type, length, PNG data) entry per size."""
    body = b""
    for code, size in ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512)):
        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not app_icon_image(size).save(buffer, "PNG"):
            raise OSError("could not encode the icon")
        png = bytes(data)
        body += code + struct.pack(">I", len(png) + 8) + png
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def install_shortcuts() -> list[str]:
    """Create the 'Desktop Cat' launcher(s) with the cat icon; returns what was created."""
    if sys.platform == "win32":
        return _install_windows_shortcuts()
    if sys.platform == "darwin":
        return _install_mac_app()
    raise OSError("shortcuts are only supported on Windows and macOS")


def _install_windows_shortcuts() -> list[str]:
    """desktop_cat.ico + shortcuts on the Desktop and in the Start menu (they launch with
    pythonw, so no console window appears)."""
    script = Path(__file__).resolve()
    icon = script.with_name("desktop_cat.ico")
    if not app_icon_image(256).save(str(icon), "ICO"):
        raise OSError("could not write the icon")

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)

    def q(value) -> str:  # PowerShell single-quoted literal
        return "'" + str(value).replace("'", "''") + "'"

    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
        "$path = Join-Path $d 'Desktop Cat.lnk'; $l = $s.CreateShortcut($path); "
        f"$l.TargetPath = {q(target)}; $l.Arguments = {q(chr(34) + str(script) + chr(34))}; "
        f"$l.WorkingDirectory = {q(script.parent)}; $l.IconLocation = {q(str(icon) + ',0')}; "
        "$l.Description = 'Desktop Cat: break reminders, file fetching and Cobra mode'; $l.Save(); $path }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_mac_app(app: Path, python: str, script: str) -> None:
    """A minimal .app bundle: Info.plist, the icon, and a launcher that runs the script."""
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    write_icns(contents / "Resources" / "desktop_cat.icns")
    launcher = contents / "MacOS" / "DesktopCat"
    launcher.write_text(f'#!/bin/bash\nexec "{python}" "{script}"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    (contents / "Info.plist").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Desktop Cat</string>
    <key>CFBundleDisplayName</key><string>Desktop Cat</string>
    <key>CFBundleIdentifier</key><string>local.desktopcat.app</string>
    <key>CFBundleExecutable</key><string>DesktopCat</string>
    <key>CFBundleIconFile</key><string>desktop_cat</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
""", encoding="utf-8", newline="\n")


def _install_mac_app() -> list[str]:
    app = Path.home() / "Desktop" / "Desktop Cat.app"
    build_mac_app(app, sys.executable, str(Path(__file__).resolve()))
    subprocess.run(["touch", str(app)], check=False)  # makes Finder pick up the new icon
    return [str(app)]


# =================================================================================
# Entry point
# =================================================================================
def parse_args(argv: list[str]) -> tuple[Config, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Desktop cat: break reminders, file fetching and Cobra mode.")
    parser.add_argument("--demo", action="store_true", help="fast timers for testing (30 s alert, 20 s idle)")
    parser.add_argument("--alert-minutes", type=float, help="continuous activity before the alert (default 120)")
    parser.add_argument("--idle-minutes", type=float, help="inactivity that counts as away (default 5)")
    parser.add_argument("--idle-policy", choices=["reset", "pause"], help="what being away does to the session")
    parser.add_argument("--scale", type=int, help="sprite pixel size (default 5)")
    parser.add_argument("--index", action="append", metavar="PATH",
                        help="index this folder instead of all fixed drives (repeatable)")
    parser.add_argument("--water-minutes", type=float, help="sip-of-water reminder interval (default 30)")
    parser.add_argument("--lunch", metavar="HH:MM", help="lunch reminder time, 24 h (default 13:00)")
    parser.add_argument("--home", metavar="HH:MM", help="go-home reminder time, 24 h (default 17:45)")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="create the Desktop Cat launcher with its icon (Windows: Desktop + Start menu; macOS: Desktop app)")
    args = parser.parse_args(argv)

    cfg = Config.demo() if args.demo else Config()
    if args.alert_minutes:
        cfg.alert_after_s = int(args.alert_minutes * 60)
    if args.idle_minutes:
        cfg.idle_threshold_s = int(args.idle_minutes * 60)
    if args.idle_policy:
        cfg.idle_policy = args.idle_policy
    if args.scale:
        cfg.pixel_scale = max(1, args.scale)
    if args.index:
        cfg.index_roots = tuple(str(Path(p).resolve()) for p in args.index)
    if args.water_minutes:
        cfg.water_every_s = int(args.water_minutes * 60)
    for name in ("lunch", "home"):
        value = getattr(args, name)
        if value:
            fmt_clock(value)  # validates HH:MM (raises ValueError otherwise)
            setattr(cfg, f"{name}_time", value)
    return cfg, args


def main() -> int:
    cfg, args = parse_args(sys.argv[1:])
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C in the terminal quit
    app = QtWidgets.QApplication(sys.argv)
    if args.install_shortcut:
        for path in install_shortcuts():
            print("Created", path)
        if sys.platform == "win32":
            print("To pin it: right-click the Desktop Cat shortcut > Show more options > Pin to taskbar.")
        return 0
    if notify_running_instance():
        return 0  # already running: the existing cat says hello instead
    app.setQuitOnLastWindowClosed(False)          # bubble/menu closing must not quit
    pet = DesktopPet(cfg)
    server = listen_for_launches(pet)  # noqa: F841 - keep a reference so it stays alive
    pet.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
