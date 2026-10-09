"""Conditional-request cache, GitHub CLI token import and legacy token encryption."""

from __future__ import annotations

import json

import requests

from portable_manager.github_client import GitHubClient
from portable_manager.http_cache import ConditionalCache


class _Resp(requests.Response):
    def __init__(self, status, body=b"", etag=None, remaining="4999"):
        super().__init__()
        self.status_code = status
        self._content = body
        self.headers.update({"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": remaining})
        if etag:
            self.headers["ETag"] = etag
        self.url = "https://api.github.com/repos/o/r/releases/latest"


def test_conditional_request_reuses_cached_body_on_304(tmp_path, monkeypatch):
    cache = ConditionalCache(tmp_path / "http_cache.json")
    client = GitHubClient(token="t" * 40, cache=cache)
    body = json.dumps({"id": 1, "tag_name": "v2", "assets": []}).encode()
    sent_headers = []
    responses = [_Resp(200, body, etag='W/"abc"'), _Resp(304, remaining="4998")]

    def fake_request(method, url, **kw):
        sent_headers.append(kw.get("headers", {}))
        return responses.pop(0)

    monkeypatch.setattr(client.session, "request", fake_request)
    first = client.get_latest_release("o/r")
    second = client.get_latest_release("o/r")
    assert first.tag_name == second.tag_name == "v2"
    assert "If-None-Match" not in sent_headers[0]
    assert sent_headers[1]["If-None-Match"] == 'W/"abc"'
    assert client.rate_limit_remaining == 4998  # fresh headers from the 304

    cache.save(force=True)
    reloaded = ConditionalCache(tmp_path / "http_cache.json")
    key = ConditionalCache.make_key("https://api.github.com/repos/o/r/releases/latest", None, "t" * 8)
    assert reloaded.get(key)["etag"] == 'W/"abc"'


def test_cache_keys_are_separated_by_token():
    a = ConditionalCache.make_key("u", {"q": "x"}, "token-a")
    b = ConditionalCache.make_key("u", {"q": "x"}, "token-b")
    anon = ConditionalCache.make_key("u", {"q": "x"}, "")
    assert len({a, b, anon}) == 3


def test_cache_is_bounded(tmp_path):
    cache = ConditionalCache(None, max_entries=3)
    for i in range(5):
        cache.put(f"k{i}", "e", 200, b"{}")
    assert sum(1 for i in range(5) if cache.get(f"k{i}")) == 3


def test_read_gh_cli_token(monkeypatch):
    from portable_manager.ui import settings_dialog as sd

    class _Proc:
        returncode = 0
        stdout = "gho_" + "x" * 36 + "\n"

    monkeypatch.setattr(sd, "find_gh_cli", lambda: "gh.exe")
    monkeypatch.setattr(sd.subprocess, "run", lambda *a, **k: _Proc())
    assert sd.read_gh_cli_token() == ("gho_" + "x" * 36, "")

    _Proc.returncode, _Proc.stdout = 1, ""
    token, error = sd.read_gh_cli_token()
    assert token == "" and "gh auth login" in error

    monkeypatch.setattr(sd, "find_gh_cli", lambda: None)
    assert sd.read_gh_cli_token()[0] == ""


def test_plaintext_token_is_encrypted_on_load(tmp_storage, tmp_path):
    import os

    from portable_manager.models import AppSettings

    tmp_storage.save_settings(AppSettings(install_root=str(tmp_path / "apps"), download_cache=str(tmp_path / "cache")))
    data = json.loads(tmp_storage.settings_path.read_text(encoding="utf-8"))
    data["github_token"] = "ghp_legacyplaintexttoken1234567890"
    tmp_storage.settings_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = tmp_storage.load_settings()
    assert loaded.github_token == "ghp_legacyplaintexttoken1234567890"
    on_disk = json.loads(tmp_storage.settings_path.read_text(encoding="utf-8"))["github_token"]
    if os.name == "nt":
        assert on_disk.startswith("dpapi:") and "legacyplaintext" not in on_disk
