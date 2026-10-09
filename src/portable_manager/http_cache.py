"""A small on-disk cache of API responses for conditional requests.

Each cached entry keeps the server's ``ETag`` and the response body. The next
request for the same URL sends ``If-None-Match``; when the server answers
``304 Not Modified`` the cached body is used instead. GitHub doesn't count an
authenticated 304 against the rate limit, so repeated update checks for
programs that haven't changed are nearly free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_MAX_ENTRIES = 500
_SAVE_INTERVAL_SECONDS = 30


class ConditionalCache:
    def __init__(self, path: Path | None, max_entries: int = _MAX_ENTRIES) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    @staticmethod
    def make_key(url: str, params: dict | None = None, auth_scope: str = "") -> str:
        """Cache key for a GET. *auth_scope* separates answers seen with different tokens."""
        query = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        scope = hashlib.sha256(auth_scope.encode()).hexdigest()[:12] if auth_scope else "anon"
        return f"{scope} {url}?{query}"

    def get(self, key: str) -> dict | None:
        """``{"etag", "status", "body"}`` or None."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry["used"] = time.time()
            return entry

    def put(self, key: str, etag: str, status: int, body: bytes) -> None:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return
        with self._lock:
            self._entries[key] = {"etag": etag, "status": status, "body": text, "used": time.time()}
            if len(self._entries) > self.max_entries:
                oldest = sorted(self._entries, key=lambda k: self._entries[k]["used"])
                for stale in oldest[: len(self._entries) - self.max_entries]:
                    del self._entries[stale]
            self._dirty = True
        self.save(force=False)

    def save(self, force: bool = True) -> None:
        """Write to disk (at most every 30 s unless *force*). Never raises."""
        if self.path is None:
            return
        with self._lock:
            if not self._dirty or (not force and time.time() - self._last_save < _SAVE_INTERVAL_SECONDS):
                return
            payload = json.dumps(self._entries)
            self._dirty = False
            self._last_save = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.stem + "_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            log.info("Couldn't save the HTTP cache", exc_info=True)

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._entries = {
                    k: v for k, v in data.items()
                    if isinstance(v, dict) and isinstance(v.get("etag"), str) and isinstance(v.get("body"), str)
                }
        except (OSError, ValueError):
            log.info("Ignoring an unreadable HTTP cache", exc_info=True)
