from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        if not payload:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


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

    def to_dict(self) -> dict:
        data = asdict(self)
        data["update_policy"] = self.update_policy.to_dict()
        return data

    @classmethod
    def from_dict(cls, payload: dict) -> "ManagedProgram":
        payload = dict(payload)
        payload["update_policy"] = UpdatePolicy.from_dict(payload.get("update_policy"))
        # Sanitize list/str fields that may be stored as null in legacy JSON.
        if not isinstance(payload.get("tags"), list):
            payload["tags"] = []
        for str_field in ("notes", "launch_args"):
            if payload.get(str_field) is None:
                payload[str_field] = ""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "AppSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})
