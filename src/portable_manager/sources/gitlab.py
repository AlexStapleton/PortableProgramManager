"""GitLab (gitlab.com or a self-hosted instance) as a code-hosting source.

Projects are identified by their path, which may include subgroups
(``group/sub/project``). A release's downloadable files are its asset *links*
(``assets.links``); the source archives GitLab generates for every release
(``assets.sources``) are ignored.
"""

from __future__ import annotations

import math
import re
import time
from urllib.parse import quote, unquote, urlsplit

import requests

from ..github_client import (
    DEFAULT_TIMEOUT,
    MAX_RATE_LIMIT_WAIT,
    MAX_RETRIES,
    USER_AGENT,
    GitHubClient,
    GitHubError,
    GitHubRelease,
    GitHubReleaseAsset,
    GitHubRepo,
    RateLimitError,
)
from ..http_cache import ConditionalCache

# "group/project" or "group/sub/project": 2+ segments of safe characters.
_PROJECT_PATH_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
# Upload links look like /<project>/uploads/<32 hex characters>/<file>.
_UPLOAD_SECRET_RE = re.compile(r"[0-9a-f]{32}")
# Wait assumed when GitLab reports a rate limit without any reset information.
_UNKNOWN_RESET_WAIT = 60
# First path segments on a GitLab host that are site pages, never project groups.
_RESERVED_FIRST = {
    "explore", "users", "dashboard", "help", "groups", "search", "admin", "-", "api", "uploads",
}
# Sub-pages after "/-/" that serve a file rather than a project page.
_FILE_SUBPAGES = {"raw", "blob", "archive"}


def _project_path(path: str) -> str:
    """Return *path* if it is a safe ``group/.../project`` path, else raise GitHubError."""
    if (
        not isinstance(path, str)
        or not _PROJECT_PATH_RE.fullmatch(path)
        or any(segment in (".", "..") for segment in path.split("/"))
    ):
        raise GitHubError(f"Invalid project path: {path!r}. Expected 'group/project'.")
    return path


def _site_segments(url: str, hosts: frozenset[str]) -> list[str] | None:
    """Non-empty path segments of *url* when it is on one of *hosts*, else None.

    Accepts URLs with or without a scheme.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in hosts:
        return None
    return [segment for segment in parts.path.split("/") if segment]


def _is_https(url: str) -> bool:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _asset_filename(url: str) -> str:
    """The file name at the end of a download URL, percent-decoded ("" if none)."""
    try:
        tail = urlsplit(url or "").path.rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        return ""
    return unquote(tail)


class GitLabProvider:
    """GitLab API client; the "gitlab" source provider."""

    kind = "gitlab"

    def __init__(
        self,
        token: str = "",
        cache: ConditionalCache | None = None,
        base_url: str = "https://gitlab.com",
        source_id: str = "gitlab",
        name: str = "GitLab",
    ) -> None:
        self.source_id = source_id
        self.name = name
        self.base_url = base_url.rstrip("/")
        host = (urlsplit(self.base_url).hostname or "gitlab.com").lower()
        self.web_hosts = frozenset({host, f"www.{host}"})
        self.api_root = f"{self.base_url}/api/v4"
        self.session = requests.Session()
        # Conditional-request cache (shared across clients); None disables it.
        self.cache = cache
        self._cache_scope = token[-8:] if token else ""
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        if token:
            self.session.headers["PRIVATE-TOKEN"] = token
        # Quota as last reported by GitLab (None = not seen yet).
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: int | None = None

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    # ------------------------------------------------------------------ rate limits

    def refresh_rate_limit(self) -> int | None:
        """GitLab has no free quota endpoint, so this returns the last known value."""
        return self.rate_limit_remaining

    def can_afford(self, calls: int) -> bool:
        """True when the quota is unknown or has more than *calls* requests left."""
        return self.rate_limit_remaining is None or self.rate_limit_remaining > calls

    def _record_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("RateLimit-Remaining", "")
        if remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        reset_at = response.headers.get("RateLimit-Reset", "")
        if reset_at.isdigit():
            self.rate_limit_reset_at = int(reset_at)

    def _rate_limit_error(self, response: requests.Response, reset_at: int | None, now: int) -> RateLimitError:
        seconds = max(0, reset_at - now) if reset_at is not None else _UNKNOWN_RESET_WAIT
        minutes = max(1, math.ceil(seconds / 60))
        return RateLimitError(
            f"{self.name} rate limit reached. Resets in ~{minutes} min. "
            "Add a token for this source in Settings → Sources.",
            reset_at=reset_at,
            response=response,
        )

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue an HTTP request, retrying briefly when GitLab answers 429.

        A rate-limited request is retried (up to ``MAX_RETRIES`` times) only when
        GitLab asks for a wait of at most ``MAX_RATE_LIMIT_WAIT`` seconds; otherwise
        ``RateLimitError`` is raised without sleeping.
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        cache_key = cached = None
        if method == "GET" and self.cache is not None:
            cache_key = ConditionalCache.make_key(url, kwargs.get("params"), self._cache_scope)
            cached = self.cache.get(cache_key)
            if cached:
                kwargs["headers"] = {**kwargs.get("headers", {}), "If-None-Match": cached["etag"]}
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.Timeout:
                raise GitHubError(f"{self.name} did not answer in time.") from None
            except requests.RequestException:
                raise GitHubError(f"Couldn't connect to {self.name}.") from None
            self._record_rate_limit(response)
            if response.status_code == 304 and cached:
                return GitHubClient._from_cache(response, cached)
            if cache_key and response.status_code == 200 and response.headers.get("ETag"):
                self.cache.put(cache_key, response.headers["ETag"], 200, response.content)
            if response.status_code != 429:
                return response

            now = int(time.time())
            retry_after = response.headers.get("Retry-After", "")
            reset_header = response.headers.get("RateLimit-Reset", "")
            if retry_after.isdigit():
                wait = int(retry_after)
                reset_at: int | None = now + wait
            elif reset_header.isdigit():
                reset_at = int(reset_header)
                wait = max(0, reset_at - now) + 1
            else:
                reset_at = None
                wait = _UNKNOWN_RESET_WAIT
            if wait > MAX_RATE_LIMIT_WAIT or attempt >= MAX_RETRIES:
                raise self._rate_limit_error(response, reset_at, now)
            time.sleep(wait)
        raise GitHubError(f"{self.name} request failed.")  # not reached: the last attempt returns or raises

    @staticmethod
    def _raise_for_status(response: requests.Response, what: str) -> None:
        if response.status_code >= 400:
            raise GitHubError(f"GitLab returned HTTP {response.status_code} for {what}.")

    @staticmethod
    def _parse_json(response: requests.Response) -> dict | list:
        try:
            return response.json()
        except (ValueError, requests.JSONDecodeError):
            raise GitHubError(f"GitLab API returned invalid JSON (HTTP {response.status_code}).")

    def _project_url(self, path: str) -> str:
        return f"{self.api_root}/projects/{quote(path, safe='')}"

    def _get_project(self, path: str) -> dict:
        path = _project_path(path)
        response = self._request("GET", self._project_url(path))
        if response.status_code == 404:
            raise GitHubError(
                f"Project {path} was not found on {self.name}. It may be private, renamed or deleted."
            )
        self._raise_for_status(response, f"project {path}")
        data = self._parse_json(response)
        if not isinstance(data, dict):
            raise GitHubError("GitLab returned an unexpected project format.")
        return data

    # ------------------------------------------------------------------ provider protocol

    def search_repositories(
        self, query: str, limit: int = 20, sort: str = "stars", include_forks: bool = False
    ) -> list[GitHubRepo]:
        order_by = "last_activity_at" if sort == "updated" else "star_count"
        response = self._request(
            "GET",
            f"{self.api_root}/projects",
            params={
                "search": query,
                "order_by": order_by,
                "sort": "desc",
                "per_page": max(1, min(limit, 50)),
            },
        )
        self._raise_for_status(response, "project search")
        data = self._parse_json(response)
        items = data if isinstance(data, list) else []
        repos = [self._repo_from_api(item) for item in items if isinstance(item, dict)]
        if not include_forks:
            repos = [repo for repo in repos if not repo.is_fork]
        return repos

    def get_repo(self, full_name: str) -> GitHubRepo:
        return self._repo_from_api(self._get_project(full_name))

    def get_latest_release(self, full_name: str, include_prereleases: bool = False) -> GitHubRelease | None:
        path = _project_path(full_name)
        response = self._request(
            "GET", f"{self._project_url(path)}/releases", params={"per_page": 20}
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response, f"releases of {path}")
        releases = self._parse_json(response)
        if not isinstance(releases, list):
            return None
        for payload in releases:
            if not isinstance(payload, dict):
                continue
            if payload.get("upcoming_release") and not include_prereleases:
                continue
            return self._release_from_api(path, payload)
        return None

    def get_latest_release_info(self, full_name: str) -> tuple[str, bool]:
        """``(released_at, has a Windows-looking asset)``; ``("", False)`` when none or on error."""
        try:
            release = self.get_latest_release(full_name)
        except Exception:
            return "", False
        if release is None:
            return "", False
        has_win = any(
            GitHubClient._WIN_ASSET_RE.search(name)
            for asset in release.assets
            for name in (asset.name, _asset_filename(asset.download_url))
        )
        return release.published_at, bool(has_win)

    def compare_fork_with_parent(self, full_name: str) -> tuple[str, int, int]:
        """``(parent path, ahead_by, behind_by)`` for a fork, from two compare calls.

        *ahead* counts commits on the fork's default branch that the parent's default
        branch doesn't have; *behind* the reverse. Raises GitHubError if the project
        isn't a fork or the comparison fails.
        """
        project = self._get_project(full_name)
        parent = project.get("forked_from_project")
        if not isinstance(parent, dict) or not parent.get("path_with_namespace"):
            raise GitHubError(f"{full_name} is not a fork.")
        if not isinstance(parent.get("id"), int) or not isinstance(project.get("id"), int):
            raise GitHubError(f"{self.name} didn't report project ids for {full_name}.")
        parent_path = _project_path(parent["path_with_namespace"])
        fork_branch = project.get("default_branch") or "main"
        parent_branch = parent.get("default_branch") or "main"
        ahead = self._count_commits(
            target=_project_path(full_name), from_ref=parent_branch,
            from_project_id=parent["id"], to_ref=fork_branch,
        )
        behind = self._count_commits(
            target=parent_path, from_ref=fork_branch,
            from_project_id=project["id"], to_ref=parent_branch,
        )
        return parent_path, ahead, behind

    def _count_commits(self, target: str, from_ref: str, from_project_id: int, to_ref: str) -> int:
        """Commits reachable from *to_ref* in *target* but not from *from_ref* in the other project."""
        response = self._request(
            "GET",
            f"{self._project_url(target)}/repository/compare",
            params={"from": from_ref, "from_project_id": from_project_id, "to": to_ref},
        )
        self._raise_for_status(response, f"comparison of {target}")
        data = self._parse_json(response)
        commits = data.get("commits") if isinstance(data, dict) else None
        if not isinstance(commits, list):
            raise GitHubError("GitLab returned an unexpected comparison format.")
        # The commit list stays complete even when compare_timeout is set; the diffs are not used.
        return len(commits)

    def extract_repo_full_name(self, url: str) -> str | None:
        """Project path when *url* is a project page on this GitLab host, else None.

        Everything after a ``/-/`` segment is a sub-page of the project and is
        dropped. Release downloads, uploads and raw or blob file links return None.
        """
        segments = _site_segments(url, self.web_hosts)
        if not segments:
            return None
        if "-" in segments:
            dash = segments.index("-")
            tail = segments[dash + 1:]
            if tail and tail[0] in _FILE_SUBPAGES:
                return None
            if len(tail) >= 4 and tail[0] == "releases" and tail[2] == "downloads":
                return None
            project = segments[:dash]
        else:
            project = segments
        for i, segment in enumerate(project):
            if segment == "uploads" and i >= 2 and len(project) == i + 3 and _UPLOAD_SECRET_RE.fullmatch(project[i + 1]):
                return None
        if len(project) < 2 or project[0].lower() in _RESERVED_FIRST:
            return None
        project = project[:-1] + [project[-1].removesuffix(".git")]
        path = "/".join(project)
        try:
            return _project_path(path)
        except GitHubError:
            return None

    def parse_release_asset_url(self, url: str) -> tuple[str, str] | None:
        """``(project path, tag)`` for ``.../-/releases/<tag>/downloads/<file>`` on this host, else None."""
        segments = _site_segments(url, self.web_hosts)
        if not segments or "-" not in segments:
            return None
        dash = segments.index("-")
        tail = segments[dash + 1:]
        if len(tail) < 4 or tail[0] != "releases" or tail[2] != "downloads":
            return None
        try:
            path = _project_path("/".join(segments[:dash]))
        except GitHubError:
            return None
        tag = unquote(tail[1])
        if not tag:
            return None
        return path, tag

    def infer_repo_from_asset_url(self, url: str) -> str | None:
        """Project path for a release-download or upload link on this host, else None."""
        asset = self.parse_release_asset_url(url)
        if asset is not None:
            return asset[0]
        segments = _site_segments(url, self.web_hosts)
        if not segments:
            return None
        for i, segment in enumerate(segments):
            if segment == "uploads" and i >= 2 and len(segments) == i + 3 and _UPLOAD_SECRET_RE.fullmatch(segments[i + 1]):
                try:
                    return _project_path("/".join(segments[:i]))
                except GitHubError:
                    return None
        return None

    # ------------------------------------------------------------------ result mapping

    def _repo_from_api(self, item: dict) -> GitHubRepo:
        path = item.get("path_with_namespace") or ""
        namespace = item.get("namespace")
        owner = namespace.get("full_path", "") if isinstance(namespace, dict) else ""
        web_url = item.get("web_url") or f"{self.base_url}/{path}"
        last_activity = item.get("last_activity_at") or ""
        return GitHubRepo(
            full_name=path,
            name=item.get("path") or item.get("name") or path.rsplit("/", 1)[-1],
            owner=owner,
            description=item.get("description") or "",
            html_url=web_url,
            homepage=web_url,
            stars=item.get("star_count") or 0,
            updated_at=last_activity,
            default_branch=item.get("default_branch") or "main",
            pushed_at=last_activity,
            is_fork=bool(item.get("forked_from_project")),
            source_id=self.source_id,
        )

    def _release_from_api(self, path: str, payload: dict) -> GitHubRelease:
        tag = payload.get("tag_name") or ""
        assets_block = payload.get("assets")
        links = assets_block.get("links") if isinstance(assets_block, dict) else None
        assets: list[GitHubReleaseAsset] = []
        for link in links or []:
            if not isinstance(link, dict):
                continue
            url = link.get("direct_asset_url") or link.get("url") or ""
            if not _is_https(url):
                continue
            assets.append(
                GitHubReleaseAsset(
                    # The file name, so the installer sees ".exe" etc.; link names are labels like "windows/amd64".
                    name=_asset_filename(url) or link.get("name") or "",
                    download_url=url,
                    size=0,
                    content_type="",
                    digest="",
                )
            )
        links_block = payload.get("_links")
        self_link = links_block.get("self") if isinstance(links_block, dict) else None
        html_url = self_link if isinstance(self_link, str) and self_link else (
            f"{self.base_url}/{path}/-/releases/{quote(tag, safe='')}"
        )
        return GitHubRelease(
            release_id=0,  # GitLab releases have no numeric id in the API
            tag_name=tag,
            name=payload.get("name") or tag,
            html_url=html_url,
            published_at=payload.get("released_at") or payload.get("created_at") or "",
            prerelease=bool(payload.get("upcoming_release")),
            assets=assets,
        )
