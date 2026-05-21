#!/usr/bin/env python3
"""
Shimekami - Phase 2
Python Shimeji desktop mascot for Linux Mint (X11).

Controls:
  Left-click drag  - grab and throw mascot
  Right-click      - context menu
"""

import sys
import math
import random
import queue
import threading
import json
import urllib.request
import unicodedata
from pathlib import Path
from enum import Enum, auto
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QObject
from PySide6.QtGui import QPixmap, QPainter, QTransform, QCursor, QFont, QColor, QPen, QBrush, QPolygon
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QTextEdit, QVBoxLayout

# ── Paths ─────────────────────────────────────────────────────────────────────

SPRITE_DIR = Path(__file__).parent / "SHIMEJI-TEMPLATE" / "img" / "Shimeji"

# ── Physics / timing ──────────────────────────────────────────────────────────

TICK_MS       = 40       # ~25 fps
GRAVITY       = 0.8
MAX_FALL_SPD  = 25.0
DRAG          = 0.03
WALK_SPEED    = 2.0
CLIMB_SPEED   = 1.5      # px / tick on walls
CEIL_SPEED    = 1.5      # px / tick on ceiling
FLOOR_MARGIN  = 6
WEIGHT        = 1.0      # future: throw mass scaling

MAX_MASCOTS   = 5
MERGE_DIST    = 60       # px anchor-to-anchor before merge roll
WIN_THROW_VX  = 28.0    # px / tick for thrown windows

# Ceiling sprite uses a different anchor point (ImageAnchor="64,48" in actions.xml)
CEIL_ANC_Y    = 48
WALL_INSET    = -196  # extra inset from screen edge where wall-cling anchor sits

JUMP_VY       = -50.0  # initial upward speed for spontaneous jumps (px/tick)
JUMP_RANGE_Y  = 200    # max upward distance to a jump target (px)
JUMP_RANGE_X  = 400    # max horizontal distance to a jump target (px)

CHAT_DIST     = 160    # max anchor distance to start a conversation
BUBBLE_MS     = 1600   # ms each speech bubble is visible
BUBBLE_GAP_MS = 300    # ms pause between turns

# All named codepoints in the standard emoji unicode ranges.
_EMOJI_RANGES = [
    (0x1F600, 0x1F64F),   # Emoticons
    (0x1F300, 0x1F5FF),   # Misc symbols & pictographs
    (0x1F680, 0x1F6FF),   # Transport & map
    (0x1F900, 0x1F9FF),   # Supplemental symbols & pictographs
    (0x1FA70, 0x1FAFF),   # Symbols & pictographs extended-A
    (0x2600,  0x26FF),    # Misc symbols
    (0x2700,  0x27BF),    # Dingbats
]
CHAT_EMOJIS: list[str] = [
    chr(cp)
    for start, end in _EMOJI_RANGES
    for cp in range(start, end + 1)
    if unicodedata.name(chr(cp), "")
]

# ── LLM settings ─────────────────────────────────────────────────────────────

LLM_MODEL         = "qwen2.5:1.5b"        # ollama model tag
HOST_IP           = "192.168.0.161"
LLM_ENDPOINT      = f"http://{HOST_IP}:11434/api/chat"
PERSONALITIES_DIR = Path(__file__).parent / "personalities"

# ── LLM memory + personality helpers ─────────────────────────────────────────

@dataclass
class MemoryEvent:
    role:    str   # "said" | "heard" | "partner_action" | "location"
    content: str
    partner: str = ""


class MemoryBuffer:
    def __init__(self):
        self._events: deque[MemoryEvent] = deque(maxlen=10)

    def record(self, role: str, content: str, partner: str = ""):
        self._events.append(MemoryEvent(role, content, partner))

    def to_text(self) -> str:
        if not self._events:
            return "(no memory yet)"
        lines = []
        for ev in self._events:
            if ev.role == "said":
                lines.append(f"I said: {ev.content}")
            elif ev.role == "heard":
                lines.append(f"Heard {ev.content} from {ev.partner}")
            elif ev.role == "partner_action":
                lines.append(f"Partner was: {ev.content}")
            elif ev.role == "location":
                lines.append(f"I was at: {ev.content}")
        return "\n".join(lines)


def get_personality(sprite_dir: Path) -> str:
    char_name = sprite_dir.name.lower()
    p = PERSONALITIES_DIR / f"{char_name}.txt"
    if not p.exists():
        p = PERSONALITIES_DIR / "default.txt"
    try:
        return p.read_text().strip()
    except Exception:
        return "You are a Shimeji desktop mascot. Reply ONLY with 1-3 emojis."


def _location_str(mascot: "Mascot") -> str:
    if mascot._state in (State.CEIL_CLING, State.CEIL_WALK):
        return "ceiling"
    if mascot._state in (State.WALL_CLING, State.WALL_CLIMB):
        return "wall"
    if Mascot._valid_win:
        for w in Mascot._valid_win:
            if abs(mascot._ay - w.rect.top()) < 12:
                return f"top of '{w.name}'"
    return "floor"


class LLMController:
    """
    Singleton that dispatches ollama API calls on a background thread and
    delivers results back to the Qt main thread via a polled result queue.
    """
    _instance: "LLMController | None" = None

    @classmethod
    def get(cls) -> "LLMController":
        if cls._instance is None:
            cls._instance = LLMController()
        return cls._instance

    def __init__(self):
        self._req_q:    queue.Queue = queue.Queue()
        self._res_q:    queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self._poll = QTimer()
        self._poll.timeout.connect(self._drain)
        self._poll.start(100)

    def request(self, personality: str, memory_text: str,
                partner_name: str, partner_last_emoji: str, callback):
        self._req_q.put((personality, memory_text, partner_name,
                          partner_last_emoji, callback))

    def _worker(self):
        while True:
            personality, memory_text, partner_name, partner_last, cb = self._req_q.get()
            try:
                result = self._call_ollama(personality, memory_text,
                                           partner_name, partner_last)
            except Exception:
                result = None
            self._res_q.put((cb, result))

    def _call_ollama(self, personality: str, memory_text: str,
                     partner_name: str, partner_last: str) -> str | None:
        prompt = (
            f"[Recent memory]\n{memory_text}\n\n"
            f"[Situation]\n"
            f"Your conversation partner ({partner_name}) just said: {partner_last}\n"
            "Respond in character. Reply ONLY with 1-3 emojis separated by spaces, nothing else."
        )
        body = json.dumps({
            "model":    LLM_MODEL,
            "messages": [
                {"role": "system", "content": personality},
                {"role": "user",   "content": prompt},
            ],
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            LLM_ENDPOINT, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        return data["message"]["content"].strip() or None

    def _drain(self):
        while not self._res_q.empty():
            cb, result = self._res_q.get_nowait()
            cb(result)


# ── Animation sequences ───────────────────────────────────────────────────────

ANIM: dict[str, list[tuple[str, int]]] = {
    "idle": [("stand.png", 1)],
    "walk": [
        ("stand.png",  6), ("walk_a.png", 6),
        ("stand.png",  6), ("walk_b.png", 6),
    ],
    "run": [
        ("stand.png",  2), ("walk_a.png", 2),
        ("stand.png",  2), ("walk_b.png", 2),
    ],
    "jump":  [("jump.png", 1)],
    "fall":  [("fall.png", 1)],
    "land":  [("land_crouch.png", 5), ("land_roll.png", 5)],
    "sit":   [("sit.png", 1)],
    "sit_ledge": [
        ("sit_lookup.png",    20), ("sit_spinhead_a.png", 20), ("sit_spinhead_d.png", 20),
        ("sit_spinhead_b.png",20), ("sit_spinhead_e.png", 20), ("sit_spinhead_c.png", 20),
        ("sit_spinhead_f.png",20), ("sit.png",            20),
    ],
    "sit_spinhead": [
        ("ledge_sit_up.png", 8), ("ledge_sit.png",      8),
        ("ledge_dangle_a.png",20), ("ledge_dangle_b.png", 20),
        ("ledge_dangle_a.png",20), ("ledge_dangle_b.png", 20),
        ("ledge_dangle_a.png",20), ("ledge_dangle_b.png", 20),
    ],
    "sprawl":[("sprawl.png", 1)],

    # Wall
    "wall_cling": [("wall_cling.png", 1)],
    "wall_climb": [
        ("wall_climb_b.png", 4), ("wall_cling.png",   4), ("wall_climb_a.png", 4),
        ("wall_cling.png",   4), ("wall_cling.png",   4), ("wall_climb_a.png", 4),
        ("wall_cling.png",   4), ("wall_climb_b.png", 4),
    ],

    # Ceiling
    "ceil_cling": [("ceiling_cling.png", 1)],
    "ceil_walk":  [
        ("ceiling_walk_b.png", 4), ("ceiling_cling.png",  4), ("ceiling_walk_a.png", 4),
        ("ceiling_walk_a.png", 4), ("ceiling_cling.png",  4), ("ceiling_walk_b.png", 4),
    ],

    # Window carry / throw
    "carry": [
        ("carry_walk_a.png", 6), ("carry_walk_b.png", 6),
        ("carry_walk_a.png", 6), ("carry_idle.png",   6),
    ],
    "throw_win": [("throw_window.png", 35)],

    # Clone / merge animation
    "clone": [
        ("clone_a.png", 5), ("clone_b.png", 2), ("clone_c.png", 2),
        ("clone_d.png", 5), ("clone_e.png", 20),
    ],
    "clone2": [
        ("breed_a.png", 5), ("breed_b.png", 2), ("breed_c.png", 2),
        ("breed_d.png", 20)
    ],

    # Crawl
    "sprawl": [("sprawl", 1)],
    "crawl": [
        ("crawl", 10), ("sprawl", 10)
    ]
}

# ── States ────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE       = auto()
    WALK       = auto()
    RUN        = auto()
    FALL       = auto()
    LAND       = auto()
    SIT        = auto()
    DRAG       = auto()
    THROWN     = auto()
    JUMP       = auto()
    FOLLOWING  = auto()    # walking toward a social target
    CHATTING   = auto()    # mid-emoji conversation
    WALL_CLING = auto()
    WALL_CLIMB = auto()
    CEIL_CLING = auto()
    CEIL_WALK  = auto()
    CARRY        = auto()
    THROW_WIN    = auto()
    CLONING      = auto()
    MERGING      = auto()
    SIT_SPINHEAD = auto()
    SIT_LEDGE    = auto()
    WIN_CLIMB    = auto()
    SPRAWL       = auto()
    CRAWL        = auto()

# ── Window info ───────────────────────────────────────────────────────────────

class WindowInfo:
    def __init__(self, wid: int, rect: QRect, name: str = ""):
        self.wid  = wid
        self.rect = rect
        self.name = name

# ── X11 environment ───────────────────────────────────────────────────────────

class Environment:
    """
    Tracks open desktop windows and can move them via X11.
    Refreshes the window list at a shared rate regardless of mascot count.
    """

    _refresh_cd   = 0
    _REFRESH_RATE = 20   # ticks between X11 polls

    def __init__(self):
        self._windows:  list[WindowInfo] = []
        self._own_wids: set[int]         = set()
        self._xd   = None
        self._X    = None
        self._root = None
        self._atom_cl = None
        self._atom_fe = None
        try:
            from Xlib import display as xdsp, X
            self._X    = X
            self._xd   = xdsp.Display()
            self._root = self._xd.screen().root
            self._atom_cl         = self._xd.intern_atom("_NET_CLIENT_LIST")
            self._atom_cls        = self._xd.intern_atom("_NET_CLIENT_LIST_STACKING")
            self._atom_fe         = self._xd.intern_atom("_NET_FRAME_EXTENTS")
            self._atom_mr         = self._xd.intern_atom("_NET_MOVERESIZE_WINDOW")
            self._atom_ws         = self._xd.intern_atom("_NET_WM_STATE")
            self._atom_hidden     = self._xd.intern_atom("_NET_WM_STATE_HIDDEN")
            self._atom_fullscreen = self._xd.intern_atom("_NET_WM_STATE_FULLSCREEN")
            self._atom_wmname     = self._xd.intern_atom("_NET_WM_NAME")
            self._atom_cd         = self._xd.intern_atom("_NET_CURRENT_DESKTOP")
            self._atom_wd         = self._xd.intern_atom("_NET_WM_DESKTOP")
        except Exception:
            pass

    def register_wid(self, wid: int):
        self._own_wids.add(wid)

    def tick(self):
        Environment._refresh_cd -= 1
        if Environment._refresh_cd <= 0:
            Environment._refresh_cd = Environment._REFRESH_RATE
            self._refresh()

    def _refresh(self):
        if not self._xd:
            return
        try:
            root_geom = self._root.get_geometry()
            cd_prop = self._root.get_full_property(
                self._atom_cd, self._X.AnyPropertyType
            )
            current_desktop = int(cd_prop.value[0]) if (cd_prop and cd_prop.value) else -1

            # Use stacking order (bottom → top) so we can detect occlusion.
            # Fall back to mapping order if the atom is unsupported.
            stk_prop = self._root.get_full_property(
                self._atom_cls, self._X.AnyPropertyType
            ) or self._root.get_full_property(
                self._atom_cl, self._X.AnyPropertyType
            )
            if not stk_prop:
                return
            stacked_wids = [int(w) for w in stk_prop.value]

            # Single pass: collect valid window info in stacking order.
            # Track fullscreen windows as (stack_idx, rect) pairs so we can do
            # per-monitor occlusion: a window is hidden only when a *higher-stacked*
            # fullscreen window geometrically overlaps it on the same monitor.
            # Pure stacking-order exclusion wrongly drops windows on other monitors.
            fullscreen_info: list[tuple[int, QRect]] = []  # (stack_idx, rect)
            candidates: list[tuple[int, WindowInfo]] = []

            for idx, wid in enumerate(stacked_wids):
                if wid in self._own_wids:
                    continue
                try:
                    w = self._xd.create_resource_object("window", wid)

                    attrs = w.get_attributes()
                    if attrs.map_state != self._X.IsViewable:
                        continue

                    ws_prop = w.get_full_property(
                        self._atom_ws, self._X.AnyPropertyType
                    )
                    states = set(ws_prop.value) if (ws_prop and ws_prop.value) else set()

                    if self._atom_fullscreen in states:
                        try:
                            geom_fs = w.get_geometry()
                            tr_fs   = self._root.translate_coords(w, 0, 0)
                            fullscreen_info.append(
                                (idx, QRect(tr_fs.x, tr_fs.y, geom_fs.width, geom_fs.height))
                            )
                        except Exception:
                            pass
                        continue
                    if self._atom_hidden in states:
                        continue

                    if current_desktop >= 0:
                        try:
                            wd_prop = w.get_full_property(
                                self._atom_wd, self._X.AnyPropertyType
                            )
                            if wd_prop and wd_prop.value:
                                win_desktop = int(wd_prop.value[0]) & 0xFFFFFFFF
                                if win_desktop != 0xFFFFFFFF and win_desktop != current_desktop:
                                    continue
                        except Exception:
                            pass

                    geom = w.get_geometry()
                    tr   = self._root.translate_coords(w, 0, 0)
                    rect = QRect(tr.x, tr.y, geom.width, geom.height)
                    if rect.width() <= 80 or rect.height() <= 80:
                        continue
                    if (rect.right() < -64 or rect.bottom() < -64
                            or rect.left() > root_geom.width + 64
                            or rect.top() > root_geom.height + 64):
                        continue

                    name = ""
                    try:
                        np = w.get_full_property(
                            self._atom_wmname, self._X.AnyPropertyType
                        )
                        if np and np.value:
                            raw = np.value
                            name = (raw.decode("utf-8", errors="replace")
                                    if isinstance(raw, bytes) else str(raw)
                                    ).strip("\x00")
                    except Exception:
                        pass
                    if not name:
                        try:
                            name = str(w.get_wm_name() or "")
                        except Exception:
                            pass

                    candidates.append((idx, WindowInfo(wid, rect, name)))
                except Exception:
                    pass

            # A window is occluded when a *higher-stacked* fullscreen window
            # geometrically intersects it (same monitor).  Using stacking idx
            # means popups that appear *above* the fullscreen stay visible.
            def _occluded(candidate_idx: int, wi_rect: QRect) -> bool:
                for fs_idx, fs_rect in fullscreen_info:
                    if fs_idx > candidate_idx and fs_rect.intersects(wi_rect):
                        return True
                return False

            self._windows = [
                wi for idx, wi in candidates if not _occluded(idx, wi.rect)
            ]
            self._xd.flush()
        except Exception:
            pass

    def get_window_rect(self, wid: int) -> "QRect | None":
        """Fetch the current position of a single window without a full refresh."""
        if not self._xd:
            return None
        try:
            w  = self._xd.create_resource_object("window", wid)
            g  = w.get_geometry()
            tr = self._root.translate_coords(w, 0, 0)
            return QRect(tr.x, tr.y, g.width, g.height)
        except Exception:
            return None

    def move_window(self, wid: int, x: int, y: int):
        if not self._xd:
            return
        try:
            from Xlib.protocol.event import ClientMessage as XClientMessage
            w    = self._xd.create_resource_object("window", wid)
            # flags: bit8=x present, bit9=y present; gravity=0 (use WM default)
            flags = 0x100 | 0x200
            ev = XClientMessage(
                window=w,
                client_type=self._atom_mr,
                data=(32, [flags, x, y, 0, 0]),
            )
            mask = self._X.SubstructureNotifyMask | self._X.SubstructureRedirectMask
            self._root.send_event(ev, event_mask=mask)
            self._xd.flush()
        except Exception:
            pass

    @property
    def windows(self) -> list[WindowInfo]:
        return self._windows

# ── Sprite cache ──────────────────────────────────────────────────────────────

class SpriteCache:
    def __init__(self, sprite_dir: Path):
        self._dir  = sprite_dir
        self._orig: dict[str, QPixmap] = {}
        self._flip: dict[str, QPixmap] = {}

    def get(self, name: str, flipped: bool = False) -> QPixmap:
        cache = self._flip if flipped else self._orig
        if name not in cache:
            pm = QPixmap(str(self._dir / name))
            if pm.isNull():
                pm = QPixmap(128, 128)
                pm.fill(Qt.GlobalColor.transparent)
            if flipped:
                pm = pm.transformed(QTransform().scale(-1, 1))
            cache[name] = pm
        return cache[name]

    def size(self, name: str) -> tuple[int, int]:
        pm = self.get(name)
        return pm.width(), pm.height()

# ── Animation player ──────────────────────────────────────────────────────────

class AnimPlayer:
    def __init__(self):
        self._frames: list[tuple[str, int]] = []
        self._idx  = 0
        self._tick = 0
        self.done  = False

    def play(self, frames: list[tuple[str, int]]):
        self._frames = frames
        self._idx  = 0
        self._tick = 0
        self.done  = False

    def current(self) -> str:
        if not self._frames:
            return "stand.png"
        return self._frames[self._idx][0]

    def advance(self):
        if not self._frames:
            return
        _, dur = self._frames[self._idx]
        self._tick += 1
        if self._tick >= dur:
            self._tick = 0
            self._idx += 1
            if self._idx >= len(self._frames):
                self._idx = 0
                self.done = True

# ── Mascot ────────────────────────────────────────────────────────────────────

class Mascot(QWidget):
    # Shared class state
    _all:          list["Mascot"] = []
    _env:          Environment    = None   # set by main() or launcher
    _cloning_on:   bool                                                                                                                            = True
    _win_throw_on: bool           = False
    _aware_same:   bool           = False   # social behaviours between same-character mascots
    _aware_other:  bool           = True  # social behaviours between different characters
    _keep_alive:   bool           = False  # set True by launcher to prevent app quit
    _class_screen: "QRect | None" = None   # set by main(); launcher uses per-instance screen
    _valid_win:    "list | None"  = None   # populated each tick by _valid_windows()
    _llm_enabled:  bool           = False  # enable ollama LLM for conversations

    def __init__(
        self,
        sprites:  SpriteCache,
        screen:   QRect,
        start_x:  int | None = None,
    ):
        super().__init__()
        self._sprites = sprites
        self._screen  = screen
        self._anim    = AnimPlayer()

        sw, sh = sprites.size("stand.png")
        self._sw    = sw
        self._sh    = sh
        self._anc_x = sw // 2   # 64
        self._anc_y = sh        # 128  (floor / wall anchor)

        sx = start_x if start_x is not None else random.randint(
            screen.left() + sw, screen.right() - sw
        )
        self._ax = float(sx)
        self._ay = float(screen.bottom())

        self._vx = 0.0
        self._vy = 0.0

        self._facing_right = False

        # Wall / ceiling sub-state
        self._wall_side:     str   = "L"   # "L" or "R"
        self._wall_climb_dir: int  = -1    # -1 = up, +1 = down

        # Carried window state
        self._carry_win:   WindowInfo | None = None
        self._carry_ticks: int               = 0
        self._throw_dir:   int               = 1    # +1 = right, -1 = left
        self._win_fly_x:   float             = 0.0
        self._win_fly_y:   float             = 0.0

        # Window-climb state
        self._climb_win: WindowInfo | None = None

        # Floor-window tracking (to move with a window the mascot stands on)
        self._floor_win_wid:       int | None  = None
        self._floor_win_prev_rect: QRect | None = None

        # Drag / throw tracking
        self._dragging      = False
        self._drag_offset   = QPoint()
        self._last_cursor   = QPoint()
        self._last_event_ms = 0.0
        self._throw_vx      = 0.0
        self._throw_vy      = 0.0

        self._state         = State.FALL
        self._state_ticks   = 0
        self._social_target:  "Mascot | None"        = None
        self._conversation:   "Conversation | None"  = None
        self._memory:         MemoryBuffer            = MemoryBuffer()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint    |
            Qt.WindowType.WindowStaysOnTopHint   |
            Qt.WindowType.Tool                   |
            Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(sw, sh)
        self._sync_window()
        self.show()

        Mascot._all.append(self)
        if Mascot._env:
            Mascot._env.register_wid(int(self.winId()))

        self._enter(State.FALL)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(TICK_MS)

    # ── Surface helpers ───────────────────────────────────────────────────────

    def _floor_y(self) -> float:
        """Nearest floor surface below the anchor (screen bottom or window top)."""
        fy = float(self._screen.bottom())
        screen_top = float(self._screen.top())
        if Mascot._env and Mascot._valid_win:
            for w in Mascot._valid_win:
                # Clamp to screen top — compositor shadows can push rect.top()
                # into negative territory; treat those as starting at screen top.
                top = max(float(w.rect.top()), screen_top)
                if (w.rect.left() - self._anc_x <= self._ax <= w.rect.right() + self._anc_x
                        and self._ay <= top + FLOOR_MARGIN
                        and top < fy):
                    fy = top
        return fy

    def _on_floor(self) -> bool:
        return self._ay >= self._floor_y() - FLOOR_MARGIN

    def _on_ceiling(self) -> bool:
        # Sprite top (ay - anc_y) has hit the screen top.
        return self._ay - self._anc_y <= float(self._screen.top()) + FLOOR_MARGIN

    def _left_x(self) -> float:
        return float(self._screen.left() + self._anc_x)

    def _right_x(self) -> float:
        return float(self._screen.right() - self._anc_x)

    def _wall_left_x(self) -> float:
        """Anchor x when clinging/climbing the left wall (inset from edge)."""
        return float(self._screen.left() + self._anc_x + WALL_INSET)

    def _wall_right_x(self) -> float:
        """Anchor x when clinging/climbing the right wall (inset from edge)."""
        return float(self._screen.right() - self._anc_x - WALL_INSET)

    def _clamp_x(self):
        lo, hi = self._left_x(), self._right_x()
        if self._ax < lo:
            self._ax = lo
            self._vx =  abs(self._vx) * 0.4
        elif self._ax > hi:
            self._ax = hi
            self._vx = -abs(self._vx) * 0.4

    def _sync_window(self, ceil_mode: bool = False):
        """Move Qt window so the anchor sits at (_ax, _ay)."""
        eff_anc_y = CEIL_ANC_Y if ceil_mode else self._anc_y
        self.move(int(self._ax) - self._anc_x, int(self._ay) - eff_anc_y)

    # ── Drag lean frame ───────────────────────────────────────────────────────

    def _drag_frame(self) -> str:
        vx = self._throw_vx
        if vx >  8: return "drag_lean_left_3.png"
        if vx >  4: return "drag_lean_left_2.png"
        if vx >  1: return "drag_lean_left_1.png"
        if vx < -8: return "drag_lean_right_3.png"
        if vx < -4: return "drag_lean_right_2.png"
        if vx < -1: return "drag_lean_right_1.png"
        return "stand.png"

    # ── State transitions ─────────────────────────────────────────────────────

    def uni_tick(self):
        ''' Set state_ticks between 1-60 seconds for the current state. '''
        self._state_ticks = random.randint(1, 60) * (1000 // TICK_MS)

    def _enter(self, state: State):
        self._state    = state
        self._anim.done = False

        if state == State.IDLE:
            self._anim.play(ANIM["idle"])
            self._state_ticks = random.randint(2, 5) * (1000 // TICK_MS)
            self._vx = 0.0; self._vy = 0.0

        elif state == State.WALK or state == State.RUN:
            self._anim.play(ANIM["walk" if state == State.WALK else "run"]) 
            self.uni_tick()
            near_L = self._ax < self._screen.left()  + self._sw * 2
            near_R = self._ax > self._screen.right() - self._sw * 2
            if   near_L and not near_R: self._facing_right = True
            elif near_R and not near_L: self._facing_right = False
            else:                       self._facing_right = random.choice([True, False])
            self._vx = WALK_SPEED if self._facing_right else -WALK_SPEED
            self._vx *= 4 if state == State.RUN else 1
            self._vy = 0.0


        elif state == State.FALL:
            self._anim.play(ANIM["fall"])

        elif state == State.LAND:
            self._anim.play(ANIM["land"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.SIT:
            self._anim.play(ANIM["sit"])
            self.uni_tick()
            self._vx = 0.0; self._vy = 0.0

        elif state == State.DRAG:
            self._vx = 0.0; self._vy = 0.0

        elif state == State.THROWN:
            self._anim.play(ANIM["fall"])

        elif state == State.JUMP:
            self._anim.play(ANIM["jump"])
            # _vx and _vy are already set by _jump_to() before entering this state.

        elif state == State.WALL_CLING:
            self._anim.play(ANIM["wall_cling"])
            self._state_ticks = random.randint(100,300)
            self._vx = 0.0; self._vy = 0.0
            ax = self._wall_left_x() if self._wall_side == "L" else self._wall_right_x()
            # Clamp to own screen so we don't drift onto an adjacent monitor.
            self._ax = max(float(self._screen.left()), min(float(self._screen.right()), ax))

        elif state == State.WALL_CLIMB:
            self._anim.play(ANIM["wall_climb"])
            self._state_ticks = random.randint(200,500)
            self._vx = 0.0
            ax = self._wall_left_x() if self._wall_side == "L" else self._wall_right_x()
            self._ax = max(float(self._screen.left()), min(float(self._screen.right()), ax))

        elif state == State.CEIL_CLING:
            self._anim.play(ANIM["ceil_cling"])
            self._state_ticks = random.randint(2, 5) * (1000 // TICK_MS)
            self._vx = 0.0; self._vy = 0.0
            self._ay = float(self._screen.top()) + CEIL_ANC_Y

        elif state == State.CEIL_WALK:
            self._anim.play(ANIM["ceil_walk"])
            self.uni_tick()
            # Pick a random target on the ceiling
            tx = float(random.randint(
                int(self._left_x()), int(self._right_x())
            ))
            self._facing_right = tx > self._ax
            self._vx = CEIL_SPEED if self._facing_right else -CEIL_SPEED
            self._vy = 0.0

        elif state == State.CARRY:
            self._anim.play(ANIM["carry"])
            self.uni_tick()
            self._throw_dir   = 1 if self._facing_right else -1
            self._vx = WALK_SPEED * 0.75 * self._throw_dir
            self._vy = 0.0

        elif state == State.THROW_WIN:
            self._anim.play(ANIM["throw_win"])
            self._vx = 0.0; self._vy = 0.0
            if self._carry_win:
                self._win_fly_x = float(self._carry_win.rect.x())
                self._win_fly_y = float(self._carry_win.rect.y())

        elif state == State.SIT_SPINHEAD:
            self._anim.play(ANIM["sit_spinhead"])
            self.uni_tick()
            self._vx = 0.0; self._vy = 0.0

        elif state == State.SIT_LEDGE:
            self._anim.play(ANIM["sit_ledge"])
            self.uni_tick()
            self._vx = 0.0; self._vy = 0.0

        elif state == State.WIN_CLIMB:
            self._anim.play(ANIM["wall_climb"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.CLONING:
            self._anim.play(ANIM["clone"] if random.random() < 0.9 else ANIM["clone2"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.MERGING:
            self._anim.play(ANIM["clone"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.FOLLOWING:
            self._anim.play(ANIM["run"])
            self._state_ticks = random.randint(3, 8) * (1000 // TICK_MS)
            self._vy = 0.0

        elif state == State.CHATTING:
            self._anim.play(ANIM["idle"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.SPRAWL:
            self._anim.play(ANIM["sprawl"])
            self._vx = 0.0; self._vy = 0.0

        elif state == State.CRAWL:
            self._anim.play(ANIM["crawl"])
            self.uni_tick()
            near_L = self._ax < self._screen.left()  + self._sw * 2
            near_R = self._ax > self._screen.right() - self._sw * 2
            if   near_L and not near_R: self._facing_right = True
            elif near_R and not near_L: self._facing_right = False
            else:                       self._facing_right = random.choice([True, False])
            self._vx = WALK_SPEED if self._facing_right else -WALK_SPEED
            self._vx *= 0.25
            self._vy = 0.0


    # ── Main tick ─────────────────────────────────────────────────────────────

    def _tick(self):
        # Only the first mascot drives the shared env refresh.
        if Mascot._env and Mascot._all and Mascot._all[0] is self:
            Mascot._env.tick()
            Mascot._valid_windows()       # set valid windows

        if self._dragging:
            self.update()
            return

        # Ride a moving floor window before any state decisions
        _floor_states = (State.IDLE, State.WALK, State.RUN, State.SIT,
                         State.SIT_SPINHEAD, State.SIT_LEDGE,
                         State.LAND, State.CLONING, State.MERGING)
        if self._state in _floor_states:
            self._track_floor_window()
        else:
            self._floor_win_wid       = None
            self._floor_win_prev_rect = None

        ceil_mode = self._state in (State.CEIL_CLING, State.CEIL_WALK)

        # ── per-state logic ────────────────────────────────────────────────────

        if self._state == State.IDLE:
            # Fall if no longer on a surface (e.g. window underneath moved away)
            if not self._on_floor():
                self._enter(State.FALL)
            else:
                self._state_ticks -= 1
                if self._state_ticks <= 0:
                    self._idle_transition()
                elif random.random() < 0.002:
                    if random.random() < 0.5:
                        self._try_jump()
                    else:
                        self._try_win_climb()


        elif self._state == State.WALK or self._state == State.RUN or self._state == State.CRAWL:
            self._ax += self._vx
            near_L = self._ax <= self._left_x()
            near_R = self._ax >= self._right_x()
            if near_L or near_R:
                side = "L" if near_L else "R"
                self._ax = self._left_x() if near_L else self._right_x()
                if random.random() < 0.35:
                    # Climb instead of bounce
                    self._wall_side      = side
                    self._wall_climb_dir = -1
                    self._enter(State.WALL_CLING)
                else:
                    self._facing_right = not near_L
                    mods = {State.WALK:1, State.RUN:2, State.CRAWL:0.25}
                    speed = WALK_SPEED * mods[self._state]
                    self._vx = speed if self._facing_right else -speed

            self._anim.advance()
            # Fall if walked off a window edge
            if not self._on_floor():
                self._enter(State.FALL)
            else:
                self._state_ticks -= 1
                if self._state_ticks <= 0:
                    self._enter(random.choices(
                        [State.IDLE, State.SIT, State.WALK, State.SPRAWL],
                        weights=[40, 35, 25, 2],
                    )[0])
                # Occasionally grab a nearby window or jump to a surface
                if Mascot._win_throw_on and self._carry_win is None:
                    self._maybe_grab_window()
                if random.random() < 0.001:
                    if random.random():
                        self._try_jump()
                    else:
                        self._try_win_climb()

        elif self._state == State.FOLLOWING:
            t = self._social_target
            if (t is None or t not in Mascot._all
                    or not self._on_floor()
                    or self._state_ticks <= 0):
                self._social_target = None
                self._enter(State.IDLE)
            else:
                self._state_ticks -= 1
                dx = t._ax - self._ax
                dist = abs(dx)
                if dist < MERGE_DIST:
                    self._social_target = None
                    self._social_arrive(t)
                else:
                    self._facing_right = dx > 0
                    self._vx = (WALK_SPEED if self._facing_right else -WALK_SPEED) * 1.5
                    self._ax += self._vx
                    self._anim.advance()
                    if not self._on_floor():
                        self._enter(State.FALL)

        elif self._state == State.CHATTING:
            if not self._on_floor() or self._conversation is None:
                self._end_conversation()

        elif self._state == State.FALL:
            self._vy = min(self._vy + GRAVITY, MAX_FALL_SPD)
            self._ay += self._vy
            floor = self._floor_y()
            if self._ay >= floor - FLOOR_MARGIN:
                self._ay = floor
                self._enter(State.LAND)

        elif self._state == State.THROWN:
            self._vx *= (1.0 - DRAG)
            self._vy  = min(self._vy + GRAVITY, MAX_FALL_SPD)
            self._ax += self._vx
            self._ay += self._vy
            # self._clamp_x()

            # throw to the wall
            if self._ax < self._left_x():
                self._ax = self._left_x()
                self._wall_side      = "L"
                self._enter(State.WALL_CLING)

            elif self._ax > self._right_x():
                self._ax = self._right_x()
                self._wall_side      = "R"
                self._enter(State.WALL_CLING)

            
            # Ceiling bounce
            if self._on_ceiling():
                self._ay = float(self._screen.top()) + self._anc_y
                self._vy = abs(self._vy) * 0.3
            floor = self._floor_y()
            if self._ay >= floor - FLOOR_MARGIN:
                self._ay = floor
                self._enter(State.LAND)

        elif self._state == State.JUMP:
            self._vy = min(self._vy + GRAVITY, MAX_FALL_SPD)
            self._ax += self._vx
            self._ay += self._vy
            # Wall contact: cling at whatever height the mascot reaches.
            if self._ax <= self._left_x():
                self._ax = self._left_x()
                self._wall_side = "L"
                self._wall_climb_dir = -1
                self._vx = 0.0; self._vy = 0.0
                self._enter(State.WALL_CLING)
            elif self._ax >= self._right_x():
                self._ax = self._right_x()
                self._wall_side = "R"
                self._wall_climb_dir = -1
                self._vx = 0.0; self._vy = 0.0
                self._enter(State.WALL_CLING)
            # Grab ceiling on the way up
            elif self._on_ceiling():
                self._ay = float(self._screen.top()) + CEIL_ANC_Y
                self._vx = 0.0; self._vy = 0.0
                self._wall_side = "L" if self._ax < self._screen.center().x() else "R"
                self._enter(State.CEIL_CLING)
            else:
                floor = self._floor_y()
                if self._ay >= floor - FLOOR_MARGIN:
                    self._ay = floor
                    self._enter(State.LAND)

        elif self._state == State.LAND:
            self._anim.advance()
            if self._anim.done:
                self._enter(State.IDLE)

        elif self._state == State.SIT:
            if not self._on_floor():
                self._enter(State.FALL)
            else:
                self._state_ticks -= 1
                if self._state_ticks <= 0:
                    self._enter(random.choices(
                        [State.IDLE, State.WALK], weights=[50, 50]
                    )[0])

        elif self._state in (State.SIT_SPINHEAD, State.SIT_LEDGE):
            if not self._on_floor():
                self._enter(State.FALL)
            else:
                self._state_ticks -= 1
                self._anim.advance()
                if self._state_ticks <= 0:
                    self._enter(random.choices(
                        [State.SIT, State.SIT_SPINHEAD, State.SIT_LEDGE], weights=[20, 70, 10]
                    )[0])

        elif self._state == State.WALL_CLING:
            self._state_ticks -= 1
            if self._state_ticks <= 0:
                if random.random() < 0.9:
                    self._enter(State.WALL_CLIMB)
                else:
                    self._enter(State.FALL)   # give up, drop off

        elif self._state == State.WALL_CLIMB:
            self._ay += CLIMB_SPEED * self._wall_climb_dir
            self._state_ticks -= 1
            self._anim.advance()

            # interrupt and stop for a bit then continue
            if random.random() < 0.01:
                self._enter(State.WALL_CLING)
                return

            # Ceiling reached → swing to ceiling walk
            ceil_thresh = float(self._screen.top()) + self._anc_y + 40
            if self._wall_climb_dir == -1 and self._ay <= ceil_thresh:
                self._ay = float(self._screen.top()) + CEIL_ANC_Y
                self._enter(State.CEIL_CLING)
            # Floor reached → land
            floor = self._floor_y()
            if self._wall_climb_dir == 1 and self._ay >= floor - FLOOR_MARGIN:
                self._ay = floor
                self._enter(State.IDLE)

        elif self._state == State.CEIL_CLING:
            self._state_ticks -= 1
            if self._state_ticks <= 0:
                if random.random() < 0.8:
                    self._enter(State.CEIL_WALK)
                else:
                    # Let go and fall
                    self._ay = float(self._screen.top()) + self._anc_y + 1
                    self._vy = 0.0
                    self._enter(State.FALL)

        elif self._state == State.CEIL_WALK:
            self._ax += self._vx
            self._anim.advance()
            # Hit ceiling edge → descend wall.  Use screen bounds as the trigger
            # so the mascot never walks off onto an adjacent monitor.
            sl, sr = float(self._screen.left()), float(self._screen.right())
            edge_l = max(self._wall_left_x(),  sl)
            edge_r = min(self._wall_right_x(), sr)
            if self._ax <= edge_l:
                self._ax = edge_l
                self._wall_side      = "L"
                self._wall_climb_dir = 1
                self._enter(State.WALL_CLIMB)
            elif self._ax >= edge_r:
                self._ax = edge_r
                self._wall_side      = "R"
                self._wall_climb_dir = 1
                self._enter(State.WALL_CLIMB)
            else:
                self._state_ticks -= 1
                if self._state_ticks <= 0:
                    # Randomly drop
                    if random.random() < 0.8:
                        self._enter(State.CEIL_CLING)
                    else:
                        self._ay = float(self._screen.top()) + self._anc_y + 1
                        self._vy = 0.0
                        self._enter(State.FALL)

        elif self._state == State.CARRY:
            self._ax += self._vx
            # Bounce at screen edges while carrying
            if self._ax <= self._left_x() or self._ax >= self._right_x():
                self._clamp_x()
                self._vx        = -self._vx
                self._facing_right = not self._facing_right
                self._throw_dir = -self._throw_dir
            self._anim.advance()
            # Move carried window above mascot's head each tick
            if self._carry_win and Mascot._env:
                ww = self._carry_win.rect.width()
                wh = self._carry_win.rect.height()
                # Place the window's bottom-centre at the mascot's raised hands
                # (~10 px below the sprite top, where carry_idle shows the grip).
                hold_y = int(self._ay) - self._anc_y + 10
                wx = int(self._ax) - ww // 2
                wy = hold_y - wh
                Mascot._env.move_window(self._carry_win.wid, wx, wy)
            self._carry_ticks -= 1
            if self._carry_ticks <= 0:
                self._enter(State.THROW_WIN)

        elif self._state == State.THROW_WIN:
            # Slide window off screen each tick
            if self._carry_win and Mascot._env:
                self._win_fly_x += WIN_THROW_VX * self._throw_dir
                self._win_fly_y += 1.5
                Mascot._env.move_window(
                    self._carry_win.wid,
                    int(self._win_fly_x),
                    int(self._win_fly_y),
                )
            self._anim.advance()
            if self._anim.done:
                self._carry_win = None
                self._enter(State.IDLE)

        elif self._state == State.CLONING:
            self._anim.advance()
            if self._anim.done:
                if len(Mascot._all) < MAX_MASCOTS:
                    offset = random.choice([-64, 64])
                    _spawn(self._sprites, self._screen, int(self._ax) + offset)
                self._enter(State.IDLE)

        elif self._state == State.WIN_CLIMB:
            self._anim.advance()
            if not self._climb_win:
                self._enter(State.FALL)
            else:
                # Fetch live position so the mascot rides the window as it moves
                live = (Mascot._env.get_window_rect(self._climb_win.wid)
                        if Mascot._env else None)

                if live is None:
                    self._climb_win = None
                    self._enter(State.FALL)
                else:
                    self._climb_win.rect = live
                    self._ay -= CLIMB_SPEED
                    # Lock horizontally to the chosen edge
                    if self._wall_side == "L":
                        self._ax = float(live.left())
                    else:
                        self._ax = float(live.right())
                    # Clamp to screen top — compositor shadows can push rect.top()
                    # slightly negative; never let the mascot climb off-screen.
                    win_top = max(float(live.top()), float(self._screen.top()))
                    if self._ay <= win_top + FLOOR_MARGIN:
                        self._ay = win_top + FLOOR_MARGIN
                        if self._wall_side == "R":
                            self._ax = self._ax - 8
                        else:
                            self._ax = self._ax + 8
                        self._facing_right = (self._wall_side != "L")
                        self._climb_win = None
                        self._enter(State.IDLE)

        elif self._state == State.MERGING:
            self._anim.advance()
            if self._anim.done:
                self._dismiss()
                return   # widget is gone; stop processing

        elif self._state == State.SPRAWL:
            self._state_ticks -= 1
            self._anim.advance()
            if self._state_ticks <= 0:
                self._enter(random.choices(
                    [State.IDLE, State.CRAWL, State.SIT], weights=[20, 50, 30]
                )[0])
    

        # ── Cross-state checks ─────────────────────────────────────────────────

        # Merge check (stable floor states only)
        if self._state in (State.IDLE, State.SIT) and Mascot._cloning_on:
            self._check_merge()

        # Social awareness tick (idle / walk / sit only, low probability)
        if self._state in (State.IDLE, State.WALK, State.SIT):
            if random.random() < 0.003:
                self._social_tick()

        # Validate carried / climbed window is still alive
        if Mascot._env and Mascot._valid_win is not None:
            active  = {w.wid for w in Mascot._valid_win}
            if self._carry_win and self._carry_win.wid not in active:
                self._carry_win = None
                if self._state in (State.CARRY, State.THROW_WIN):
                    self._enter(State.IDLE)
            if self._climb_win and self._climb_win.wid not in active:
                self._climb_win = None
                if self._state == State.WIN_CLIMB:
                    self._enter(State.FALL)

        self._sync_window(ceil_mode)
        self.update()

    # ── Behaviour helpers ─────────────────────────────────────────────────────

    def _idle_transition(self):
        choices = [State.WALK, State.RUN, State.SIT, State.SIT_SPINHEAD, State.SIT_LEDGE, State.IDLE]
        weights = [35, 10, 25, 10, 10, 10]

        # randomly change direction
        if random.random() < 0.05:
            self._facing_right = not self._facing_right

        if Mascot._cloning_on and len(Mascot._all) < MAX_MASCOTS:
            choices.append(State.CLONING)
            weights.append(2)
        self._enter(random.choices(choices, weights=weights)[0])

    def _valid_windows():
        """Sets a list of valid windows that the shimeji can interact with."""
        if not Mascot._env:
            Mascot._valid_win = None
            return

        all_win = Mascot._env.windows
        if not all_win:
            Mascot._valid_win = None
            return

        # Collect FULL screen geometry (not availableGeometry) so that a
        # maximised browser that fills only the available area isn't confused
        # with the desktop background, which fills the full screen including
        # the taskbar strip.  Rects come from _refresh() with no abs() applied.
        app = QApplication.instance()
        screen_full_geoms: list[QRect] = (
            [s.geometry() for s in app.screens()] if app else []
        )

        def _is_desktop(w: WindowInfo) -> bool:
            for g in screen_full_geoms:
                # Desktop background / root-window proxy: exactly spans a
                # screen's full geometry and is anchored at its origin.
                if (w.rect.size() == g.size()):
                    return True
            return False

        def _in_corner(w: WindowInfo):
            # for g in screen_full_geoms:
            #     # Windows anchored in a screen corner with a size smaller than
            #     # the full screen are likely docked toolbars or panels, not
            #     # valid surfaces for the mascot to interact with.
            #     wh = (w.rect.width()  < g.width() * 0.75 and w.rect.height() < g.height() * 0.75)
            #     hs = (abs(w.rect.left() - g.left()) <= 4 or abs(w.rect.right() - g.right()) <= 4) 
            #     vs = (abs(w.rect.top() - g.top()) <= 4 or  abs(w.rect.bottom() - g.bottom()) <= 4)

            #     if wh or hs or vs:
            #         return True
            # return False
            app = QApplication.instance()
            screen_full_geoms: list[QRect] = (
                [s.geometry() for s in app.screens()] if app else []
            )

            for g in screen_full_geoms:
                # Windows anchored in a screen corner with a size smaller than
                # the full screen are likely docked toolbars or panels, not
                # valid surfaces for the mascot to interact with.
                wh = not (w.rect.width()  < g.width() * 0.75 and w.rect.height() < g.height() * 0.75)
                hs = (abs(w.rect.left() - g.left()) <= 4 or abs(w.rect.right() - g.right()) <= 4) 
                vs = (abs(w.rect.top() - g.top()) <= 4 or  abs(w.rect.bottom() - g.bottom()) <= 4)

                if wh or hs or vs:
                    return True, [wh, hs, vs]
            return False, ['x','x','x']

        Mascot._valid_win = [w for w in all_win if not (_in_corner(w)[0] or _is_desktop(w))]
        # for w in all_win:
        #     ic, d = _in_corner(w)
        #     print(f"{w.name}: {ic} [{d}] - ({w.rect.left()}, {w.rect.top()})")

    def _track_floor_window(self):
        """
        If the mascot is standing on a window, apply that window's per-tick
        movement delta to _ax/_ay so the mascot rides it.
        Resets tracking when not on any window.
        """
        if not Mascot._env:
            self._floor_win_wid = None
            self._floor_win_prev_rect = None
            return

        if self._floor_win_wid is not None:
            live = Mascot._env.get_window_rect(self._floor_win_wid)
            if live is not None and self._floor_win_prev_rect is not None:
                dy = float(live.top()  - self._floor_win_prev_rect.top())
                dx = float(live.left() - self._floor_win_prev_rect.left())
                if abs(dy) < 200:   # ignore teleports
                    self._ay += dy
                if abs(dx) < 500:
                    self._ax += dx
                    # Clamp after riding a window so we don't cross into another monitor.
                    self._ax = max(float(self._screen.left()),
                                   min(float(self._screen.right()), self._ax))
            self._floor_win_prev_rect = live
            if live is None:
                self._floor_win_wid = None
            return

        # Not yet tracking — find the window we're standing on.
        for w in (Mascot._valid_win or []):
            if not w:
                continue
            top = float(w.rect.top())
            if (w.rect.left() - self._anc_x <= self._ax <= w.rect.right() + self._anc_x
                    and abs(self._ay - top) <= FLOOR_MARGIN * 3):
                self._floor_win_wid       = w.wid
                self._floor_win_prev_rect = w.rect
                return

        self._floor_win_wid       = None
        self._floor_win_prev_rect = None

    def _jump_to(self, tx: float, ty: float) -> bool:
        """
        Set _vx/_vy for a parabolic arc whose descending half lands at (tx, ty).
        Returns False if the target is unreachable with JUMP_VY.
        """
        vy0  = JUMP_VY
        disc = vy0 * vy0 - 2.0 * GRAVITY * (self._ay - ty)
        if disc < 0:
            return False
        t = (-vy0 + math.sqrt(disc)) / GRAVITY  # time of descending crossing
        if t <= 0:
            return False
        self._vx = (tx - self._ax) / t
        self._vy = vy0
        self._facing_right = self._vx >= 0
        return True

    def _try_jump(self):
        """
        Scan for reachable jump targets (window tops, ceiling, screen sides)
        and launch a JUMP arc toward one.  Picks randomly from the best few
        so the mascot doesn't always go to the same place.
        """
        floor = self._floor_y()
        candidates: list[tuple[float, float, float]] = []  # (priority, tx, ty)

        # Window-top candidates: above current floor and within reach
        if Mascot._env and Mascot._valid_win:
            for w in Mascot._valid_win:
                if not w:
                    continue
                win_top = float(w.rect.top())
                if win_top >= floor:
                    continue  # at or below current surface
                dy = floor - win_top
                if dy > JUMP_RANGE_Y:
                    continue
                cx = float(w.rect.center().x())
                dx = abs(cx - self._ax)
                if dx > JUMP_RANGE_X:
                    continue
                candidates.append((dx + dy, cx, win_top))

        # Ceiling candidate: only if close enough to reach
        ceil_y = float(self._screen.top()) + CEIL_ANC_Y
        ceil_dy = floor - ceil_y
        if 0 < ceil_dy <= JUMP_RANGE_Y:
            candidates.append((ceil_dy, self._ax, ceil_y))

        # Screen-side candidates: aim toward left or right wall.
        # Target a point on the arc partway above the floor; the JUMP tick will
        # detect the wall hit and convert it into WALL_CLING at whatever height
        # the mascot reaches.
        arc_ty = max(floor - 180.0,
                     float(self._screen.top()) + self._anc_y + 20.0)
        l_dist = self._ax - self._left_x()
        r_dist = self._right_x() - self._ax
        if l_dist > 80:
            candidates.append((l_dist * 0.8, self._left_x(), arc_ty))
        if r_dist > 80:
            candidates.append((r_dist * 0.8, self._right_x(), arc_ty))

        if not candidates:
            return

        # Randomly pick from the top few candidates, weighted toward closer ones.
        candidates.sort()
        pool = candidates[:min(4, len(candidates))]
        weights = [1.0 / (p + 1.0) for p, _, _ in pool]
        total   = sum(weights)
        weights = [wt / total for wt in weights]
        _, tx, ty = random.choices(pool, weights=weights, k=1)[0]
        if self._jump_to(tx, ty):
            self._enter(State.JUMP)

    def _try_win_climb(self):
        if not Mascot._env or not Mascot._valid_win:
            return

        wins = Mascot._valid_win
        w = min(wins, key=lambda w: abs(float(w.rect.center().x()) - self._ax))
        
        self._climb_win = w
        # print(f"Nearest: {w.rect} (center {w.rect.center()})")
        
        # Choose the closer vertical edge of the window
        left_dist  = abs(self._ax - w.rect.left())
        right_dist = abs(self._ax - w.rect.right())
        if left_dist <= right_dist:
            self._wall_side = "L"
            # self._ax = float(w.rect.left())
        else:
            self._wall_side = "R"
            # self._ax = float(w.rect.right())

        # if not close enough, don't bother
        if min(left_dist, right_dist) > JUMP_RANGE_X:
            return

        # Start at the window's bottom edge (clamped to the floor so we don't
        # teleport the mascot if the window bottom is somehow off-screen)
        self._ay  = min(float(w.rect.bottom()), self._floor_y())
        self._vx  = 0.0
        self._vy  = 0.0
        self._enter(State.WIN_CLIMB)
        

    def _maybe_grab_window(self):
        """Small chance each WALK tick to grab a nearby window and carry it."""
        if not Mascot._env or not Mascot._valid_win:
            return
        if random.random() > 0.00005:
            return
        wins = Mascot._valid_win
        w = min(wins, key=lambda w: abs(w.rect.center().x() - self._ax))
        self._carry_win    = w
        self._facing_right = w.rect.center().x() > self._ax
        self._throw_dir    = 1 if self._facing_right else -1
        self._enter(State.CARRY)

    def _force_grab_window(self):
        """Immediately grab the nearest window and carry it (used by Force action menu)."""
        if not Mascot._env or not Mascot._valid_win:
            return
        wins = Mascot._valid_win
        w = min(wins, key=lambda w: abs(w.rect.center().x() - self._ax))
        print(f"Nearest: {w.rect} (center {w.rect.center()})")
        self._carry_win    = w
        self._facing_right = w.rect.center().x() > self._ax
        self._throw_dir    = 1 if self._facing_right else -1
        self._ay = self._floor_y()
        self._enter(State.CARRY)

    def _force_climb_window(self):
        """Cling to the nearest window's side and crawl up to sit on its top."""
        if not Mascot._env or not Mascot._valid_win:
            return
        wins = Mascot._valid_win
        w = min(wins, key=lambda w: abs(float(w.rect.center().x()) - self._ax))
        
        self._climb_win = w
        # print(f"Nearest: {w.rect} (center {w.rect.center()})")
        
        # Choose the closer vertical edge of the window
        left_dist  = abs(self._ax - w.rect.left())
        right_dist = abs(self._ax - w.rect.right())
        if left_dist <= right_dist:
            self._wall_side = "L"
            # self._ax = float(w.rect.left())
        else:
            self._wall_side = "R"
            # self._ax = float(w.rect.right())
        # Start at the window's bottom edge (clamped to the floor so we don't
        # teleport the mascot if the window bottom is somehow off-screen)
        self._ay  = min(float(w.rect.bottom()), self._floor_y())
        self._vx  = 0.0
        self._vy  = 0.0
        self._enter(State.WIN_CLIMB)

    def _check_merge(self):
        """Absorb a nearby mascot of the same character (random chance per tick)."""
        for other in Mascot._all:
            if other is self:
                continue
            if other._sprites._dir != self._sprites._dir:
                continue
            if other._state not in (State.IDLE, State.SIT, State.WALK):
                continue
            close = (
                abs(self._ax - other._ax) < MERGE_DIST and
                abs(self._ay - other._ay) < MERGE_DIST
            )
            if close and random.random() < 0.0008:
                other._enter(State.MERGING)
                break

    def _social_candidates(self) -> "list[Mascot]":
        """Return mascots this instance is allowed to be socially aware of."""
        candidates = []
        for other in Mascot._all:
            if other is self:
                continue
            if other._state in (State.DRAG, State.THROWN, State.MERGING,
                                 State.CLONING, State.FOLLOWING, State.CHATTING):
                continue
            same = other._sprites._dir == self._sprites._dir
            if same and Mascot._aware_same:
                candidates.append(other)
            elif not same and Mascot._aware_other:
                candidates.append(other)
        return candidates

    def _social_tick(self):
        """Occasionally pick a social behaviour toward a nearby mascot."""
        candidates = self._social_candidates()
        if not candidates:
            return

        on_floor = [c for c in candidates
                    if abs(c._ay - self._ay) < self._sh * 1.5]
        pool = on_floor if on_floor else candidates
        target = random.choice(pool)

        roll = random.random()
        if roll < 0.35:
            # Follow / approach
            self._social_target = target
            self._enter(State.FOLLOWING)
        elif roll < 0.55:
            # Start a conversation if close enough, otherwise approach first
            if abs(target._ax - self._ax) < CHAT_DIST:
                self._begin_chat(target)
            else:
                self._social_target = target
                self._enter(State.FOLLOWING)
        elif roll < 0.78:
            self._social_mimic(target)
        else:
            self._facing_right = target._ax > self._ax
            self._enter(State.SIT)

    def _social_arrive(self, target: "Mascot"):
        """Decide what to do once we've walked close to a target."""
        roll = random.random()
        if roll < 0.45:
            self._begin_chat(target)
        elif roll < 0.75:
            self._facing_right = target._ax > self._ax
            self._enter(State.SIT)
        elif roll < 0.90:
            self._social_mimic(target)
        else:
            self._enter(State.IDLE)

    def _begin_chat(self, other: "Mascot"):
        """Start a 3-5 turn emoji conversation with other."""
        if other._state == State.CHATTING or self._conversation is not None:
            self._enter(State.IDLE)
            return
        turns = random.randint(3, 5)
        conv = Conversation(self, other, turns)
        self._conversation = conv
        other._conversation = conv
        self._facing_right = other._ax > self._ax
        other._facing_right = self._ax > other._ax
        self._enter(State.CHATTING)
        other._enter(State.CHATTING)

    def _end_conversation(self):
        """Called by Conversation when it finishes, or if interrupted."""
        self._conversation = None
        if self._state == State.CHATTING:
            self._enter(State.IDLE)

    def _social_mimic(self, target: "Mascot"):
        """Copy a simple visible state from the target."""
        copyable = {
            State.IDLE:        State.IDLE,
            State.SIT:         State.SIT,
            State.SIT_SPINHEAD: State.SIT_SPINHEAD,
            State.SPRAWL:      State.SPRAWL,
        }
        new_state = copyable.get(target._state)
        if new_state is not None:
            self._facing_right = target._ax > self._ax
            self._enter(new_state)
        else:
            self._enter(State.IDLE)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        ceil_mode = self._state in (State.CEIL_CLING, State.CEIL_WALK)

        if self._dragging:
            frame, flipped = self._drag_frame(), False

        elif self._state in (State.WALL_CLING, State.WALL_CLIMB):
            frame   = self._anim.current()
            flipped = (self._wall_side != "L")   # L wall → face right → flip

        elif self._state == State.WIN_CLIMB:
            frame   = self._anim.current()
            flipped = (self._wall_side == "L")

        elif self._state in (State.CEIL_CLING, State.CEIL_WALK):
            frame   = self._anim.current()
            flipped = self._facing_right
            if self._state == State.CEIL_CLING:
                # Flip ceiling cling sprites to face the wall they're on.
                flipped = (self._wall_side == "L")

        elif self._state == State.JUMP:
            frame   = "jump.png" if self._vy < 0 else "fall.png"
            flipped = self._facing_right

        elif self._state in (State.FALL, State.THROWN):
            frame   = "fall.png"
            flipped = self._facing_right

        elif self._state == State.LAND:
            frame   = self._anim.current()
            flipped = self._facing_right

        elif self._state in (State.SIT, State.SIT_SPINHEAD, State.SIT_LEDGE, State.SPRAWL):
            frame   = self._anim.current()
            flipped = self._facing_right

        elif self._state in (State.CARRY, State.THROW_WIN, State.CLONING, State.MERGING):
            frame   = self._anim.current()
            flipped = self._facing_right

        elif self._state in (State.WALK, State.RUN, State.CRAWL, State.FOLLOWING):
            frame   = self._anim.current()
            flipped = self._facing_right

        elif self._state == State.CHATTING:
            frame   = self._anim.current()
            flipped = self._facing_right

        else:   # IDLE, DRAG (non-mouse-dragging)
            frame   = "stand.png"
            flipped = self._facing_right

        pm = self._sprites.get(frame, flipped)
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        offset = QPoint(0, 0)
        if ceil_mode:
            # Nudge up a few pixels to better align the ceiling sprite's anchor.
            offset.setY(-48)
        if self._state in (State.WALL_CLIMB, State.WALL_CLING):
            # Nudge left/right to align the climbing sprite's anchor with the wall edge.
            offset.setX(-64 if self._wall_side == "L" else 64)

        # if self._state == State.WIN_CLIMB:
        #     offset.setX(-64 if self._wall_side == "L" else 64)

        # if self._state == State.SIT_LEDGE:
        #     # Nudge down a few pixels to make the "ledge" sprites sit on the floor better.
        #     offset.setY(16)

        if not offset.isNull():
            p.translate(offset)
        

        p.drawPixmap(0, 0, pm)
        p.end()

    # ── Mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging      = True
            self._drag_offset   = event.position().toPoint()
            self._last_cursor   = event.globalPosition().toPoint()
            self._last_event_ms = 0.0
            self._throw_vx      = 0.0
            self._throw_vy      = 0.0
            self._timer.stop()
            self._enter(State.DRAG)

    def mouseMoveEvent(self, event):
        if self._dragging:
            gpos   = event.globalPosition().toPoint()
            new_tl = gpos - self._drag_offset
            self._ax = float(new_tl.x() + self._anc_x)
            self._ay = float(new_tl.y() + self._anc_y)
            self.move(new_tl)

            t_ms = float(event.timestamp())
            dt   = t_ms - self._last_event_ms
            if dt > 0 and self._last_event_ms > 0:
                scale  = TICK_MS / dt
                raw_vx = (gpos.x() - self._last_cursor.x()) * scale
                raw_vy = (gpos.y() - self._last_cursor.y()) * scale
                self._throw_vx = self._throw_vx * 0.35 + raw_vx * 0.65
                self._throw_vy = self._throw_vy * 0.35 + raw_vy * 0.65

            self._last_cursor   = gpos
            self._last_event_ms = t_ms
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging      = False
            self._last_event_ms = 0.0
            self._vx = self._throw_vx
            self._vy = self._throw_vy
            self._enter(State.THROWN)
            self._timer.start(TICK_MS)

    def contextMenuEvent(self, event):
        menu = QMenu(self)

        clone_act = menu.addAction("Clone")
        clone_act.setEnabled(
            Mascot._cloning_on and len(Mascot._all) < MAX_MASCOTS
        )

        menu.addSeparator()

        tog_clone = menu.addAction("Allow cloning")
        tog_clone.setCheckable(True)
        tog_clone.setChecked(Mascot._cloning_on)

        tog_throw = menu.addAction("Allow window throwing")
        tog_throw.setCheckable(True)
        tog_throw.setChecked(Mascot._win_throw_on)

        tog_aware_same = menu.addAction("Aware of same character")
        tog_aware_same.setCheckable(True)
        tog_aware_same.setChecked(Mascot._aware_same)

        tog_aware_other = menu.addAction("Aware of other characters")
        tog_aware_other.setCheckable(True)
        tog_aware_other.setChecked(Mascot._aware_other)

        menu.addSeparator()

        # ── Force action submenu ──────────────────────────────────────────────
        force_menu = menu.addMenu("Force action…")

        fa_idle      = force_menu.addAction("Idle")
        fa_sit       = force_menu.addAction("Sit")
        fa_walk      = force_menu.addAction("Walk")
        fa_run       = force_menu.addAction("Run")
        fa_jump       = force_menu.addAction("Jump")
        fa_crawl     = force_menu.addAction("Crawl")
        force_menu.addSeparator()
        fa_wall_l    = force_menu.addAction("Climb left wall")
        fa_wall_r    = force_menu.addAction("Climb right wall")
        fa_ceiling   = force_menu.addAction("Go to ceiling")
        force_menu.addSeparator()
        has_wins = bool(Mascot._env and Mascot._valid_win)
        fa_win_climb = force_menu.addAction("Climb nearest window")
        fa_win_climb.setEnabled(has_wins)
        fa_win_throw = force_menu.addAction("Throw nearest window")
        fa_win_throw.setEnabled(has_wins)
        force_menu.addSeparator()
        fa_clone     = force_menu.addAction("Clone now")
        fa_clone.setEnabled(len(Mascot._all) < MAX_MASCOTS)
        force_menu.addSeparator()
        fa_chat = force_menu.addAction("Chat with nearest")
        fa_chat.setEnabled(len(Mascot._all) > 1)

        menu.addSeparator()
        debug_act = menu.addAction("Debug panel")
        debug_act.setCheckable(True)
        debug_act.setChecked(DebugPanel.is_visible())

        menu.addSeparator()
        dismiss_act = menu.addAction("Dismiss")
        dismiss_all_one = menu.addAction("Dismiss all (except this one)")
        exit = menu.addAction("Exit")

        action = menu.exec(event.globalPos())

        # ── Normal actions ────────────────────────────────────────────────────
        if action == clone_act:
            if self._state in (State.IDLE, State.SIT, State.WALK):
                self._enter(State.CLONING)
        elif action == tog_clone:
            Mascot._cloning_on = not Mascot._cloning_on
        elif action == tog_throw:
            Mascot._win_throw_on = not Mascot._win_throw_on
        elif action == tog_aware_same:
            Mascot._aware_same = not Mascot._aware_same
        elif action == tog_aware_other:
            Mascot._aware_other = not Mascot._aware_other
        elif action == debug_act:
            DebugPanel.toggle(self._screen)
        elif action == dismiss_act:
            self._dismiss()
        elif action == dismiss_all_one:
            for m in Mascot._all[:]:
                if m is not self:
                    m._dismiss()
        elif action == exit:
            for m in Mascot._all[:]:
                m._dismiss()

        # ── Force actions ─────────────────────────────────────────────────────
        elif action == fa_idle:
            self._enter(State.IDLE)

        elif action == fa_sit:
            self._ay = self._floor_y()
            self._enter(State.SIT)

        elif action == fa_walk:
            self._ay = self._floor_y()
            self._enter(State.WALK)

        elif action == fa_run:
            self._ay = self._floor_y()
            self._enter(State.RUN)

        elif action == fa_crawl:
            self._ay = self._floor_y()
            self._enter(State.CRAWL)

        elif action in (fa_wall_l, fa_wall_r):
            side = "L" if action == fa_wall_l else "R"
            self._wall_side      = side
            self._wall_climb_dir = -1
            self._ax  = self._wall_left_x() if side == "L" else self._wall_right_x()
            self._ay  = self._floor_y()
            self._vx  = 0.0
            self._vy  = 0.0
            self._enter(State.WALL_CLIMB)

        elif action == fa_ceiling:
            # Snap to nearest wall and climb straight up.
            side = "L" if self._ax < self._screen.center().x() else "R"
            self._wall_side      = side
            self._wall_climb_dir = -1
            self._ax  = self._wall_left_x() if side == "L" else self._wall_right_x()
            self._ay  = self._floor_y()
            self._vx  = 0.0
            self._vy  = 0.0
            self._enter(State.WALL_CLIMB)

        elif action == fa_win_climb:
            self._force_climb_window()

        elif action == fa_win_throw:
            self._force_grab_window()

        elif action == fa_clone:
            if len(Mascot._all) < MAX_MASCOTS:
                self._enter(State.CLONING)

        elif action == fa_jump:
            # Try to jump to a random nearby surface; if none are in range, do a vertical jump.
            if not self._try_jump():
                self._vx = 0.0
                self._vy = JUMP_VY
                self._enter(State.JUMP)

        elif action == fa_chat:
            others = [m for m in Mascot._all if m is not self
                      and m._state not in (State.DRAG, State.THROWN, State.CHATTING)]
            if others:
                nearest = min(others, key=lambda m: abs(m._ax - self._ax))
                self._begin_chat(nearest)

    def _dismiss(self):
        if self._conversation is not None:
            self._conversation.abort()
        self._timer.stop()
        self.close()
        if self in Mascot._all:
            Mascot._all.remove(self)
        if not Mascot._all and not Mascot._keep_alive:
            QApplication.instance().quit()

# ── Speech bubble ────────────────────────────────────────────────────────────

class SpeechBubble(QWidget):
    """
    Frameless, transparent overlay that draws a rounded speech bubble
    with an emoji, positioned above a mascot's head.
    Auto-closes after `duration_ms` milliseconds.
    """

    _PAD    = 10   # px padding inside bubble
    _POINT  = 10   # px height of the pointer triangle
    _RADIUS = 10   # corner radius
    _FONT_SIZE = 28

    def __init__(self, mascot: "Mascot", emoji: str, duration_ms: int):
        super().__init__()
        self._emoji = emoji

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool                 |
            Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Measure emoji to size the widget
        self.setFont(self._emoji_font())
        fm = self.fontMetrics()
        ew   = fm.horizontalAdvance(emoji) + self._PAD * 2
        eh   = fm.height() + self._PAD * 2
        total_w = max(ew, 48)
        total_h = eh + self._POINT
        self.setFixedSize(total_w, total_h)

        # Position: centered above the mascot's head
        head_x = mascot.x() + mascot.width() // 2
        head_y = mascot.y()
        self.move(head_x - total_w // 2, head_y - total_h - 4)

        self.show()

        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.close)
        self._close_timer.start(duration_ms)

    def _emoji_font(self) -> QFont:
        f = QFont()
        for family in ("Noto Color Emoji", "Segoe UI Emoji", "Apple Color Emoji", ""):
            f.setFamily(family)
            if family:
                break
        f.setPointSize(self._FONT_SIZE)
        return f

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        bh   = h - self._POINT          # bubble body height
        cx   = w // 2

        # Bubble body
        p.setPen(QPen(QColor(200, 200, 200), 1))
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        p.drawRoundedRect(0, 0, w, bh, self._RADIUS, self._RADIUS)

        # Pointer triangle (downward)
        tri = QPolygon([
            QPoint(cx - 7, bh),
            QPoint(cx + 7, bh),
            QPoint(cx,     bh + self._POINT),
        ])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 230)))
        p.drawPolygon(tri)

        # Emoji text
        p.setFont(self._emoji_font())
        p.setPen(QColor(0, 0, 0))
        p.drawText(0, 0, w, bh, Qt.AlignmentFlag.AlignCenter, self._emoji)


# ── Conversation coordinator ──────────────────────────────────────────────────

class Conversation(QObject):
    """
    Manages a 3-5 turn emoji exchange between two mascots.
    Uses a single-shot QTimer to drive alternating turns.
    When Mascot._llm_enabled, uses LLMController to generate emoji responses;
    a "💭" thinking bubble is shown while the LLM processes.
    Either mascot can call abort() to cancel early (e.g. on dismiss).
    """

    def __init__(self, initiator: "Mascot", responder: "Mascot", total_turns: int):
        super().__init__()
        self._a          = initiator
        self._b          = responder
        self._turns_left = total_turns
        self._whose      = 0          # 0 = initiator's turn, 1 = responder's
        self._aborted    = False
        self._bubble: SpeechBubble | None = None
        self._last_emoji: dict[int, str]  = {}   # id(mascot) → last emoji said
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)
        self._do_turn()

    def _pick_emoji(self) -> str:
        return random.choice(CHAT_EMOJIS)

    def _speaker(self) -> "Mascot":
        return self._a if self._whose == 0 else self._b

    def _listener(self) -> "Mascot":
        return self._b if self._whose == 0 else self._a

    def _do_turn(self):
        if self._aborted:
            return
        speaker  = self._speaker()
        listener = self._listener()
        if speaker not in Mascot._all:
            self.abort()
            return

        if Mascot._llm_enabled:
            personality  = get_personality(speaker._sprites._dir)
            memory_text  = speaker._memory.to_text()
            partner_name = listener._sprites._dir.name
            partner_last = self._last_emoji.get(id(listener), "")
            LLMController.get().request(
                personality, memory_text, partner_name, partner_last,
                lambda emoji, sp=speaker, li=listener: self._on_llm_ready(emoji, sp, li),
            )
        else:
            self._deliver(speaker, listener, self._pick_emoji())

    def _on_llm_ready(self, emoji: str | None, speaker: "Mascot", listener: "Mascot"):
        if self._aborted:
            return
        self._deliver(speaker, listener, emoji or self._pick_emoji())

    def _deliver(self, speaker: "Mascot", listener: "Mascot", emoji: str):
        """Show the speech bubble and record the turn in both mascots' memories."""
        speaker._memory.record("said",           emoji)
        speaker._memory.record("location",       _location_str(speaker))
        listener._memory.record("heard",         emoji, speaker._sprites._dir.name)
        listener._memory.record("partner_action", speaker._state.name.lower())
        self._last_emoji[id(speaker)] = emoji
        if self._bubble and not self._bubble.isHidden():
            self._bubble.close()
        self._bubble = SpeechBubble(speaker, emoji, BUBBLE_MS)
        self._timer.start(BUBBLE_MS + BUBBLE_GAP_MS)

    def _advance(self):
        if self._aborted:
            return
        self._turns_left -= 1
        if self._turns_left <= 0:
            self._finish()
        else:
            self._whose ^= 1
            self._do_turn()

    def _finish(self):
        self._aborted = True
        self._timer.stop()
        for m in (self._a, self._b):
            if m in Mascot._all:
                m._end_conversation()

    def abort(self):
        if self._aborted:
            return
        self._aborted = True
        self._timer.stop()
        if self._bubble and not self._bubble.isHidden():
            self._bubble.close()
        for m in (self._a, self._b):
            if m in Mascot._all and m._conversation is self:
                m._conversation = None
                if m._state == State.CHATTING:
                    m._enter(State.IDLE)


# ── Debug panel ───────────────────────────────────────────────────────────────

class DebugPanel(QWidget):
    _instance: "DebugPanel | None" = None

    @classmethod
    def toggle(cls, screen: QRect):
        if cls._instance is None:
            cls._instance = DebugPanel(screen)
        if cls._instance.isVisible():
            cls._instance.hide()
        else:
            cls._instance.show()
            cls._instance._refresh()

    @classmethod
    def is_visible(cls) -> bool:
        return cls._instance is not None and cls._instance.isVisible()

    def __init__(self, screen: QRect):
        super().__init__()
        self.setWindowTitle("Shimekami – Debug")
        self.resize(460, 420)
        self.move(screen.right() - 480, screen.top() + 20)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._text = QTextEdit(self)
        self._text.setReadOnly(True)
        f = QFont("Monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self._text.setFont(f)
        layout.addWidget(self._text)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(TICK_MS * 2)

    def in_corner_ext(self,w: WindowInfo):
        app = QApplication.instance()
        screen_full_geoms: list[QRect] = (
            [s.geometry() for s in app.screens()] if app else []
        )

        for g in screen_full_geoms:
            # Windows anchored in a screen corner with a size smaller than
            # the full screen are likely docked toolbars or panels, not
            # valid surfaces for the mascot to interact with.
            wh = not (w.rect.width()  < g.width() * 0.75 and w.rect.height() < g.height() * 0.75)
            hs = (abs(w.rect.left() - g.left()) <= 4 or abs(w.rect.right() - g.right()) <= 4) 
            vs = (abs(w.rect.top() - g.top()) <= 4 or  abs(w.rect.bottom() - g.bottom()) <= 4)

            if wh or hs or vs:
                return True, [wh, hs, vs]
        return False, ['x','x','x']

    def _refresh(self):
        lines: list[str] = []

        for i, m in enumerate(Mascot._all):
            char_name = m._sprites._dir.name
            sep = "─" * max(1, 28 - len(char_name))
            lines.append(f"── {char_name} #{i + 1}  {sep}")
            lines.append(f"  state        {m._state.name}")
            lines.append(f"  state_ticks  {m._state_ticks}")
            lines.append(f"  pos          ({m._ax:.1f}, {m._ay:.1f})")
            lines.append(f"  vel          vx={m._vx:+.2f}  vy={m._vy:+.2f}")
            lines.append(f"  left(): {m._left_x()}    right(): {m._right_x()}")
            lines.append(f"  floor_y      {m._floor_y():.1f}  on_floor={m._on_floor()}")
            lines.append(f"  facing       {'→' if m._facing_right else '←'}  "
                         f"wall_side={m._wall_side}  climb_dir={m._wall_climb_dir:+d}")


            if m._climb_win:
                r = m._climb_win.rect
                lines.append(f"  climb_win    wid={m._climb_win.wid}  \"{m._climb_win.name}\"")
                lines.append(f"               side={m._wall_side}  top={r.top()}  "
                             f"left={r.left()}  right={r.right()}")
                lines.append(f"               size={r.width()}×{r.height()}")
            elif m._carry_win:
                r = m._carry_win.rect
                lines.append(f"  carry_win    wid={m._carry_win.wid}  \"{m._carry_win.name}\"")
                lines.append(f"               ({r.left()},{r.top()}) {r.width()}×{r.height()}")
            else:
                if Mascot._env and Mascot._valid_win:
                    same_screen = [w for w in Mascot._valid_win if m._screen.intersects(w.rect)]
                    if not same_screen:
                        same_screen = Mascot._valid_win
                    nearest = min(same_screen,
                                  key=lambda w: abs(float(w.rect.center().x()) - m._ax))
                    r = nearest.rect
                    left_d  = abs(m._ax - r.left())
                    right_d = abs(m._ax - r.right())
                    side    = "L" if left_d <= right_d else "R"
                    lines.append(f"  nearest_win  wid={nearest.wid}  \"{nearest.name}\"")
                    lines.append(f"               would-climb-side={side}  top={r.top()}")
                    lines.append(f"               left={r.left()}  right={r.right()}")
            
            if m._social_target:
                lines.append(f"  social_target  #{Mascot._all.index(m._social_target) + 1} "
                             f"\"{m._social_target._sprites._dir}\" in state {m._social_target._state.name}")
            
            lines.append("")

        if Mascot._env and Mascot._valid_win is not None:
            nw = len(Mascot._valid_win)
            lines.append(f"── Detected windows ({nw})  {'─' * 20}")
            for w in Mascot._valid_win:
                r = w.rect
                tags: list[str] = []
                if any(m._climb_win and m._climb_win.wid == w.wid for m in Mascot._all):
                    tags.append("CLIMB")
                if any(m._carry_win and m._carry_win.wid == w.wid for m in Mascot._all):
                    tags.append("CARRY")
                tag_str = f"  ← {', '.join(tags)}" if tags else ""
                title = (w.name[:32] + "…") if len(w.name) > 33 else w.name
                lines.append(
                    f"  wid={w.wid:<9} "
                    f"({r.left():5},{r.top():5}) "
                    f"{r.width():4}×{r.height():<4} "
                    f"\"{title}\""
                    f"{tag_str}"
                )
            
            # show other windows
            # lines.append(f"=> All windows ({len(Mascot._env.windows)}) <{'='*20}")
            # for w in Mascot._env.windows:
            #     r = w.rect
            #     title = "-+-" + ((w.name[:32] + "…") if len(w.name) > 33 else w.name)
            #     lines.append(title)
            #     in_corn = self.in_corner_ext(w)
            #     lines.append(f"     in_corner  {in_corn[0]}  (wh={in_corn[1][0]} hs={in_corn[1][1]} vs={in_corn[1][2]})")
            #     lines.append(f"     ({r.left():5},{r.top():5}) @ {r.width():4}×{r.height():<4} ")
        else:
            lines.append("── No X11 environment ─────────────────")

        sb = self._text.verticalScrollBar()
        pos = sb.value()
        self._text.setPlainText("\n".join(lines))
        sb.setValue(pos)

# ── Spawn helper ──────────────────────────────────────────────────────────────

def _spawn(sprites: SpriteCache, screen: QRect, x: int | None = None) -> "Mascot":
    return Mascot(sprites, screen, start_x=x)

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Shimekami desktop mascot")
    parser.add_argument(
        "--character", default=None, metavar="DIR",
        help="Path to a sprite directory (overrides built-in SHIMEJI-TEMPLATE)",
    )
    args, _unknown = parser.parse_known_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    Mascot._env = Environment()

    screen = app.primaryScreen().availableGeometry()
    sprite_dir = Path(args.character) if args.character else SPRITE_DIR
    sprites = SpriteCache(sprite_dir)
    
    Mascot._class_screen = screen

    _spawn(sprites, screen)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
