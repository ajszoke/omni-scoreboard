"""Tests for the provider boundary contract: updates, and observations of a scope."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import Contest
from omni.providers.base import (
    ScopeSpan,
    CurrentSlate,
    LocalDate,
    PollScope,
    Provider,
    ProviderError,
    ProviderUpdate,
    ScopeObservation,
    ScopeOutcome,
)
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry

NOW = datetime(2026, 6, 17, 21, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
SOURCE = SourceRef("mlb_statsapi")
TODAY = PollScope(SOURCE, League.MLB, LocalDate(date(2026, 6, 17)))
RECEIVED = ClockReading(NOW, MonotonicTime(5.0))


def _contest(raw: str, league: League = League.MLB) -> Contest:
    return Contest(
        id=LeagueScopedId(league, SOURCE, raw), league=league, status=GameStatus.SCHEDULED, scheduled_start=NOW
    )


def test_provider_update_defaults_are_empty_tuples() -> None:
    update = ProviderUpdate(source=SourceRef("mlb_statsapi"), observed_at=NOW)
    assert update.contests == ()
    assert update.warnings == ()
    assert update.observed_at == NOW


def test_provider_error_is_an_exception() -> None:
    assert issubclass(ProviderError, Exception)


def test_concrete_provider_satisfies_protocol() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_schedule=lambda d, s: [])
    assert isinstance(provider, Provider)
    assert provider.league is League.MLB
    assert provider.source.name == "mlb_statsapi"


def test_duck_typed_provider_satisfies_protocol() -> None:
    class FakeProvider:
        source = SourceRef("fake")
        league = League.NFL

        def refresh(self, now: datetime) -> ProviderUpdate:
            return ProviderUpdate(source=self.source, observed_at=now)

    assert isinstance(FakeProvider(), Provider)


def test_two_leagues_slates_for_one_date_are_two_scopes() -> None:
    other = PollScope(SOURCE, League.NBA, LocalDate(date(2026, 6, 17)))
    assert other != TODAY and len({TODAY, other, PollScope(SOURCE, League.MLB, LocalDate(date(2026, 6, 17)))}) == 2
    assert PollScope(SOURCE, League.MLB, CurrentSlate()) != TODAY


def test_an_answer_is_complete_unless_the_source_skipped_rows() -> None:
    games = (_contest("g1"),)
    complete = ScopeObservation.answer(TODAY, ProviderUpdate(source=SOURCE, observed_at=NOW, contests=games), RECEIVED)
    assert complete.outcome is ScopeOutcome.COMPLETE and complete.contests == games and complete.received == RECEIVED
    skipped = ProviderUpdate(source=SOURCE, observed_at=NOW, contests=games, warnings=("row 3: unknown team 999",))
    partial = ScopeObservation.answer(TODAY, skipped, RECEIVED)
    assert partial.outcome is ScopeOutcome.PARTIAL and partial.warnings == ("row 3: unknown team 999",)
    failed = ScopeObservation.failure(TODAY, "ProviderError: down", RECEIVED)
    assert failed.outcome is ScopeOutcome.FAILED and failed.error == "ProviderError: down" and failed.contests == ()


def test_an_observation_s_outcome_error_and_contests_must_agree() -> None:
    with pytest.raises(ValueError, match="says why"):
        ScopeObservation(scope=TODAY, outcome=ScopeOutcome.FAILED, received=RECEIVED)
    with pytest.raises(ValueError, match="says why"):
        ScopeObservation(scope=TODAY, outcome=ScopeOutcome.COMPLETE, received=RECEIVED, error="down")
    with pytest.raises(ValueError, match="lists no contests"):
        ScopeObservation(
            scope=TODAY, outcome=ScopeOutcome.FAILED, received=RECEIVED, error="down", contests=(_contest("g1"),)
        )
    with pytest.raises(ValueError, match="every contest of a mlb scope"):
        ScopeObservation(
            scope=TODAY, outcome=ScopeOutcome.COMPLETE, received=RECEIVED, contests=(_contest("n1", League.NBA),)
        )


def test_a_coverage_names_one_source_and_league_s_scopes_once_each() -> None:
    yesterday = PollScope(SOURCE, League.MLB, LocalDate(date(2026, 6, 16)))
    span = ScopeSpan(current=TODAY, earlier=(yesterday,), max_age=timedelta(minutes=2))
    assert span.scopes == (TODAY, yesterday)
    with pytest.raises(ValueError, match="one source and one league"):
        ScopeSpan(current=TODAY, earlier=(PollScope(SOURCE, League.NBA, LocalDate(date(2026, 6, 16))),), max_age=60 * S)
    with pytest.raises(ValueError, match="one source and one league"):
        ScopeSpan(current=TODAY, earlier=(PollScope(SourceRef("x"), League.MLB, CurrentSlate()),), max_age=60 * S)
    with pytest.raises(ValueError, match="each scope once"):
        ScopeSpan(current=TODAY, earlier=(TODAY,), max_age=60 * S)
    with pytest.raises(ValueError, match="each scope once"):
        ScopeSpan(current=TODAY, earlier=(yesterday, yesterday), max_age=60 * S)
    with pytest.raises(ValueError, match="must be positive"):
        ScopeSpan(current=TODAY, max_age=timedelta(0))
