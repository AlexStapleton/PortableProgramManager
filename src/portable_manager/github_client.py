from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
REPO_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/#?]+)")
# Validates that a full_name looks like "owner/repo" with safe characters only.
_REPO_FULL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")


@dataclass
class GitHubReleaseAsset:
    name: str
    download_url: str
    size: int
    content_type: str


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


class GitHubClient:
    def __init__(self, token: str = "") -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "PortableProgramManager/0.7",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

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

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Issue an HTTP request with automatic retry on rate-limit (403/429).

        Respects ``Retry-After`` and ``X-RateLimit-Reset`` headers.  Retries up
        to ``MAX_RETRIES`` times with exponential back-off capped at 60 s.
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        last_response: requests.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            response = self.session.request(method, url, **kwargs)
            if response.status_code not in (403, 429):
                return response

            # Check if this is actually a rate-limit response (vs. a true 403).
            remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code == 403 and remaining and int(remaining) > 0:
                return response  # genuine permission error, not rate-limit

            last_response = response
            if attempt >= MAX_RETRIES:
                break

            # Determine how long to wait.
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                reset_at = response.headers.get("X-RateLimit-Reset")
                if reset_at and reset_at.isdigit():
                    wait = max(0, int(reset_at) - int(time.time())) + 1
                else:
                    wait = 2 ** attempt  # exponential back-off fallback

            wait = min(wait, 60)  # cap at 60 s so we don't block forever
            time.sleep(wait)

        # All retries exhausted — raise with a clear message.
        if last_response is None:
            raise requests.ConnectionError("GitHub API request failed: no response received.")
        remaining = last_response.headers.get("X-RateLimit-Remaining", "?")
        reset_at = last_response.headers.get("X-RateLimit-Reset", "")
        reset_msg = ""
        if reset_at and reset_at.isdigit():
            minutes = max(1, (int(reset_at) - int(time.time())) // 60)
            reset_msg = f" Resets in ~{minutes} min."
        raise requests.HTTPError(
            f"GitHub API rate limit exceeded (remaining: {remaining}).{reset_msg} "
            "Set a GitHub token in Settings to increase your rate limit.",
            response=last_response,
        )

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
        response.raise_for_status()
        data = self._parse_json(response)
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._repo_from_api(item) for item in items]

    def get_repo(self, full_name: str) -> GitHubRepo:
        self._validate_full_name(full_name)
        response = self._request("GET", f"{API_ROOT}/repos/{full_name}")
        response.raise_for_status()
        return self._repo_from_api(self._parse_json(response))

    def get_latest_release(self, full_name: str, include_prereleases: bool = False) -> Optional[GitHubRelease]:
        self._validate_full_name(full_name)
        if include_prereleases:
            response = self._request("GET", f"{API_ROOT}/repos/{full_name}/releases")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            releases = self._parse_json(response)
            if not releases or not isinstance(releases, list):
                return None
            return self._release_from_api(releases[0])

        response = self._request("GET", f"{API_ROOT}/repos/{full_name}/releases/latest")
        if response.status_code == 404:
            return None
        response.raise_for_status()
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
        match = REPO_URL_RE.match(url.strip())
        if not match:
            return None
        owner, repo = match.groups()
        repo = repo.removesuffix(".git")
        return f"{owner}/{repo}"

    def infer_repo_from_asset_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if parsed.netloc not in {"github.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}:
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
                )
                for asset in payload.get("assets", [])
            ],
        )
