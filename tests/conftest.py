from __future__ import annotations

import os

import pytest

# Qt must never try to open real windows during tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def tmp_storage(tmp_path):
    """A Storage rooted in a temp dir so tests never touch the user's real data."""
    from portable_manager.storage import Storage

    return Storage(base_dir=tmp_path / "appdata")


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole test session (widgets need QApplication, not QCoreApplication)."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
