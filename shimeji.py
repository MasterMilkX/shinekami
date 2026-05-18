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
from pathlib import Path
from enum import Enum, auto

from PySide6.QtCore import Qt, QTimer, QPoint, QRect
from PySide6.QtGui import QPixmap, QPainter, QTransform, QCursor, QFont
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
JUMP_RANGE_Y  = 400    # max upward distance to a jump target (px)
JUMP_RANGE_X  = 500    # max horizontal distance to a jump target (px)

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
        ("sit_lookup.png",    5), ("sit_spinhead_a.png", 5), ("sit_spinhead_d.png", 5),
        ("sit_spinhead_b.png",5), ("sit_spinhead_e.png", 5), ("sit_spinhead_c.png", 5),
        ("sit_spinhead_f.png",5), ("sit.png",            5),
    ],
    "sit_spinhead": [
        ("ledge_sit_up.png", 8), ("ledge_sit.png",      8),
        ("ledge_dangle_a.png",6), ("ledge_dangle_b.png", 6),
        ("ledge_dangle_a.png",6), ("ledge_dangle_b.png", 6),
        ("ledge_dangle_a.png",6), ("ledge_dangle_b.png", 6),
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

            # Single pass: collect valid window info in stacking order and track
            # the highest index occupied by a fullscreen window.
            # stacked_wids[0] is the bottommost window, stacked_wids[-1] is topmost.
            top_fs_idx  = -1
            candidates  = []   # (stack_idx, WindowInfo)

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
                        top_fs_idx = idx
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
                    tr   = w.translate_coords(self._root, 0, 0)
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

            # Discard any window that sits below (or at) the topmost fullscreen
            # window — those are fully occluded and should be off-limits.
            self._windows = [wi for idx, wi in candidates if idx > top_fs_idx]
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
            tr = w.translate_coords(self._root, 0, 0)
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
    _cloning_on:   bool           = True
    _win_throw_on: bool           = False
    _aware_same:   bool           = False   # social behaviours between same-character mascots
    _aware_other:  bool           = True  # social behaviours between different characters
    _keep_alive:   bool           = False  # set True by launcher to prevent app quit
    _class_screen: "QRect | None" = None   # set by main(); launcher uses per-instance screen
    _valid_win:    "list | None"  = None   # populated each tick by _valid_windows()

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
        self._social_target: "Mascot | None" = None

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
                # Clamp to screen top — some windows extend slightly above the screen
                # due to compositor shadows; treat them as starting at screen top.
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
            self._ax = self._wall_left_x() if self._wall_side == "L" else self._wall_right_x()

        elif state == State.WALL_CLIMB:
            self._anim.play(ANIM["wall_climb"])
            self._state_ticks = random.randint(200,500)
            self._vx = 0.0
            self._ax = self._wall_left_x() if self._wall_side == "L" else self._wall_right_x()

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
            self._anim.play(ANIM["walk"])
            self._state_ticks = random.randint(3, 8) * (1000 // TICK_MS)
            self._vy = 0.0

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
            self._anim.advance()
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
                    # Close enough — stop and react socially
                    self._social_target = None
                    self._social_arrive(t)
                else:
                    self._facing_right = dx > 0
                    self._vx = (WALK_SPEED if self._facing_right else -WALK_SPEED) * 1.5
                    self._ax += self._vx
                    self._anim.advance()
                    if not self._on_floor():
                        self._enter(State.FALL)

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
            self._clamp_x()
            # Grab ceiling on the way up
            if self._on_ceiling():
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
                if random.random() < 0.7:
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
            # Hit ceiling edge → descend wall
            if self._ax <= self._wall_left_x():
                self._ax = self._wall_left_x()
                self._wall_side      = "L"
                self._wall_climb_dir = 1
                self._enter(State.WALL_CLIMB)
            elif self._ax >= self._wall_right_x():
                self._ax = self._wall_right_x()
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
                # convert to positive
                if live:
                    live = QRect(abs(live.x()), abs(live.y()), live.width(), live.height())

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
        """Sets a list of valid windows that the shimeji can interact with"""
        if not Mascot._env:
            Mascot._valid_win = None
            return

        all_win = Mascot._env.windows
        if not all_win:
            Mascot._valid_win = None
            return

        # Determine screen size — prefer the cached class-level screen rect,
        # fall back to Qt's primary screen if main() hasn't set it yet.
        if Mascot._class_screen is not None:
            screen_size = Mascot._class_screen.size()
        else:
            app = QApplication.instance()
            screen_size = (
                app.primaryScreen().availableGeometry().size() if app else None
            )

        # Exclude screen-sized windows (desktop background / root window proxies).
        # Windows hidden behind fullscreen apps are already excluded by Environment._refresh().
        if screen_size is not None:
            Mascot._valid_win = [w for w in all_win if w.rect.size() != screen_size and w.rect.top() != 0 and w.rect.left() != 0]
        else:
            Mascot._valid_win = list(all_win)

        # change the rect to a different value
        for vw in Mascot._valid_win:
            vw.rect = Mascot._env.get_window_rect(vw.wid)
            vw.rect = QRect(abs(vw.rect.x()), abs(vw.rect.y()), vw.rect.width(), vw.rect.height())

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
            self._floor_win_prev_rect = live
            if live is None:
                self._floor_win_wid = None
            return

        # Not yet tracking — find the window we're standing on
        for w in (Mascot._valid_win or []):
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
        Scan for reachable jump targets (nearby window tops or the ceiling)
        and launch a JUMP arc toward one if found.
        """
        floor = self._floor_y()
        candidates: list[tuple[float, float, float]] = []  # (priority, tx, ty)

        # Window-top candidates: above current floor and within reach
        if Mascot._env and Mascot._valid_win:
            for w in Mascot._valid_win:
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

        if not candidates:
            return
        candidates.sort()
        _, tx, ty = candidates[0]
        if self._jump_to(tx, ty):
            self._enter(State.JUMP)

    def _try_win_climb(self):
        if not Mascot._env or not Mascot._valid_win:
            return

        wins = Mascot._valid_win
        # for wi in wins:
        #     print(f"Window {wi.wid} at {wi.rect} (center {wi.rect.center()})")
        w = min(wins, key=lambda w: abs(abs(float(w.rect.center().x())) - self._ax))
        
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
        # Prefer the window whose horizontal centre is closest to the mascot.
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
        # for wi in wins:
        #     print(f"Window {wi.wid} at {wi.rect} (center {wi.rect.center()})")
        w = min(wins, key=lambda w: abs(abs(float(w.rect.center().x())) - self._ax))
        
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
                                 State.CLONING, State.FOLLOWING):
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

        # Prefer nearby mascots but allow any within a generous range
        on_floor = [c for c in candidates
                    if abs(c._ay - self._ay) < self._sh * 1.5]
        pool = on_floor if on_floor else candidates
        target = random.choice(pool)

        roll = random.random()
        if roll < 0.45:
            # Follow / approach
            self._social_target = target
            self._enter(State.FOLLOWING)
        elif roll < 0.75:
            # Copy target's visible state
            self._social_mimic(target)
        else:
            # Sit facing the target
            self._facing_right = target._ax > self._ax
            self._enter(State.SIT)

    def _social_arrive(self, target: "Mascot"):
        """Decide what to do once we've walked close to a target."""
        roll = random.random()
        if roll < 0.5:
            # Mirror sit — face toward target
            self._facing_right = target._ax > self._ax
            self._enter(State.SIT)
        elif roll < 0.80:
            self._social_mimic(target)
        else:
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

        elif self._state in (State.WALK, State.RUN, State.CRAWL):
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

        if self._state == State.SIT_LEDGE:
            # Nudge down a few pixels to make the "ledge" sprites sit on the floor better.
            offset.setY(16)

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

    def _dismiss(self):
        self._timer.stop()
        self.close()
        if self in Mascot._all:
            Mascot._all.remove(self)
        if not Mascot._all and not Mascot._keep_alive:
            QApplication.instance().quit()

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

    def _refresh(self):
        lines: list[str] = []

        for i, m in enumerate(Mascot._all):
            sep = "─" * 28
            lines.append(f"── Mascot #{i + 1}  {sep}")
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
                    nearest = min(Mascot._valid_win,
                                  key=lambda w: abs(abs(float(w.rect.center().x())) - m._ax))
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
        else:
            lines.append("── No X11 environment ─────────────────")

        self._text.setPlainText("\n".join(lines))

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
