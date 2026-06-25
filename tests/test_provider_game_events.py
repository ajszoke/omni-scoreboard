"""Tests for parsing the MLB game feed's play-by-play into typed BaseballGameEvents."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from omni.core.enum import DisplayPriority, GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import BaseballScoringImpact, InningPhase, PitchType, scoring_impact
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEventType
from omni.providers.mlb_statsapi import (
    MlbStatsApiProvider,
    _decisive_pitch_type,
    _event_importance,
    _parse_game_events,
    _parse_iso,
    _parse_one_play,
    _safe_count,
    _safe_int,
)
from omni.providers.mlb_teams import MlbTeamRegistry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "providers" / "mlb_game_live.json"
NOW = datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc)
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
_REG = MlbTeamRegistry.from_color_file()


def _feed() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _game() -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, "700001"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=NOW,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


def _events() -> tuple[Any, ...]:
    events, _warnings = _parse_game_events(_feed(), contest=_game(), source=SOURCE, observed_at=NOW)
    return events


def _play(**about: Any) -> dict[str, Any]:
    """A minimal mapped (home_run) play with overridable `about` fields."""
    base = {"atBatIndex": 7, "halfInning": "bottom", "inning": 9, "endTime": "2026-06-17T23:00:00.000Z"}
    base.update(about)
    return {
        "atBatIndex": base["atBatIndex"],
        "result": {"eventType": "home_run", "description": "walk-off bomb", "rbi": 1},
        "about": base,
        "count": {"balls": 1, "strikes": 1, "outs": 1},
    }


def test_parses_mapped_plays_and_skips_routine_outs() -> None:
    events = _events()
    # 5 plays in the fixture; the strikeout is unmapped and dropped, leaving 4.
    assert [e.event_type for e in events] == [
        BaseballGameEventType.SINGLE,
        BaseballGameEventType.HOME_RUN,
        BaseballGameEventType.WALK,
        BaseballGameEventType.TRIPLE,
    ]


def test_event_lineage_ids_are_stable_scoped_and_unique() -> None:
    raws = [e.id.raw for e in _events()]
    assert raws == ["700001:ab:0", "700001:ab:11", "700001:ab:22", "700001:ab:30"]
    assert len(set(raws)) == len(raws)
    # Re-parsing the same feed yields the same ids — so a poll loop can dedupe.
    assert raws == [e.id.raw for e in _events()]


def test_home_run_event_payload_and_times() -> None:
    home_run = next(e for e in _events() if e.event_type is BaseballGameEventType.HOME_RUN)
    assert home_run.contest.id.raw == "700001"
    assert home_run.observed_at == NOW
    assert home_run.source_time == datetime(2026, 6, 17, 22, 45, 10, tzinfo=timezone.utc)
    p = home_run.payload
    assert p.inning == 3 and p.phase is InningPhase.BOTTOM and p.rbi == 2
    assert "Betts" in p.description
    assert p.count is not None and (p.count.balls, p.count.strikes, p.count.outs) == (1, 1, 1)


def test_decisive_pitch_type_is_the_last_pitch_of_the_at_bat() -> None:
    # The home run was hit on a sweeper — the last pitch, past the mound visit and the fastball.
    home_run = next(e for e in _events() if e.event_type is BaseballGameEventType.HOME_RUN)
    assert home_run.payload.pitch_type is PitchType.SWEEPER


def test_decisive_pitch_type_is_none_without_pitch_detail() -> None:
    # The fixture's other plays carry no playEvents, so their pitch type is simply absent.
    single = next(e for e in _events() if e.event_type is BaseballGameEventType.SINGLE)
    assert single.payload.pitch_type is None


def test_decisive_pitch_type_skips_non_pitch_events_and_handles_gaps() -> None:
    pitched = {"playEvents": [{"isPitch": True, "details": {"type": {"code": "SL"}}}, {"isPitch": False}]}
    assert _decisive_pitch_type(pitched) is PitchType.SLIDER  # the mound-visit-style entry is skipped
    assert _decisive_pitch_type({}) is None  # no playEvents at all
    assert _decisive_pitch_type({"playEvents": [{"isPitch": False}, {"foo": "bar"}]}) is None  # no pitch among them
    assert _decisive_pitch_type({"playEvents": [{"isPitch": True, "details": {}}]}) is None  # pitch, no type
    assert _decisive_pitch_type({"playEvents": [{"isPitch": True, "details": {"type": {"code": "ZZ"}}}]}) is None


def test_play_carries_its_post_play_score() -> None:
    # The score the play *left the game at* — what a delayed big-play card shows.
    events = {e.event_type: e for e in _events()}
    hr = events[BaseballGameEventType.HOME_RUN].payload
    assert (hr.away_score, hr.home_score) == (0, 2)
    triple = events[BaseballGameEventType.TRIPLE].payload
    assert (triple.away_score, triple.home_score) == (1, 5)


def test_safe_int_coerces_or_returns_none() -> None:
    assert _safe_int(3) == 3
    assert _safe_int("4") == 4
    assert _safe_int(None) is None
    assert _safe_int("nope") is None


def test_importance_ranks_home_run_above_single() -> None:
    events = {e.event_type: e for e in _events()}
    hr = events[BaseballGameEventType.HOME_RUN].importance
    single = events[BaseballGameEventType.SINGLE].importance
    assert hr.priority is DisplayPriority.ALERT
    assert hr.combined_score() > single.combined_score()
    assert hr.reasons == ("home_run", "go_ahead")  # type + scoring context, not a bare float


def test_fetch_live_feed_returns_state_and_events_together() -> None:
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda pk: _feed())
    feed = provider.fetch_live_feed(_game(), now=NOW)
    assert feed.state.home_score == 5  # one fetch...
    assert len(feed.events) == 4  # ...yields both state and events


def test_no_plays_yields_no_events() -> None:
    feed: dict[str, Any] = {"liveData": {"linescore": {}}}
    assert _parse_game_events(feed, contest=_game(), source=SOURCE, observed_at=NOW) == ((), ())


def test_non_list_allplays_yields_no_events() -> None:
    feed = {"liveData": {"plays": {"allPlays": "nope"}}}
    assert _parse_game_events(feed, contest=_game(), source=SOURCE, observed_at=NOW) == ((), ())


@pytest.mark.parametrize(
    "bad_play, exc_name",
    [
        ({"atBatIndex": 6, "result": None, "about": {}, "count": {}}, "AttributeError"),  # null result
        ({"atBatIndex": 6, "result": {"eventType": "home_run"}, "about": None, "count": {}}, "AttributeError"),  # about
        ({"atBatIndex": 6, "result": {"eventType": "home_run", "rbi": "lots"}, "about": {}, "count": {}}, "ValueError"),
    ],
)
def test_malformed_play_is_isolated_with_a_warning(bad_play: dict[str, Any], exc_name: str) -> None:
    # A malformed play must not sink the parse or escape the boundary: drop it, warn, keep the rest.
    feed = {"liveData": {"plays": {"allPlays": [_play(atBatIndex=5), bad_play]}}}
    events, warnings = _parse_game_events(feed, contest=_game(), source=SOURCE, observed_at=NOW)
    assert len(events) == 1 and events[0].event_type is BaseballGameEventType.HOME_RUN  # the good play survived
    assert len(warnings) == 1 and "play 1" in warnings[0] and exc_name in warnings[0]


def test_fetch_live_feed_isolates_a_malformed_play_as_a_feed_warning() -> None:
    # End to end: one bad play in an otherwise-good feed surfaces as a feed warning, not a crash.
    raw = _feed()
    raw["liveData"]["plays"]["allPlays"].append({"atBatIndex": 99, "result": None, "about": {}, "count": {}})
    provider = MlbStatsApiProvider(MlbTeamRegistry({}), fetch_game=lambda _pk: raw)
    feed = provider.fetch_live_feed(_game(), now=NOW)
    assert len(feed.events) == 4  # the four good events still parse
    assert len(feed.warnings) == 1 and "AttributeError" in feed.warnings[0]


def test_unmapped_event_type_is_skipped() -> None:
    play = {"atBatIndex": 1, "result": {"eventType": "field_out"}, "about": {}, "count": {}}
    assert _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW) is None


def test_non_dict_play_is_skipped() -> None:
    assert _parse_one_play(["not", "a", "play"], contest=_game(), source=SOURCE, observed_at=NOW) is None


def test_play_without_atbat_index_is_skipped() -> None:
    # A mapped outcome but no stable index anywhere -> cannot dedupe -> dropped.
    play = {"result": {"eventType": "home_run"}, "about": {}, "count": {}}
    assert _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW) is None


def test_atbat_index_falls_back_to_about_block() -> None:
    play = {"result": {"eventType": "single", "description": "bloop"}, "about": {"atBatIndex": 42}, "count": {}}
    event = _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW)
    assert event is not None and event.id.raw == "700001:ab:42"


def test_intent_walk_maps_to_walk() -> None:
    play = {"atBatIndex": 3, "result": {"eventType": "intent_walk"}, "about": {}, "count": {}}
    event = _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW)
    assert event is not None and event.event_type is BaseballGameEventType.WALK


def test_unknown_half_inning_defaults_to_top() -> None:
    event = _parse_one_play(_play(halfInning="?"), contest=_game(), source=SOURCE, observed_at=NOW)
    assert event is not None and event.payload.phase is InningPhase.TOP


def test_source_time_falls_back_to_start_then_observed() -> None:
    # No endTime: fall back to startTime.
    play = _play(endTime=None, startTime="2026-06-17T22:00:00.000Z")
    started = _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW)
    assert started is not None and started.source_time == datetime(2026, 6, 17, 22, 0, tzinfo=timezone.utc)
    # No times at all: fall back to when we observed it.
    play = _play(endTime=None, startTime=None)
    observed = _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW)
    assert observed is not None and observed.source_time == NOW


def test_malformed_count_drops_count_not_the_event() -> None:
    play = _play()
    play["count"] = {"balls": 9, "strikes": 9, "outs": 9}  # impossible
    event = _parse_one_play(play, contest=_game(), source=SOURCE, observed_at=NOW)
    assert event is not None and event.payload.count is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-06-17T22:45:10.000Z", datetime(2026, 6, 17, 22, 45, 10, tzinfo=timezone.utc)),
        ("2026-06-17T22:45:10+00:00", datetime(2026, 6, 17, 22, 45, 10, tzinfo=timezone.utc)),
        ("2026-06-17T22:45:10", datetime(2026, 6, 17, 22, 45, 10, tzinfo=timezone.utc)),  # naive -> UTC
        ("not-a-time", None),
        ("", None),
        (12345, None),
    ],
)
def test_parse_iso(raw: Any, expected: datetime | None) -> None:
    assert _parse_iso(raw) == expected


def test_safe_count_handles_missing_and_bad() -> None:
    full = _safe_count({"balls": 2, "strikes": 1, "outs": 0})
    assert full is not None and (full.balls, full.strikes, full.outs) == (2, 1, 0)
    empty = _safe_count({})  # missing keys default to 0-0-0, a valid count
    assert empty is not None and (empty.balls, empty.strikes, empty.outs) == (0, 0, 0)
    assert _safe_count("not a dict") is None
    assert _safe_count({"balls": 9, "strikes": 0, "outs": 0}) is None  # impossible


def test_event_importance_default_for_untabled_type() -> None:
    # A mapped type always has a table entry; an off-table type takes the modest default.
    imp = _event_importance(BaseballGameEventType.PITCH, BaseballScoringImpact(rbi=0))
    assert imp.priority is DisplayPriority.NORMAL and imp.rarity == pytest.approx(0.1)


def test_scoring_context_lifts_a_walk_off_single_to_alert() -> None:
    # The headline H3 fix: a walk-off single is intrinsically NORMAL but, in context, an ALERT
    # that clears the big-play bar — so it flashes like the dramatic play it is.
    walk_off = scoring_impact(phase=InningPhase.BOTTOM, inning=9, rbi=1, away_score=3, home_score=4)
    imp = _event_importance(BaseballGameEventType.SINGLE, walk_off)
    assert imp.priority is DisplayPriority.ALERT and "walk_off" in imp.reasons and imp.leverage == 1.0


def test_scoring_context_makes_a_go_ahead_single_eligible() -> None:
    # A go-ahead RBI single (intrinsically NORMAL) reaches the HIGH_LEVERAGE big-play floor.
    go_ahead = scoring_impact(phase=InningPhase.TOP, inning=7, rbi=1, away_score=4, home_score=3)
    imp = _event_importance(BaseballGameEventType.SINGLE, go_ahead)
    assert imp.priority is DisplayPriority.HIGH_LEVERAGE and "go_ahead" in imp.reasons


def test_a_non_scoring_single_stays_normal() -> None:
    # No RBI, no context: a routine single informs the live card but never takes over.
    routine = scoring_impact(phase=InningPhase.TOP, inning=2, rbi=0, away_score=0, home_score=0)
    imp = _event_importance(BaseballGameEventType.SINGLE, routine)
    assert imp.priority is DisplayPriority.NORMAL and imp.reasons == ("single",)


def test_tying_and_plain_scoring_runs_clear_the_big_play_floor() -> None:
    tying = scoring_impact(phase=InningPhase.TOP, inning=8, rbi=1, away_score=3, home_score=3)
    tying_imp = _event_importance(BaseballGameEventType.SINGLE, tying)
    assert tying_imp.priority is DisplayPriority.HIGH_LEVERAGE and "tying" in tying_imp.reasons
    plain = scoring_impact(phase=InningPhase.BOTTOM, inning=6, rbi=1, away_score=1, home_score=6)
    plain_imp = _event_importance(BaseballGameEventType.SINGLE, plain)
    assert plain_imp.priority is DisplayPriority.HIGH_LEVERAGE and "scored" in plain_imp.reasons


def test_parsed_play_carries_its_scoring_impact() -> None:
    # The impact rides on the payload (for a card/badge later), computed once at parse.
    hr = next(e for e in _events() if e.event_type is BaseballGameEventType.HOME_RUN)
    impact = hr.payload.scoring_impact
    assert impact is not None and impact.rbi == 2 and impact.go_ahead and not impact.walk_off
