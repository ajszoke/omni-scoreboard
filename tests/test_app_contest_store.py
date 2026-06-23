"""Tests for ContestStore: snapshot reconciliation and version ordering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omni.app.contest_store import ContestStore
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest
from omni.providers.base import ProviderUpdate

SOURCE = SourceRef("mlb_statsapi")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)


def _id(raw: str) -> LeagueScopedId:
    return LeagueScopedId(League.MLB, SOURCE, raw)


def _contest(raw: str, status: GameStatus = GameStatus.SCHEDULED) -> Contest:
    return Contest(id=_id(raw), league=League.MLB, status=status, scheduled_start=T)


def _update(*contests: Contest, at: datetime = T) -> ProviderUpdate:
    return ProviderUpdate(source=SOURCE, observed_at=at, contests=contests)


def test_empty_store() -> None:
    store = ContestStore()
    assert len(store) == 0
    assert store.contests == ()
    assert store.get(_id("g1")) is None
    assert _id("g1") not in store


def test_first_snapshot_adds_all() -> None:
    store = ContestStore()
    rec = store.apply(_update(_contest("g1"), _contest("g2")))
    assert rec.applied and rec.changed
    assert rec.added == (_id("g1"), _id("g2"))
    assert rec.updated == () and rec.removed == ()
    assert len(store) == 2
    assert store.get(_id("g1")) is not None and _id("g2") in store


def test_idempotent_reapply_reports_no_changes() -> None:
    store = ContestStore()
    snap = _update(_contest("g1"))
    store.apply(snap)
    rec = store.apply(snap)  # same observed_at, same data
    assert rec.applied and not rec.changed
    assert rec.added == () and rec.updated == () and rec.removed == ()


def test_reconcile_adds_updates_and_removes() -> None:
    store = ContestStore()
    store.apply(_update(_contest("g1", GameStatus.SCHEDULED), _contest("g2"), at=T))
    rec = store.apply(
        _update(
            _contest("g1", GameStatus.LIVE),  # g1 status changed -> updated
            _contest("g3"),  # new -> added; g2 absent -> removed
            at=T + timedelta(seconds=30),
        )
    )
    assert rec.added == (_id("g3"),)
    assert rec.updated == (_id("g1"),)
    assert rec.removed == (_id("g2"),)
    assert {c.id for c in store.contests} == {_id("g1"), _id("g3")}
    assert store.get(_id("g1")).status is GameStatus.LIVE  # type: ignore[union-attr]


def test_stale_snapshot_is_ignored() -> None:
    store = ContestStore()
    store.apply(_update(_contest("g1"), at=T))
    rec = store.apply(_update(_contest("g1"), _contest("g2"), at=T - timedelta(seconds=10)))  # older
    assert not rec.applied and not rec.changed
    assert len(store) == 1  # the late-arriving older poll did not rewind state


def test_equal_observed_at_is_still_applied() -> None:
    store = ContestStore()
    store.apply(_update(_contest("g1"), at=T))
    rec = store.apply(_update(_contest("g1"), _contest("g2"), at=T))  # same instant, more data
    assert rec.applied and rec.added == (_id("g2"),)


def test_empty_snapshot_clears_the_store() -> None:
    store = ContestStore()
    store.apply(_update(_contest("g1"), _contest("g2"), at=T))
    rec = store.apply(_update(at=T + timedelta(seconds=30)))  # authoritative off-day
    assert rec.applied
    assert rec.removed == (_id("g1"), _id("g2"))
    assert len(store) == 0


def test_change_lists_are_sorted_for_deterministic_traces() -> None:
    store = ContestStore()
    # Insert in non-sorted order; the reconciliation must come back sorted by str(id).
    rec = store.apply(_update(_contest("g3"), _contest("g1"), _contest("g2")))
    assert rec.added == (_id("g1"), _id("g2"), _id("g3"))
