APP_STYLESHEET = """
QWidget {
    background-color: #111827;
    color: #e5e7eb;
    font-family: 'Segoe UI';
    font-size: 10pt;
}
QMainWindow, QDialog {
    background-color: #0f172a;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QListWidget, QTreeWidget, QTableWidget {
    background-color: #1f2937;
    border: 1px solid #374151;
    border-radius: 8px;
    padding: 6px;
    selection-background-color: #2563eb;
}
QPushButton {
    background-color: #2563eb;
    border: none;
    border-radius: 8px;
    padding: 8px 14px;
    color: white;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #1d4ed8;
}
QPushButton:disabled {
    background-color: #475569;
    color: #cbd5e1;
}
QFrame#Card {
    background-color: #111827;
    border: 1px solid #243041;
    border-radius: 14px;
}
QHeaderView::section {
    background-color: #1e293b;
    padding: 8px;
    border: none;
    color: #cbd5e1;
}
QTableWidget {
    gridline-color: #334155;
}
QTableWidget::item:selected {
    background-color: #1d4ed8;
}
QTabWidget::pane {
    border: 1px solid #243041;
    border-radius: 12px;
}
QTabBar::tab {
    background: #111827;
    padding: 10px 14px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}
QTabBar::tab:selected {
    background: #1e293b;
}
QLabel[role='heading'] {
    font-size: 18pt;
    font-weight: 700;
}
QLabel[role='subtle'] {
    color: #94a3b8;
}
"""

# ---------------------------------------------------------------------------
# Public theme API used by the tabs and dialogs.
#
# Conventions widgets rely on:
# - Buttons: ``button.setProperty("variant", "primary" | "secondary" | "danger")``.
# - Labels: ``setProperty("role", "heading" | "subtle" | "empty-title")``.
# - Colours for status text come from :func:`status_color`, never hard-coded,
#   so they stay readable in both light and dark mode.
# ---------------------------------------------------------------------------

from PySide6.QtGui import QColor, QGuiApplication, QPalette

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


def is_dark() -> bool:
    """True when the active palette is dark."""
    app = QGuiApplication.instance()
    if app is None:
        return True
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def status_color(kind: str) -> QColor:
    """Colour for a status *kind*: ok, update, error, warning, muted, running, accent."""
    light, dark = _STATUS_COLORS.get(kind, _STATUS_COLORS["muted"])
    return QColor(dark if is_dark() else light)
