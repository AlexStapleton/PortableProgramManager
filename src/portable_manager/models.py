from __future__ import annotations

import functools
import types
from dataclasses import MISSING, Field, asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Type-safe loading of JSON payloads ────────────────────────────────────
#
# Registry and settings files are hand-editable JSON, so a value can arrive
# with any JSON type. These helpers coerce each value to the field's declared
# type and fall back to the field default when that is not possible, so bad
# data never reaches the UI as the wrong type.

_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no"})


@functools.cache
def _type_hints(cls: type) -> dict[str, Any]:
    """Resolved annotations for *cls* (they are strings under ``from __future__``)."""
    return get_type_hints(cls)


def _field_default(f: Field) -> Any:
    """The field's default, calling its factory if needed. ``MISSING`` if it has none."""
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:
        return f.default_factory()
    return MISSING


def _is_required(f: Field) -> bool:
    return f.default is MISSING and f.default_factory is MISSING


def _default_of(cls: type, name: str) -> Any:
    return _field_default(next(f for f in fields(cls) if f.name == name))


def _strip_optional(hint: Any) -> Any:
    """Return ``T`` for ``Optional[T]`` / ``T | None``; any other hint is returned unchanged."""
    if get_origin(hint) in (Union, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass; treat it as invalid here
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _coerce_field(hint: Any, value: Any) -> Any:
    """Convert one JSON *value* to the declared *hint*.

    Returns ``None`` when the value cannot be used, which tells the caller to
    fall back to the field default.
    """
    if value is None:
        return None
    inner = _strip_optional(hint)
    if inner is UpdatePolicy:
        return UpdatePolicy.from_dict(value) if isinstance(value, dict) else None
    if get_origin(inner) is list:
        if not isinstance(value, list):
            return None
        return [item for item in value if isinstance(item, str)]
    if inner is bool:
        return _coerce_bool(value)
    if inner is int:
        return _coerce_int(value)
    if inner is str:
        return value if isinstance(value, str) else str(value)
    return value


def _coerce_payload(cls: type, payload: Any) -> dict[str, Any]:
    """Return ``{field: coerced value}`` for every field of dataclass *cls*.

    Unknown keys are dropped. Missing or unusable values take the field default.
    Fields with no default must end up as non-empty values, otherwise a
    ``ValueError`` is raised.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{cls.__name__} payload must be a JSON object, got {type(payload).__name__}")
    hints = _type_hints(cls)
    values: dict[str, Any] = {}
    for f in fields(cls):
        value = _coerce_field(hints[f.name], payload.get(f.name))
        if value is None:
            value = _field_default(f)
        if _is_required(f) and (value is MISSING or (isinstance(value, str) and not value.strip())):
            raise ValueError(f"{cls.__name__} missing required field {f.name}")
        values[f.name] = value
    return values


@dataclass
class UpdatePolicy:
    check_enabled: bool = True
    update_mode: str = "manual"
    schedule_type: str = "app_start"
    interval_hours: int = 24
    channel: str = "latest_release"
    asset_selection_override: str = ""
    notify_on_available_update: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "UpdatePolicy":
        if not isinstance(payload, dict):
            payload = {}
        values = _coerce_payload(cls, payload)
        # An update check must run at least once an hour; anything smaller falls back to the default.
        if values["interval_hours"] < 1:
            values["interval_hours"] = _default_of(cls, "interval_hours")
        return cls(**values)


@dataclass
class ManagedProgram:
    program_id: str
    name: str
    source_type: str
    source_value: str
    install_dir: str
    launch_path: Optional[str] = None
    version: Optional[str] = None
    asset_name: Optional[str] = None
    repo_full_name: Optional[str] = None
    homepage_url: Optional[str] = None
    notes: str = ""
    source_id: Optional[str] = None
    source_kind: Optional[str] = None
    installed_asset_url: Optional[str] = None
    installed_asset_name: Optional[str] = None
    installed_asset_size: Optional[int] = None
    installed_hash: Optional[str] = None
    launch_args: str = ""
    working_directory_override: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    run_as_admin: bool = False
    installed_at: str = field(default_factory=utc_now_iso)
    last_run_at: Optional[str] = None
    update_policy: UpdatePolicy = field(default_factory=UpdatePolicy)
    latest_upstream_version: Optional[str] = None
    latest_upstream_published_at: Optional[str] = None
    update_available: bool = False
    update_available_asset_name: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_update_found_at: Optional[str] = None
    last_update_attempt_at: Optional[str] = None
    last_update_status: str = "not_yet_checked"
    last_error_message: Optional[str] = None
    last_error_at: Optional[str] = None
    # An update downloaded ahead of time ("download only" mode), ready to apply.
    pending_update_version: Optional[str] = None
    pending_update_file: Optional[str] = None
    pending_update_asset_name: Optional[str] = None
    pending_update_hash: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["update_policy"] = self.update_policy.to_dict()
        return data

    @classmethod
    def from_dict(cls, payload: dict) -> "ManagedProgram":
        # Legacy null tags/notes/launch_args and non-dict update_policy are handled by the coercion rules.
        return cls(**_coerce_payload(cls, payload))


@dataclass
class AppSettings:
    install_root: str
    download_cache: str
    github_token: str = ""
    auto_open_folder_after_install: bool = True
    search_result_limit: int = 20
    default_update_mode: str = "manual"
    default_update_schedule_type: str = "app_start"
    default_update_interval_hours: int = 24
    run_update_check_on_startup: bool = False
    show_system_notifications: bool = True
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    # "system" follows Windows light/dark mode; "light" or "dark" force one.
    theme: str = "system"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AppSettings":
        values = _coerce_payload(cls, payload)
        if values["default_update_interval_hours"] < 1:
            values["default_update_interval_hours"] = _default_of(cls, "default_update_interval_hours")
        # Keep the result list within the range the search UI supports.
        values["search_result_limit"] = min(max(values["search_result_limit"], 5), 50)
        if values["theme"] not in THEME_MODES:
            values["theme"] = "system"
        return cls(**values)


THEME_MODES = ("system", "light", "dark")
