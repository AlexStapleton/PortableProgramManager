"""Gitea, Forgejo and Codeberg as a code-hosting source.

Their REST API (``/api/v1``) is close enough to GitHub's that the result types
from :mod:`..github_client` are reused unchanged. Gitea-family servers rarely
send rate-limit headers, so the quota is only known when a server does send them.
"""

from __future__ import annotations

import math
import time
from typing import Optional
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
    _github_path_segments,
    _owner_repo,
    _REPO_FULL_NAME_RE,
)
from ..http_cache import ConditionalCache

# Wait assumed when a 429 comes without a usable Retry-After header.
_UNKNOWN_WAIT = 60
# First path segments that are site pages, never repository owners.
_RESERVED_OWNERS = {
    "explore", "user", "users", "org", "admin", "api", "assets", "-",
    "repo", "notifications", "attachments",
}
# Third path segments (after owner/repo) that point at a file, not at the repo page.
_FILE_PATH_SEGMENTS = {"raw", "media", "archive", "src"}


def extract_repo_full_name(url: str, hosts: frozenset[str]) -> Optional[str]:
    """``owner/repo`` when *url* is a project page on one of *hosts*, else None."""
    segments = _github_path_segments(url, hosts)
    if not segments or len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1].removesuffix(".git")
    if owner.lower() in _RESERVED_OWNERS:
        return None
    if len(segments) >= 3 and segments[2] in _FILE_PATH_SEGMENTS:
        return None
    if len(segments) >= 4 and segments[2] == "releases" and segments[3] == "download":
        return None
    return _owner_repo(owner, repo)


def parse_release_asset_url(url: str, hosts: frozenset[str]) -> tuple[str, str] | None:
    """``(owner/repo, tag)`` for ``/{owner}/{repo}/releases/download/{tag}/{file}`` on *hosts*, else None."""
    segments = _github_path_segments(url, hosts)
    if not segments or len(segments) < 6:
        return None
    if segments[2] != "releases" or segments[3] != "download":
        return None
    full_name = _owner_repo(segments[0], segments[1])
    if full_name is None:
        return None
    tag = unquote(segments[4])
    if not tag:
        return None
    return full_name, tag


class GiteaProvider:
    """Gitea, Forgejo or Codeberg instance; the "gitea" source provider."""

    kind = "gitea"

    def __init__(
        self,
        token: str = "",
        cache: ConditionalCache | None = None,
        base_url: str = "https://codeberg.org",
        source_id: str = "gitea",
        name: str = "Gitea",
    ) -> None:
        self.source_id = source_id
        self.name = name
        self.base_url = base_url.rstrip("/")
        host = (urlsplit(self.base_url).hostname or "").lower()
        self.web_hosts = frozenset({host, f"www.{host}"})
        self.api_root = f"{self.base_url}/api/v1"
        self.session = requests.Session()
        # Conditional-request cache (shared across clients); None disables it.
        self.cache = cache
        self._cache_scope = token[-8:] if token else ""
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        if token:
            self.session.headers["Authorization"] = f"token {token}"
        # Quota as last reported by the server (None = never reported).
        self.rate_limit_remaining: int | None = None

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> "GiteaProvider":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ quota

    def refresh_rate_limit(self) -> int | None:
        """Gitea has no free quota endpoint, so this returns the last known value."""
        return self.rate_limit_remaining

    def can_afford(self, calls: int) -> bool:
        """True when the quota is unknown or has more than *calls* requests left."""
        return self.rate_limit_remaining is None or self.rate_limit_remaining > calls

    def _record_rate_limit(self, response: requests.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining", "")
        if remaining.isdigit():
            self.rate_limit_remaining = int(remaining)

    def _rate_limit_error(self, wait: int | None, response: requests.Response) -> RateLimitError:
        seconds = wait if wait is not None else _UNKNOWN_WAIT
        minutes = max(1, math.ceil(seconds / 60))
        return RateLimitError(
            f"{self.name} rate limit reached. Try again in ~{minutes} min.",
            response=response,
        )

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue an HTTP request with conditional caching and short retries on HTTP 429.

        A 429 is retried (up to ``MAX_RETRIES`` times) only when ``Retry-After`` asks
        for at most ``MAX_RATE_LIMIT_WAIT`` seconds; otherwise ``RateLimitError`` is
        raised without sleeping.
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        cache_key = cached = None
        if method == "GET" and self.cache is not None:
            cache_key = ConditionalCache.make_key(url, kwargs.get("params"), self._cache_scope)
            cached = self.cache.get(cache_key)
            if cached:
                kwargs["headers"] = {**kwargs.get("headers", {}), "If-None-Match": cached["etag"]}
        for attempt in range(MAX_RETRIES + 1):
            response = self.session.request(method, url, **kwargs)
            self._record_rate_limit(response)
            if response.status_code == 304 and cached:
                return GitHubClient._from_cache(response, cached)
            if cache_key and response.status_code == 200 and response.headers.get("ETag"):
                self.cache.put(cache_key, response.headers["ETag"], 200, response.content)
            if response.status_code != 429:
                return response

            retry_after = response.headers.get("Retry-After", "").strip()
            wait = int(retry_after) if retry_after.isdigit() else None
            if wait is None or wait > MAX_RATE_LIMIT_WAIT or attempt >= MAX_RETRIES:
                raise self._rate_limit_error(wait, response)
            time.sleep(wait)
        raise GitHubError(f"{self.name} request failed.")  # not reached: the last attempt returns or raises

    def _raise_for_status(self, response: requests.Response, what: str) -> None:
        """Raise a short GitHubError for any HTTP error status (no URL or headers included)."""
        if response.status_code >= 400:
            raise GitHubError(f"{self.name} returned HTTP {response.status_code} for {what}.")

    def _json(self, response: requests.Response) -> dict | list:
        try:
            return response.json()
        except ValueError:
            raise requests.RequestException(
                f"{self.name} API returned invalid JSON (HTTP {response.status_code})."
            )

    def _validate_full_name(self, full_name: str) -> None:
        if not _REPO_FULL_NAME_RE.match(full_name):
            raise GitHubError(f"Invalid repository name format: {full_name!r}. Expected 'owner/repo'.")

    # ------------------------------------------------------------------ API

    def search_repositories(
        self, query: str, limit: int = 20, sort: str = "stars", include_forks: bool = False
    ) -> list[GitHubRepo]:
        params: dict = {"q": query, "order": "desc", "limit": max(1, min(limit, 50))}
        if sort in ("stars", "updated"):
            params["sort"] = sort
        response = self._request("GET", f"{self.api_root}/repos/search", params=params)
        if response.status_code >= 400 and sort == "stars":
            # Older servers don't know sort=stars; retry with their default order.
            unsorted = {key: value for key, value in params.items() if key != "sort"}
            response = self._request("GET", f"{self.api_root}/repos/search", params=unsorted)
        self._raise_for_status(response, "repository search")
        data = self._json(response)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        repos = [self._tag_source(self._repo_from_api(item)) for item in items if isinstance(item, dict)]
        return repos if include_forks else [repo for repo in repos if not repo.is_fork]

    def get_repo(self, full_name: str) -> GitHubRepo:
        self._validate_full_name(full_name)
        response = self._request("GET", f"{self.api_root}/repos/{full_name}")
        if response.status_code == 404:
            raise GitHubError(
                f"Repository {full_name} was not found on {self.name}. It may be private, renamed or deleted."
            )
        self._raise_for_status(response, f"repository {full_name}")
        return self._tag_source(self._repo_from_api(self._json(response)))

    def get_latest_release(self, full_name: str, include_prereleases: bool = False) -> Optional[GitHubRelease]:
        self._validate_full_name(full_name)
        if include_prereleases:
            response = self._request("GET", f"{self.api_root}/repos/{full_name}/releases", params={"limit": 10})
            if response.status_code == 404:
                return None
            self._raise_for_status(response, f"releases of {full_name}")
            releases = self._json(response)
            if not isinstance(releases, list):
                return None
            for payload in releases:
                if isinstance(payload, dict) and not payload.get("draft"):
                    return self._release_from_api(payload)
            return None

        response = self._request("GET", f"{self.api_root}/repos/{full_name}/releases/latest")
        if response.status_code == 404:
            return None
        self._raise_for_status(response, f"latest release of {full_name}")
        return self._release_from_api(self._json(response))

    def get_latest_release_info(self, full_name: str) -> tuple[str, bool]:
        """``(published_at, has a Windows-looking asset)``; ``("", False)`` on any error."""
        try:
            self._validate_full_name(full_name)
            response = self._request("GET", f"{self.api_root}/repos/{full_name}/releases/latest")
            if response.status_code == 404:
                return "", False
            self._raise_for_status(response, f"latest release of {full_name}")
            data = self._json(response)
            if not isinstance(data, dict):
                return "", False
            assets = data.get("assets") or []
            has_win = any(
                GitHubClient._WIN_ASSET_RE.search(asset.get("name") or "")
                for asset in assets
                if isinstance(asset, dict)
            )
            return data.get("published_at") or "", has_win
        except Exception:
            return "", False

    def compare_fork_with_parent(self, full_name: str) -> tuple[str, int, int]:
        """``(parent path, ahead_by, behind_by)`` for a fork, using each side's default branch.

        Two compare calls: commits on the fork missing from the parent (ahead), and
        commits on the parent missing from the fork (behind). Raises GitHubError when
        the repo isn't a fork or the server can't compare them.
        """
        self._validate_full_name(full_name)
        response = self._request("GET", f"{self.api_root}/repos/{full_name}")
        self._raise_for_status(response, f"repository {full_name}")
        data = self._json(response)
        parent = data.get("parent") if isinstance(data, dict) else None
        if not isinstance(parent, dict) or not parent.get("full_name"):
            raise GitHubError(f"{full_name} is not a fork.")
        parent_name = parent["full_name"]
        self._validate_full_name(parent_name)
        parent_branch = parent.get("default_branch") or "main"
        fork_branch = data.get("default_branch") or "main"
        fork_owner = full_name.split("/", 1)[0]
        parent_owner = parent_name.split("/", 1)[0]
        ahead = self._compare_count(parent_name, parent_branch, fork_owner, fork_branch)
        behind = self._compare_count(full_name, fork_branch, parent_owner, parent_branch)
        return parent_name, ahead, behind

    def _compare_count(self, repo: str, base: str, head_owner: str, head_branch: str) -> int:
        """Commits reachable from ``head_owner:head_branch`` but not from ``base`` in *repo*.

        Gitea resolves ``owner:branch`` heads inside a repo with the same name as *repo*.
        """
        url = (
            f"{self.api_root}/repos/{repo}/compare/"
            f"{quote(base, safe='')}...{quote(head_owner, safe='')}:{quote(head_branch, safe='')}"
        )
        response = self._request("GET", url)
        if response.status_code >= 400:
            # 404/405 on servers without the compare API; 5xx when the server can't diff the two.
            raise GitHubError(f"{self.name} can't compare forks on this server.")
        data = self._json(response)
        total = data.get("total_commits") if isinstance(data, dict) else None
        if not isinstance(total, int) or isinstance(total, bool):
            raise GitHubError(f"{self.name} can't compare forks on this server.")
        return total

    # ------------------------------------------------------------------ URLs

    def extract_repo_full_name(self, url: str) -> Optional[str]:
        return extract_repo_full_name(url, self.web_hosts)

    def parse_release_asset_url(self, url: str) -> tuple[str, str] | None:
        return parse_release_asset_url(url, self.web_hosts)

    def infer_repo_from_asset_url(self, url: str) -> Optional[str]:
        asset = parse_release_asset_url(url, self.web_hosts)
        return asset[0] if asset else None

    # --------------------------------------------------------------- mapping

    def _tag_source(self, repo: GitHubRepo) -> GitHubRepo:
        repo.source_id = self.source_id
        return repo

    def _repo_from_api(self, item: dict) -> GitHubRepo:
        owner_obj = item.get("owner")
        owner = (owner_obj.get("login") or "") if isinstance(owner_obj, dict) else ""
        name = item.get("name") or ""
        html_url = item.get("html_url") or ""
        updated_at = item.get("updated_at") or ""
        return GitHubRepo(
            full_name=item.get("full_name") or (f"{owner}/{name}" if owner and name else ""),
            name=name,
            owner=owner,
            description=item.get("description") or "",
            html_url=html_url,
            homepage=item.get("website") or html_url,
            stars=int(item.get("stars_count") or 0),
            updated_at=updated_at,
            default_branch=item.get("default_branch") or "main",
            pushed_at=updated_at,
            is_fork=bool(item.get("fork", False)),
            source_id=self.source_id,
        )

    def _release_from_api(self, payload: dict) -> GitHubRelease:
        if not isinstance(payload, dict):
            raise requests.RequestException(f"{self.name} API returned an unexpected release format.")
        assets = []
        for asset in payload.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            url = asset.get("browser_download_url") or ""
            if not url.lower().startswith("https://"):
                continue
            assets.append(
                GitHubReleaseAsset(
                    name=asset.get("name") or "",
                    download_url=url,
                    size=int(asset.get("size") or 0),
                    content_type="",
                    digest="",
                )
            )
        tag = payload.get("tag_name") or ""
        return GitHubRelease(
            release_id=payload.get("id", 0),
            tag_name=tag,
            name=payload.get("name") or tag,
            html_url=payload.get("html_url") or "",
            published_at=payload.get("published_at") or "",
            prerelease=bool(payload.get("prerelease", False)),
            assets=assets,
        )
