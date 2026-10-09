from __future__ import annotations

import pytest

from portable_manager.models import AppSettings, ManagedProgram, UpdatePolicy


def _program(**overrides) -> dict:
    """A minimal valid program payload, with optional overrides."""
    payload = {
        "program_id": "p1",
        "name": "Tool",
        "source_type": "github",
        "source_value": "owner/repo",
        "install_dir": "C:/Apps/Tool",
    }
    payload.update(overrides)
    return payload


def _settings(**overrides) -> dict:
    payload = {"install_root": "C:/Apps", "download_cache": "C:/Cache"}
    payload.update(overrides)
    return payload


# ── UpdatePolicy ──────────────────────────────────────────────────────────


class TestUpdatePolicy:
    def test_none_and_empty_payloads_give_defaults(self):
        assert UpdatePolicy.from_dict(None) == UpdatePolicy()
        assert UpdatePolicy.from_dict({}) == UpdatePolicy()

    @pytest.mark.parametrize("raw, expected", [
        (6, 6),
        ("6", 6),
        (" 12 ", 12),
        (12.0, 12),
        (None, 24),
        (12.5, 24),
        ("abc", 24),
        (True, 24),
        (0, 24),
        (-3, 24),
        ("0", 24),
    ])
    def test_interval_hours_coercion(self, raw, expected):
        assert UpdatePolicy.from_dict({"interval_hours": raw}).interval_hours == expected

    @pytest.mark.parametrize("raw, expected", [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("FALSE", False),
        ("Yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
        ("maybe", True),   # unrecognised string -> default (True)
        (2, True),         # unrecognised int -> default (True)
        (None, True),
    ])
    def test_bool_coercion(self, raw, expected):
        assert UpdatePolicy.from_dict({"check_enabled": raw}).check_enabled is expected

    def test_unknown_keys_are_dropped(self):
        policy = UpdatePolicy.from_dict({"interval_hours": 3, "bogus": 1})
        assert policy.interval_hours == 3
        assert not hasattr(policy, "bogus")

    def test_non_dict_payload_gives_defaults(self):
        assert UpdatePolicy.from_dict("weird") == UpdatePolicy()  # type: ignore[arg-type]

    def test_round_trip(self):
        policy = UpdatePolicy(check_enabled=False, interval_hours=6, channel="pre_release")
        assert UpdatePolicy.from_dict(policy.to_dict()) == policy


# ── ManagedProgram ────────────────────────────────────────────────────────


class TestManagedProgram:
    def test_legacy_nulls_become_defaults(self):
        program = ManagedProgram.from_dict(_program(tags=None, notes=None, launch_args=None))
        assert program.tags == []
        assert program.notes == ""
        assert program.launch_args == ""

    @pytest.mark.parametrize("raw", ["abc", 5, {"a": 1}, None])
    def test_non_list_tags_become_empty_list(self, raw):
        assert ManagedProgram.from_dict(_program(tags=raw)).tags == []

    def test_tags_keep_only_strings(self):
        program = ManagedProgram.from_dict(_program(tags=["a", 1, None, "b", ["c"]]))
        assert program.tags == ["a", "b"]

    def test_optional_str_stays_none_and_non_str_is_stringified(self):
        program = ManagedProgram.from_dict(_program(launch_path=None, version=42))
        assert program.launch_path is None
        assert program.version == "42"

    def test_non_optional_str_none_takes_default(self):
        program = ManagedProgram.from_dict(_program(last_update_status=None))
        assert program.last_update_status == "not_yet_checked"

    def test_non_optional_str_non_str_is_stringified(self):
        assert ManagedProgram.from_dict(_program(name=123)).name == "123"

    @pytest.mark.parametrize("raw, expected", [("2048", 2048), (None, None), ("big", None), (1.5, None)])
    def test_optional_int_coercion(self, raw, expected):
        assert ManagedProgram.from_dict(_program(installed_asset_size=raw)).installed_asset_size == expected

    @pytest.mark.parametrize("raw, expected", [("yes", True), (0, False), (7, False), (None, False)])
    def test_bool_coercion(self, raw, expected):
        assert ManagedProgram.from_dict(_program(pinned=raw)).pinned is expected

    @pytest.mark.parametrize("field_name", ["program_id", "name", "source_type", "source_value", "install_dir"])
    def test_missing_required_field_raises(self, field_name):
        payload = _program()
        del payload[field_name]
        with pytest.raises(ValueError, match=field_name):
            ManagedProgram.from_dict(payload)

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_blank_required_field_raises(self, bad):
        with pytest.raises(ValueError, match="install_dir"):
            ManagedProgram.from_dict(_program(install_dir=bad))

    def test_unknown_keys_are_dropped(self):
        program = ManagedProgram.from_dict(_program(bogus="x", another=[1]))
        assert not hasattr(program, "bogus")
        assert not hasattr(program, "another")

    def test_non_dict_payload_raises_value_error(self):
        with pytest.raises(ValueError):
            ManagedProgram.from_dict("not a dict")  # type: ignore[arg-type]

    def test_nested_update_policy_dict_is_coerced(self):
        program = ManagedProgram.from_dict(_program(
            update_policy={"interval_hours": "12", "check_enabled": "no"},
        ))
        assert program.update_policy == UpdatePolicy(interval_hours=12, check_enabled=False)

    @pytest.mark.parametrize("raw", ["weird", 5, None, ["x"]])
    def test_non_dict_update_policy_gives_default(self, raw):
        assert ManagedProgram.from_dict(_program(update_policy=raw)).update_policy == UpdatePolicy()

    def test_round_trip_to_dict_and_back(self):
        original = ManagedProgram.from_dict(_program(
            version="1.2.3",
            tags=["utility", "portable"],
            pinned=True,
            update_policy={"interval_hours": 6, "update_mode": "auto"},
            installed_asset_size=1024,
        ))
        assert ManagedProgram.from_dict(original.to_dict()) == original


# ── AppSettings ───────────────────────────────────────────────────────────


class TestAppSettings:
    @pytest.mark.parametrize("raw, expected", [
        (48, 48),
        ("48", 48),
        (0, 24),
        (-1, 24),
        (None, 24),
        ("often", 24),
    ])
    def test_default_update_interval_coercion(self, raw, expected):
        settings = AppSettings.from_dict(_settings(default_update_interval_hours=raw))
        assert settings.default_update_interval_hours == expected

    @pytest.mark.parametrize("raw, expected", [
        (1, 5),
        (500, 50),
        ("30", 30),
        (None, 20),
        ("lots", 20),
    ])
    def test_search_result_limit_is_clamped(self, raw, expected):
        assert AppSettings.from_dict(_settings(search_result_limit=raw)).search_result_limit == expected

    def test_bool_fields_accept_strings(self):
        settings = AppSettings.from_dict(_settings(minimize_to_tray="false", close_to_tray="garbage"))
        assert settings.minimize_to_tray is False
        assert settings.close_to_tray is True

    def test_missing_install_root_raises(self):
        with pytest.raises(ValueError, match="install_root"):
            AppSettings.from_dict({"download_cache": "C:/Cache"})

    @pytest.mark.parametrize("bad", ["", None])
    def test_blank_download_cache_raises(self, bad):
        with pytest.raises(ValueError, match="download_cache"):
            AppSettings.from_dict(_settings(download_cache=bad))

    def test_unknown_keys_are_dropped(self):
        settings = AppSettings.from_dict(_settings(legacy_flag=True))
        assert not hasattr(settings, "legacy_flag")

    def test_round_trip(self):
        original = AppSettings.from_dict(_settings(github_token="abc", search_result_limit=33))
        assert AppSettings.from_dict(original.to_dict()) == original
