"""The shell imports and builds with throwaway data (catches wiring/syntax errors in main_window)."""

from __future__ import annotations

from PySide6.QtCore import QSettings

from portable_manager.controller import AppController
from portable_manager.models import AppSettings


def test_main_window_builds_and_quits(qapp, tmp_storage, tmp_path, monkeypatch):
    import portable_manager.ui.main_window as mw

    monkeypatch.setattr(mw, "QSettings", lambda *a, **k: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    tmp_storage.save_settings(AppSettings(
        install_root=str(tmp_path / "apps"), download_cache=str(tmp_path / "cache"),
        run_update_check_on_startup=True,
    ))
    window = mw.MainWindow(AppController(tmp_storage))
    try:
        assert window.tabs.count() == 2
        assert window._check_timer.isActive()  # follows the setting
        window.controller.settings.run_update_check_on_startup = False
        window._apply_update_schedule()
        assert not window._check_timer.isActive()
        window._update_tray_menu()
        assert [a.text() for a in window._tray_launch_menu.actions()] == ["No programs yet"]
    finally:
        window._quitting = True
        window._check_timer.stop()
        window._tray_icon.hide()
        window.deleteLater()
