from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse, urlsplit

import requests

from . import __version__

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
# Longest single sleep allowed before retrying a rate-limited request.
MAX_RATE_LIMIT_WAIT = 10
# Wait assumed when GitHub reports a rate limit without any reset information.
_UNKNOWN_RESET_WAIT = 60
USER_AGENT = f"PortableProgramManager/{__version__}"
# Validates that a full_name looks like "owner/repo" with safe characters only.
_REPO_FULL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}
# First path segments on github.com that are site pages, never repository owners.
_RESERVED_OWNERS = {
    "orgs", "settings", "marketplace", "topics", "search",
    "features", "sponsors", "apps", "login", "about",
}
# Second path segments (after owner/repo) that point at a file, not at the repo page.
_FILE_PATH_SEGMENTS = {"blob", "raw", "archive"}
_SHA256_PREFIX = "sha256:"


class GitHubError(requests.RequestException):
    """A GitHub API failure with a message that is safe to show to the user."""


class RateLimitError(GitHubError):
    """The GitHub API rate limit is exhausted and the reset is too far away to wait for."""

    def __init__(self, message: str, reset_at: int | None = None, response: requests.Response | None = None) -> None:
        super().__init__(message, response=response)
        self.reset_at = reset_at


@dataclass
class GitHubReleaseAsset:
    name: str
    download_url: str
    size: int
    content_type: str
    digest: str = ""  # lowercase hex SHA-256 when GitHub provides one, else ""


@dataclass
class GitHubRelease:
    release_id: int
    tag_name: str
    name: str
    html_url: str
    published_at: str
    prerelease: bool
    assets: list[GitHubReleaseAsset]


@dataclass
class GitHubRepo:
    full_name: str
    name: str
    owner: str
    description: str
    html_url: str
    homepage: str
    stars: int
    updated_at: str
    default_branch: str
    pushed_at: str = ""
    latest_release_at: str = ""
    has_windows_release: bool | None = None  # None = not yet checked


def _github_path_segments(url: str) -> list[str] | None:
    """Return the non-empty path segments of a github.com URL, or None for any other host."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _GITHUB_HOSTS:
        return None
    return [segment for segment in parts.path.split("/") if segment]


def _owner_repo(owner: str, repo: str) -> str | None:
    """Return ``owner/repo`` if both parts are safe GitHub names, else None."""
    if owner in (".", "..") or repo in (".", ".."):
        return None
    full_name = f"{owner}/{repo}"
    return full_name if _REPO_FULL_NAME_RE.match(full_name) else None


def parse_release_asset_url(url: str) -> tuple[str, str] | None:
    """Return ``(owner/repo, tag)`` for a ``github.com/{owner}/{repo}/releases/download/{tag}/{file}`` URL.

    The tag is percent-decoded. Returns None for any other URL.
    """
    segments = _github_path_segments(url)
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


def _sha256_digest(value: object) -> str:
    """Return the lowercase hex of a ``sha256:<hex>`` digest, or ``""`` for anything else."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if not text.startswith(_SHA256_PREFIX):
        return ""
    return text[len(_SHA256_PREFIX):]


class GitHubClient:
    def __init__(self, token: str = "") -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        # Core-resource quota as last reported by GitHub (None = not seen yet).
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset_at: int | None = None

    def close(self) -> None:
        """Close the underlying HTTP session and release connection pool resources."""
        self.session.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def can_afford(self, calls: int) -> bool:
        """True when the core quota is unknown or has more than *calls* requests left."""
        return self.rate_limit_remaining is None or self.rate_limit_remaining > calls

    def _record_rate_limit(self, response: requests.Response) -> None:
        """Remember core rate-limit headers. Search has its own quota and is ignored."""
        resource = response.headers.get("X-RateLimit-Resource", "")
        if resource and resource != "core":
            return
        remaining = response.headers.get("X-RateLimit-Remaining", "")
        if remaining.isdigit():
            self.rate_limit_remaining = int(remaining)
        reset_at = response.headers.get("X-RateLimit-Reset", "")
        if reset_at.isdigit():
            self.rate_limit_reset_at = int(reset_at)

    @staticmethod
    def _rate_limit_error(response: requests.Response, reset_at: int | None, now: int) -> RateLimitError:
        seconds = max(0, reset_at - now) if reset_at is not None else _UNKNOWN_RESET_WAIT
        minutes = max(1, math.ceil(seconds / 60))
        return RateLimitError(
            f"GitHub API rate limit reached. Resets in ~{minutes} min. "
            "Add a GitHub token in Settings to raise the limit.",
            reset_at=reset_at,
            response=response,
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue an HTTP request, retrying briefly when GitHub rate-limits it.

        A 403 or 429 counts as a rate limit only when ``X-RateLimit-Remaining`` is
        ``0`` or a ``Retry-After`` header is present. Any other response is returned
        as-is without retrying. A rate-limited request is retried (up to
        ``MAX_RETRIES`` times) only when the wait is at most ``MAX_RATE_LIMIT_WAIT``
        seconds; otherwise ``RateLimitError`` is raised without sleeping.
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        for attempt in range(MAX_RETRIES + 1):
            response = self.session.request(method, url, **kwargs)
            self._record_rate_limit(response)
            if response.status_code not in (403, 429):
                return response

            retry_after = response.headers.get("Retry-After", "")
            is_rate_limit = retry_after.isdigit() or response.headers.get("X-RateLimit-Remaining") == "0"
            if not is_rate_limit:
                return response

            now = int(time.time())
            if retry_after.isdigit():
                wait = int(retry_after)
                reset_at = now + wait
            else:
                reset_header = response.headers.get("X-RateLimit-Reset", "")
                reset_at = int(reset_header) if reset_header.isdigit() else None
                wait = max(0, reset_at - now) + 1 if reset_at is not None else _UNKNOWN_RESET_WAIT

            if wait > MAX_RATE_LIMIT_WAIT or attempt >= MAX_RETRIES:
                raise self._rate_limit_error(response, reset_at, now)
            time.sleep(wait)
        raise GitHubError("GitHub API request failed.")  # not reached: the last attempt always returns or raises

    @staticmethod
    def _raise_for_status(response: requests.Response, what: str) -> None:
        """Raise a short GitHubError for any HTTP error status (no URL or headers included)."""
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned HTTP {response.status_code} for {what}.")

    @staticmethod
    def _parse_json(response: requests.Response) -> dict | list:
        """Parse JSON from a response, raising a clear error on failure."""
        try:
            return response.json()
        except (ValueError, requests.JSONDecodeError):
            raise requests.RequestException(
                f"GitHub API returned invalid JSON (HTTP {response.status_code})."
            )

    @staticmethod
    def _validate_full_name(full_name: str) -> None:
        """Raise if *full_name* does not match the ``owner/repo`` pattern.

        Prevents API path injection via crafted strings like ``../../other``.
        """
        if not _REPO_FULL_NAME_RE.match(full_name):
            raise requests.RequestException(
                f"Invalid repository name format: {full_name!r}. Expected 'owner/repo'."
            )

    def search_repositories(self, query: str, limit: int = 20, sort: str = "stars") -> list[GitHubRepo]:
        response = self._request(
            "GET",
            f"{API_ROOT}/search/repositories",
            params={"q": query, "sort": sort, "order": "desc", "per_page": max(1, min(limit, 50))},
        )
        self._raise_for_status(response, "repository search")
        data = self._parse_json(response)
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._repo_from_api(item) for item in items]

    def get_repo(self, full_name: str) -> GitHubRepo:
        self._validate_full_name(full_name)
        response = self._request("GET", f"{API_ROOT}/repos/{full_name}")
        if response.status_code == 404:
            raise GitHubError(
                f"Repository {full_name} was not found on GitHub. It may be private, renamed or deleted."
            )
        self._raise_for_status(response, f"repository {full_name}")
        return self._repo_from_api(self._parse_json(response))

    def get_latest_release(self, full_name: str, include_prereleases: bool = False) -> Optional[GitHubRelease]:
        self._validate_full_name(full_name)
        if include_prereleases:
            response = self._request("GET", f"{API_ROOT}/repos/{full_name}/releases")
            if response.status_code == 404:
                return None
            self._raise_for_status(response, f"releases of {full_name}")
            releases = self._parse_json(response)
            if not releases or not isinstance(releases, list):
                return None
            return self._release_from_api(releases[0])

        response = self._request("GET", f"{API_ROOT}/repos/{full_name}/releases/latest")
        if response.status_code == 404:
            return None
        self._raise_for_status(response, f"latest release of {full_name}")
        return self._release_from_api(self._parse_json(response))

    # Asset name patterns that indicate a Windows build.
    _WIN_ASSET_RE = re.compile(
        r"(win|windows|win32|win64|x86_64.*pc.*windows|msvc|mingw"
        r"|\.msi\b|\.msix\b|\.appx\b|\.appxbundle\b|\.exe\b|setup\.zip)",
        re.IGNORECASE,
    )

    def get_latest_release_info(self, full_name: str) -> tuple[str, bool]:
        """Return ``(published_at, has_windows_asset)`` for the latest release.

        Returns ``("", False)`` when no release exists or on error.
        """
        self._validate_full_name(full_name)
        response = self._request("GET", f"{API_ROOT}/repos/{full_name}/releases/latest")
        if response.status_code == 404:
            return "", False
        try:
            response.raise_for_status()
            data = self._parse_json(response)
            if not isinstance(data, dict):
                return "", False
            published_at = data.get("published_at", "")
            assets = data.get("assets") or []
            has_win = any(
                self._WIN_ASSET_RE.search(a.get("name", ""))
                for a in assets
                if isinstance(a, dict)
            )
            return published_at, has_win
        except Exception:
            return "", False

    def get_latest_release_date(self, full_name: str) -> str:
        """Return the ``published_at`` ISO timestamp of the latest release, or empty string."""
        date, _ = self.get_latest_release_info(full_name)
        return date

    def extract_repo_full_name(self, url: str) -> Optional[str]:
        """Return ``owner/repo`` when *url* is a GitHub repository page, else None.

        Release download links and other file links return None, so they can be
        installed as direct URLs instead of being treated as the whole repository.
        """
        segments = _github_path_segments(url)
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

    def infer_repo_from_asset_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if parsed.netloc.lower() in _GITHUB_HOSTS:
            asset = parse_release_asset_url(url)
            return asset[0] if asset else None

        if parsed.netloc not in {"objects.githubusercontent.com", "github-releases.githubusercontent.com"}:
            return None

        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 4 and parts[2] == "releases":
            return f"{parts[0]}/{parts[1]}"
        return None

    @staticmethod
    def _repo_from_api(item: dict) -> GitHubRepo:
        owner_obj = item.get("owner")
        owner = owner_obj.get("login", "") if isinstance(owner_obj, dict) else ""
        return GitHubRepo(
            full_name=item.get("full_name", ""),
            name=item.get("name", ""),
            owner=owner,
            description=item.get("description") or "",
            html_url=item.get("html_url", ""),
            homepage=item.get("homepage") or "",
            stars=item.get("stargazers_count", 0),
            updated_at=item.get("updated_at", ""),
            default_branch=item.get("default_branch", "main"),
            pushed_at=item.get("pushed_at", ""),
        )

    @staticmethod
    def _release_from_api(payload: dict) -> GitHubRelease:
        if not isinstance(payload, dict):
            raise requests.RequestException("GitHub API returned an unexpected release format.")
        return GitHubRelease(
            release_id=payload.get("id", 0),
            tag_name=payload.get("tag_name", ""),
            name=payload.get("name") or payload.get("tag_name", ""),
            html_url=payload.get("html_url", ""),
            published_at=payload.get("published_at", ""),
            prerelease=bool(payload.get("prerelease", False)),
            assets=[
                GitHubReleaseAsset(
                    name=asset.get("name", ""),
                    download_url=asset.get("browser_download_url", ""),
                    size=asset.get("size", 0),
                    content_type=asset.get("content_type", "application/octet-stream"),
                    digest=_sha256_digest(asset.get("digest")),
                )
                for asset in payload.get("assets") or []
                if isinstance(asset, dict)
            ],
        )
