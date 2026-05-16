"""Source-update interfaces — capability protocols consumed by the
ConfigurationService's dynamic-choices resolver."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class RemoteBranchLister(Protocol):
    """Capability for reporting the last-known branches on ``origin``.

    Implemented by ``SourceUpdateService``. The cache is populated at
    service start and refreshed on demand via the ``refresh_branches``
    config action — there's no event-bus signal we can subscribe to
    for "someone pushed to origin," so a manual refresh is the only
    way to pick up branches that appeared after Gilbert started.

    Lives on a capability protocol so ``ConfigurationService.
    _resolve_dynamic_choices`` can look up the cache without
    duck-typing the concrete service class.
    """

    @property
    def cached_remote_branches(self) -> list[str]:
        """Return the last-known list of branches on ``origin``.

        Sorted alphabetically. Empty list when the cache hasn't been
        populated yet (e.g. when ``git fetch`` failed at service start).
        """
        ...
