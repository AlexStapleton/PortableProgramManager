"""The provider interface and the registry that builds providers from settings.

Every provider returns the shared result types from :mod:`..github_client`
(``GitHubRepo``, ``GitHubRelease``, ``GitHubReleaseAsset``; the names are
historical, they're used for every site). A repository is identified by its
*path* on that site: ``owner/repo`` on GitHub and Gitea, and possibly
``group/subgroup/project`` on GitLab. ``GitHubRepo.full_name`` holds that path
and ``GitHubRepo.source_id`` the id of the :class:`SourceConfig` it came from.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol
from urllib.parse import urlsplit

from ..github_client import GitHubClient, GitHubRelease, GitHubRepo
from ..http_cache import ConditionalCache
from ..models import SourceConfig

log = logging.getLogger(__name__)


class SourceProvider(Protocol):
    """What the controller needs from a code-hosting site.

    Providers are constructed as ``Provider(token=..., cache=..., base_url=...,
    source_id=..., name=...)``. Network/API failures raise
    ``github_client.GitHubError`` (or its ``RateLimitError`` subclass) with a
    short, user-readable message that never contains the token.
    """

    kind: str               # "github" | "gitlab" | "gitea"
    source_id: str          # SourceConfig.id
    name: str               # display name, e.g. "Codeberg"
    base_url: str           # web address without trailing slash, e.g. "https://gitlab.com"
    web_hosts: frozenset[str]  # lower-case host names whose links belong to this source
    rate_limit_remaining: int | None

    def search_repositories(
        self, query: str, limit: int = 20, sort: str = "stars", include_forks: bool = False
    ) -> list[GitHubRepo]:
        """Search projects. *sort* is "stars" or "updated" (most recent activity first).
        Results must have ``source_id``, ``is_fork`` and ``html_url`` set."""

    def get_repo(self, full_name: str) -> GitHubRepo: ...

    def get_latest_release(self, full_name: str, include_prereleases: bool = False) -> Optional[GitHubRelease]:
        """Newest release, or None when the project has none. Assets must be downloadable
        files (not auto-generated source archives); ``download_url`` must be https."""

    def get_latest_release_info(self, full_name: str) -> tuple[str, bool]:
        """``(published_at ISO string, has a Windows-looking asset)``; ``("", False)`` on any problem."""

    def compare_fork_with_parent(self, full_name: str) -> tuple[str, int, int]:
        """``(parent path, ahead_by, behind_by)``. Raise GitHubError if not a fork or unsupported."""

    def extract_repo_full_name(self, url: str) -> Optional[str]:
        """Project path when *url* is a project page on this site, else None
        (release-download and file links must return None)."""

    def parse_release_asset_url(self, url: str) -> tuple[str, str] | None:
        """``(project path, tag)`` when *url* downloads a release file from this site, else None."""

    def infer_repo_from_asset_url(self, url: str) -> Optional[str]: ...

    def refresh_rate_limit(self) -> int | None:
        """Update ``rate_limit_remaining`` without spending quota where the site allows; may return None."""

    def can_afford(self, calls: int) -> bool: ...

    def close(self) -> None: ...


def build_provider(config: SourceConfig, cache: ConditionalCache | None = None) -> SourceProvider | None:
    """Create the provider for *config*, or None if its kind isn't available."""
    kwargs = dict(token=config.token, cache=cache, base_url=config.base_url, source_id=config.id, name=config.name)
    if config.kind == "github":
        return GitHubClient(**kwargs)
    try:
        if config.kind == "gitlab":
            from .gitlab import GitLabProvider

            return GitLabProvider(**kwargs)
        if config.kind == "gitea":
            from .gitea import GiteaProvider

            return GiteaProvider(**kwargs)
    except ImportError:
        log.warning("Source kind %r is not available in this build", config.kind)
    return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class SourceRegistry:
    """Providers for the user's enabled sources, rebuilt whenever settings change."""

    def __init__(self, configs: list[SourceConfig], cache: ConditionalCache | None = None) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, SourceProvider] = {}
        self._configs: list[SourceConfig] = []
        self.cache = cache
        self.reload(configs)

    def reload(self, configs: list[SourceConfig]) -> None:
        new: dict[str, SourceProvider] = {}
        for config in configs:
            if not config.enabled:
                continue
            provider = build_provider(config, self.cache)
            if provider is not None:
                new[config.id] = provider
        with self._lock:
            old, self._providers, self._configs = self._providers, new, list(configs)
        for provider in old.values():
            try:
                provider.close()
            except Exception:
                pass

    def get(self, source_id: str) -> SourceProvider | None:
        with self._lock:
            return self._providers.get(source_id)

    def set(self, source_id: str, provider: SourceProvider) -> None:
        """Replace one provider (used by tests and by the controller's ``github`` property)."""
        with self._lock:
            self._providers[source_id] = provider

    def all(self) -> list[SourceProvider]:
        with self._lock:
            return list(self._providers.values())

    def searchable(self) -> list[SourceProvider]:
        with self._lock:
            ids = [c.id for c in self._configs if c.enabled and c.searchable]
            return [self._providers[i] for i in ids if i in self._providers]

    def match_url(self, url: str) -> tuple[SourceProvider, str | None] | None:
        """The provider whose site *url* points to, and the project path if it's a project page."""
        host = _host(url if "://" in url else f"https://{url}")
        if not host:
            return None
        for provider in self.all():
            if host in provider.web_hosts:
                return provider, provider.extract_repo_full_name(url)
        return None

    def close(self) -> None:
        self.reload([])
