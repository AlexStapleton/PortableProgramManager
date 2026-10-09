"""Icon factory for Portable Program Manager.

Loads icons from the ``icons/`` asset folder that lives next to this module.
If an asset file is missing or unreadable, each function falls back to a
QPainter-drawn icon so the app always has a visible icon regardless of whether
the asset files are present.

Asset files used
----------------
icons/PortableProgramManager_icon_opt_16.png   – 16 px window icon
icons/PortableProgramManager_icon_opt_32.png   – 32 px window icon
icons/PortableProgramManager_icon_opt_48.png   – 48 px window icon
icons/PortableProgramManager_icon_opt_256.png  – 256 px window icon
icons/PortableProgramManager_icon_transparent.png – full-size fallback PNG
icons/PortableProgramManager_icon_small_optimized.ico – tray icon (preferred)
icons/PortableProgramManager_icon_optimized.ico       – tray icon (fallback)
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap

# Folder that contains the bundled asset files.
# Path(__file__) is   …/src/portable_manager/ui/icons.py
# _ICONS_DIR  is       …/src/portable_manager/ui/icons/
_ICONS_DIR = Path(__file__).parent / "icons"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_window_icon() -> QIcon:
    """Return the application window / taskbar icon.

    Builds a multi-resolution QIcon from the bundled PNG assets.
    Falls back to a programmatic icon if the asset files are not found.
    """
    icon = QIcon()

    # Add each available sized PNG at its natural resolution.
    sized_pngs = [
        ("PortableProgramManager_icon_opt_16.png",  16),
        ("PortableProgramManager_icon_opt_32.png",  32),
        ("PortableProgramManager_icon_opt_48.png",  48),
        ("PortableProgramManager_icon_opt_256.png", 256),
    ]
    for filename, _size in sized_pngs:
        path = _ICONS_DIR / filename
        if path.is_file():
            pix = QPixmap(str(path))
            if not pix.isNull():
                icon.addPixmap(pix)

    if not icon.availableSizes():
        # Try the single full-size transparent PNG as a last resort.
        full_png = _ICONS_DIR / "PortableProgramManager_icon_transparent.png"
        if full_png.is_file():
            pix = QPixmap(str(full_png))
            if not pix.isNull():
                icon.addPixmap(pix)

    if not icon.availableSizes():
        # No asset files found — draw the icon programmatically.
        return _make_window_icon_programmatic()

    return icon


def make_tray_icon() -> QIcon:
    """Return the system-tray icon.

    Loads the bundled .ico file, which already contains multiple sizes.
    Falls back to a programmatic icon if the file is not found.
    """
    for filename in (
        "PortableProgramManager_icon_small_optimized.ico",
        "PortableProgramManager_icon_optimized.ico",
    ):
        path = _ICONS_DIR / filename
        if path.is_file():
            icon = QIcon(str(path))
            if icon.availableSizes():
                return icon

    # No .ico found — draw the tray icon programmatically.
    return _make_tray_icon_programmatic()


# ---------------------------------------------------------------------------
# Programmatic fallbacks (no external files required)
# ---------------------------------------------------------------------------

def _make_window_icon_programmatic() -> QIcon:
    """Draw a multi-resolution window icon with QPainter."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw_window_icon(size))
    return icon


def _draw_window_icon(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Rounded-square background: deep navy blue
    p.setBrush(QBrush(QColor("#1A3C5C")))
    p.setPen(Qt.PenStyle.NoPen)
    radius = max(2, int(size * 0.18))
    p.drawRoundedRect(0, 0, size, size, radius, radius)

    # Bold white "P" centred in the square
    p.setPen(QColor("white"))
    font = QFont()
    font.setPixelSize(max(8, int(size * 0.58)))
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "P")

    p.end()
    return pix


def _make_tray_icon_programmatic() -> QIcon:
    """Draw a multi-resolution tray icon with QPainter.

    Uses a circle (vs. rounded square for the window icon) and bright teal
    (vs. navy) so it stands out in the Windows notification area.
    """
    icon = QIcon()
    for size in (16, 22, 24, 32, 48):
        icon.addPixmap(_draw_tray_icon(size))
    return icon


def _draw_tray_icon(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Circular background: bright teal — pops against both dark and light trays
    p.setBrush(QBrush(QColor("#0AAFE6")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)

    # Bold white "P" centred in the circle
    p.setPen(QColor("white"))
    font = QFont()
    font.setPixelSize(max(7, int(size * 0.56)))
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "P")

    p.end()
    return pix
