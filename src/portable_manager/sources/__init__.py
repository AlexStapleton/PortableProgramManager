"""Code-hosting sites (GitHub, GitLab, Gitea/Forgejo/Codeberg) behind one interface.

See :mod:`.base` for the :class:`SourceProvider` protocol every provider
implements and :class:`SourceRegistry`, which builds providers from the user's
``AppSettings.sources``.
"""

from .base import SourceProvider, SourceRegistry, build_provider

__all__ = ["SourceProvider", "SourceRegistry", "build_provider"]
