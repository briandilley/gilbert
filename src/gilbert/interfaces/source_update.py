"""Source-update interfaces — capability protocols consumed by the
ConfigurationService's dynamic-choices resolver."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class GitRemoteLister(Protocol):
    """Capability for reporting locally-known git remote names.

    Implemented by ``SourceUpdateService``. The list comes from
    ``git remote`` and is refreshed on demand via the
    ``refresh_branches`` action — there's no event we can subscribe to
    for "user added a new remote," so any remote that wasn't present
    at the last refresh won't show up in the dropdown.
    """

    @property
    def cached_remotes(self) -> list[str]:
        """Return the last-known list of local git remote names.

        Sorted alphabetically. Empty list when the cache hasn't been
        populated yet (e.g. when ``git remote`` failed at service start).
        """
        ...


@runtime_checkable
class RemoteBranchLister(Protocol):
    """Capability for reporting branches on the configured target remote.

    Implemented by ``SourceUpdateService``. The cache is populated per
    remote at service start and refreshed on demand via the
    ``refresh_branches`` action. This property returns branches for
    whichever remote the user has selected as ``target_remote``, so
    the ``target_branch`` dropdown reflects the active remote without
    a save-round-trip.
    """

    @property
    def cached_target_remote_branches(self) -> list[str]:
        """Return the last-known list of branches on the user's
        configured ``target_remote``.

        Sorted alphabetically. Empty list when no branches have been
        cached for that remote yet (fresh install or a failed
        ``git ls-remote``).
        """
        ...
