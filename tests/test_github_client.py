from __future__ import annotations

import json
import types

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from portable_manager import __version__, github_client
from portable_manager.github_client import (
    USER_AGENT,
    GitHubClient,
    GitHubError,
    GitHubReleaseAsset,
    RateLimitError,
    parse_release_asset_url,
)

API = "https://api.github.com"
NOW = 1_700_000_000


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP send in this module is a test bug, so fail loudly."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("tests must not hit the network")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)


@pytest.fixture
def clock(monkeypatch):
    """Replace the ``time`` module seen by github_client with a fake clock and sleep recorder."""
    state = types.SimpleNamespace(now=NOW, sleeps=[])
    fake_time = types.SimpleNamespace(
        time=lambda: state.now,
        sleep=lambda seconds: state.sleeps.append(seconds),
    )
    monkeypatch.setattr(github_client, "time", fake_time)
    return state


def make_response(status=200, headers=None, body=None, url=f"{API}/x"):
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.url = url
    response._content = json.dumps(body).encode() if body is not None else b""
    return response


def script_session(client, monkeypatch, responses):
    """Make ``client.session.request`` return *responses* in order and record each call."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        return queue.pop(0)

    monkeypatch.setattr(client.session, "request", fake_request)
    return calls


def api_release(**overrides):
    payload = {
        "id": 7,
        "tag_name": "v1.2",
        "name": "v1.2",
        "html_url": "https://github.com/foo/bar/releases/tag/v1.2",
        "published_at": "2026-01-01T00:00:00Z",
        "prerelease": False,
        "assets": [],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# User-Agent and asset digests
# ---------------------------------------------------------------------------

def test_user_agent_contains_package_version():
    assert USER_AGENT == f"PortableProgramManager/{__version__}"
    client = GitHubClient()
    try:
        assert client.session.headers["User-Agent"] == USER_AGENT
    finally:
        client.close()


def test_asset_digest_is_parsed_as_lowercase_hex(monkeypatch):
    digest = "ABCDEF" + "0" * 58
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(200, body=api_release(assets=[
        {"name": "bar.zip", "browser_download_url": "https://x/bar.zip", "size": 3,
         "content_type": "application/zip", "digest": f"sha256:{digest}"},
    ]))])
    try:
        release = client.get_latest_release("foo/bar")
    finally:
        client.close()
    assert release.assets[0].digest == digest.lower()


@pytest.mark.parametrize("digest", [None, "", "md5:abc123", "sha1:abc"])
def test_asset_digest_is_empty_when_missing_or_not_sha256(monkeypatch, digest):
    asset = {"name": "bar.zip", "browser_download_url": "https://x/bar.zip", "size": 3}
    if digest is not None:
        asset["digest"] = digest
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(200, body=api_release(assets=[asset]))])
    try:
        release = client.get_latest_release("foo/bar")
    finally:
        client.close()
    assert release.assets[0].digest == ""


def test_asset_digest_is_last_field_with_empty_default():
    asset = GitHubReleaseAsset("bar.zip", "https://x/bar.zip", 3, "application/zip")
    assert asset.digest == ""
    assert list(GitHubReleaseAsset.__dataclass_fields__)[-1] == "digest"


# ---------------------------------------------------------------------------
# Repository URL parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/foo/bar",
        "https://www.github.com/foo/bar",
        "github.com/foo/bar",
        "http://github.com/foo/bar/",
        "https://github.com/foo/bar.git",
        "https://github.com/foo/bar?tab=readme-ov-file",
        "https://github.com/foo/bar#readme",
        "  https://github.com/foo/bar  ",
        "https://github.com/foo/bar/tree/main",
        "https://github.com/foo/bar/releases",
        "https://github.com/foo/bar/releases/tag/v1",
        "https://github.com/foo/bar/releases/latest",
    ],
)
def test_extract_repo_full_name_accepts_repository_pages(url):
    client = GitHubClient()
    try:
        assert client.extract_repo_full_name(url) == "foo/bar"
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/foo/bar/releases/download/v1.2/bar-win64.zip",
        "https://github.com/foo/bar/releases/download/v1.2",
        "https://github.com/foo/bar/blob/main/tool.exe",
        "https://github.com/foo/bar/raw/main/tool.exe",
        "https://github.com/foo/bar/archive/refs/tags/v1.zip",
        "https://gitlab.com/foo/bar",
        "https://example.com/github.com/foo/bar",
        "https://github.com/orgs/foo",
        "https://github.com/settings/profile",
        "https://github.com/marketplace/x",
        "https://github.com/topics/python",
        "https://github.com/search?q=x",
        "https://github.com/features/actions",
        "https://github.com/sponsors/foo",
        "https://github.com/apps/bot",
        "https://github.com/login",
        "https://github.com/about",
        "https://github.com/foo",
        "https://github.com/",
        "https://github.com/foo/bad$name",
        "https://github.com/../x",
        "",
        "not a url",
    ],
)
def test_extract_repo_full_name_rejects_non_repository_urls(url):
    client = GitHubClient()
    try:
        assert client.extract_repo_full_name(url) is None
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Release asset URL parsing
# ---------------------------------------------------------------------------

def test_parse_release_asset_url_normal():
    assert parse_release_asset_url(
        "https://github.com/foo/bar/releases/download/v1.2/bar-win64.zip"
    ) == ("foo/bar", "v1.2")


def test_parse_release_asset_url_decodes_encoded_tag():
    assert parse_release_asset_url(
        "https://github.com/foo/bar/releases/download/v1.0%2Bbuild/app.zip"
    ) == ("foo/bar", "v1.0+build")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/foo/bar",
        "https://github.com/foo/bar/releases/tag/v1",
        "https://github.com/foo/bar/releases/latest",
        "https://github.com/foo/bar/releases/download/v1",
        "https://gitlab.com/foo/bar/releases/download/v1/x.zip",
        "https://github.com/foo/bar/blob/main/x.zip",
    ],
)
def test_parse_release_asset_url_returns_none_for_other_urls(url):
    assert parse_release_asset_url(url) is None


def test_infer_repo_from_asset_url_still_works():
    client = GitHubClient()
    try:
        assert client.infer_repo_from_asset_url(
            "https://github.com/foo/bar/releases/download/v1.2/bar-win64.zip"
        ) == "foo/bar"
        assert client.infer_repo_from_asset_url("https://github.com/foo/bar") is None
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_short_retry_after_sleeps_then_succeeds(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [
        make_response(429, headers={"Retry-After": "2"}),
        make_response(200, body={"full_name": "foo/bar", "name": "bar", "owner": {"login": "foo"}}),
    ])
    try:
        repo = client.get_repo("foo/bar")
    finally:
        client.close()
    assert repo.full_name == "foo/bar"
    assert len(calls) == 2
    assert clock.sleeps == [2]


def test_short_reset_is_retried_with_reset_based_wait(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [
        make_response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 3)}),
        make_response(200, body={"full_name": "foo/bar", "name": "bar", "owner": {"login": "foo"}}),
    ])
    try:
        client.get_repo("foo/bar")
    finally:
        client.close()
    assert len(calls) == 2
    assert clock.sleeps == [4]  # reset - now + 1


def test_long_reset_raises_without_sleeping(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [
        make_response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 600)}),
    ])
    try:
        with pytest.raises(RateLimitError) as excinfo:
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert len(calls) == 1
    assert clock.sleeps == []
    assert excinfo.value.reset_at == NOW + 600
    assert "Resets in ~10 min." in str(excinfo.value)
    assert "Add a GitHub token in Settings" in str(excinfo.value)


def test_long_retry_after_raises_without_sleeping(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [make_response(429, headers={"Retry-After": "120"})])
    try:
        with pytest.raises(RateLimitError):
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert len(calls) == 1
    assert clock.sleeps == []


def test_rate_limit_retries_are_bounded(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [
        make_response(429, headers={"Retry-After": "1"}) for _ in range(github_client.MAX_RETRIES + 1)
    ])
    try:
        with pytest.raises(RateLimitError):
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert len(calls) == github_client.MAX_RETRIES + 1
    assert clock.sleeps == [1] * github_client.MAX_RETRIES


def test_plain_403_is_returned_immediately_without_retry(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [make_response(403, body={"message": "Forbidden"})])
    try:
        with pytest.raises(GitHubError) as excinfo:
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert len(calls) == 1
    assert clock.sleeps == []
    assert str(excinfo.value) == "GitHub returned HTTP 403 for repository foo/bar."


def test_search_responses_do_not_overwrite_core_quota(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [
        make_response(200, headers={"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": "4000",
                                    "X-RateLimit-Reset": str(NOW + 900)},
                      body={"full_name": "foo/bar", "name": "bar", "owner": {"login": "foo"}}),
        make_response(403, headers={"X-RateLimit-Resource": "search", "X-RateLimit-Remaining": "0",
                                    "X-RateLimit-Reset": str(NOW + 30)}, body={"items": []}),
        make_response(200, headers={"X-RateLimit-Resource": "search", "X-RateLimit-Remaining": "2"},
                      body={"items": []}),
    ])
    try:
        client.get_repo("foo/bar")
        assert client.rate_limit_remaining == 4000
        assert client.rate_limit_reset_at == NOW + 900
        with pytest.raises(GitHubError):
            client.search_repositories("bar")
        assert client.search_repositories("bar") == []
    finally:
        client.close()
    assert client.rate_limit_remaining == 4000
    assert client.rate_limit_reset_at == NOW + 900


def test_core_headers_update_quota(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [
        make_response(200, headers={"X-RateLimit-Remaining": "17"},
                      body={"full_name": "foo/bar", "name": "bar", "owner": {"login": "foo"}}),
    ])
    try:
        client.get_repo("foo/bar")
    finally:
        client.close()
    assert client.rate_limit_remaining == 17


def test_can_afford_uses_remaining_quota():
    client = GitHubClient()
    try:
        assert client.can_afford(100) is True  # unknown quota
        client.rate_limit_remaining = 5
        assert client.can_afford(4) is True
        assert client.can_afford(5) is False
        assert client.can_afford(6) is False
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Friendly errors
# ---------------------------------------------------------------------------

def test_get_repo_404_has_friendly_message(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(404, body={"message": "Not Found"})])
    try:
        with pytest.raises(GitHubError) as excinfo:
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert str(excinfo.value) == (
        "Repository foo/bar was not found on GitHub. It may be private, renamed or deleted."
    )


def test_get_latest_release_404_returns_none(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(404, body={"message": "Not Found"})])
    try:
        assert client.get_latest_release("foo/bar") is None
    finally:
        client.close()


def test_get_latest_release_other_error_is_friendly(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(500, body={})])
    try:
        with pytest.raises(GitHubError) as excinfo:
            client.get_latest_release("foo/bar")
    finally:
        client.close()
    assert str(excinfo.value) == "GitHub returned HTTP 500 for latest release of foo/bar."


def test_search_error_is_friendly(monkeypatch, clock):
    client = GitHubClient()
    script_session(client, monkeypatch, [make_response(422, body={})])
    try:
        with pytest.raises(GitHubError) as excinfo:
            client.search_repositories("bad:query")
    finally:
        client.close()
    assert str(excinfo.value) == "GitHub returned HTTP 422 for repository search."


def test_errors_never_include_token(monkeypatch, clock):
    client = GitHubClient(token="ghp_secrettoken123")
    script_session(client, monkeypatch, [make_response(500, body={})])
    try:
        with pytest.raises(GitHubError) as excinfo:
            client.get_repo("foo/bar")
    finally:
        client.close()
    assert "ghp_secrettoken123" not in str(excinfo.value)


def test_github_error_is_a_request_exception():
    assert issubclass(GitHubError, requests.RequestException)
    assert issubclass(RateLimitError, GitHubError)


def test_invalid_full_name_is_rejected_before_any_request(monkeypatch, clock):
    client = GitHubClient()
    calls = script_session(client, monkeypatch, [])
    try:
        with pytest.raises(requests.RequestException):
            client.get_repo("../../other")
        with pytest.raises(requests.RequestException):
            client.get_latest_release("foo/bar/baz")
    finally:
        client.close()
    assert calls == []


def test_search_with_forks_adds_qualifier_and_marks_forks(monkeypatch):
    client = GitHubClient()
    seen = {}

    class _Resp:
        status_code = 200
        headers = {"X-RateLimit-Resource": "search"}
        url = "https://api.github.com/search/repositories"

        def json(self):
            return {"items": [
                {"full_name": "a/x", "name": "x", "owner": {"login": "a"}, "fork": False},
                {"full_name": "b/x", "name": "x", "owner": {"login": "b"}, "fork": True},
            ]}

    def fake_request(method, url, **kw):
        seen["q"] = kw["params"]["q"]
        return _Resp()

    monkeypatch.setattr(client.session, "request", fake_request)
    repos = client.search_repositories("SMUDebugTool", include_forks=True)
    assert seen["q"] == "SMUDebugTool fork:true"
    assert [r.is_fork for r in repos] == [False, True]
    client.search_repositories("SMUDebugTool")
    assert seen["q"] == "SMUDebugTool"


def test_compare_fork_with_parent(monkeypatch):
    client = GitHubClient()
    urls = []

    class _Resp:
        def __init__(self, payload):
            self.status_code = 200
            self.headers = {}
            self.url = ""
            self._payload = payload

        def json(self):
            return self._payload

    def fake_request(method, url, **kw):
        urls.append(url)
        if url.endswith("/repos/AlexStapleton/SMUDebugTool"):
            return _Resp({"default_branch": "master", "parent": {"full_name": "irusanov/SMUDebugTool", "default_branch": "master"}})
        return _Resp({"ahead_by": 124, "behind_by": 4})

    monkeypatch.setattr(client.session, "request", fake_request)
    assert client.compare_fork_with_parent("AlexStapleton/SMUDebugTool") == ("irusanov/SMUDebugTool", 124, 4)
    assert urls[1].endswith("/repos/irusanov/SMUDebugTool/compare/master...AlexStapleton:SMUDebugTool:master")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("app-win64.zip", True), ("App_Windows_x64.zip", True), ("tool-win.zip", True),
        ("setup.exe", True), ("tool-x86_64-pc-windows-msvc.zip", True), ("App.msi", True),
        ("release-cli-darwin-amd64.tar.gz", False), ("twinkle-linux.tar.gz", False),
        ("app-macos.dmg", False),
    ],
)
def test_windows_asset_detection(name, expected):
    assert bool(GitHubClient._WIN_ASSET_RE.search(name)) is expected
