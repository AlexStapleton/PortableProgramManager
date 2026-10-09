from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from portable_manager.ui import theme


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        # test_single_instance leaves a bare QCoreApplication behind; theming needs
        # widgets, so replace it with a real QApplication.
        shiboken6.delete(app)
        app = None
    if app is None:
        app = QApplication([])
    yield app
    # Leave a known mode behind so other tests are unaffected.
    theme.apply_theme(app, "light")


def test_light_mode_applies_light_palette_and_stylesheet(qapp):
    theme.apply_theme(qapp, "light")

    assert theme.is_dark() is False
    window = qapp.palette().color(QPalette.ColorRole.Window)
    assert window.lightness() > 128
    assert 'variant="danger"' in qapp.styleSheet()
    assert qapp.styleSheet().strip()


def test_dark_mode_applies_dark_palette_and_stylesheet(qapp):
    theme.apply_theme(qapp, "dark")

    assert theme.is_dark() is True
    window = qapp.palette().color(QPalette.ColorRole.Window)
    assert window.lightness() < 128
    assert 'variant="danger"' in qapp.styleSheet()


def test_switching_modes_replaces_theme(qapp):
    theme.apply_theme(qapp, "dark")
    dark_sheet = qapp.styleSheet()
    theme.apply_theme(qapp, "light")

    assert theme.is_dark() is False
    assert qapp.styleSheet() != dark_sheet


def test_status_colors_differ_between_modes(qapp):
    theme.apply_theme(qapp, "light")
    light = theme.status_color("error")
    theme.apply_theme(qapp, "dark")
    dark = theme.status_color("error")

    assert light.name() != dark.name()


def test_status_color_unknown_kind_falls_back_to_muted(qapp):
    theme.apply_theme(qapp, "light")
    assert theme.status_color("no-such-kind").name() == theme.status_color("muted").name()


def test_system_mode_does_not_raise(qapp):
    theme.apply_theme(qapp, "system")
    # Offscreen tests report an Unknown scheme, which must resolve to light.
    assert theme.is_dark() is False


def test_unknown_mode_is_treated_as_system(qapp):
    theme.apply_theme(qapp, "bogus")
    assert theme.is_dark() is False


def test_install_title_bar_sync_does_not_raise_offscreen(qapp):
    theme.apply_theme(qapp, "dark")
    theme.install_title_bar_sync(qapp)
    # Installing twice must be harmless.
    theme.install_title_bar_sync(qapp)
