"""MLB break detection: synthetic StatsAPI answers through the provider's parse, to break and action evidence."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import (
    BaseballAction,
    BaseballBreakSignals,
    BaseballFinishedPlay,
    BaseballReview,
    BaseballStoppage,
    BaseballStoppageKind,
    InningPhase,
)
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.events.baseball import LiveBaseballFeed
from omni.events.break_detection import ActionEvidence, credible_onset, onset_age
from omni.events.mlb_breaks import mlb_break_evidence, mlb_latest_action
from omni.providers.mlb_statsapi import _GAME_FIELDS, MlbStatsApiProvider, _parse_game_state, _schedule_rows
from omni.providers.mlb_teams import MlbTeamRegistry
from tests.mlb_break_feeds import RECEIVED, SOURCE, T, action, answer, game, pitch, play, provider, read, stamp

S = timedelta(seconds=1)
_GAME_ID = LeagueScopedId(League.MLB, SOURCE, "700001")
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "providers" / "mlb_game_live.json"


def _breaks(feed: LiveBaseballFeed, *, status: GameStatus = GameStatus.LIVE) -> tuple[BreakEvidence, ...]:
    return mlb_break_evidence(feed, game(status), RECEIVED)


def _one(feed: LiveBaseballFeed) -> BreakEvidence:
    (evidence,) = _breaks(feed)
    return evidence


def _fifth(inning_state: str = "Middle", *after: dict[str, Any]) -> dict[str, Any]:
    """The 5th inning after its top half, whose last plate appearance ended 20 s before the answer, and any plate
    appearances ``after`` it."""
    third_out = play(39, inning=5, half="top", ended=T - 20 * S, events=(pitch(),))
    return answer(inning=5, inning_state=inning_state, plays=(play(38), third_out, *after))


# --- inning breaks ---------------------------------------------------------------------------------------------


def test_an_inning_break_carries_its_scheduled_length_and_began_when_its_half_ended() -> None:
    evidence = _one(read(_fifth(), inning_break_length=145))
    assert evidence == BreakEvidence(
        id=BreakId(_GAME_ID, BreakKind.MLB_INNING_BREAK, "top 5 middle"),
        received=RECEIVED,
        confidence=BreakConfidence.EXPLICIT,
        onset=T - 20 * S,
        stated=timedelta(seconds=145),
    )
    assert onset_age(evidence) == 22 * S  # the answer stood 20 s after the third out and arrived 2 s later


def test_the_end_of_an_inning_follows_its_bottom_half() -> None:
    last = play(44, inning=6, half="bottom", ended=T - 5 * S)
    evidence = _one(read(answer(inning=6, inning_state="End", plays=(last,)), inning_break_length=120))
    assert evidence.id.marker == "bottom 6 end"
    assert (evidence.onset, evidence.stated) == (T - 5 * S, timedelta(seconds=120))


def test_the_same_inning_break_read_again_is_the_same_break_and_a_change_inside_it_adds_nothing() -> None:
    first = _one(read(_fifth()))
    change = play(40, inning=5, half="bottom", complete=False, events=(action("pitching_substitution", at=T),))
    later = ClockReading(RECEIVED.utc + 15 * S, RECEIVED.monotonic + 15 * S)
    (again,) = mlb_break_evidence(read(_fifth("Middle", change)), game(), later)
    assert again.id == first.id and again.kind is BreakKind.MLB_INNING_BREAK


@pytest.mark.parametrize(
    "last",
    [
        play(33, inning=4, half="bottom", ended=T - 200 * S),  # the log's newest finished play is another half's
        play(39, inning=5, half="top"),  # the right half, but the feed doesn't say when it ended
        play(39, inning=5, half="sideways", ended=T - 20 * S),  # it doesn't say which half
    ],
)
def test_an_inning_break_without_its_halfs_end_or_a_stated_length_has_neither(last: dict[str, Any]) -> None:
    evidence = _one(read(answer(inning=5, inning_state="Middle", plays=(last,))))
    assert (evidence.id.marker, evidence.onset, evidence.stated) == ("top 5 middle", None, None)


def test_the_scheduled_length_can_come_from_the_games_context_metrics() -> None:
    metrics: list[dict[str, Any]] = [{"game": {"inningBreakLength": 175}, "homeWinProbability": 51.0}]
    mlb = provider(_fifth(), context=lambda pk: metrics[-1])
    assert mlb.fetch_live_feed(game(), now=T).state.break_signals.inning_break_length is None
    mlb.fetch_win_probability(game())
    assert mlb.fetch_live_feed(game(), now=T).state.break_signals.inning_break_length == timedelta(seconds=175)
    metrics.append({"inningBreakLength": 145})  # stated outside a game block
    mlb.fetch_win_probability(game())
    assert _one(mlb.fetch_live_feed(game(), now=T)).stated == timedelta(seconds=145)


def test_a_schedule_responses_inning_break_reaches_the_games_live_feed() -> None:
    teams = {side: {"team": {"id": team_id}} for side, team_id in (("away", 115), ("home", 119))}
    scheduled = {"gamePk": 700001, "gameDate": stamp(T), "teams": teams, "inningBreakLength": 145}
    rows = _schedule_rows({"dates": [{"date": "2026-06-17", "games": [scheduled]}]})
    assert rows[0]["inning_break_length"] == 145
    mlb = MlbStatsApiProvider(
        MlbTeamRegistry.from_color_file(), lambda day, sport_ids: rows, fetch_game=lambda pk: _fifth()
    )
    mlb.refresh(T)
    assert _one(mlb.fetch_live_feed(game(), now=T)).stated == timedelta(seconds=145)


@pytest.mark.parametrize("stated", [0, -120, True, "120", 120.0])
def test_a_scheduled_length_that_isnt_a_positive_whole_number_of_seconds_is_left_unknown(stated: Any) -> None:
    mlb = provider(_fifth(), inning_break_length=145, context=lambda pk: {"game": {"inningBreakLength": stated}})
    mlb.fetch_win_probability(game())  # a garbled length doesn't replace the schedule's
    assert _one(mlb.fetch_live_feed(game(), now=T)).stated == timedelta(seconds=145)
    assert _one(read(_fifth(), inning_break_length=stated)).stated is None


# --- micro-breaks: visits, changes, injuries ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "kind"),
    [
        ("mound_visit", BreakKind.MLB_MOUND_VISIT),
        ("pitching_substitution", BreakKind.MLB_PITCHING_CHANGE),
        ("injury", BreakKind.MLB_STOPPAGE),
    ],
)
def test_a_stoppage_with_no_pitch_since_is_evidenced_from_when_it_began(event_type: str, kind: BreakKind) -> None:
    current = play(41, complete=False, events=(pitch(index=0), action(event_type, at=T - 8 * S, index=1)))
    evidence = _one(read(answer(plays=(play(40), current))))
    assert evidence.id == BreakId(_GAME_ID, kind, "at-bat 41 event 1")
    assert (evidence.onset, evidence.confidence, evidence.ended, evidence.revision) == (
        T - 8 * S,
        BreakConfidence.EXPLICIT,
        False,
        0,
    )


def test_a_stoppage_followed_by_a_pitch_or_by_the_plate_appearances_end_is_over() -> None:
    pitched = play(41, complete=False, events=(action("mound_visit", at=T - 30 * S), pitch(at=T - 4 * S)))
    assert _breaks(read(answer(plays=(play(40), pitched)))) == ()
    ended = play(41, complete=True, events=(action("mound_visit", at=T - 30 * S),))
    assert _breaks(read(answer(plays=(play(40), ended)))) == ()


def test_a_stoppage_first_seen_long_after_it_happened_keeps_its_onset_so_its_age_refuses_it_a_lease() -> None:
    backfilled = play(41, complete=False, events=(action("mound_visit", at=T - 300 * S),))
    evidence = _one(read(answer(plays=(backfilled,))))
    assert evidence.kind is BreakKind.MLB_MOUND_VISIT
    assert evidence.onset == T - 300 * S and onset_age(evidence) == 302 * S


def test_a_change_between_batters_opens_a_new_plate_appearance_and_is_evidenced_there() -> None:
    change = play(42, complete=False, events=(action("pitching_substitution", at=T - 3 * S),))
    feed = read(answer(plays=(play(41, ended=T - 40 * S), change)))
    assert _one(feed).id.marker == "at-bat 42 event 0"
    action_seen = mlb_latest_action(feed, game(), RECEIVED)
    assert action_seen == ActionEvidence(contest=_GAME_ID, marker="at-bat 41 end", received=RECEIVED, at=T - 40 * S)


@pytest.mark.parametrize(
    "event_type",
    [
        "batter_timeout",
        "pickoff_1b",
        "pitcher_step_off",
        "offensive_substitution",  # a pinch hitter
        "runner_placed",  # the automatic runner
        "defensive_switch",
        None,
    ],
)
def test_a_pause_that_lends_nothing_is_not_evidenced(event_type: str | None) -> None:
    current = play(41, complete=False, events=(pitch(), action(str(event_type), at=T - 5 * S)))
    assert _breaks(read(answer(plays=(current,)))) == ()


def test_a_ball_strike_challenge_on_a_pitch_is_not_a_review() -> None:
    challenged = pitch(at=T - 6 * S)
    challenged["reviewDetails"] = {"inProgress": True, "isOverturned": False}
    assert _breaks(read(answer(plays=(play(41, complete=False, events=(challenged,)),)))) == ()


# --- replay reviews -------------------------------------------------------------------------------------------


def test_a_review_in_progress_is_evidenced_and_its_ruling_ends_the_same_break() -> None:
    def reviewed(in_progress: bool) -> LiveBaseballFeed:
        result = play(40, ended=T - 50 * S, review={"inProgress": in_progress, "isOverturned": False})
        return read(answer(plays=(result, play(41, complete=False))))  # the next batter is already up

    active, ruled = _one(reviewed(True)), _one(reviewed(False))
    assert active.id == ruled.id == BreakId(_GAME_ID, BreakKind.MLB_REVIEW, "at-bat 40")
    assert (active.ended, active.onset) == (False, None)  # the feed doesn't time a review
    assert ruled.ended


def test_a_review_of_a_steal_names_the_event_under_review() -> None:
    steal = action("stolen_base_2b", at=T - 30 * S, index=2, review={"inProgress": True})
    evidence = _one(read(answer(plays=(play(41, complete=False, events=(pitch(), pitch(), steal)),))))
    assert evidence.id.marker == "at-bat 41 event 2" and evidence.kind is BreakKind.MLB_REVIEW


@pytest.mark.parametrize("review", [{"isOverturned": True}, {"inProgress": "yes"}, ["inProgress"], None])
def test_a_review_that_doesnt_say_whether_it_is_in_progress_is_not_read(review: Any) -> None:
    result = play(40, ended=T - 50 * S, review=review)
    assert _breaks(read(answer(plays=(result, play(41, complete=False))))) == ()


# --- weather and finals ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detailed", "marker"), [("Delayed: Rain", "delayed top 7"), ("Suspended: Rain", "suspended top 7")]
)
def test_a_rain_delay_or_a_suspension_is_weather_and_nothing_else(detailed: str, marker: str) -> None:
    visit = play(50, inning=7, complete=False, events=(action("mound_visit", at=T - 2 * S),))
    feed = read(answer(inning=7, plays=(visit,), status=detailed))
    assert _one(feed) == BreakEvidence(
        id=BreakId(_GAME_ID, BreakKind.MLB_WEATHER, marker), received=RECEIVED, confidence=BreakConfidence.EXPLICIT
    )


def test_a_feed_that_doesnt_say_its_status_is_read_by_the_schedules() -> None:
    feed = read(answer(inning=3, inning_state="Middle", plays=(play(20, inning=3),)))
    silent = LiveBaseballFeed(state=feed.state, as_of=feed.as_of)
    assert _breaks(silent, status=GameStatus.DELAYED)[0].id.marker == "delayed middle 3"
    assert _breaks(silent)[0].kind is BreakKind.MLB_INNING_BREAK


def test_a_final_game_has_no_breaks() -> None:
    final = answer(inning=9, inning_state="End", plays=(play(70, inning=9, half="bottom", ended=T),), status="Final")
    assert _breaks(read(final)) == ()


# --- onsets ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("as_of", "began", "credible"),
    [
        (T, T - 8 * S, True),
        (None, T - 8 * S, False),  # the answer doesn't say when it stood: the source's clock can't be checked
        (T + 30 * S, T + 20 * S, False),  # the source's clock runs ahead of ours: every age would read short
        (T, T + 10 * S, False),  # dated after it reached us
        (T + 7 * S, T + 7 * S, True),  # within the second's rounding and the clocks' play
    ],
)
def test_an_onset_is_given_only_when_the_feeds_time_for_it_is_credible(as_of: Any, began: Any, credible: bool) -> None:
    current = play(41, complete=False, events=(action("mound_visit", at=began),))
    evidence = _one(read(answer(plays=(current,), as_of=as_of)))
    assert evidence.onset == (began if credible else None)


def test_an_onset_needs_a_time_for_the_break() -> None:
    assert credible_onset(None, as_of=T, received=RECEIVED) is None
    untimed = play(41, complete=False, events=(action("mound_visit"),))
    assert _one(read(answer(plays=(untimed,)))).onset is None


def test_an_onsets_age_is_never_below_zero_and_unknown_without_an_onset() -> None:
    evidence = _one(read(_fifth()))
    assert onset_age(BreakEvidence(id=evidence.id, received=RECEIVED, confidence=evidence.confidence)) is None
    early = BreakEvidence(id=evidence.id, received=RECEIVED, confidence=evidence.confidence, onset=T + 4 * S)
    assert onset_age(early) == timedelta(0)


# --- action evidence ------------------------------------------------------------------------------------------


def test_the_newest_action_is_the_last_pitch_of_the_plate_appearance_under_way() -> None:
    current = play(
        41, complete=False, events=(pitch(at=T - 30 * S), action("mound_visit", at=T - 25 * S), pitch(at=T - 2 * S))
    )
    feed = read(answer(plays=(play(40, ended=T - 60 * S), current)))
    assert mlb_latest_action(feed, game(), RECEIVED) == ActionEvidence(
        contest=_GAME_ID, marker="at-bat 41 pitch 2", received=RECEIVED, at=T - 2 * S
    )


def test_after_the_break_the_next_halfs_first_pitch_is_newer_action() -> None:
    during = read(_fifth())
    leadoff = play(40, inning=5, half="bottom", complete=False, events=(pitch(at=T + 140 * S),))
    after = _fifth("Bottom", leadoff)
    seen, newer = (mlb_latest_action(feed, game(), RECEIVED) for feed in (during, read(after)))
    assert seen is not None and newer is not None
    assert (seen.marker, seen.at) == ("at-bat 39 end", T - 20 * S)
    assert (newer.marker, newer.at) == ("at-bat 40 pitch 0", T + 140 * S)
    assert _breaks(read(after)) == ()  # play has resumed


def test_a_log_with_no_finished_play_and_no_pitch_shows_no_action() -> None:
    assert mlb_latest_action(read(answer(plays=(play(0, complete=False),))), game(), RECEIVED) is None
    assert mlb_latest_action(read(answer()), game(), RECEIVED) is None


# --- the provider boundary ------------------------------------------------------------------------------------


def test_the_recorded_fixture_shows_no_break_and_its_last_pitch_as_the_newest_action() -> None:
    raw = json.loads(_FIXTURE.read_text())
    state = _parse_game_state(raw)
    # Its mound visit names no event type, and a pitch came after it; the top of the 7th is under way.
    assert state.break_signals == BaseballBreakSignals(
        last_finished=BaseballFinishedPlay(
            at_bat=30, inning=6, phase=InningPhase.BOTTOM, ended=T - 9 * 60 * S - 15 * S
        ),
        last_action=BaseballAction(at_bat=38, pitch=2),
    )
    feed = LiveBaseballFeed(state=state)
    assert mlb_break_evidence(feed, game(), RECEIVED) == ()
    seen = mlb_latest_action(feed, game(), RECEIVED)
    assert seen is not None and (seen.marker, seen.at) == ("at-bat 38 pitch 2", None)


def test_the_live_feed_request_keeps_what_the_break_signals_read() -> None:
    read_names = {"playEvents", "index", "isPitch", "eventType", "startTime", "endTime", "isComplete"}
    read_names |= {"reviewDetails", "inProgress", "currentPlay", "allPlays", "atBatIndex", "inning", "halfInning"}
    assert read_names <= set(_GAME_FIELDS.split(","))


@pytest.mark.parametrize("index", [True, -1, "8", None])
def test_a_play_without_a_usable_at_bat_index_is_left_out(index: Any) -> None:
    entry = {"about": {"atBatIndex": index, "isComplete": True, "inning": 5, "halfInning": "top"}}
    assert _parse_game_state(answer(plays=(entry,))).break_signals == BaseballBreakSignals()


def test_a_garbled_play_log_reads_as_absent_and_never_sinks_the_state() -> None:
    raw = answer(
        plays=(
            {"atBatIndex": 7, "about": {"isComplete": True, "inning": "5", "halfInning": "top"}, "playEvents": 4},
            {
                "atBatIndex": 8,
                "about": {"isComplete": False},
                "reviewDetails": "under review",
                "playEvents": ["pitch", {"isPitch": True, "index": -3}, {"details": None, "reviewDetails": []}],
            },
        )
    )
    signals = _parse_game_state(raw).break_signals
    assert signals == BaseballBreakSignals(last_finished=None, last_action=BaseballAction(at_bat=8, pitch=0))


def test_signals_refuse_what_cant_be() -> None:
    with pytest.raises(ValueError, match="top or the bottom"):
        BaseballFinishedPlay(at_bat=3, inning=1, phase=InningPhase.MIDDLE)
    with pytest.raises(ValueError, match="must be positive"):
        BaseballBreakSignals(inning_break_length=timedelta(0))
    stoppage = BaseballStoppage(kind=BaseballStoppageKind.INJURY, at_bat=3, event=0)
    assert not stoppage.resumed and stoppage.began is None
    assert BaseballReview(at_bat=3, in_progress=True).event is None


def test_evidence_is_stamped_with_the_answers_arrival() -> None:
    later = ClockReading(RECEIVED.utc + 30 * S, MonotonicTime(530.0))
    current = play(41, complete=False, events=(action("injury", at=T - 1 * S),))
    (evidence,) = mlb_break_evidence(read(answer(plays=(current,))), game(), later)
    assert evidence.received == later
