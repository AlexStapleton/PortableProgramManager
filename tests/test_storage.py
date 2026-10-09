from __future__ import annotations

import json
import sys

import pytest

from portable_manager.models import AppSettings, ManagedProgram
from portable_manager.storage import Storage


def _entry(program_id: str, **overrides) -> dict:
    payload = {
        "program_id": program_id,
        "name": f"Program {program_id}",
        "source_type": "github",
        "source_value": "owner/repo",
        "install_dir": f"C:/Apps/{program_id}",
    }
    payload.update(overrides)
    return payload


def _program(program_id: str) -> ManagedProgram:
    return ManagedProgram.from_dict(_entry(program_id))


@pytest.fixture
def safe_defaults(tmp_storage, tmp_path, monkeypatch):
    """Keep default settings out of the real home directory during tests."""
    def _defaults(self):
        return AppSettings(
            install_root=str(tmp_path / "apps"),
            download_cache=str(tmp_path / "cache"),
        )
    monkeypatch.setattr(Storage, "_default_settings", _defaults)
    return tmp_storage


def _corrupt_files(storage: Storage, name: str) -> list:
    return sorted(storage.base_dir.glob(f"{name}.corrupt-*"))


# ── Programs: basic round trip and per-entry loading ──────────────────────


def test_programs_save_load_round_trip(tmp_storage):
    programs = [_program("a"), _program("b")]
    tmp_storage.save_programs(programs)
    assert tmp_storage.load_programs() == programs


def test_missing_registry_is_created_empty(tmp_storage):
    assert tmp_storage.load_programs() == []
    assert json.loads(tmp_storage.registry_path.read_text(encoding="utf-8")) == []


def test_bad_entries_are_skipped_and_written_to_rejected_file(tmp_storage):
    good_a = _entry("a")
    good_b = _entry("b")
    missing_ids = {"name": "no ids"}
    not_an_object = "just a string"
    raw = [good_a, missing_ids, not_an_object, good_b]
    tmp_storage.registry_path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = tmp_storage.load_programs()

    assert [p.program_id for p in loaded] == ["a", "b"]

    rejected = list(tmp_storage.base_dir.glob("programs.rejected-*.json"))
    assert len(rejected) == 1
    assert json.loads(rejected[0].read_text(encoding="utf-8")) == [missing_ids, not_an_object]

    # The original registry is left untouched until the next normal save.
    assert json.loads(tmp_storage.registry_path.read_text(encoding="utf-8")) == raw


def test_rejected_files_never_overwrite_each_other(tmp_storage, monkeypatch):
    monkeypatch.setattr("portable_manager.storage._timestamp", lambda: "20260101-120000")
    raw = [{"name": "bad"}]
    tmp_storage.registry_path.write_text(json.dumps(raw), encoding="utf-8")

    tmp_storage.load_programs()
    tmp_storage.load_programs()

    names = sorted(p.name for p in tmp_storage.base_dir.glob("programs.rejected-*.json"))
    assert names == [
        "programs.rejected-20260101-120000-1.json",
        "programs.rejected-20260101-120000.json",
    ]


# ── Programs: corruption recovery ─────────────────────────────────────────


def test_invalid_json_recovers_from_valid_bak(tmp_storage):
    first = [_program("a")]
    second = [_program("b"), _program("c")]
    tmp_storage.save_programs(first)
    tmp_storage.save_programs(second)  # .bak now holds `first`
    tmp_storage.registry_path.write_text("{not valid json", encoding="utf-8")

    loaded = tmp_storage.load_programs()

    assert loaded == first
    # The corrupt file is preserved and the recovered programs are written back.
    assert len(_corrupt_files(tmp_storage, "programs.json")) == 1
    assert tmp_storage.load_programs() == first


def test_invalid_json_without_bak_returns_empty_and_backs_up(tmp_storage):
    tmp_storage.registry_path.write_text("{not valid json", encoding="utf-8")

    assert tmp_storage.load_programs() == []

    corrupt = _corrupt_files(tmp_storage, "programs.json")
    assert len(corrupt) == 1
    assert corrupt[0].read_text(encoding="utf-8") == "{not valid json"
    assert json.loads(tmp_storage.registry_path.read_text(encoding="utf-8")) == []


def test_top_level_non_list_is_treated_as_corrupt(tmp_storage):
    tmp_storage.registry_path.write_text(json.dumps({"programs": []}), encoding="utf-8")

    assert tmp_storage.load_programs() == []
    assert len(_corrupt_files(tmp_storage, "programs.json")) == 1


def test_unusable_bak_is_not_used(tmp_storage):
    (tmp_storage.base_dir / "programs.json.bak").write_text("garbage", encoding="utf-8")
    tmp_storage.registry_path.write_text("also garbage", encoding="utf-8")

    assert tmp_storage.load_programs() == []
    assert len(_corrupt_files(tmp_storage, "programs.json")) == 1


def test_two_corruptions_produce_two_distinct_backups(tmp_storage, monkeypatch):
    monkeypatch.setattr("portable_manager.storage._timestamp", lambda: "20260101-120000")

    tmp_storage.registry_path.write_text("first bad", encoding="utf-8")
    assert tmp_storage.load_programs() == []

    tmp_storage.registry_path.write_text("second bad", encoding="utf-8")
    assert tmp_storage.load_programs() == []

    backups = _corrupt_files(tmp_storage, "programs.json")
    assert len(backups) == 2
    contents = {b.read_text(encoding="utf-8") for b in backups}
    assert contents == {"first bad", "second bad"}


# ── Programs: rolling backup on save ──────────────────────────────────────


def test_save_creates_bak_of_previous_content(tmp_storage):
    first = [_program("a")]
    second = [_program("b")]
    tmp_storage.save_programs(first)
    assert not (tmp_storage.base_dir / "programs.json.bak").exists()

    tmp_storage.save_programs(second)

    bak = json.loads((tmp_storage.base_dir / "programs.json.bak").read_text(encoding="utf-8"))
    assert bak == [p.to_dict() for p in first]
    assert tmp_storage.load_programs() == second


def test_save_does_not_replace_good_bak_with_corrupt_file(tmp_storage):
    good = [_program("a")]
    tmp_storage.save_programs(good)
    tmp_storage.save_programs([_program("b")])  # .bak == good
    tmp_storage.registry_path.write_text("{broken", encoding="utf-8")

    tmp_storage.save_programs([_program("c")])

    bak = json.loads((tmp_storage.base_dir / "programs.json.bak").read_text(encoding="utf-8"))
    assert bak == [p.to_dict() for p in good]


# ── Corrupt backup naming ─────────────────────────────────────────────────


def test_backup_corrupt_file_never_overwrites(tmp_storage, monkeypatch):
    monkeypatch.setattr("portable_manager.storage._timestamp", lambda: "20260101-120000")
    path = tmp_storage.settings_path

    for text in ("one", "two", "three"):
        path.write_text(text, encoding="utf-8")
        Storage._backup_corrupt_file(path)

    names = sorted(p.name for p in tmp_storage.base_dir.glob("settings.json.corrupt-*"))
    assert names == [
        "settings.json.corrupt-20260101-120000",
        "settings.json.corrupt-20260101-120000-1",
        "settings.json.corrupt-20260101-120000-2",
    ]
    assert not path.exists()


# ── Settings ──────────────────────────────────────────────────────────────


def test_settings_with_garbage_types_load_defaults(safe_defaults):
    storage = safe_defaults
    storage.settings_path.write_text(json.dumps({
        "install_root": "C:/Apps",
        "download_cache": "C:/Cache",
        "search_result_limit": "huge",
        "default_update_interval_hours": None,
        "minimize_to_tray": "maybe",
        "github_token": 123,
        "tags_that_do_not_exist": [1, 2],
    }), encoding="utf-8")

    settings = storage.load_settings()

    assert settings.install_root == "C:/Apps"
    assert settings.search_result_limit == 20
    assert settings.default_update_interval_hours == 24
    assert settings.minimize_to_tray is True
    assert settings.github_token == ""


def test_settings_missing_required_field_resets_to_defaults(safe_defaults):
    storage = safe_defaults
    storage.settings_path.write_text(json.dumps({"download_cache": "C:/Cache"}), encoding="utf-8")

    settings = storage.load_settings()

    assert settings.install_root.endswith("apps")
    assert len(_corrupt_files(storage, "settings.json")) == 1


def test_settings_top_level_non_object_resets_to_defaults(safe_defaults):
    storage = safe_defaults
    storage.settings_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    storage.load_settings()

    assert len(_corrupt_files(storage, "settings.json")) == 1


def test_settings_invalid_json_resets_to_defaults(safe_defaults):
    storage = safe_defaults
    storage.settings_path.write_text("{broken", encoding="utf-8")

    storage.load_settings()

    assert len(_corrupt_files(storage, "settings.json")) == 1


def test_github_token_round_trips(tmp_storage, tmp_path):
    token = "ghp_example_token_value"
    settings = AppSettings(
        install_root=str(tmp_path / "apps"),
        download_cache=str(tmp_path / "cache"),
        github_token=token,
    )
    tmp_storage.save_settings(settings)

    raw = tmp_storage.settings_path.read_text(encoding="utf-8")
    if sys.platform == "win32":
        # DPAPI-encrypted on Windows: the plaintext must not be on disk.
        assert token not in raw
    assert tmp_storage.load_settings().github_token == token


def test_legacy_plaintext_token_still_loads(tmp_storage, tmp_path):
    tmp_storage.settings_path.write_text(json.dumps({
        "install_root": str(tmp_path / "apps"),
        "download_cache": str(tmp_path / "cache"),
        "github_token": "legacy_plain",
    }), encoding="utf-8")

    assert tmp_storage.load_settings().github_token == "legacy_plain"


def test_acl_hardening_is_removed(tmp_storage):
    assert not hasattr(Storage, "_restrict_file_acl")
