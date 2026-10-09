from __future__ import annotations

import json
import types

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from portable_manager.github_client import GitHubError, RateLimitError
from portable_manager.http_cache import ConditionalCache
from portable_manager.models import SourceConfig
from portable_manager.sources import gitlab
from portable_manager.sources.base import build_provider
from portable_manager.sources.gitlab import GitLabProvider

API = "https://gitlab.com/api/v4"
NOW = 1_700_000_000
TOKEN = "glpat-abcdefghijklmnop1234"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP send in this module is a test bug, so fail loudly."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("tests must not hit the network")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)


@pytest.fixture
def clock(monkeypatch):
    """Replace the ``time`` module seen by the gitlab module with a fake clock and sleep recorder."""
    state = types.SimpleNamespace(now=NOW, sleeps=[])
    fake_time = types.SimpleNamespace(
        time=lambda: state.now,
        sleep=lambda seconds: state.sleeps.append(seconds),
    )
    monkeypatch.setattr(gitlab, "time", fake_time)
    return state


def make_response(status=200, headers=None, body=None, url=f"{API}/x"):
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.url = url
    response._content = json.dumps(body).encode() if body is not None else b""
    return response


def script(provider, monkeypatch, responses):
    """Make ``provider.session.request`` return *responses* in order and record each call."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return queue.pop(0)

    monkeypatch.setattr(provider.session, "request", fake_request)
    return calls


def project(path, **extra):
    namespace, _, name = path.rpartition("/")
    payload = {
        "id": 1,
        "path_with_namespace": path,
        "path": name,
        "name": name.title(),
        "namespace": {"full_path": namespace},
        "web_url": f"https://gitlab.com/{path}",
        "description": "A tool",
        "star_count": 5,
        "last_activity_at": "2026-03-01T10:00:00.000Z",
        "default_branch": "main",
    }
    payload.update(extra)
    return payload


def release(tag, links=None, sources=None, **extra):
    payload = {
        "name": f"Release {tag}",
        "tag_name": tag,
        "released_at": "2026-02-01T09:00:00.000Z",
        "created_at": "2026-02-01T08:00:00.000Z",
        "upcoming_release": False,
        "_links": {"self": f"https://gitlab.com/g/p/-/releases/{tag}"},
        "assets": {"count": 0, "links": links or [], "sources": sources or []},
    }
    payload.update(extra)
    return payload


WIN_LINK = {
    "id": 7,
    "name": "windows/amd64",
    "url": f"{API}/projects/9/packages/generic/app/v1.2.0/app-windows-amd64.exe",
    "direct_asset_url": "https://gitlab.com/g/p/-/releases/v1.2.0/downloads/bin/app-windows-amd64.exe",
    "link_type": "other",
}
SOURCE_ZIP = {"format": "zip", "url": "https://gitlab.com/g/p/-/archive/v1.2.0/p-v1.2.0.zip"}


# ---------------------------------------------------------------- construction and build_provider


def test_build_provider_returns_gitlab_provider():
    provider = build_provider(SourceConfig(id="gitlab", name="GitLab", kind="gitlab", base_url="https://gitlab.com"))
    assert isinstance(provider, GitLabProvider)
    assert provider.kind == "gitlab"
    assert provider.source_id == "gitlab"
    assert provider.api_root == API
    provider.close()


def test_web_hosts_and_api_root_for_self_hosted_instance():
    provider = GitLabProvider(base_url="https://git.example.org/", source_id="work", name="Work GitLab")
    assert provider.base_url == "https://git.example.org"
    assert provider.web_hosts == frozenset({"git.example.org", "www.git.example.org"})
    assert provider.api_root == "https://git.example.org/api/v4"
    assert provider.extract_repo_full_name("https://git.example.org/g/p/-/tree/main") == "g/p"
    assert provider.extract_repo_full_name("https://gitlab.com/g/p") is None


def test_token_header_only_when_token_set():
    with_token = GitLabProvider(token=TOKEN)
    assert with_token.session.headers["PRIVATE-TOKEN"] == TOKEN
    anonymous = GitLabProvider()
    assert "PRIVATE-TOKEN" not in anonymous.session.headers


# ---------------------------------------------------------------- search


def test_search_maps_projects_including_subgroups(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=[
        project("inkscape/inkscape", star_count=4010, default_branch="master"),
        project("group/sub/tool", description=None, forked_from_project=None),
    ])])

    repos = provider.search_repositories("inkscape")

    assert calls[0]["url"] == f"{API}/projects"
    assert calls[0]["params"] == {"search": "inkscape", "order_by": "star_count", "sort": "desc", "per_page": 20}
    assert "simple" not in calls[0]["params"]
    first, second = repos
    assert first.full_name == "inkscape/inkscape"
    assert first.name == "inkscape"
    assert first.owner == "inkscape"
    assert first.html_url == first.homepage == "https://gitlab.com/inkscape/inkscape"
    assert first.stars == 4010
    assert first.default_branch == "master"
    assert first.updated_at == first.pushed_at == "2026-03-01T10:00:00.000Z"
    assert first.source_id == "gitlab"
    assert first.is_fork is False
    assert second.full_name == "group/sub/tool"
    assert second.owner == "group/sub"
    assert second.name == "tool"
    assert second.description == ""


def test_search_sort_updated_and_caps_per_page(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=[]), make_response(200, body=[])])

    provider.search_repositories("x", limit=200, sort="updated")
    provider.search_repositories("x", limit=0)

    assert calls[0]["params"]["order_by"] == "last_activity_at"
    assert calls[0]["params"]["per_page"] == 50
    assert calls[1]["params"]["per_page"] == 1


def test_search_drops_forks_unless_requested(monkeypatch):
    fork = project("someone/tool", forked_from_project={"id": 9, "path_with_namespace": "orig/tool", "default_branch": "main"})
    provider = GitLabProvider()
    script(provider, monkeypatch, [
        make_response(200, body=[project("orig/tool"), fork]),
        make_response(200, body=[project("orig/tool"), fork]),
    ])

    assert [r.full_name for r in provider.search_repositories("tool")] == ["orig/tool"]
    with_forks = provider.search_repositories("tool", include_forks=True)
    assert [r.full_name for r in with_forks] == ["orig/tool", "someone/tool"]
    assert with_forks[1].is_fork is True


# ---------------------------------------------------------------- get_repo


def test_get_repo_encodes_subgroup_path(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=project("group/sub/project"))])

    repo = provider.get_repo("group/sub/project")

    assert calls[0]["url"] == f"{API}/projects/group%2Fsub%2Fproject"
    assert repo.full_name == "group/sub/project"


def test_get_repo_404_gives_friendly_message(monkeypatch):
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(404, body={"message": "404 Project Not Found"})])

    with pytest.raises(GitHubError) as exc:
        provider.get_repo("group/private")
    assert str(exc.value) == (
        "Project group/private was not found on GitLab. It may be private, renamed or deleted."
    )


def test_invalid_project_paths_are_rejected_before_any_request(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [])
    for bad in ["single", "../x", "a/../b", "a//b", "a/b c", "https://gitlab.com/a/b", "a/."]:
        with pytest.raises(GitHubError):
            provider.get_repo(bad)
    assert calls == []


# ---------------------------------------------------------------- releases


def test_latest_release_uses_direct_asset_url_and_ignores_sources(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=[
        release("v1.2.0", links=[WIN_LINK, {"name": "bad", "url": "http://insecure.example/a.exe", "direct_asset_url": ""}], sources=[SOURCE_ZIP]),
    ])])

    result = provider.get_latest_release("g/p")

    assert calls[0]["url"] == f"{API}/projects/g%2Fp/releases"
    assert calls[0]["params"] == {"per_page": 20}
    assert result.tag_name == "v1.2.0"
    assert result.name == "Release v1.2.0"
    assert result.html_url == "https://gitlab.com/g/p/-/releases/v1.2.0"
    assert result.published_at == "2026-02-01T09:00:00.000Z"
    assert result.prerelease is False
    assert result.release_id == 0
    assert len(result.assets) == 1  # the http link and the source archive are both dropped
    asset = result.assets[0]
    assert asset.download_url == WIN_LINK["direct_asset_url"]
    assert asset.name == "app-windows-amd64.exe"
    assert asset.size == 0
    assert asset.content_type == ""
    assert asset.digest == ""


def test_latest_release_falls_back_to_url_and_builds_html_url(monkeypatch):
    provider = GitLabProvider()
    link = {"name": "linux/arm64", "url": "https://gitlab.com/api/v4/projects/9/packages/generic/p/v1/app-linux-arm64", "direct_asset_url": None}
    release_payload = release("v1 beta", links=[link])
    release_payload.pop("_links")
    script(provider, monkeypatch, [make_response(200, body=[release_payload])])

    result = provider.get_latest_release("g/p")

    assert result.html_url == "https://gitlab.com/g/p/-/releases/v1%20beta"
    assert result.assets[0].download_url == link["url"]
    assert result.assets[0].name == "app-linux-arm64"


def test_latest_release_skips_upcoming_unless_prereleases_requested(monkeypatch):
    upcoming = release("v2.0.0-rc1", upcoming_release=True)
    stable = release("v1.9.0")
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(200, body=[upcoming, stable])])
    assert provider.get_latest_release("g/p").tag_name == "v1.9.0"

    script(provider, monkeypatch, [make_response(200, body=[upcoming, stable])])
    pre = provider.get_latest_release("g/p", include_prereleases=True)
    assert pre.tag_name == "v2.0.0-rc1"
    assert pre.prerelease is True


def test_no_releases_returns_none(monkeypatch):
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(200, body=[])])
    assert provider.get_latest_release("g/p") is None

    script(provider, monkeypatch, [make_response(404, body={"message": "404"})])
    assert provider.get_latest_release("g/p") is None


def test_release_info_reports_windows_asset(monkeypatch):
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(200, body=[release("v1.2.0", links=[WIN_LINK])])])
    assert provider.get_latest_release_info("g/p") == ("2026-02-01T09:00:00.000Z", True)

    linux = {"name": "linux/amd64", "url": "https://gitlab.com/x/app-linux.tar.gz", "direct_asset_url": ""}
    script(provider, monkeypatch, [make_response(200, body=[release("v1.2.0", links=[linux])])])
    assert provider.get_latest_release_info("g/p") == ("2026-02-01T09:00:00.000Z", False)


def test_release_info_is_empty_on_error_or_no_release(monkeypatch):
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(500, body={})])
    assert provider.get_latest_release_info("g/p") == ("", False)

    script(provider, monkeypatch, [make_response(404, body={})])
    assert provider.get_latest_release_info("g/p") == ("", False)


# ---------------------------------------------------------------- fork comparison


def test_compare_fork_reports_ahead_and_behind(monkeypatch):
    fork = project(
        "me/inkscape", id=85, default_branch="master",
        forked_from_project={"id": 3, "path_with_namespace": "inkscape/inkscape", "default_branch": "master"},
    )
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [
        make_response(200, body=fork),
        make_response(200, body={"commits": [{"id": "a"}, {"id": "b"}], "compare_timeout": False}),
        make_response(200, body={"commits": [{}] * 5, "compare_timeout": False}),
    ])

    assert provider.compare_fork_with_parent("me/inkscape") == ("inkscape/inkscape", 2, 5)
    assert calls[1]["url"] == f"{API}/projects/me%2Finkscape/repository/compare"
    assert calls[1]["params"] == {"from": "master", "from_project_id": 3, "to": "master"}
    assert calls[2]["url"] == f"{API}/projects/inkscape%2Finkscape/repository/compare"
    assert calls[2]["params"] == {"from": "master", "from_project_id": 85, "to": "master"}


def test_compare_non_fork_raises(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=project("me/original"))])

    with pytest.raises(GitHubError, match="^me/original is not a fork\\.$"):
        provider.compare_fork_with_parent("me/original")
    assert len(calls) == 1


def test_compare_failure_raises_github_error(monkeypatch):
    fork = project("me/tool", id=5, forked_from_project={"id": 3, "path_with_namespace": "o/tool", "default_branch": "main"})
    provider = GitLabProvider()
    script(provider, monkeypatch, [make_response(200, body=fork), make_response(404, body={})])

    with pytest.raises(GitHubError, match="HTTP 404"):
        provider.compare_fork_with_parent("me/tool")


# ---------------------------------------------------------------- URL helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gitlab.com/inkscape/inkscape", "inkscape/inkscape"),
        ("gitlab.com/inkscape/inkscape", "inkscape/inkscape"),
        ("https://www.gitlab.com/inkscape/inkscape/", "inkscape/inkscape"),
        ("https://gitlab.com/group/sub/project", "group/sub/project"),
        ("https://gitlab.com/group/sub/project/-/tree/main", "group/sub/project"),
        ("https://gitlab.com/group/project/-/releases/v1.0", "group/project"),
        ("https://gitlab.com/group/project/-/issues/3", "group/project"),
        ("https://gitlab.com/group/project.git", "group/project"),
        ("https://gitlab.com/explore", None),
        ("https://gitlab.com/explore/projects", None),
        ("https://gitlab.com/groups/inkscape", None),
        ("https://gitlab.com/-/profile", None),
        ("https://gitlab.com/onlyone", None),
        ("https://gitlab.com/group/..", None),
        ("https://gitlab.com/group/project/-/releases/v1.0/downloads/app.exe", None),
        ("https://gitlab.com/group/project/uploads/0123456789abcdef0123456789abcdef/app.zip", None),
        ("https://gitlab.com/group/project/-/raw/main/README.md", None),
        ("https://gitlab.com/group/project/-/blob/main/x.txt", None),
        ("https://gitlab.example.org/group/project", None),
        ("https://github.com/owner/repo", None),
        ("", None),
    ],
)
def test_extract_repo_full_name(url, expected):
    assert GitLabProvider().extract_repo_full_name(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://gitlab.com/gitlab-org/release-cli/-/releases/v0.24.0/downloads/bin/release-cli-windows-amd64.exe",
            ("gitlab-org/release-cli", "v0.24.0"),
        ),
        ("https://gitlab.com/group/sub/proj/-/releases/v1.0%2Bbeta/downloads/app.zip", ("group/sub/proj", "v1.0+beta")),
        ("https://gitlab.com/group/proj/-/releases/v1.0", None),
        ("https://gitlab.com/group/proj/-/releases/v1.0/downloads", None),
        ("https://gitlab.com/group/proj/-/releases/permalink/latest/downloads/app.exe", None),
        ("https://example.org/group/proj/-/releases/v1/downloads/a.exe", None),
    ],
)
def test_parse_release_asset_url(url, expected):
    assert GitLabProvider().parse_release_asset_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://gitlab.com/group/proj/-/releases/v1/downloads/app.exe", "group/proj"),
        ("https://gitlab.com/group/sub/proj/uploads/0123456789abcdef0123456789abcdef/app.zip", "group/sub/proj"),
        ("https://gitlab.com/uploads/0123456789abcdef0123456789abcdef/app.zip", None),
        ("https://gitlab.com/group/proj", None),
        ("https://objects.githubusercontent.com/a/b/releases/x", None),
    ],
)
def test_infer_repo_from_asset_url(url, expected):
    assert GitLabProvider().infer_repo_from_asset_url(url) == expected


# ---------------------------------------------------------------- secrets and errors


def test_token_never_appears_in_error_messages(monkeypatch):
    provider = GitLabProvider(token=TOKEN)
    script(provider, monkeypatch, [make_response(401, body={"message": "401 Unauthorized"})])
    with pytest.raises(GitHubError) as exc:
        provider.get_repo("g/p")
    assert TOKEN not in str(exc.value)

    def broken(*_args, **_kwargs):
        raise requests.ConnectionError(f"failed to reach {API}/projects/g%2Fp?token={TOKEN}")

    monkeypatch.setattr(provider.session, "request", broken)
    with pytest.raises(GitHubError) as exc:
        provider.get_repo("g/p")
    assert str(exc.value) == "Couldn't connect to GitLab."
    assert TOKEN not in str(exc.value)


# ---------------------------------------------------------------- rate limits


def test_rate_limit_headers_are_recorded_and_refresh_is_free(monkeypatch):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(200, body=[], headers={
        "RateLimit-Remaining": "499", "RateLimit-Reset": str(NOW + 60),
    })])

    provider.search_repositories("x")

    assert provider.rate_limit_remaining == 499
    assert provider.rate_limit_reset_at == NOW + 60
    assert provider.can_afford(10) is True
    assert provider.can_afford(500) is False
    assert provider.refresh_rate_limit() == 499
    assert len(calls) == 1


def test_short_retry_after_is_waited_and_retried(monkeypatch, clock):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [
        make_response(429, headers={"Retry-After": "3"}),
        make_response(200, body=project("g/p")),
    ])

    assert provider.get_repo("g/p").full_name == "g/p"
    assert clock.sleeps == [3]
    assert len(calls) == 2


def test_long_retry_after_raises_friendly_rate_limit_error(monkeypatch, clock):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(429, headers={"Retry-After": "120"})])

    with pytest.raises(RateLimitError) as exc:
        provider.search_repositories("x")

    assert str(exc.value) == (
        "GitLab rate limit reached. Resets in ~2 min. Add a token for this source in Settings → Sources."
    )
    assert exc.value.reset_at == NOW + 120
    assert clock.sleeps == []
    assert len(calls) == 1


def test_rate_limit_gives_up_after_max_retries(monkeypatch, clock):
    provider = GitLabProvider()
    calls = script(provider, monkeypatch, [make_response(429, headers={"Retry-After": "1"}) for _ in range(4)])

    with pytest.raises(RateLimitError):
        provider.get_repo("g/p")
    assert clock.sleeps == [1, 1, 1]
    assert len(calls) == 4


def test_conditional_request_reuses_cached_body_on_304(monkeypatch):
    cache = ConditionalCache(None)
    provider = GitLabProvider(cache=cache)
    calls = script(provider, monkeypatch, [
        make_response(200, body=project("g/p"), headers={"ETag": 'W/"abc"'}),
        make_response(304, headers={"RateLimit-Remaining": "498"}),
    ])

    first = provider.get_repo("g/p")
    second = provider.get_repo("g/p")

    assert first.full_name == second.full_name == "g/p"
    assert "If-None-Match" not in calls[0].get("headers", {})
    assert calls[1]["headers"]["If-None-Match"] == 'W/"abc"'
    assert provider.rate_limit_remaining == 498
