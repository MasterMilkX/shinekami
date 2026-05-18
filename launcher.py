#!/usr/bin/env python3
"""
Shimekami Launcher
Character importer and session manager for Shimekami desktop mascots.

Usage:
    python launcher.py
"""

import re
import sys
import shutil
import zipfile
import tempfile
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QTimer, QPoint
from PySide6.QtGui import QPixmap, QIcon, QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel,
    QCheckBox, QFileDialog, QInputDialog, QMessageBox,
    QGroupBox, QFrame, QSizePolicy, QMenu,
)

# ── Paths ──────────────────────────────────────────────────────────────────────

_HERE          = Path(__file__).parent
CHARACTERS_DIR = _HERE / "characters"
TEMPLATE_DIR   = _HERE / "SHIMEJI-TEMPLATE" / "img" / "Shimeji"

# ── shime## → animation filename map (1-indexed) ──────────────────────────────
#
# Derived from the standard Shimeji-ee action/image ordering used by the
# original Group Finity Shimeji-ee and all compatible sprite packs.

SHIME_MAP: dict[int, str] = {
    1:  "stand.png",
    2:  "walk_a.png",
    3:  "walk_b.png",
    4:  "fall.png",
    5:  "drag_lean_left_1.png",
    6:  "drag_lean_right_1.png",
    7:  "drag_lean_left_2.png",
    8:  "drag_lean_right_2.png",
    9:  "drag_lean_left_3.png",
    10: "drag_lean_right_3.png",
    11: "sit.png",
    12: "wall_climb_a.png",
    13: "wall_cling.png",
    14: "wall_climb_b.png",
    15: "sit_spinhead_a.png",
    16: "sit_spinhead_b.png",
    17: "sit_spinhead_c.png",
    18: "land_crouch.png",
    19: "land_roll.png",
    20: "crawl.png",
    21: "sprawl.png",
    22: "jump.png",
    23: "ceiling_cling.png",
    24: "ceiling_walk_a.png",
    25: "ceiling_walk_b.png",
    26: "sit_lookup.png",
    27: "sit_spinhead_d.png",
    28: "sit_spinhead_e.png",
    29: "sit_spinhead_f.png",
    30: "ledge_sit_up.png",
    31: "ledge_sit.png",
    32: "ledge_dangle_a.png",
    33: "ledge_dangle_b.png",
    34: "carry_walk_a.png",
    35: "carry_walk_b.png",
    36: "carry_idle.png",
    37: "throw_window.png",
    38: "breed_a.png",
    39: "breed_b.png",
    40: "breed_c.png",
    41: "breed_d.png",
    42: "clone_a.png",
    43: "clone_b.png",
    44: "clone_c.png",
    45: "clone_d.png",
    46: "clone_e.png",
}

# ── Import helpers ─────────────────────────────────────────────────────────────

def _collect_shime_images(root: Path) -> dict[int, Path]:
    """
    Recursively scan root for files matching shime<N>.png.
    Returns {N: path}, zero-indexed if shime0.png is present.
    """
    pat = re.compile(r"^shime(\d+)\.png$", re.IGNORECASE)
    found: dict[int, Path] = {}
    for p in root.rglob("*.png"):
        m = pat.match(p.name)
        if m:
            found[int(m.group(1))] = p
    return found


def _to_one_indexed(raw: dict[int, Path]) -> dict[int, Path]:
    """Shift a zero-indexed mapping to 1-indexed."""
    if raw and min(raw) == 0:
        return {k + 1: v for k, v in raw.items()}
    return raw


def _write_frames(numbered: dict[int, Path], dest: Path, overwrite: bool) -> int:
    """Copy numbered frames to dest using SHIME_MAP names. Returns count copied."""
    dest.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for f in dest.glob("*.png"):
            f.unlink(missing_ok=True)
    count = 0
    for num, src in sorted(numbered.items()):
        name = SHIME_MAP.get(num)
        if name:
            shutil.copy2(src, dest / name)
            count += 1
        else:
            shutil.copy2(src, dest / src.name)
    return count


def import_from_zip(zip_path: Path, char_name: str, overwrite: bool = False) -> str:
    """
    Extract a traditional Shimeji ZIP and import its numbered sprites.
    Handles both img/Shimeji/ sub-folder layouts and flat ZIPs.
    """
    dest = CHARACTERS_DIR / char_name
    if dest.exists() and not overwrite:
        raise FileExistsError(f"'{char_name}' already exists.")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)

        numbered = _to_one_indexed(_collect_shime_images(tmp_dir))
        if not numbered:
            raise ValueError("No shime*.png files found in the ZIP archive.")

        count = _write_frames(numbered, dest, overwrite)

    return f"Imported {count} frames from ZIP."


def import_from_spritesheet(img_path: Path, char_name: str, overwrite: bool = False) -> str:
    """
    Split a 6×8 grid of 128×128 frames and save as a named character.
    Frames are mapped using SHIME_MAP (shime1 → stand.png, etc.).
    """
    try:
        from PIL import Image
    except ImportError:
        raise ImportError("Pillow is required.  Run:  pip install Pillow")

    dest = CHARACTERS_DIR / char_name
    if dest.exists() and not overwrite:
        raise FileExistsError(f"'{char_name}' already exists.")

    img = Image.open(img_path).convert("RGBA")
    cols, rows = 6, 8
    fw = img.width  // cols
    fh = img.height // rows

    dest.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for f in dest.glob("*.png"):
            f.unlink(missing_ok=True)

    count = 0
    for i in range(cols * rows):
        num = i + 1
        col, row = i % cols, i // cols
        frame = img.crop((col * fw, row * fh, (col + 1) * fw, (row + 1) * fh))
        name = SHIME_MAP.get(num)
        if name:
            frame.save(dest / name)
            count += 1

    return f"Imported {count} frames from spritesheet."


# ── Character discovery ────────────────────────────────────────────────────────

def list_characters() -> list[tuple[str, Path]]:
    """
    Return [(display_name, sprite_dir), ...].
    "default" (the built-in SHIMEJI-TEMPLATE) is always first.
    """
    chars: list[tuple[str, Path]] = [("default", TEMPLATE_DIR)]
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    for d in sorted(CHARACTERS_DIR.iterdir()):
        if d.is_dir() and (d / "stand.png").exists():
            chars.append((d.name, d))
    return chars


# ── Launcher window ────────────────────────────────────────────────────────────

class LauncherWindow(QWidget):
    """
    Main launcher GUI.  Manages character imports and spawns/kills Mascot instances
    within the same Qt application, so settings toggles take effect immediately.
    """

    _ICON_SIZE = 56

    def __init__(self, screen, app: QApplication):
        super().__init__()
        self._screen = screen
        self._app    = app

        # Lazy import — shimeji.py's module level is safe to import.
        from shimeji import Mascot, Environment, SpriteCache, DebugPanel, _spawn
        self._Mascot     = Mascot
        self._Env        = Environment
        self._SprCache   = SpriteCache
        self._DebugPanel = DebugPanel
        self._spawn      = _spawn

        # Tell mascots not to quit the app when the last one is dismissed.
        Mascot._keep_alive = True

        # Shared environment (one per app)
        if Mascot._env is None:
            Mascot._env = Environment()

        self._last_import_dir = str(Path.home())

        self._setup_ui()

        # Refresh running-count display every 500 ms
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync_ui)
        self._timer.start(500)

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("SHIMEKAMI")
        self.setFixedSize(420, 530)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ── Header ────────────────────────────────────────────────────────────
        header = QLabel("Shimekami")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        header.setFont(f)
        root.addWidget(header)

        # ── Character list + preview ──────────────────────────────────────────
        body = QHBoxLayout()
        body.setSpacing(8)
        root.addLayout(body)

        # Left: list
        list_col = QVBoxLayout()
        list_col.setSpacing(4)
        body.addLayout(list_col, stretch=1)

        list_col.addWidget(QLabel("Characters"))
        self._char_list = QListWidget()
        self._char_list.setIconSize(QSize(self._ICON_SIZE, self._ICON_SIZE))
        self._char_list.setSpacing(2)
        self._char_list.setMinimumHeight(200)
        self._char_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._char_list.currentRowChanged.connect(self._on_select)
        self._char_list.itemDoubleClicked.connect(self._on_double_click)
        self._char_list.customContextMenuRequested.connect(self._on_context_menu)
        list_col.addWidget(self._char_list)

        # Character action buttons
        char_btns = QHBoxLayout()
        char_btns.setSpacing(4)
        self._btn_launch = QPushButton("Launch")
        self._btn_launch.clicked.connect(self._launch)
        self._btn_kill   = QPushButton("Kill Selected")
        self._btn_kill.clicked.connect(self._kill_selected)
        char_btns.addWidget(self._btn_launch)
        char_btns.addWidget(self._btn_kill)
        list_col.addLayout(char_btns)

        # Right: preview panel
        right_col = QVBoxLayout()
        right_col.setSpacing(6)
        right_col.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.addLayout(right_col, stretch=0)

        self._preview = QLabel()
        self._preview.setFixedSize(110, 110)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            "background: rgba(128,128,128,0.12); border-radius: 6px;"
        )
        right_col.addWidget(self._preview)

        self._lbl_char = QLabel("—")
        f2 = QFont()
        f2.setBold(True)
        self._lbl_char.setFont(f2)
        self._lbl_char.setWordWrap(True)
        self._lbl_char.setFixedWidth(110)
        right_col.addWidget(self._lbl_char)

        self._lbl_count = QLabel("0 running")
        self._lbl_count.setFixedWidth(110)
        right_col.addWidget(self._lbl_count)

        right_col.addStretch()

        # ── Separator ─────────────────────────────────────────────────────────
        root.addWidget(self._hline())

        # ── Settings group ────────────────────────────────────────────────────
        settings_box = QGroupBox("Global Settings")
        settings_layout = QVBoxLayout(settings_box)
        settings_layout.setSpacing(4)
        settings_layout.setContentsMargins(8, 6, 8, 6)

        self._cb_debug  = QCheckBox("Debug Panel")
        self._cb_clone  = QCheckBox("Allow Cloning")
        self._cb_throw  = QCheckBox("Allow Window Throwing")

        self._cb_clone.setChecked(self._Mascot._cloning_on)
        self._cb_throw.setChecked(self._Mascot._win_throw_on)

        self._cb_debug.toggled.connect(self._toggle_debug)
        self._cb_clone.toggled.connect(self._toggle_clone)
        self._cb_throw.toggled.connect(self._toggle_throw)

        row1 = QHBoxLayout()
        row1.addWidget(self._cb_debug)
        row1.addWidget(self._cb_clone)
        row1.addWidget(self._cb_throw)
        row1.addStretch()
        settings_layout.addLayout(row1)

        root.addWidget(settings_box)

        # ── Separator ─────────────────────────────────────────────────────────
        root.addWidget(self._hline())

        # ── Import + kill-all row ─────────────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        root.addLayout(bottom)

        btn_zip     = QPushButton("Import ZIP…")
        btn_sheet   = QPushButton("Import Spritesheet…")
        btn_folder  = QPushButton("Show Folder")
        btn_killall = QPushButton("Kill All")
        btn_killall.setStyleSheet("QPushButton { color: #cc3333; }")

        btn_zip.clicked.connect(self._import_zip)
        btn_sheet.clicked.connect(self._import_spritesheet)
        btn_folder.clicked.connect(self._open_characters_folder)
        btn_killall.clicked.connect(self._kill_all)

        bottom.addWidget(btn_zip)
        bottom.addWidget(btn_sheet)
        bottom.addWidget(btn_folder)
        bottom.addStretch()
        bottom.addWidget(btn_killall)

        # Populate list
        self._populate_list()

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    # ── Character list helpers ─────────────────────────────────────────────────

    def _make_icon(self, sprite_dir: Path) -> QIcon:
        stand = sprite_dir / "stand.png"
        if stand.exists():
            pm = QPixmap(str(stand)).scaled(
                self._ICON_SIZE, self._ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            pm = QPixmap(self._ICON_SIZE, self._ICON_SIZE)
            pm.fill(Qt.GlobalColor.transparent)
        return QIcon(pm)

    def _populate_list(self):
        """Rebuild the character list from disk."""
        prev_row = self._char_list.currentRow()
        self._char_list.clear()
        for name, sprite_dir in list_characters():
            item = QListWidgetItem(self._make_icon(sprite_dir), name)
            item.setData(Qt.ItemDataRole.UserRole, str(sprite_dir))
            self._char_list.addItem(item)
        if prev_row >= 0 and prev_row < self._char_list.count():
            self._char_list.setCurrentRow(prev_row)
        elif self._char_list.count():
            self._char_list.setCurrentRow(0)

    def _current_char(self) -> tuple[str, Path] | None:
        item = self._char_list.currentItem()
        if not item:
            return None
        return item.text(), Path(item.data(Qt.ItemDataRole.UserRole))

    def _running_for(self, sprite_dir: Path) -> list:
        """Mascots whose sprite directory matches sprite_dir (includes clones)."""
        return [m for m in self._Mascot._all if m._sprites._dir == sprite_dir]

    # ── Sync UI state ─────────────────────────────────────────────────────────

    def _sync_ui(self):
        """Called periodically to sync running counts and checkbox states."""
        # Update running count label
        sel = self._current_char()
        if sel:
            running = len(self._running_for(sel[1]))
            self._lbl_count.setText(
                f"{running} running" if running != 1 else "1 running"
            )

        # Sync debug checkbox without triggering the signal
        self._cb_debug.blockSignals(True)
        self._cb_debug.setChecked(self._DebugPanel.is_visible())
        self._cb_debug.blockSignals(False)

        # Sync cloning / throwing (user might change via right-click menu)
        self._cb_clone.blockSignals(True)
        self._cb_clone.setChecked(self._Mascot._cloning_on)
        self._cb_clone.blockSignals(False)

        self._cb_throw.blockSignals(True)
        self._cb_throw.setChecked(self._Mascot._win_throw_on)
        self._cb_throw.blockSignals(False)

    def _on_select(self):
        """Update the preview panel when the selection changes."""
        sel = self._current_char()
        if not sel:
            self._preview.clear()
            self._lbl_char.setText("—")
            self._lbl_count.setText("0 running")
            return

        name, sprite_dir = sel
        stand = sprite_dir / "stand.png"
        if stand.exists():
            pm = QPixmap(str(stand)).scaled(
                100, 100,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview.setPixmap(pm)
        else:
            self._preview.clear()

        self._lbl_char.setText(name)
        running = len(self._running_for(sprite_dir))
        self._lbl_count.setText(
            f"{running} running" if running != 1 else "1 running"
        )

    # ── Launch / kill ─────────────────────────────────────────────────────────

    def _launch(self):
        sel = self._current_char()
        if not sel:
            return
        _, sprite_dir = sel
        sprites = self._SprCache(sprite_dir)
        self._spawn(sprites, self._screen)
        self._on_select()

    def _kill_selected(self):
        sel = self._current_char()
        if not sel:
            return
        _, sprite_dir = sel
        for m in self._running_for(sprite_dir):
            m._dismiss()
        self._on_select()

    def _kill_all(self):
        for m in list(self._Mascot._all):
            m._dismiss()
        self._on_select()

    def _on_double_click(self, item: QListWidgetItem):
        """Double-click: spawn one more of this character."""
        self._launch()

    def _on_context_menu(self, pos: QPoint):
        """Right-click: show context menu with delete option."""
        item = self._char_list.itemAt(pos)
        if not item:
            return
        name = item.text()
        if name == "default":
            return  # built-in character cannot be deleted

        menu = QMenu(self)
        act_delete = menu.addAction(f"Delete '{name}'")
        chosen = menu.exec(self._char_list.viewport().mapToGlobal(pos))
        if chosen is act_delete:
            self._delete_character(name)

    def _delete_character(self, name: str):
        char_dir = CHARACTERS_DIR / name
        # Kill any running mascots for this character first
        for m in self._running_for(char_dir):
            m._dismiss()

        reply = QMessageBox.question(
            self, "Delete character",
            f"Permanently delete '{name}' and all its sprites?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        shutil.rmtree(char_dir, ignore_errors=True)
        self._populate_list()

    # ── Settings toggles ──────────────────────────────────────────────────────

    def _toggle_debug(self, checked: bool):
        if checked != self._DebugPanel.is_visible():
            self._DebugPanel.toggle(self._screen)

    def _toggle_clone(self, checked: bool):
        self._Mascot._cloning_on = checked

    def _toggle_throw(self, checked: bool):
        self._Mascot._win_throw_on = checked

    # ── Import helpers ────────────────────────────────────────────────────────

    def _ask_char_name(self, default: str) -> str | None:
        """Prompt for a character name; return None if cancelled."""
        name, ok = QInputDialog.getText(
            self, "Character Name",
            "Name for this character\n(lowercase, no spaces):",
            text=re.sub(r"[^a-z0-9_]", "_", default.lower()),
        )
        if not ok or not name.strip():
            return None
        return re.sub(r"[^a-z0-9_]", "_", name.strip().lower())

    def _confirm_overwrite(self, name: str) -> bool:
        reply = QMessageBox.question(
            self, "Character exists",
            f"'{name}' already exists.\nOverwrite it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _run_import(self, fn, *args, **kwargs):
        """Execute an import function and show result/error in a message box."""
        try:
            msg = fn(*args, **kwargs)
            QMessageBox.information(self, "Import complete", msg)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))
        self._populate_list()

    def _import_zip(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import Shimeji ZIP(s)", self._last_import_dir, "ZIP files (*.zip)"
        )
        if not paths:
            return
        self._last_import_dir = str(Path(paths[0]).parent)

        errors: list[str] = []
        for path in paths:
            zip_path = Path(path)
            name = self._ask_char_name(zip_path.stem)
            if not name:
                continue
            overwrite = False
            if (CHARACTERS_DIR / name).exists():
                if not self._confirm_overwrite(name):
                    continue
                overwrite = True
            try:
                msg = import_from_zip(zip_path, name, overwrite=overwrite)
                QMessageBox.information(self, "Import complete", msg)
            except Exception as e:
                errors.append(f"{zip_path.name}: {e}")

        if errors:
            QMessageBox.critical(self, "Import failed", "\n".join(errors))
        self._populate_list()

    def _import_spritesheet(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import Spritesheet(s)", self._last_import_dir,
            "Images (*.png *.jpg *.jpeg *.webp)",
        )
        if not paths:
            return
        self._last_import_dir = str(Path(paths[0]).parent)

        errors: list[str] = []
        for path in paths:
            img_path = Path(path)
            name = self._ask_char_name(img_path.stem)
            if not name:
                continue
            overwrite = False
            if (CHARACTERS_DIR / name).exists():
                if not self._confirm_overwrite(name):
                    continue
                overwrite = True
            try:
                msg = import_from_spritesheet(img_path, name, overwrite=overwrite)
                QMessageBox.information(self, "Import complete", msg)
            except Exception as e:
                errors.append(f"{img_path.name}: {e}")

        if errors:
            QMessageBox.critical(self, "Import failed", "\n".join(errors))
        self._populate_list()

    def _open_characters_folder(self):
        CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(CHARACTERS_DIR)])

    # ── Window events ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        """Closing the launcher kills all mascots and exits the app."""
        for m in list(self._Mascot._all):
            m._timer.stop()
            m.close()
        self._Mascot._all.clear()
        self._Mascot._keep_alive = False
        event.accept()
        QApplication.instance().quit()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    screen = app.primaryScreen().availableGeometry()

    launcher = LauncherWindow(screen, app)
    launcher.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
