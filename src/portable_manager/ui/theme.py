"""Light/dark theme for the whole application.

Three layers are applied together by :func:`apply_theme`:

1. Fusion style with a complete ``QPalette`` (so natively drawn parts such as
   scrollbars, menus and indicators follow the mode).
2. One application stylesheet generated from a single template and two token
   sets (light and dark).
3. Windows title bars switched to dark or light through DWM.

Mode ``"system"`` follows the Windows light/dark setting and tracks it live.

Public API (keep names stable; other UI modules depend on them):

- ``is_dark()``                       current effective mode
- ``status_color(kind)``              readable status colour for the mode
- ``apply_theme(app, mode)``          mode is "system" | "light" | "dark"
- ``install_title_bar_sync(app)``     keep every top-level title bar in sync
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tempfile
from pathlib import Path
from string import Template

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour tokens
# ---------------------------------------------------------------------------

_DARK = {
    # Surfaces
    "bg": "#0f172a",            # window background
    "surface": "#111827",       # cards, tables, tab pages
    "surface_alt": "#0b1120",   # alternate rows, status bar
    "field": "#1f2937",         # text inputs, combo/spin boxes
    "field_border": "#374151",
    "border": "#243041",        # hairlines around cards, panes, tables
    "border_strong": "#3f4d63",
    "border_hover": "#56657d",
    # Text
    "text": "#e5e7eb",
    "text_muted": "#94a3b8",
    "text_disabled": "#64748b",
    "placeholder": "#64748b",
    "link": "#60a5fa",
    # Buttons
    "button_bg": "#1e293b",
    "button_text": "#e5e7eb",
    "button_hover": "#263349",
    "button_pressed": "#334155",
    "disabled_bg": "#1a2332",
    # Accent and states
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_pressed": "#1e40af",
    "accent_text": "#ffffff",
    "focus": "#3b82f6",
    "danger": "#ef4444",
    "danger_text": "#fca5a5",
    "danger_pressed": "#b91c1c",
    # Item views
    "header_bg": "#1e293b",
    "header_text": "#cbd5e1",
    "grid": "#2b3a50",
    "row_alt": "#151f30",
    "row_hover": "#1e2a3f",
    "row_selected": "#1d4ed8",
    "row_selected_text": "#ffffff",
    # Menus, tooltips, chrome
    "menu_bg": "#1e293b",
    "tooltip_bg": "#334155",
    "tooltip_text": "#f1f5f9",
    "scroll": "#475569",
    "scroll_hover": "#64748b",
    "check_border": "#64748b",
    "track": "#1e293b",
    "status_bg": "#0b1120",
    # Palette-only bevel roles
    "mid": "#243041",
    "midlight": "#334155",
    "dark": "#0b1120",
    "light": "#334155",
    "shadow": "#000000",
    "bright": "#ffffff",
}

_LIGHT = {
    "bg": "#f3f4f6",
    "surface": "#ffffff",
    "surface_alt": "#f9fafb",
    "field": "#ffffff",
    "field_border": "#d1d5db",
    "border": "#e2e5ea",
    "border_strong": "#cfd4dc",
    "border_hover": "#9ca3af",
    "text": "#111827",
    "text_muted": "#6b7280",
    "text_disabled": "#9ca3af",
    "placeholder": "#9ca3af",
    "link": "#1d4ed8",
    "button_bg": "#ffffff",
    "button_text": "#111827",
    "button_hover": "#f3f4f6",
    "button_pressed": "#e5e7eb",
    "disabled_bg": "#eef0f3",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_pressed": "#1e40af",
    "accent_text": "#ffffff",
    "focus": "#2563eb",
    "danger": "#dc2626",
    "danger_text": "#b91c1c",
    "danger_pressed": "#991b1b",
    "header_bg": "#f9fafb",
    "header_text": "#374151",
    "grid": "#e5e7eb",
    "row_alt": "#f8fafc",
    "row_hover": "#eef2ff",
    "row_selected": "#dbeafe",
    "row_selected_text": "#111827",
    "menu_bg": "#ffffff",
    "tooltip_bg": "#1f2937",
    "tooltip_text": "#f9fafb",
    "scroll": "#c3c9d3",
    "scroll_hover": "#9ca3af",
    "check_border": "#6b7280",
    "track": "#e5e7eb",
    "status_bg": "#e9ebef",
    "mid": "#d1d5db",
    "midlight": "#e5e7eb",
    "dark": "#9ca3af",
    "light": "#ffffff",
    "shadow": "#6b7280",
    "bright": "#ffffff",
}

# kind -> (light-mode colour, dark-mode colour)
_STATUS_COLORS = {
    "ok": ("#15803d", "#4ade80"),
    "update": ("#1d4ed8", "#60a5fa"),
    "error": ("#b91c1c", "#f87171"),
    "warning": ("#b45309", "#fbbf24"),
    "muted": ("#6b7280", "#94a3b8"),
    "running": ("#7e22ce", "#c084fc"),
    "accent": ("#2563eb", "#3b82f6"),
}

# ---------------------------------------------------------------------------
# Stylesheet template: one template, two token sets.
# ---------------------------------------------------------------------------

_STYLE_TEMPLATE = Template(
    """
QWidget {
    font-family: "Segoe UI";
    font-size: 10pt;
}
QToolTip {
    background-color: $tooltip_bg;
    color: $tooltip_text;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 5px 8px;
}

/* Labels ------------------------------------------------------------- */
QLabel[role="heading"] {
    font-size: 18pt;
    font-weight: 700;
}
QLabel[role="subtle"] {
    color: $text_muted;
}
QLabel[role="empty-title"] {
    font-size: 14pt;
    font-weight: 600;
}

/* Text input --------------------------------------------------------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: $field;
    color: $text;
    border: 1px solid $field_border;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: $accent;
    selection-color: $accent_text;
}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover,
QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
    border-color: $border_hover;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid $focus;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    background-color: $disabled_bg;
    color: $text_disabled;
    border-color: $disabled_bg;
}
QComboBox {
    padding-right: 26px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("$arrow_down_png");
    width: 10px;
    height: 10px;
}
QComboBox::down-arrow:disabled {
    image: url("$arrow_disabled_png");
}
QComboBox QAbstractItemView {
    background-color: $surface;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: 6px;
    padding: 4px;
    outline: 0;
    selection-background-color: $accent;
    selection-color: $accent_text;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 2px 8px;
}
QComboBox QAbstractItemView::item:hover {
    background-color: $row_hover;
    color: $text;
}
QComboBox QAbstractItemView::item:selected {
    background-color: $accent;
    color: $accent_text;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    width: 18px;
    border: none;
    background: transparent;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-position: top right;
    margin: 2px 2px 0 0;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-position: bottom right;
    margin: 0 2px 2px 0;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: $button_hover;
    border-radius: 4px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url("$arrow_up_png");
    width: 8px;
    height: 8px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url("$arrow_down_png");
    width: 8px;
    height: 8px;
}
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled,
QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {
    image: url("$arrow_disabled_png");
}

/* Buttons: default and "secondary" ----------------------------------- */
QPushButton, QPushButton[variant="secondary"] {
    background-color: $button_bg;
    color: $button_text;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover, QPushButton[variant="secondary"]:hover {
    background-color: $button_hover;
    border-color: $border_hover;
}
QPushButton:pressed, QPushButton[variant="secondary"]:pressed {
    background-color: $button_pressed;
}
QPushButton:focus, QPushButton[variant="secondary"]:focus {
    border-color: $focus;
}
QPushButton:disabled, QPushButton[variant="secondary"]:disabled {
    background-color: $disabled_bg;
    color: $text_disabled;
    border-color: $disabled_bg;
}

/* Buttons: primary (filled accent) ------------------------------------ */
QPushButton[variant="primary"] {
    background-color: $accent;
    color: $accent_text;
    border: 1px solid $accent;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background-color: $accent_hover;
    border-color: $accent_hover;
}
QPushButton[variant="primary"]:pressed {
    background-color: $accent_pressed;
    border-color: $accent_pressed;
}
QPushButton[variant="primary"]:focus {
    border-color: $focus;
}
QPushButton[variant="primary"]:disabled {
    background-color: $disabled_bg;
    color: $text_disabled;
    border-color: $disabled_bg;
}

/* Buttons: danger (outline that fills on hover) ----------------------- */
QPushButton[variant="danger"] {
    background-color: transparent;
    color: $danger_text;
    border: 1px solid $danger;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton[variant="danger"]:hover {
    background-color: $danger;
    color: #ffffff;
    border-color: $danger;
}
QPushButton[variant="danger"]:pressed {
    background-color: $danger_pressed;
    color: #ffffff;
    border-color: $danger_pressed;
}
QPushButton[variant="danger"]:focus {
    border-color: $danger;
}
QPushButton[variant="danger"]:disabled {
    background-color: transparent;
    color: $text_disabled;
    border-color: $border_strong;
}

QToolButton {
    background-color: transparent;
    color: $text;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 8px;
}
QToolButton:hover {
    background-color: $button_hover;
    border-color: $border;
}
QToolButton:pressed, QToolButton:checked {
    background-color: $button_pressed;
}
QToolButton:disabled {
    color: $text_disabled;
}

/* Item views ---------------------------------------------------------- */
QTableView, QTableWidget, QTreeView, QTreeWidget, QListView, QListWidget {
    background-color: $surface;
    alternate-background-color: $row_alt;
    color: $text;
    border: 1px solid $border;
    border-radius: 10px;
    gridline-color: $grid;
    selection-background-color: $row_selected;
    selection-color: $row_selected_text;
    outline: 0;
}
QTableView::item, QTableWidget::item, QTreeView::item, QTreeWidget::item,
QListView::item, QListWidget::item {
    padding: 4px 6px;
    border: none;
}
QTableView::item:hover, QTableWidget::item:hover,
QTreeView::item:hover, QTreeWidget::item:hover,
QListView::item:hover, QListWidget::item:hover {
    background-color: $row_hover;
    color: $text;
}
QTableView::item:selected, QTableWidget::item:selected,
QTreeView::item:selected, QTreeWidget::item:selected,
QListView::item:selected, QListWidget::item:selected {
    background-color: $row_selected;
    color: $row_selected_text;
}
QTableCornerButton::section {
    background-color: $header_bg;
    border: none;
}
QHeaderView::section {
    background-color: $header_bg;
    color: $header_text;
    padding: 8px;
    border: none;
    border-bottom: 1px solid $grid;
    font-weight: 600;
}

/* Tabs ---------------------------------------------------------------- */
QTabWidget::pane {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 10px;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: $text_muted;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 8px 14px;
    margin-right: 4px;
}
QTabBar::tab:hover {
    color: $text;
    background-color: $button_hover;
}
QTabBar::tab:selected {
    color: $text;
    background-color: $surface;
    border-color: $border;
    font-weight: 600;
}

/* Menus --------------------------------------------------------------- */
QMenuBar {
    background-color: transparent;
}
QMenuBar::item {
    background: transparent;
    padding: 4px 8px;
    border-radius: 6px;
}
QMenuBar::item:selected {
    background-color: $button_hover;
}
QMenu {
    background-color: $menu_bg;
    color: $text;
    border: 1px solid $border_strong;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    background-color: transparent;
    padding: 6px 28px 6px 12px;
    border-radius: 6px;
    margin: 1px 2px;
}
QMenu::item:selected {
    background-color: $accent;
    color: $accent_text;
}
QMenu::item:disabled {
    color: $text_disabled;
}
QMenu::separator {
    height: 1px;
    background-color: $border;
    margin: 6px 8px;
}

/* Scroll bars: slim, rounded, no arrows ------------------------------- */
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: $scroll;
    border-radius: 4px;
    min-height: 28px;
    margin: 2px 3px;
}
QScrollBar::handle:vertical:hover {
    background-color: $scroll_hover;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: $scroll;
    border-radius: 4px;
    min-width: 28px;
    margin: 3px 2px;
}
QScrollBar::handle:horizontal:hover {
    background-color: $scroll_hover;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
    border: none;
    background: none;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}

/* Check boxes and radio buttons --------------------------------------- */
QCheckBox, QRadioButton {
    spacing: 8px;
}
QCheckBox:disabled, QRadioButton:disabled {
    color: $text_disabled;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
    background-color: $field;
    border: 1px solid $check_border;
}
QCheckBox::indicator {
    border-radius: 4px;
}
QRadioButton::indicator {
    border-radius: 8px;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: $accent;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: $accent;
    border-color: $accent;
}
QCheckBox::indicator:checked {
    image: url("$check_png");
}
QRadioButton::indicator:checked {
    image: url("$dot_png");
}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background-color: $disabled_bg;
    border-color: $border_strong;
}
/* Checkboxes inside tables and lists (e.g. Settings → Sources) */
QAbstractItemView::indicator {
    width: 15px;
    height: 15px;
    background-color: $field;
    border: 1px solid $check_border;
    border-radius: 4px;
}
QAbstractItemView::indicator:checked {
    background-color: $accent;
    border-color: $accent;
    image: url("$check_png");
}

/* Progress bar: thin with accent chunk -------------------------------- */
QProgressBar {
    background-color: $track;
    border: none;
    border-radius: 4px;
    min-height: 8px;
    max-height: 8px;
    color: transparent;
    text-align: center;
}
QProgressBar::chunk {
    background-color: $accent;
    border-radius: 4px;
}

/* Status bar, group boxes, splitters, cards --------------------------- */
QStatusBar {
    background-color: $status_bg;
    color: $text_muted;
    border-top: 1px solid $border;
}
QStatusBar::item {
    border: none;
}
QGroupBox {
    border: 1px solid $border;
    border-radius: 10px;
    margin-top: 14px;
    padding: 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    color: $text_muted;
}
QSplitter::handle {
    background-color: $border;
}
QSplitter::handle:hover {
    background-color: $border_hover;
}
QFrame#Card {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 14px;
}
"""
)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_state = {"mode": "system", "dark": False}
_scheme_hooked = False
_title_filter: QObject | None = None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_dark() -> bool:
    """True when the currently applied theme is dark."""
    return bool(_state["dark"])


def status_color(kind: str) -> QColor:
    """Colour for a status *kind*: ok, update, error, warning, muted, running, accent."""
    light, dark = _STATUS_COLORS.get(kind, _STATUS_COLORS["muted"])
    return QColor(dark if is_dark() else light)


def apply_theme(app: QApplication, mode: str) -> None:
    """Apply "system" | "light" | "dark" to the application.

    Safe to call repeatedly; each call fully replaces the previous theme.
    """
    if mode not in ("system", "light", "dark"):
        mode = "system"
    _state["mode"] = mode
    dark = _resolve_dark(app, mode)
    _state["dark"] = dark

    tokens = _DARK if dark else _LIGHT
    app.setStyle("Fusion")
    app.setPalette(_build_palette(tokens))
    app.setStyleSheet(_build_stylesheet(tokens, "dark" if dark else "light"))

    _hook_scheme_changes(app)
    _refresh_title_bars(app, dark)


def install_title_bar_sync(app: QApplication) -> None:
    """Keep every top-level window's title bar matching the current theme.

    Installs one application-wide event filter that re-applies the DWM dark
    attribute when a window is shown. Windows only; a no-op elsewhere.
    """
    global _title_filter
    if _title_filter is not None:
        return
    _title_filter = _TitleBarSyncFilter(app)
    app.installEventFilter(_title_filter)
    _refresh_title_bars(app, is_dark())


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_dark(app: QApplication, mode: str) -> bool:
    if mode == "dark":
        return True
    if mode == "light":
        return False
    return _system_scheme_is_dark(app)


def _system_scheme_is_dark(app: QApplication) -> bool:
    try:
        scheme = app.styleHints().colorScheme()
    except Exception:
        return False
    # Unknown falls back to light.
    return scheme == Qt.ColorScheme.Dark


def _hook_scheme_changes(app: QApplication) -> None:
    """Re-apply the theme when Windows switches mode while mode is "system"."""
    global _scheme_hooked
    if _scheme_hooked:
        return
    try:
        app.styleHints().colorSchemeChanged.connect(_on_scheme_changed)
    except Exception:
        log.debug("colorSchemeChanged not available", exc_info=True)
        return
    _scheme_hooked = True


def _on_scheme_changed(*_args) -> None:
    app = QGuiApplication.instance()
    if not isinstance(app, QApplication) or _state["mode"] != "system":
        return
    apply_theme(app, "system")


def _build_palette(t: dict) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["bg"],
        QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["field"],
        QPalette.ColorRole.AlternateBase: t["surface_alt"],
        QPalette.ColorRole.Text: t["text"],
        QPalette.ColorRole.Button: t["button_bg"],
        QPalette.ColorRole.ButtonText: t["button_text"],
        QPalette.ColorRole.Highlight: t["accent"],
        QPalette.ColorRole.HighlightedText: t["accent_text"],
        QPalette.ColorRole.ToolTipBase: t["tooltip_bg"],
        QPalette.ColorRole.ToolTipText: t["tooltip_text"],
        QPalette.ColorRole.PlaceholderText: t["placeholder"],
        QPalette.ColorRole.Link: t["link"],
        QPalette.ColorRole.LinkVisited: t["link"],
        QPalette.ColorRole.Mid: t["mid"],
        QPalette.ColorRole.Midlight: t["midlight"],
        QPalette.ColorRole.Dark: t["dark"],
        QPalette.ColorRole.Shadow: t["shadow"],
        QPalette.ColorRole.Light: t["light"],
        QPalette.ColorRole.BrightText: t["bright"],
        QPalette.ColorRole.Accent: t["accent"],
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))

    disabled = QPalette.ColorGroup.Disabled
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.WindowText,
    ):
        pal.setColor(disabled, role, QColor(t["text_disabled"]))
    pal.setColor(disabled, QPalette.ColorRole.Base, QColor(t["disabled_bg"]))
    pal.setColor(disabled, QPalette.ColorRole.Button, QColor(t["disabled_bg"]))
    pal.setColor(disabled, QPalette.ColorRole.Highlight, QColor(t["border_strong"]))
    pal.setColor(disabled, QPalette.ColorRole.HighlightedText, QColor(t["text_disabled"]))
    return pal


def _build_stylesheet(t: dict, mode: str) -> str:
    tokens = dict(t)
    tokens.update(_theme_images(t, mode))
    return _STYLE_TEMPLATE.substitute(tokens)


def _theme_images(t: dict, mode: str) -> dict[str, str]:
    """Write the small PNGs the stylesheet needs and return their paths.

    Stylesheets that restyle an indicator or arrow drop Fusion's native glyph,
    so each one is drawn as an image. Files go to a per-user temp folder. The
    file name includes the mode, so Qt's pixmap cache never serves a stale
    colour after a switch.
    """
    folder = Path(tempfile.gettempdir()) / "portable_program_manager_theme"
    paths = {
        "check_png": folder / "check.png",
        "dot_png": folder / "dot.png",
        "arrow_up_png": folder / f"arrow_up_{mode}.png",
        "arrow_down_png": folder / f"arrow_down_{mode}.png",
        "arrow_disabled_png": folder / f"arrow_disabled_{mode}.png",
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _render_glyph(paths["check_png"], "tick", "#ffffff")
        _render_glyph(paths["dot_png"], "dot", "#ffffff")
        _render_glyph(paths["arrow_up_png"], "up", t["text_muted"])
        _render_glyph(paths["arrow_down_png"], "down", t["text_muted"])
        _render_glyph(paths["arrow_disabled_png"], "down", t["text_disabled"])
    except Exception:
        log.debug("Could not write theme images", exc_info=True)
    return {key: path.as_posix() for key, path in paths.items()}


def _render_glyph(path: Path, glyph: str, colour: str) -> None:
    size = 16 if glyph in ("tick", "dot") else 10
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(colour))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    if glyph == "tick":
        painter.drawPolyline([QPointF(4.0, 8.5), QPointF(6.8, 11.3), QPointF(12.0, 5.0)])
    elif glyph == "dot":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QPointF(8.0, 8.0), 3.0, 3.0)
    elif glyph == "down":
        painter.drawPolyline([QPointF(2.0, 3.5), QPointF(5.0, 6.5), QPointF(8.0, 3.5)])
    else:  # up
        painter.drawPolyline([QPointF(2.0, 6.5), QPointF(5.0, 3.5), QPointF(8.0, 6.5)])
    painter.end()
    image.save(str(path), "PNG")


def _is_windows_platform() -> bool:
    if sys.platform != "win32":
        return False
    app = QGuiApplication.instance()
    return app is not None and QGuiApplication.platformName() == "windows"


def _set_dwm_dark_mode(widget: QWidget, dark: bool) -> None:
    """Ask DWM for a dark or light title bar. Attribute 20, falling back to 19."""
    if not _is_windows_platform():
        return
    try:
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwmapi = ctypes.windll.dwmapi
        hwnd_arg = ctypes.c_void_p(hwnd)
        # DWMWA_USE_IMMERSIVE_DARK_MODE is 20 on Windows 11 and 19 on older builds.
        hr = dwmapi.DwmSetWindowAttribute(hwnd_arg, 20, ctypes.byref(value), 4)
        if hr != 0:
            dwmapi.DwmSetWindowAttribute(hwnd_arg, 19, ctypes.byref(value), 4)
    except Exception:
        log.debug("Could not set DWM dark mode", exc_info=True)


def _refresh_title_bars(app: QApplication, dark: bool) -> None:
    for widget in app.topLevelWidgets():
        if widget.isVisible():
            _set_dwm_dark_mode(widget, dark)


class _TitleBarSyncFilter(QObject):
    """Application event filter: sync a window's title bar when it is shown."""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override name
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            _set_dwm_dark_mode(obj, is_dark())
        return False
