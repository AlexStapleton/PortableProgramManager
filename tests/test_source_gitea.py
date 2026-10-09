from __future__ import annotations

import json
import types

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from portable_manager import github_client
from portable_manager.github_client import GitHubError, RateLimitError
from portable_manager.http_cache import ConditionalCache
from portable_manager.models import SourceConfig
from portable_manager.sources import gitea
from portable_manager.sources.base import build_provider
from portable_manager.sources.gitea import GiteaProvider

BASE = "https://codeberg.org"
API = f"{BASE}/api/v1"
NOW = 1_700_000_000
TOKEN = "tok-secret-1234567890"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP send in this module is a test bug, so fail loudly."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("tests must not hit the network")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)


@pytest.fixture
def clock(monkeypatch):
    """Replace the ``time`` module seen by the Gitea client with a sleep recorder."""
    state = types.SimpleNamespace(now=NOW, sleeps=[])
    fake_time = types.SimpleNamespace(time=lambda: state.now, sleep=lambda seconds: state.sleeps.append(seconds))
    monkeypatch.setattr(gitea, "time", fake_time)
    return state


def make_provider(token: str = "", cache: ConditionalCache | None = None) -> GiteaProvider:
    return GiteaProvider(token=token, cache=cache, base_url=BASE, source_id="codeberg", name="Codeberg")


def make_response(status=200, headers=None, body=None, url=f"{API}/x", raw=None):
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.url = url
    if raw is not None:
        response._content = raw
    else:
        response._content = json.dumps(body).encode() if body is not None else b""
    return response


def script_session(client, monkeypatch, responses):
    """Make ``client.session.request`` return *responses* in order; record (method, url, kwargs)."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return queue.pop(0)

    monkeypatch.setattr(client.session, "request", fake_request)
    return calls


def api_repo(**overrides):
    payload = {
        "id": 42,
        "owner": {"login": "forgejo", "username": "forgejo"},
        "name": "forgejo",
        "full_name": "forgejo/forgejo",
        "description": "Beyond coding.",
        "html_url": f"{BASE}/forgejo/forgejo",
        "website": "https://forgejo.org",
        "stars_count": 5611,
        "updated_at": "2026-10-10T00:08:54+02:00",
        "default_branch": "forgejo",
        "fork": False,
        "parent": None,
    }
    payload.update(overrides)
    return payload


def api_release(**overrides):
    payload = {
        "id": 7,
        "tag_name": "v1.2",
        "name": "",
        "html_url": f"{BASE}/forgejo/forgejo/releases/tag/v1.2",
        "published_at": "2026-09-17T17:27:32+02:00",
        "prerelease": False,
        "draft": False,
        "assets": [],
    }
    payload.update(overrides)
    return payload


def api_asset(name, url=None, size=10):
    return {
        "name": name,
        "browser_download_url": url or f"{BASE}/forgejo/forgejo/releases/download/v1.2/{name}",
        "size": size,
        "content_type": None,
    }


# ----------------------------------------------------------------- search


def test_search_maps_envelope_and_drops_forks(monkeypatch):
    client = make_provider()
    body = {"ok": True, "data": [
        api_repo(full_name="a/tool", name="tool", owner={"login": "a"}, fork=False, website=""),
        api_repo(full_name="b/tool", name="tool", owner={"login": "b"}, fork=True),
    ]}
    calls = script_session(client, monkeypatch, [make_response(200, body=body)])

    repos = client.search_repositories("tool", limit=5, sort="stars")

    assert [r.full_name for r in repos] == ["a/tool"]
    repo = repos[0]
    assert repo.owner == "a"
    assert repo.name == "tool"
    assert repo.homepage == repo.html_url  # no website -> the project page
    assert repo.pushed_at == repo.updated_at
    assert repo.source_id == "codeberg"
    assert repo.is_fork is False
    method, url, kwargs = calls[0]
    assert (method, url) == ("GET", f"{API}/repos/search")
    assert kwargs["params"] == {"q": "tool", "order": "desc", "limit": 5, "sort": "stars"}


def test_search_keeps_forks_when_asked(monkeypatch):
    client = make_provider()
    body = {"ok": True, "data": [api_repo(full_name="b/tool", owner={"login": "b"}, fork=True)]}
    script_session(client, monkeypatch, [make_response(200, body=body)])

    repos = client.search_repositories("tool", include_forks=True)

    assert [(r.full_name, r.is_fork) for r in repos] == [("b/tool", True)]


def test_search_caps_limit_and_sends_updated_sort(monkeypatch):
    client = make_provider()
    calls = script_session(client, monkeypatch, [make_response(200, body={"ok": True, "data": []})])

    assert client.search_repositories("x", limit=500, sort="updated") == []

    assert calls[0][2]["params"]["limit"] == 50
    assert calls[0][2]["params"]["sort"] == "updated"


def test_search_retries_without_sort_when_server_rejects_it(monkeypatch):
    client = make_provider()
    body = {"ok": True, "data": [api_repo()]}
    calls = script_session(client, monkeypatch, [
        make_response(400, body={"message": "bad sort"}),
        make_response(200, body=body),
    ])

    repos = client.search_repositories("forgejo", sort="stars")

    assert [r.full_name for r in repos] == ["forgejo/forgejo"]
    assert calls[0][2]["params"]["sort"] == "stars"
    assert "sort" not in calls[1][2]["params"]


def test_search_error_after_sort_fallback_is_still_friendly(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(500, body={}), make_response(500, body={})])

    with pytest.raises(GitHubError, match="HTTP 500 for repository search"):
        client.search_repositories("forgejo")


def test_search_error_is_friendly_and_has_no_url(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(500, body={})])

    with pytest.raises(GitHubError) as info:
        client.search_repositories("forgejo", sort="updated")

    assert str(info.value) == "Codeberg returned HTTP 500 for repository search."


# ----------------------------------------------------------------- get_repo


def test_get_repo_maps_fields(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(200, body=api_repo())])

    repo = client.get_repo("forgejo/forgejo")

    assert repo.full_name == "forgejo/forgejo"
    assert repo.owner == "forgejo"
    assert repo.homepage == "https://forgejo.org"
    assert repo.stars == 5611
    assert repo.default_branch == "forgejo"
    assert repo.source_id == "codeberg"


def test_get_repo_404_has_friendly_message(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(404, body={"message": "not found"})])

    with pytest.raises(GitHubError) as info:
        client.get_repo("nobody/gone")

    assert str(info.value) == (
        "Repository nobody/gone was not found on Codeberg. It may be private, renamed or deleted."
    )


def test_get_repo_rejects_bad_names_before_any_request(monkeypatch):
    client = make_provider()
    calls = script_session(client, monkeypatch, [])

    with pytest.raises(GitHubError):
        client.get_repo("../../other")

    assert calls == []


# ------------------------------------------------------------ latest release


def test_latest_release_maps_only_https_assets(monkeypatch):
    client = make_provider()
    payload = api_release(assets=[
        api_asset("forgejo-linux-amd64", size=122),
        api_asset("insecure.zip", url="http://codeberg.org/x/y/releases/download/v1/insecure.zip"),
    ])
    calls = script_session(client, monkeypatch, [make_response(200, body=payload)])

    release = client.get_latest_release("forgejo/forgejo")

    assert calls[0][1] == f"{API}/repos/forgejo/forgejo/releases/latest"
    assert release.release_id == 7
    assert release.tag_name == "v1.2"
    assert release.name == "v1.2"  # blank name falls back to the tag
    assert release.published_at == "2026-09-17T17:27:32+02:00"
    assert [(a.name, a.size) for a in release.assets] == [("forgejo-linux-amd64", 122)]
    asset = release.assets[0]
    assert asset.download_url.startswith(f"{BASE}/forgejo/forgejo/releases/download/")
    assert (asset.content_type, asset.digest) == ("", "")


def test_latest_release_404_returns_none(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(404, body={})])

    assert client.get_latest_release("forgejo/forgejo") is None


def test_prerelease_path_takes_first_non_draft(monkeypatch):
    client = make_provider()
    releases = [
        api_release(id=1, tag_name="v2.0-rc1", draft=True),
        api_release(id=2, tag_name="v2.0-rc0", prerelease=True),
        api_release(id=3, tag_name="v1.9"),
    ]
    calls = script_session(client, monkeypatch, [make_response(200, body=releases)])

    release = client.get_latest_release("forgejo/forgejo", include_prereleases=True)

    assert calls[0][1] == f"{API}/repos/forgejo/forgejo/releases"
    assert calls[0][2]["params"] == {"limit": 10}
    assert release.release_id == 2
    assert release.prerelease is True


def test_prerelease_path_404_and_empty_return_none(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(404, body={})])
    assert client.get_latest_release("forgejo/forgejo", include_prereleases=True) is None

    script_session(client, monkeypatch, [make_response(200, body=[])])
    assert client.get_latest_release("forgejo/forgejo", include_prereleases=True) is None


def test_prerelease_path_with_only_drafts_returns_none(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(200, body=[api_release(draft=True)])])

    assert client.get_latest_release("forgejo/forgejo", include_prereleases=True) is None


# ------------------------------------------------------- release info


def test_release_info_detects_windows_asset(monkeypatch):
    client = make_provider()
    payload = api_release(assets=[api_asset("tool-linux.tar.xz"), api_asset("tool-win64.zip")])
    script_session(client, monkeypatch, [make_response(200, body=payload)])

    assert client.get_latest_release_info("owner/tool") == ("2026-09-17T17:27:32+02:00", True)


def test_release_info_without_windows_asset(monkeypatch):
    client = make_provider()
    payload = api_release(assets=[api_asset("tool-linux-amd64.tar.gz")])
    script_session(client, monkeypatch, [make_response(200, body=payload)])

    assert client.get_latest_release_info("owner/tool") == ("2026-09-17T17:27:32+02:00", False)


def test_release_info_is_empty_on_404_error_or_bad_name(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(404, body={})])
    assert client.get_latest_release_info("owner/tool") == ("", False)

    script_session(client, monkeypatch, [make_response(500, body={})])
    assert client.get_latest_release_info("owner/tool") == ("", False)

    assert client.get_latest_release_info("not a name") == ("", False)


# ------------------------------------------------------------- compare


def test_compare_fork_returns_ahead_and_behind(monkeypatch):
    client = make_provider()
    fork = api_repo(
        full_name="fabian/tool", owner={"login": "fabian"}, name="tool", fork=True,
        default_branch="main",
        parent={"full_name": "sock/tool", "default_branch": "trunk"},
    )
    calls = script_session(client, monkeypatch, [
        make_response(200, body=fork),
        make_response(200, body={"total_commits": 3, "commits": [{}, {}, {}]}),
        make_response(200, body={"total_commits": 1, "commits": [{}]}),
    ])

    assert client.compare_fork_with_parent("fabian/tool") == ("sock/tool", 3, 1)

    urls = [call[1] for call in calls]
    assert urls[1] == f"{API}/repos/sock/tool/compare/trunk...fabian:main"
    assert urls[2] == f"{API}/repos/fabian/tool/compare/main...sock:trunk"


def test_compare_non_fork_raises(monkeypatch):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(200, body=api_repo(parent=None))])

    with pytest.raises(GitHubError) as info:
        client.compare_fork_with_parent("forgejo/forgejo")

    assert str(info.value) == "forgejo/forgejo is not a fork."


@pytest.mark.parametrize("status", [404, 405, 500])
def test_compare_unsupported_server_raises_cannot_compare(monkeypatch, status):
    client = make_provider()
    fork = api_repo(full_name="fabian/tool", fork=True, parent={"full_name": "sock/tool", "default_branch": "main"})
    script_session(client, monkeypatch, [
        make_response(200, body=fork),
        make_response(status, body={"message": ""}),
    ])

    with pytest.raises(GitHubError) as info:
        client.compare_fork_with_parent("fabian/tool")

    assert str(info.value) == "Codeberg can't compare forks on this server."


def test_compare_without_total_commits_raises_cannot_compare(monkeypatch):
    client = make_provider()
    fork = api_repo(full_name="fabian/tool", fork=True, parent={"full_name": "sock/tool", "default_branch": "main"})
    script_session(client, monkeypatch, [
        make_response(200, body=fork),
        make_response(200, body={"commits": []}),
    ])

    with pytest.raises(GitHubError, match="can't compare forks"):
        client.compare_fork_with_parent("fabian/tool")


# ------------------------------------------------------------ URL helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://codeberg.org/forgejo/forgejo", "forgejo/forgejo"),
        ("codeberg.org/forgejo/forgejo", "forgejo/forgejo"),
        ("https://www.codeberg.org/forgejo/forgejo/", "forgejo/forgejo"),
        ("https://codeberg.org/forgejo/forgejo.git", "forgejo/forgejo"),
        ("https://codeberg.org/forgejo/forgejo/src/branch/main/README.md", None),
        ("https://codeberg.org/forgejo/forgejo/issues/5", "forgejo/forgejo"),
        ("https://codeberg.org/explore/repos", None),
        ("https://codeberg.org/user/login", None),
        ("https://codeberg.org/users/someone", None),
        ("https://codeberg.org/org/acme", None),
        ("https://codeberg.org/admin/x", None),
        ("https://codeberg.org/api/v1/repos/a/b", None),
        ("https://codeberg.org/assets/x", None),
        ("https://codeberg.org/-/settings", None),
        ("https://codeberg.org/repo/create", None),
        ("https://codeberg.org/notifications", None),
        ("https://codeberg.org/attachments/3f2a9c1e-0000-4000-8000-000000000000", None),
        ("https://codeberg.org/forgejo/forgejo/releases/download/v1/forgejo-linux", None),
        ("https://codeberg.org/forgejo/forgejo/raw/branch/main/file.txt", None),
        ("https://codeberg.org/forgejo/forgejo/media/branch/main/logo.png", None),
        ("https://codeberg.org/forgejo/forgejo/archive/main.zip", None),
        ("https://github.com/forgejo/forgejo", None),
        ("https://gitlab.com/forgejo/forgejo", None),
        ("https://codeberg.org/forgejo", None),
        ("", None),
    ],
)
def test_extract_repo_full_name(url, expected):
    assert make_provider().extract_repo_full_name(url) == expected


def test_parse_release_asset_url_normal_and_decodes_tag():
    client = make_provider()
    url = f"{BASE}/owner/tool/releases/download/v1.2%2Bbeta/tool-win64.zip"

    assert client.parse_release_asset_url(url) == ("owner/tool", "v1.2+beta")
    assert client.infer_repo_from_asset_url(url) == "owner/tool"


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/owner/tool/releases/tag/v1.2",
        f"{BASE}/owner/tool/releases/download/v1.2",
        f"{BASE}/attachments/3f2a9c1e-0000-4000-8000-000000000000",
        f"{BASE}/owner/tool/raw/branch/main/tool.exe",
        "https://github.com/owner/tool/releases/download/v1/tool.exe",
        "https://objects.example.com/owner/tool/releases/download/v1/tool.exe",
    ],
)
def test_parse_release_asset_url_returns_none_for_other_urls(url):
    client = make_provider()

    assert client.parse_release_asset_url(url) is None
    assert client.infer_repo_from_asset_url(url) is None


# ------------------------------------------------------ auth and errors


def test_token_header_only_sent_when_a_token_is_given(monkeypatch):
    anonymous = make_provider()
    assert "Authorization" not in anonymous.session.headers

    authed = make_provider(token=TOKEN)
    assert authed.session.headers["Authorization"] == f"token {TOKEN}"
    calls = script_session(authed, monkeypatch, [make_response(200, body=api_repo())])
    authed.get_repo("forgejo/forgejo")
    assert calls[0][1] == f"{API}/repos/forgejo/forgejo"


def test_token_never_appears_in_error_messages(monkeypatch, clock):
    client = make_provider(token=TOKEN)
    script_session(client, monkeypatch, [make_response(500, body={"message": TOKEN})])
    with pytest.raises(GitHubError) as info:
        client.get_repo("forgejo/forgejo")
    assert TOKEN not in str(info.value)

    script_session(client, monkeypatch, [make_response(429, headers={"Retry-After": "600"})])
    with pytest.raises(RateLimitError) as info:
        client.get_repo("forgejo/forgejo")
    assert TOKEN not in str(info.value)

    script_session(client, monkeypatch, [make_response(200, raw=b"not json " + TOKEN.encode())])
    with pytest.raises(requests.RequestException) as info:
        client.get_repo("forgejo/forgejo")
    assert TOKEN not in str(info.value)


# ------------------------------------------------------------ rate limits


def test_short_retry_after_sleeps_then_succeeds(monkeypatch, clock):
    client = make_provider()
    calls = script_session(client, monkeypatch, [
        make_response(429, headers={"Retry-After": "5"}),
        make_response(200, body=api_repo()),
    ])

    assert client.get_repo("forgejo/forgejo").full_name == "forgejo/forgejo"
    assert clock.sleeps == [5]
    assert len(calls) == 2


def test_long_retry_after_raises_without_sleeping(monkeypatch, clock):
    client = make_provider()
    calls = script_session(client, monkeypatch, [make_response(429, headers={"Retry-After": "120"})])

    with pytest.raises(RateLimitError) as info:
        client.get_repo("forgejo/forgejo")

    assert str(info.value) == "Codeberg rate limit reached. Try again in ~2 min."
    assert isinstance(info.value, GitHubError)
    assert clock.sleeps == []
    assert len(calls) == 1


def test_missing_retry_after_on_429_raises_without_sleeping(monkeypatch, clock):
    client = make_provider()
    script_session(client, monkeypatch, [make_response(429)])

    with pytest.raises(RateLimitError, match="Try again in ~1 min"):
        client.get_repo("forgejo/forgejo")
    assert clock.sleeps == []


def test_rate_limit_retries_are_bounded(monkeypatch, clock):
    client = make_provider()
    calls = script_session(client, monkeypatch, [make_response(429, headers={"Retry-After": "1"}) for _ in range(4)])

    with pytest.raises(RateLimitError):
        client.get_repo("forgejo/forgejo")

    assert len(calls) == github_client.MAX_RETRIES + 1
    assert clock.sleeps == [1, 1, 1]


def test_plain_403_is_returned_without_retry(monkeypatch, clock):
    client = make_provider()
    calls = script_session(client, monkeypatch, [make_response(403, body={})])

    with pytest.raises(GitHubError, match="HTTP 403"):
        client.get_repo("forgejo/forgejo")
    assert len(calls) == 1
    assert clock.sleeps == []


def test_quota_header_is_recorded_when_present(monkeypatch):
    client = make_provider()
    assert client.rate_limit_remaining is None
    assert client.can_afford(10) is True
    assert client.refresh_rate_limit() is None

    script_session(client, monkeypatch, [make_response(200, body=api_repo(), headers={"X-RateLimit-Remaining": "7"})])
    client.get_repo("forgejo/forgejo")

    assert client.rate_limit_remaining == 7
    assert client.refresh_rate_limit() == 7
    assert client.can_afford(6) is True
    assert client.can_afford(7) is False


def test_conditional_request_reuses_cached_body_on_304(monkeypatch):
    cache = ConditionalCache(None)
    client = make_provider(cache=cache)
    calls = script_session(client, monkeypatch, [
        make_response(200, body=api_repo(), headers={"ETag": '"abc"'}),
        make_response(304, body=None),
    ])

    first = client.get_repo("forgejo/forgejo")
    second = client.get_repo("forgejo/forgejo")

    assert first.stars == second.stars == 5611
    assert "If-None-Match" not in calls[0][2].get("headers", {})
    assert calls[1][2]["headers"]["If-None-Match"] == '"abc"'


def test_responses_without_etag_are_not_cached(monkeypatch):
    cache = ConditionalCache(None)
    client = make_provider(cache=cache)
    calls = script_session(client, monkeypatch, [
        make_response(200, body=api_repo()),
        make_response(200, body=api_repo()),
    ])

    client.get_repo("forgejo/forgejo")
    client.get_repo("forgejo/forgejo")

    assert all("If-None-Match" not in call[2].get("headers", {}) for call in calls)


# ------------------------------------------------------------ registry


def test_build_provider_returns_gitea_provider():
    config = SourceConfig(id="codeberg", name="Codeberg", kind="gitea", base_url="https://codeberg.org")

    provider = build_provider(config)

    assert isinstance(provider, GiteaProvider)
    assert provider.kind == "gitea"
    assert provider.source_id == "codeberg"
    assert provider.name == "Codeberg"
    assert provider.api_root == "https://codeberg.org/api/v1"
    assert provider.web_hosts == frozenset({"codeberg.org", "www.codeberg.org"})
    provider.close()


def test_invalid_full_name_error_is_github_error():
    client = make_provider()
    with pytest.raises(GitHubError):
        client.compare_fork_with_parent("a/b/c")
