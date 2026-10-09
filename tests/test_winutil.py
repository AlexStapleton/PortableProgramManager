from __future__ import annotations

from portable_manager.winutil import CREATE_NO_WINDOW, is_elevated, is_windows


def test_is_elevated_returns_bool_without_raising():
    assert isinstance(is_elevated(), bool)


def test_is_windows_returns_bool():
    assert isinstance(is_windows(), bool)


def test_create_no_window_flag_value():
    assert CREATE_NO_WINDOW == 0x08000000
