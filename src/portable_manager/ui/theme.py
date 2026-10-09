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
