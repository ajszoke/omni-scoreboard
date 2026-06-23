"""ContestStore: the reconciled, current set of contests across provider polls.

Each `ProviderUpdate` is a *complete* snapshot of what the source sees right now, so
the store reconciles successive snapshots into one live view: contests that appear
are added, ones whose fields changed (e.g. PREGAME → LIVE) are updated, and ones
that dropped out of the schedule are removed. The `Reconciliation` it returns names
exactly what changed so the pipeline can prune cards for contests that vanished.

Version ordering is by ``observed_at``: a snapshot older than the last one applied
(a delayed poll landing after a newer one) is ignored rather than rewinding state.
An applied snapshot is authoritative — including an empty one (a genuine off-day) —
because the `ProviderSupervisor` upstream is what distinguishes "no games" from
"fetch failed". Pure: no clock; ordering comes from the update itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from omni.core.ids import LeagueScopedId
from omni.domain.contest import Contest
from omni.providers.base import ProviderUpdate

__all__ = ["Reconciliation", "ContestStore"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Reconciliation:
    """What changed when a snapshot was applied (ids sorted for deterministic traces)."""

    added: tuple[LeagueScopedId, ...]
    updated: tuple[LeagueScopedId, ...]
    removed: tuple[LeagueScopedId, ...]
    applied: bool  # False if the snapshot was stale (older than the last applied) and ignored

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


class ContestStore:
    """Holds the current contests, reconciled across provider snapshots."""

    def __init__(self) -> None:
        self._contests: dict[LeagueScopedId, Contest] = {}
        self._last_observed_at: datetime | None = None

    def apply(self, update: ProviderUpdate) -> Reconciliation:
        """Reconcile ``update`` into the current set; return what changed."""
        if self._last_observed_at is not None and update.observed_at < self._last_observed_at:
            return Reconciliation(added=(), updated=(), removed=(), applied=False)

        incoming = {contest.id: contest for contest in update.contests}
        added = [cid for cid in incoming if cid not in self._contests]
        removed = [cid for cid in self._contests if cid not in incoming]
        updated = [cid for cid, contest in incoming.items() if cid in self._contests and contest != self._contests[cid]]

        self._contests = incoming
        self._last_observed_at = update.observed_at
        return Reconciliation(
            added=tuple(sorted(added, key=str)),
            updated=tuple(sorted(updated, key=str)),
            removed=tuple(sorted(removed, key=str)),
            applied=True,
        )

    @property
    def contests(self) -> tuple[Contest, ...]:
        return tuple(self._contests.values())

    def get(self, contest_id: LeagueScopedId) -> Contest | None:
        return self._contests.get(contest_id)

    def __contains__(self, contest_id: object) -> bool:
        return contest_id in self._contests

    def __len__(self) -> int:
        return len(self._contests)
