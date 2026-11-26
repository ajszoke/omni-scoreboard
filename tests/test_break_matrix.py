"""The break capability matrix, walked: every kind it says is detected has a raw-to-evidence example here, read
through the league's provider and detector, with the matrix's confidence and onset; every kind it says isn't has
its nearest miss; and the hard cases, end to end."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind, OnsetBasis
from omni.events.break_detection import BREAK_CAPABILITIES, BreakCapability, onset_age
from omni.events.football import FootballEventKind
from omni.events.mlb_breaks import mlb_break_evidence
from omni.domain.basketball import BasketballGameState, BasketballPhase
from omni.events.nfl_breaks import nfl_break_evidence
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import HALFTIME_HOLD, NFLMETA_SOURCE, NflLiveSnapshot, NflMetaLiveProvider, PlayFeedPace
from tests.mlb_break_feeds import RECEIVED, SOURCE, T, action, answer, game, pitch, play, read
from tests.test_nba_breaks import GAME as NBA_GAME
from tests.test_nba_breaks import T0 as NBA_T0
from tests.test_nba_breaks import _state as nba_state
from tests.test_nba_breaks import at as nba_at
from tests.test_nba_breaks import watch as nba_watch

S = timedelta(seconds=1)
_MLB_GAME = LeagueScopedId(League.MLB, SOURCE, "700001")
_NFL_GAME = LeagueScopedId(League.NFL, NFLMETA_SOURCE, "22920")
_NFL_AT = ClockReading(datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc), MonotonicTime(1800.0))
_EXPLICIT = BreakConfidence.EXPLICIT


def _mlb(raw: dict[str, Any], *, inning_break_length: int | None = None) -> Callable[[], tuple[BreakEvidence, ...]]:
    """A synthetic StatsAPI answer, read through the provider and the MLB detector."""
    return lambda: mlb_break_evidence(read(raw, inning_break_length=inning_break_length), game(), RECEIVED)


def _stopped(event_type: str) -> dict[str, Any]:
    """The plate appearance under way, its last event a stoppage logged 8 s before the answer."""
    return answer(plays=(play(41, complete=False, events=(pitch(), action(event_type, at=T - 8 * S))),))


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _scoreboard(**changes: Any) -> dict[str, Any]:
    """NFLMeta's live-scores row for PIT @ CIN, observed 3 s before the look arrived."""
    row: dict[str, Any] = {
        "game_id": 22920,
        "kickoff_at": "2026-11-16T01:20:00.000Z",
        "away_team": {"abbr": "PIT", "score": 7},
        "home_team": {"abbr": "CIN", "score": 3},
        "phase": "in",
        "period": 2,
        "clock": "1:52",
        "status_detail": "1:52 - 2nd",
        "situation": {"possession_abbr": "CIN", "down": 2, "distance": 6, "text": "2nd & 6", "yardline_100": 75},
        "play_sync": {"status": "awaiting_play", "last_play_id": None},
        "observed_at": _iso(_NFL_AT.utc - 3 * S),
    }
    row.update(changes)
    return row


def _row(number: int, play_type: str, description: str, *, written: datetime, revision: int = 1) -> dict[str, Any]:
    """One play-changes row: a play as the feed wrote it, at ``written``."""
    play = {"play_number": number, "play_type": play_type, "quarter": 2, "description": description}
    return {"source_play_id": f"p{number}", "revision": revision, "changed_at": _iso(written), "play": play}


_Look = tuple[dict[str, Any], list[dict[str, Any]]]  # a scoreboard row and the play-feed rows read with it


def _nfl_look(*looks: _Look) -> NflLiveSnapshot:
    """Each look's raw scoreboard and play rows read through the NFLMeta provider, ten seconds apart; the last."""
    current: list[_Look] = []

    def fetch(path: str, query: Mapping[str, str | int]) -> Any:
        scoreboard, rows = current[-1]
        if path.endswith("/play-changes"):
            return {"data": rows, "meta": {"has_more": False, "next_cursor": "c"}}
        return {"data": [scoreboard], "meta": {"recommended_poll_seconds": 10}}

    nflmeta = NflMetaLiveProvider(
        NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=fetch, play_feed=PlayFeedPace.EVERY_POLL
    )
    snapshot: NflLiveSnapshot | None = None
    for look_number, look in enumerate(looks, start=1 - len(looks)):  # ..., -1, 0: the last look is at _NFL_AT
        current.append(look)
        since = look_number * 10 * S
        snapshot = nflmeta.poll(22920, ClockReading(_NFL_AT.utc + since, _NFL_AT.monotonic + since))
    assert snapshot is not None
    return snapshot


def _nfl(*looks: _Look) -> Callable[[], tuple[BreakEvidence, ...]]:
    """Raw NFLMeta looks, read through the provider and the NFL detector."""
    return lambda: nfl_break_evidence(_nfl_look(*looks), _NFL_AT)


def _stoppage_row(play_type: str, description: str) -> _Look:
    """The 2nd quarter under way, and a stoppage row the feed wrote 6 s before the look."""
    return _scoreboard(), [_row(412, play_type, description, written=_NFL_AT.utc - 6 * S)]


@dataclass(frozen=True, slots=True)
class _Example:
    """A raw answer, and the evidence the detectors make of it."""

    detect: Callable[[], tuple[BreakEvidence, ...]]
    expected: BreakEvidence


def _expect(
    kind: BreakKind,
    marker: str,
    *,
    onset: datetime | None = None,
    stated: timedelta | None = None,
    basis: OnsetBasis = OnsetBasis.EVENT,
) -> BreakEvidence:
    """The evidence an example becomes: each league's answer stood when its raw answer says (StatsAPI's at ``T``,
    NFLMeta's 3 s before the look)."""
    mlb = kind.league is League.MLB
    contest, received, as_of = (_MLB_GAME, RECEIVED, T) if mlb else (_NFL_GAME, _NFL_AT, _NFL_AT.utc - 3 * S)
    return BreakEvidence(
        id=BreakId(contest, kind, marker),
        received=received,
        confidence=_EXPLICIT,
        onset=onset,
        stated=stated,
        as_of=as_of,
        onset_basis=basis,
    )


_WRITTEN = OnsetBasis.PUBLISHED  # a stoppage row's onset is the feed's write of it


_EXAMPLES: dict[BreakKind, _Example] = {
    # linescore.inningState "Middle" in the 5th; the top's last plate appearance ended at T - 20 s; the schedule
    # states a 120 s inning break.
    BreakKind.MLB_INNING_BREAK: _Example(
        _mlb(
            answer(inning=5, inning_state="Middle", plays=(play(39, ended=T - 20 * S),)),
            inning_break_length=120,
        ),
        _expect(BreakKind.MLB_INNING_BREAK, "top 5 middle", onset=T - 20 * S, stated=timedelta(seconds=120)),
    ),
    # playEvents[1].details.eventType "mound_visit" at T - 8 s, with no pitch since.
    BreakKind.MLB_MOUND_VISIT: _Example(
        _mlb(_stopped("mound_visit")), _expect(BreakKind.MLB_MOUND_VISIT, "at-bat 41 event 1", onset=T - 8 * S)
    ),
    BreakKind.MLB_PITCHING_CHANGE: _Example(
        _mlb(_stopped("pitching_substitution")),
        _expect(BreakKind.MLB_PITCHING_CHANGE, "at-bat 41 event 1", onset=T - 8 * S),
    ),
    BreakKind.MLB_STOPPAGE: _Example(
        _mlb(_stopped("injury")), _expect(BreakKind.MLB_STOPPAGE, "at-bat 41 event 1", onset=T - 8 * S)
    ),
    # reviewDetails {"inProgress": true} on the plate appearance just finished, while the next batter is up.
    BreakKind.MLB_REVIEW: _Example(
        _mlb(answer(plays=(play(40, review={"inProgress": True}), play(41, complete=False)))),
        _expect(BreakKind.MLB_REVIEW, "at-bat 40"),
    ),
    # gameData.status.detailedState "Delayed: Rain" in the top of the 7th.
    BreakKind.MLB_WEATHER: _Example(
        _mlb(answer(inning=7, plays=(play(50, inning=7, complete=False),), status="Delayed: Rain")),
        _expect(BreakKind.MLB_WEATHER, "delayed top 7"),
    ),
    # A play-feed row {"play_type": "timeout", "description": "Timeout #2 by CIN at 01:52."}, written 6 s before
    # the look.
    BreakKind.NFL_TEAM_TIMEOUT: _Example(
        _nfl(_stoppage_row("timeout", "Timeout #2 by CIN at 01:52.")),
        _expect(BreakKind.NFL_TEAM_TIMEOUT, "play 412", onset=_NFL_AT.utc - 6 * S, basis=_WRITTEN),
    ),
    BreakKind.NFL_OFFICIAL_TIMEOUT: _Example(
        _nfl(_stoppage_row("official_timeout", "Official Timeout at 01:52.")),
        _expect(BreakKind.NFL_OFFICIAL_TIMEOUT, "play 412", onset=_NFL_AT.utc - 6 * S, basis=_WRITTEN),
    ),
    BreakKind.NFL_TWO_MINUTE_WARNING: _Example(
        _nfl(_stoppage_row("two_minute_warning", "Two-Minute Warning")),
        _expect(BreakKind.NFL_TWO_MINUTE_WARNING, "play 412", onset=_NFL_AT.utc - 6 * S, basis=_WRITTEN),
    ),
    # The scoreboard's {"status_detail": "End of 1st", "period": 1}.
    BreakKind.NFL_QUARTER_BREAK: _Example(
        _nfl((_scoreboard(period=1, clock="0:00", status_detail="End of 1st"), [])),
        _expect(BreakKind.NFL_QUARTER_BREAK, "end of period 1"),
    ),
    # The scoreboard's {"status_detail": "Halftime"}, first seen a minute after the feed wrote the half's last play:
    # the provider's 13-minute hold runs from that play.
    BreakKind.NFL_HALFTIME: _Example(
        _nfl(
            (
                _scoreboard(clock="0:00", status_detail="Halftime", situation=None),
                [_row(330, "rush", "P.Mahomes kneels to KC 30 for -1 yards.", written=_NFL_AT.utc - 60 * S)],
            )
        ),
        _expect(
            BreakKind.NFL_HALFTIME,
            "halftime",
            onset=_NFL_AT.utc - 60 * S,
            stated=HALFTIME_HOLD,
            basis=OnsetBasis.LOCAL_HOLD,  # the provider's hold, on this board's clock
        ),
    ),
}


def _nba(*reads: tuple[float, BasketballGameState]) -> Callable[[], tuple[BreakEvidence, ...]]:
    """Synthetic slate reads of one game, a second apart as given, through the feed deriver and the NBA detector: the
    evidence the last read showed."""
    return lambda: nba_watch(*reads)[-1]


def _nba_expect(kind: BreakKind, marker: str, seconds: float, *, onset: float | None = None) -> BreakEvidence:
    """The evidence an NBA example becomes: received with its last read; a timeout's onset is its inferred one."""
    return BreakEvidence(
        id=BreakId(NBA_GAME, kind, marker),
        received=nba_at(seconds),
        confidence=BreakConfidence.INFERRED if onset is not None else _EXPLICIT,
        onset=NBA_T0 + onset * S if onset is not None else None,
        onset_basis=OnsetBasis.LOCAL_READ if onset is not None else OnsetBasis.EVENT,
    )


_END = BasketballPhase.END_OF_PERIOD
_NBA_EXAMPLES: dict[BreakKind, _Example] = {
    # UTA's timeouts_remaining 7 -> 6 in the first quarter, its first: the clock stopped at 7:23 five seconds in.
    BreakKind.NBA_TV_TIMEOUT: _Example(
        _nba((0, nba_state(1, 4440)), (5, nba_state(1, 4430)), (20, nba_state(1, 4430, timeouts=(6, 7)))),
        _nba_expect(BreakKind.NBA_TV_TIMEOUT, "timeout period 1 7:23 away", 20, onset=5),
    ),
    # LAL's 2 -> 1 in overtime, where every timeout is short.
    BreakKind.NBA_TEAM_TIMEOUT: _Example(
        _nba(
            (0, nba_state(5, 3000, timeouts=(2, 2), scores=(100, 100))),
            (5, nba_state(5, 2990, timeouts=(2, 2), scores=(100, 100))),
            (9, nba_state(5, 2990, timeouts=(2, 1), scores=(100, 100))),
        ),
        _nba_expect(BreakKind.NBA_TEAM_TIMEOUT, "timeout period 5 4:59 home", 9, onset=5),
    ),
    # {"time": "END Q1", "period": 1}.
    BreakKind.NBA_QUARTER_BREAK: _Example(
        _nba((0, nba_state(1, 100)), (10, nba_state(1, None, phase=_END))),
        _nba_expect(BreakKind.NBA_QUARTER_BREAK, "end of period 1", 10),
    ),
    # {"time": "Half", "period": 2}.
    BreakKind.NBA_HALFTIME: _Example(
        _nba((0, nba_state(2, 50)), (10, nba_state(2, None, phase=BasketballPhase.HALFTIME))),
        _nba_expect(BreakKind.NBA_HALFTIME, "halftime", 10),
    ),
    # {"time": "END Q4", "period": 4} with the score tied.
    BreakKind.NBA_BEFORE_OVERTIME: _Example(
        _nba((0, nba_state(4, 10, scores=(99, 99))), (5, nba_state(4, None, phase=_END, scores=(99, 99)))),
        _nba_expect(BreakKind.NBA_BEFORE_OVERTIME, "end of period 4", 5),
    ),
}
_EXAMPLES.update(_NBA_EXAMPLES)

_REVIEWED = "J.Burrow pass short right to J.Chase to PIT 26 for 4 yards (J.Dean)."
_RULING = " The Replay Official reviewed the pass completion ruling, and the play was REVERSED."
_INJURY = {
    "player_name": "A.Player",
    "team_abbr": "CIN",
    "injury": "Knee",
    "state": "questionable_to_return",
    "reported_at": _iso(_NFL_AT.utc - 10 * S),
}

# For each kind a league's detector can't find: the look nearest to it, and the entry that look brings.
_MISSES: dict[BreakKind, tuple[tuple[_Look, ...], FootballEventKind]] = {
    # An official timeout, its reason unknown, is all a commercial break ever looks like.
    BreakKind.NFL_TV_TIMEOUT: (
        (_stoppage_row("official_timeout", "Official Timeout at 01:52."),),
        FootballEventKind.OFFICIAL_TIMEOUT,
    ),
    # A player reported hurt 10 s ago: the scoreboard can't say whether play stopped for him.
    BreakKind.NFL_INJURY_TIMEOUT: (((_scoreboard(injuries=[_INJURY]), []),), FootballEventKind.INJURY),
    # A play, then its ruling written into its row: the review is over by the time the feed says it happened.
    BreakKind.NFL_REVIEW: (
        (
            (_scoreboard(), [_row(300, "pass_reception", _REVIEWED, written=_NFL_AT.utc - 90 * S)]),
            (
                _scoreboard(),
                [_row(300, "pass_reception", _REVIEWED + _RULING, written=_NFL_AT.utc - 5 * S, revision=2)],
            ),
        ),
        FootballEventKind.REVIEW,
    ),
}


def _detected() -> list[BreakCapability]:
    return [row for row in BREAK_CAPABILITIES.values() if row.detected]


def test_every_break_kind_has_one_row_and_one_saying_why_when_it_isnt_detected() -> None:
    assert set(BREAK_CAPABILITIES) == set(BreakKind)
    assert all(row.kind is kind and row.signal for kind, row in BREAK_CAPABILITIES.items())


def test_every_kind_the_matrix_says_is_detected_has_an_example() -> None:
    assert set(_EXAMPLES) == {row.kind for row in _detected()}


@pytest.mark.parametrize("row", _detected(), ids=lambda row: row.kind.name)
def test_a_detected_kinds_example_becomes_its_evidence_with_the_matrixs_confidence_and_onset(
    row: BreakCapability,
) -> None:
    example = _EXAMPLES[row.kind]
    assert example.detect() == (example.expected,)
    assert example.expected.confidence is row.confidence
    assert (example.expected.onset is not None) is (row.onset is not None)


def test_every_kind_a_built_detector_cant_find_has_its_nearest_miss() -> None:
    built = {League.MLB, League.NFL}
    assert set(_MISSES) == {row.kind for row in BREAK_CAPABILITIES.values() if not row.detected} & {
        kind for kind in BreakKind if kind.league in built
    }


@pytest.mark.parametrize("kind", list(_MISSES), ids=lambda kind: kind.name)
def test_a_kind_that_isnt_detected_is_never_evidenced_by_its_nearest_miss(kind: BreakKind) -> None:
    looks, entry = _MISSES[kind]
    snapshot = _nfl_look(*looks)
    assert entry in {event.kind for event in snapshot.events}  # the near miss did arrive
    assert kind not in {evidence.kind for evidence in nfl_break_evidence(snapshot, _NFL_AT)}


def test_a_capability_row_can_name_an_onset_only_for_a_kind_that_is_detected() -> None:
    with pytest.raises(ValueError, match="no onset"):
        BreakCapability(kind=BreakKind.NFL_TV_TIMEOUT, confidence=None, signal="no feed says", onset="never")


# --- the hard cases -------------------------------------------------------------------------------------------


def test_a_generic_official_timeout_is_an_official_timeout_and_never_a_tv_timeout() -> None:
    (evidence,) = _nfl(_stoppage_row("official_timeout", "Official Timeout at 08:26."))()
    assert evidence.kind is BreakKind.NFL_OFFICIAL_TIMEOUT
    every_example = [evidence for example in _EXAMPLES.values() for evidence in example.detect()]
    assert BreakKind.NFL_TV_TIMEOUT not in {evidence.kind for evidence in every_example}


def test_an_active_mlb_review_is_a_break_and_its_ruling_is_the_same_break_ended() -> None:
    def review(in_progress: bool) -> BreakEvidence:
        (evidence,) = _mlb(answer(plays=(play(40, review={"inProgress": in_progress}), play(41, complete=False))))()
        return evidence

    active, ruling = review(True), review(False)
    assert active.id == ruling.id and (active.ended, ruling.ended) == (False, True)


def test_a_backfilled_mound_visit_keeps_its_onset_and_age_so_the_lease_rules_can_refuse_it() -> None:
    raw = answer(plays=(play(41, complete=False, events=(action("mound_visit", at=T - 4 * 60 * S),)),))
    (evidence,) = _mlb(raw)()
    assert evidence.onset == T - 4 * 60 * S and onset_age(evidence) == 4 * 60 * S + 2 * S


def test_a_repeated_same_half_inning_marker_is_the_same_break() -> None:
    def middle(received: ClockReading, *plays: dict[str, Any]) -> BreakEvidence:
        raw = answer(inning=5, inning_state="Middle", plays=(play(39, ended=T - 20 * S), *plays))
        (evidence,) = mlb_break_evidence(read(raw), game(), received)
        return evidence

    later = ClockReading(RECEIVED.utc + 30 * S, RECEIVED.monotonic + 30 * S)
    warming = play(40, half="bottom", complete=False, events=(action("pitching_substitution", at=T),))
    assert (
        middle(RECEIVED).id
        == middle(later, warming).id
        == BreakId(_MLB_GAME, BreakKind.MLB_INNING_BREAK, "top 5 middle")
    )
