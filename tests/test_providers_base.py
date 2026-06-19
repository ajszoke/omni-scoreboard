"""Tests for the provider boundary contract."""

from __future__ import annotations

from datetime import datetime, timezone

from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.providers.base import Provider, ProviderError, ProviderUpdate
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from omni.providers.mlb_teams import MlbTeamRegistry

NOW = datetime(2026, 6, 17, 21, 0, tzinfo=timezone.utc)


def test_provider_update_defaults_are_empty_tuples() -> None:
    update = ProviderUpdate(source=SourceRef("mlb_statsapi"), observed_at=NOW)
    assert update.contests == ()
    assert update.events == ()
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
