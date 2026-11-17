"""NFLMeta's live feed: reading a game, the halftime hold, following the play feed, and injuries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, HomeAway
from omni.core.time import DurationSeconds
from omni.domain.football import FootballInterval, SetPiece, TeamChallenges, TeamTimeouts
from omni.events.football import FootballEventKind
from omni.providers.base import ProviderError
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import (
    CORRECTION_WATCH,
    FINAL_DRAIN,
    HALFTIME_GRACE,
    HALFTIME_HOLD,
    MISMATCH_RETRY,
    NFLMETA_SOURCE,
    FeedNoteKind,
    InjuryStatus,
    NflMetaLiveProvider,
    NflPhase,
    PlayFeedPace,
    PlaySyncStatus,
    SCORE_BACKSTOP,
)

T0 = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
GAME = 22920


def _at(moment: datetime) -> ClockReading:
    """The clock's two readings at ``moment``; the monotonic one counts from T0."""
    return ClockReading(utc=moment, monotonic=MonotonicTime((moment - T0).total_seconds()))


def _row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "game_id": GAME,
        "kickoff_at": "2026-11-16T01:20:00.000Z",
        "away_team": {"abbr": "PIT", "score": 0},
        "home_team": {"abbr": "CIN", "score": 10},
        "phase": "in",
        "period": 2,
        "clock": "13:09",
        "status_detail": "13:09 - 2nd",
        "situation": {"possession_abbr": "CIN", "down": 2, "distance": 6, "text": "2nd & 6", "yardline_100": 75},
        "play_sync": {"status": "awaiting_play", "last_play_id": None},
        "observed_at": "2026-11-16T02:08:51.210Z",
    }
    row.update(changes)
    return row


def _synced(play_id: str, status: str = "matched", **changes: Any) -> dict[str, Any]:
    return _row(play_sync={"status": status, "last_play_id": play_id}, **changes)


def _scores(*rows: Any, poll: Any = 10) -> dict[str, Any]:
    return {"data": list(rows), "meta": {"recommended_poll_seconds": poll}}


def _change(play_id: Any, number: Any, description: str, revision: int = 1, **play: Any) -> dict[str, Any]:
    return {
        "source_play_id": play_id,
        "revision": revision,
        "play": {"play_number": number, "play_type": "rush", "quarter": 2, "description": description, **play},
    }


def _page(*changes: Any, more: bool = False, cursor: str | None = "c") -> dict[str, Any]:
    return {"data": list(changes), "meta": {"has_more": more, "next_cursor": cursor}}


class FakeFeed:
    """Answers each path from a queue of responses (the last repeats), recording every request."""

    def __init__(self) -> None:
        self.scores: list[Any] = []
        self.plays: list[Any] = [_page()]
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, query: Mapping[str, str | int]) -> Any:
        self.requests.append((path, dict(query)))
        queue = self.plays if "/play-changes" in path else self.scores
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def play_reads(self) -> int:
        return sum("/play-changes" in path for path, _ in self.requests)


def _provider(feed: FakeFeed, **kwargs: Any) -> NflMetaLiveProvider:
    return NflMetaLiveProvider(NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=feed, **kwargs)


def test_finds_a_game_by_either_spelling_of_its_clubs() -> None:
    feed = FakeFeed()
    feed.scores = [_scores({"game_id": 1, "away_team": {"abbr": "LA"}, "home_team": {}}, "junk", _row())]
    provider = _provider(feed)
    assert provider.find_game("pit", "CIN") == GAME
    assert provider.find_game("DAL", "NYG") is None


def test_reads_a_live_game_into_the_football_state() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(), poll=7)]
    snapshot = _provider(feed).poll(GAME, _at(T0))
    assert snapshot.phase is NflPhase.IN
    assert snapshot.game.status is GameStatus.LIVE
    assert snapshot.game.away.abbreviation == "PIT"
    assert snapshot.poll_every == DurationSeconds(7)
    assert snapshot.observed_at == datetime(2026, 11, 16, 2, 8, 51, 210000, tzinfo=timezone.utc)
    state = snapshot.state
    assert state is not None
    assert (state.period, state.clock_seconds, state.away_score, state.home_score) == (2, 789, 0, 10)
    assert (state.possession, state.down, state.distance, state.yards_to_goal) == (HomeAway.HOME, 2, 6, 75)
    assert state.timeouts == TeamTimeouts(away=3, home=3)
    assert state.challenges == TeamChallenges(away=2, home=2)
    assert snapshot.events == ()


@pytest.mark.parametrize(
    ("situation", "expected"),
    [
        ({"possession_abbr": "PIT", "down": 1, "distance": 10, "yardline_100": 30}, (HomeAway.AWAY, 1, 10, 30)),
        (
            {"possession_abbr": "PIT", "down": None, "distance": None, "yardline_100": 65},
            (HomeAway.AWAY, None, None, 65),
        ),
        ({"possession_abbr": "PIT", "down": 2, "distance": 3, "yardline_100": 0}, (HomeAway.AWAY, None, None, None)),
        ({"possession_abbr": "DAL", "down": 2, "distance": 3, "yardline_100": 40}, (None, None, None, None)),
        ({"possession_abbr": None, "down": 2, "distance": 3, "yardline_100": 40}, (None, None, None, None)),
        (None, (None, None, None, None)),
    ],
)
def test_keeps_only_a_situation_it_can_place(situation: Any, expected: tuple[Any, ...]) -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(situation=situation))]
    state = _provider(feed).poll(GAME, _at(T0)).state
    assert state is not None
    assert (state.possession, state.down, state.distance, state.yards_to_goal) == expected


@pytest.mark.parametrize(
    ("detail", "interval"),
    [
        ("End of 1st", FootballInterval.END_OF_QUARTER),
        ("Halftime", FootballInterval.HALFTIME),
        ("End of 2nd Quarter", FootballInterval.HALFTIME),
        (None, None),
    ],
)
def test_reads_the_breaks_between_periods(detail: str | None, interval: FootballInterval | None) -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(status_detail=detail, clock=None))]
    state = _provider(feed).poll(GAME, _at(T0)).state
    assert state is not None
    assert state.interval is interval
    assert state.clock_seconds == 0
    if interval is FootballInterval.HALFTIME:
        assert state.possession is None and state.timeouts is None  # no kickoff seen; timeouts start over
    else:
        assert state.possession is HomeAway.HOME  # a quarter break keeps the next snap's team


def test_a_game_before_kickoff_has_no_state_and_after_has_a_final_one() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(phase="pre", period=0)), _scores(_row(phase="post", status_detail="Final"), poll=0)]
    provider = _provider(feed)
    before = provider.poll(GAME, _at(T0))
    assert before.state is None
    assert before.game.status is GameStatus.PREGAME
    after = provider.poll(GAME, _at(T0))
    assert after.game.status is GameStatus.FINAL
    assert after.state is not None and after.state.possession is None
    assert after.state.interval is FootballInterval.FINAL
    assert after.state.timeouts is None and after.state.challenges is None
    assert after.poll_every == DurationSeconds(10)  # an unusable pace falls back to the recommended one


def test_halftime_holds_off_the_feed_for_the_hold_then_polls_again() -> None:
    feed = FakeFeed()
    halftime = _scores(_synced("p0", status_detail="Halftime", clock="0:00"))
    feed.scores = [_scores(_row()), halftime, halftime, _scores(_row(period=3, status_detail="15:00 - 3rd"))]
    feed.plays = [_page(_change("p0", 100, "A.Rodgers kneels to PIT 21 for -1 yards."))]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    assert not provider.holding(GAME, _at(T0))
    first = provider.poll(GAME, _at(T0 + timedelta(minutes=1)))
    assert first.state is not None and first.state.interval is FootballInterval.HALFTIME
    assert first.break_ends_at == T0 + timedelta(minutes=1) + HALFTIME_HOLD
    asked = len(feed.requests)
    for minute in (2, 9, 13):
        at = T0 + timedelta(minutes=minute)
        assert provider.holding(GAME, _at(at))
        held = provider.poll(GAME, _at(at))
        assert held.state == first.state and held.events == ()
    assert len(feed.requests) == asked  # not one request over the hold
    end = T0 + timedelta(minutes=1) + HALFTIME_HOLD
    assert not provider.holding(GAME, _at(end))
    provider.poll(GAME, _at(end))  # still halftime: polled, and the hold isn't restarted
    assert not provider.holding(GAME, _at(end + timedelta(seconds=10)))
    third = provider.poll(GAME, _at(end + timedelta(seconds=10)))
    assert third.state is not None and third.state.period == 3


def test_catches_up_on_the_game_then_reads_each_matched_play_once() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1", "awaiting_play")),
        _scores(_synced("p2", "awaiting_play", clock="12:30")),
        _scores(_synced("p2", clock="12:30")),
        _scores(_synced("p2", clock="12:30")),
        _scores(_synced("p3", clock="12:00")),
        _scores(_synced("p3", clock="12:00")),
    ]
    feed.plays = [
        _page(_change("p0", 100, "C.Brown up the middle for 4 yards.", yards_gained=4), more=True, cursor="c1"),
        _page(
            _change("p1", 200, "Official Timeout at 08:56.", play_type="official_timeout"),
            {"play": "junk"},
            cursor=None,
        ),
        _page(
            _change("p2", 300, "S.Perine right guard for 2 yards.", yards_gained=2),
            _change("p2b", 310, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion"),
            cursor="c2",
        ),
        _page(cursor="c3"),  # p3 is matched, but its row never comes
    ]
    provider = _provider(feed)
    caught_up = provider.poll(GAME, _at(T0))
    assert [event.kind for event in caught_up.events] == [FootballEventKind.OFFICIAL_TIMEOUT]  # the latest only
    assert provider.poll(GAME, _at(T0)).events == ()  # awaiting: the row isn't there yet
    events = provider.poll(GAME, _at(T0)).events
    assert [event.summary.headline for event in events] == ["2 YARD RUN", "INCOMPLETE"]  # every play, in order
    assert provider.poll(GAME, _at(T0)).events == ()  # already read for p2
    provider.poll(GAME, _at(T0))
    provider.poll(GAME, _at(T0 + MISMATCH_RETRY))  # p3 isn't in a complete read: read again, until the retry ends
    provider.poll(GAME, _at(T0 + MISMATCH_RETRY))
    reads = [query for path, query in feed.requests if "/play-changes" in path]
    assert reads == [
        {"limit": 100},
        {"limit": 100, "cursor": "c1"},
        {"limit": 100, "cursor": "c1"},
        {"limit": 100, "cursor": "c2"},
        {"limit": 100, "cursor": "c3"},
    ]


def test_watches_the_feed_for_a_correction_when_the_scoreboard_moves_on_its_own() -> None:
    feed = FakeFeed()
    intercepted = "A.Rodgers pass deep left INTERCEPTED by J.Battle at CIN 36."
    reversed_text = (
        intercepted + "The Replay Official reviewed the interception ruling, and the play was REVERSED."
        "A.Rodgers pass incomplete deep left to D.Washington."
    )
    feed.scores = [
        _scores(_synced("p1")),
        _scores(_synced("p1", clock="8:29")),  # moved back with no new play
        _scores(_synced("p1", clock="8:29")),
        _scores(_synced("p1", clock="8:29")),
    ]
    feed.plays = [
        _page(_change("p1", 100, intercepted, play_type="pass_interception_return", interception=True)),
        _page(),
        _page(_change("p1", 100, reversed_text, revision=2, play_type="pass_incompletion")),
        _page(),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    assert provider.poll(GAME, _at(T0 + timedelta(seconds=10))).events == ()  # watching; nothing yet
    review = provider.poll(GAME, _at(T0 + timedelta(seconds=20))).events
    assert [(event.kind, event.summary.headline) for event in review] == [
        (FootballEventKind.REVIEW, "REVIEW: REVERSED")
    ]
    reads = feed.play_reads()
    provider.poll(GAME, _at(T0 + timedelta(seconds=30)))  # the correction came: the watch is over
    assert feed.play_reads() == reads


def test_each_play_entry_carries_when_the_feed_wrote_it_and_a_correction_when_the_correction_was() -> None:
    feed = FakeFeed()
    touchdown = "C.Brown up the middle for 2 yards, TOUCHDOWN."
    hurt = {"player_name": "Dalton Risner", "team_abbr": "CIN", "state": "out"}

    def written(change: dict[str, Any], second: int) -> dict[str, Any]:
        return {**change, "changed_at": (T0 + timedelta(seconds=second)).isoformat()}

    feed.scores = [_scores(_row()), _scores(_synced("p2", injuries=[hurt])), _scores(_synced("p3", injuries=[hurt]))]
    feed.plays = [
        _page(),
        _page(
            written(_change("p1", 1, touchdown, play_type="rushing_touchdown", touchdown=True), -200),
            written(_change("p1", 1, touchdown + " Spot moved.", revision=2, touchdown=True), -150),
            written(_change("p2", 2, "S.Perine right guard for 2 yards.", yards_gained=2), 5),
            _change("p2b", 3, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion"),  # undated
        ),
        _page(
            written(
                _change(
                    "p1", 1, touchdown + " E.McPherson extra point is GOOD.", revision=3, extra_point_result="good"
                ),
                14,
            ),
            written(_change("p3", 4, "C.Brown left end for 1 yard.", yards_gained=1), 15),
            written(_change("p3", 4, "C.Brown left end for 1 yard. Corrected.", revision=2, yards_gained=1), 18),
        ),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0 - timedelta(seconds=300)))  # caught up, with nothing yet
    plays = provider.poll(GAME, _at(T0 + timedelta(seconds=10))).events
    dates = [(event.play, event.written_at) for event in plays]
    assert dates == [
        (1, T0 - timedelta(seconds=200)),  # the earliest change to the play in the read
        (2, T0 + timedelta(seconds=5)),
        (3, None),  # the feed didn't say
        (None, None),  # an injury: no play row is behind it
    ]
    later = provider.poll(GAME, _at(T0 + timedelta(seconds=20))).events
    assert [(event.kind, event.play, event.written_at) for event in later] == [
        (FootballEventKind.CONVERSION, 1, T0 + timedelta(seconds=14)),  # the try, dated by its own change
        (FootballEventKind.PLAY, 4, T0 + timedelta(seconds=15)),
    ]


def test_a_clubs_play_by_play_code_counts_as_its_abbreviation_and_reads_as_one() -> None:
    feed = FakeFeed()
    baltimore = {"abbr": "BAL", "score": 0}
    feed.scores = [_scores(_row(away_team=baltimore)), _scores(_synced("p4", away_team=baltimore))]
    feed.plays = [
        _page(),
        _page(
            _change("p1", 1, "Timeout #1 by BLT at 12:10.", play_type="timeout"),
            _change("p2", 2, "Timeout #2 by BLT at 06:02.", play_type="timeout"),
            _change("p3", 3, "Timeout #3 by BLT at 01:47.", play_type="timeout"),
            _change("p4", 4, "A.Back up the middle to BLT 12 for 3 yards (C.D).", yards_gained=3),
        ),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    snapshot = provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    assert snapshot.state is not None and snapshot.state.timeouts == TeamTimeouts(away=0, home=3)
    assert [(event.summary.headline, event.summary.detail) for event in snapshot.events] == [
        ("TIMEOUT BAL", "BAL #1 at 12:10"),
        ("TIMEOUT BAL", "BAL #2 at 6:02"),
        ("TIMEOUT BAL", "BAL #3 at 1:47"),
        ("3 YARD RUN", "A.Back up the middle to BAL 12 for 3 yards (C.D)."),
    ]


def test_a_watch_that_finds_nothing_ends_on_its_own() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1")), _scores(_synced("p1", clock="8:29"))]
    feed.plays = [_page(_change("p1", 100, "C.Brown up the middle for 4 yards.")), _page()]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    reads = feed.play_reads()
    provider.poll(GAME, _at(T0 + timedelta(seconds=10) + CORRECTION_WATCH))
    assert feed.play_reads() == reads


def test_stops_paging_a_play_feed_that_never_ends() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p9"))]
    feed.plays = [_page(more=True, cursor=None)]
    _provider(feed).poll(GAME, _at(T0))
    assert feed.play_reads() == 30  # a first look reads the whole game so far, but not forever


def test_reads_a_play_row_into_the_typed_play() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [
        _page(
            _change(
                "p1",
                100,
                "E.McPherson 43 yard field goal is No Good.",
                play_type="field_goal_missed",
                field_goal_result="missed",
                extra_point_result=None,
                two_point_conv_result=7,
                posteam_abbr="CIN",
                yards_gained="lots",
            ),
            _change(5, "x", "not a play number"),
            _change(7, 50, "C.Brown up the middle for 4 yards."),  # a play, though its id isn't one we can keep
            {"source_play_id": "p2", "play": None},
        )
    ]
    events = _provider(feed).poll(GAME, _at(T0)).events
    assert [event.summary.headline for event in events] == ["FG NO GOOD"]


def test_an_injury_is_news_once_and_again_when_it_changes() -> None:
    feed = FakeFeed()
    risner = {"player_name": "Dalton Risner", "team_abbr": "CIN", "injury": "Knee", "state": "questionable_to_return"}
    old = {
        "player_name": "Old Hurt",
        "team_abbr": "PIT",
        "injury": None,
        "state": "out",
        "reported_at": "2026-11-16T01:30:00Z",
    }
    fresh = {**risner, "reported_at": "2026-11-16T01:55:00Z"}
    newcomer = {"player_name": "New Guy", "team_abbr": "WSH", "state": "carted_off"}
    feed.scores = [
        _scores(_row(injuries=[old, fresh, "junk", {"team_abbr": "PIT"}])),
        _scores(_row(injuries=[old, fresh])),
        _scores(_row(injuries=[old, {**fresh, "state": "out"}, newcomer])),
    ]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0))
    assert [(event.kind, event.summary.headline, event.summary.detail) for event in first.events] == [
        (FootballEventKind.INJURY, "INJURY CIN", "Dalton Risner (Knee), questionable to return")
    ]  # recent enough to be news on a first look; the older one isn't
    assert provider.poll(GAME, _at(T0)).events == ()
    changed = provider.poll(GAME, _at(T0)).events
    assert [event.summary.detail for event in changed] == ["Dalton Risner (Knee), out", "New Guy, being evaluated"]
    assert changed[1].summary.headline == "INJURY WAS"


def test_unknown_feed_words_read_as_their_catch_alls() -> None:
    assert PlaySyncStatus("whatever") is PlaySyncStatus.UNKNOWN
    assert InjuryStatus("carted_off") is InjuryStatus.OTHER
    assert InjuryStatus.RETURNED.words == "returned"


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("timed out"),
        {"data": []},
        _scores(_row(phase="halfway")),
        _scores(_row(kickoff_at="soon")),
        _scores(_row(kickoff_at="2026-11-16T01:20:00")),
        _scores(_row(kickoff_at=None)),
        _scores(_row(away_team={"abbr": "XYZ", "score": 0})),
    ],
)
def test_a_failed_or_unreadable_answer_is_a_provider_error(answer: Any) -> None:
    feed = FakeFeed()
    feed.scores = [answer]
    with pytest.raises(ProviderError):
        _provider(feed).poll(GAME, _at(T0))


def test_the_halftime_hold_waits_for_the_halfs_last_play() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1")),
        _scores(_synced("p2", "awaiting_play", status_detail="Halftime", clock="0:00")),
        _scores(_synced("p2", "awaiting_play", status_detail="Halftime", clock="0:00")),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    first = provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    assert not first.caught_up and not provider.holding(GAME, _at(T0 + timedelta(seconds=10)))
    provider.poll(GAME, _at(T0 + timedelta(seconds=10) + HALFTIME_GRACE))  # the play never came: hold anyway
    assert provider.holding(GAME, _at(T0 + timedelta(seconds=11) + HALFTIME_GRACE))


def test_halftime_gives_the_ball_to_the_second_halfs_receiver() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", status_detail="Halftime", clock="0:00"))]
    opening = "C.Boswell kicks 60 yards from PIT 35 to CIN 5. D.Meyers to CIN 24 for 19 yards (L.Nichols)."
    feed.plays = [_page(_change("p1", 1, opening, play_type="kickoff", quarter=1, posteam_abbr="CIN"))]
    snapshot = _provider(feed).poll(GAME, _at(T0))
    assert snapshot.caught_up
    assert snapshot.state is not None
    assert (snapshot.state.possession, snapshot.state.timeouts) == (HomeAway.AWAY, None)
    assert snapshot.state.challenges == TeamChallenges(away=2, home=2)


def test_regulations_end_leaves_the_ball_to_overtimes_toss() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_row(status_detail="End of 4th Quarter", period=4, clock="0:00"))]
    state = _provider(feed).poll(GAME, _at(T0)).state
    assert state is not None
    assert (state.interval, state.possession, state.down, state.timeouts) == (
        FootballInterval.END_OF_QUARTER,
        None,
        None,
        None,
    )


def test_a_kickoff_after_a_score_says_so_with_the_kicker_and_the_spot() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", situation=None, period=2))]
    touchdown = "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN. E.McPherson extra point is GOOD."
    feed.plays = [
        _page(
            _change(
                "p1",
                1,
                touchdown,
                play_type="passing_touchdown",
                touchdown=True,
                extra_point_result="good",
                posteam_abbr="CIN",
            )
        )
    ]
    state = _provider(feed).poll(GAME, _at(T0)).state
    assert state is not None
    assert (state.set_piece, state.possession, state.yards_to_goal) == (SetPiece.KICKOFF, HomeAway.HOME, 65)
    assert state.ball_on is not None and (state.ball_on.side, state.ball_on.yard_line) == (HomeAway.HOME, 35)


def test_only_a_matched_play_that_has_landed_counts_as_caught_up() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_synced("p1", status="unknown")),
        _scores(_synced("p1", status="stale")),
        _scores(_synced("p9", "awaiting_play")),
        _scores(_synced("p1")),
    ]
    feed.plays = [_page(_change("p1", 100, "C.Brown up the middle for 4 yards."))]
    provider = _provider(feed)
    assert [provider.poll(GAME, _at(T0)).caught_up for _ in range(4)] == [False, False, False, True]
    assert PlaySyncStatus("stale") is PlaySyncStatus.STALE


def _half_ended(written: datetime) -> FakeFeed:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0", status="unknown", status_detail="Halftime", clock="0:00"))]
    kneel = _change("p0", 100, "A.Rodgers kneels to PIT 21 for -1 yards.")
    feed.plays = [_page({**kneel, "changed_at": written.isoformat()}, {**kneel, "changed_at": "later"})]
    return feed


def test_a_board_starting_mid_break_counts_it_from_when_the_half_ended() -> None:
    written = T0 - timedelta(minutes=4)
    provider = _provider(_half_ended(written))
    snapshot = provider.poll(GAME, _at(T0))
    assert snapshot.break_ends_at == written + HALFTIME_HOLD  # not a whole new hold from the restart
    assert provider.holding(GAME, _at(T0 + timedelta(minutes=8)))
    assert not provider.holding(GAME, _at(written + HALFTIME_HOLD))
    long_over = _provider(_half_ended(T0 - HALFTIME_HOLD - timedelta(minutes=1)))
    long_over.poll(GAME, _at(T0))
    assert not long_over.holding(GAME, _at(T0 + timedelta(seconds=1)))  # the break is as good as over: poll on


def test_a_board_starting_mid_break_with_no_record_of_its_start_shows_no_countdown() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0", status="unknown", status_detail="Halftime", clock="0:00"))]
    provider = _provider(feed)
    assert provider.poll(GAME, _at(T0)).break_ends_at is None
    assert provider.holding(GAME, _at(T0 + timedelta(minutes=1)))
    feed.scores = [_scores(_row())]
    assert _provider(feed).poll(GAME, _at(T0)).break_ends_at is None


def test_a_down_line_left_behind_by_a_score_is_dropped_until_the_feed_writes_a_new_one() -> None:
    feed = FakeFeed()
    goal = {"possession_abbr": "CIN", "down": 1, "distance": 4, "text": "1st & Goal at PIT 4", "yardline_100": 4}
    feed.scores = [
        _scores(_row(situation=goal)),
        _scores(_row(situation=goal, home_team={"abbr": "CIN", "score": 16})),  # the touchdown; the line stays
        _scores(_row(situation=goal, home_team={"abbr": "CIN", "score": 17})),
        _scores(
            _row(
                situation={**goal, "text": "1st & 10 at PIT 25", "down": 1, "distance": 10, "yardline_100": 75},
                home_team={"abbr": "CIN", "score": 17},
            )
        ),
    ]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0)).state
    assert first is not None and first.down == 1
    for _ in range(2):
        stale = provider.poll(GAME, _at(T0)).state
        assert stale is not None and (stale.possession, stale.down, stale.yards_to_goal) == (None, None, None)
    fresh = provider.poll(GAME, _at(T0)).state
    assert fresh is not None and (fresh.down, fresh.distance) == (1, 10)


_TD = "J.Burrow pass short left to C.Brown for 2 yards, TOUCHDOWN."


def _scoring(play_id: str, number: int, **play: Any) -> dict[str, Any]:
    return _change(play_id, number, _TD, play_type="passing_touchdown", touchdown=True, posteam_abbr="CIN", **play)


def _home(points: int, play_id: str, status: str = "matched", **changes: Any) -> dict[str, Any]:
    return _synced(play_id, status, home_team={"abbr": "CIN", "score": points}, **changes)


def _headlines(snapshot: Any) -> list[str]:
    return [event.summary.headline for event in snapshot.events if event.kind is FootballEventKind.SCORE]


def test_a_score_the_feed_skips_is_shown_from_the_scoreboard_once_the_feed_moves_on() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_home(10, "p1")),  # 10 points no play explains: known before the board started
        _scores(_home(17, "p1")),  # a touchdown and its try, with no play behind them
        _scores(_home(17, "p2")),  # the feed moves on to a later play without them
    ]
    feed.plays = [
        _page(_change("p1", 100, "C.Brown up the middle for 4 yards.", yards_gained=4)),
        _page(),  # the correction watch finds nothing yet
        _page(_change("p2", 300, "Official Timeout at 04:12.", play_type="official_timeout")),
    ]
    provider = _provider(feed)
    assert _headlines(provider.poll(GAME, _at(T0))) == []
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(seconds=10)))) == []  # the play may yet come
    later = provider.poll(GAME, _at(T0 + timedelta(seconds=20)))
    assert _headlines(later) == ["CIN +7"]
    assert later.events[0].summary.detail == "Score update; play not reported"
    assert [event.kind for event in later.events][-1] is FootballEventKind.OFFICIAL_TIMEOUT  # the score comes first


def test_a_score_whose_play_arrives_is_not_shown_from_the_scoreboard() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_home(10, "p1")), _scores(_home(16, "p2", "awaiting_play")), _scores(_home(16, "p2"))]
    feed.plays = [_page(_change("p1", 100, "C.Brown up the middle for 4 yards.")), _page(_scoring("p2", 200))]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    arrived = provider.poll(GAME, _at(T0 + timedelta(seconds=20)))
    assert _headlines(arrived) == [] and [event.summary.headline for event in arrived.events] == ["TOUCHDOWN CIN"]


def _score_after_backstop(points: int) -> list[str]:
    feed = FakeFeed()
    feed.scores = [_scores(_home(10, "p1")), _scores(_home(10 + points, "p1"))]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(seconds=10)))) == []  # the gap opens
    shown = _headlines(provider.poll(GAME, _at(T0 + timedelta(seconds=10) + SCORE_BACKSTOP)))
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(minutes=30)))) == []  # and is shown once
    return shown


@pytest.mark.parametrize("points", [1, 2, 3, 4, 6, 7, 8])
def test_a_score_no_play_explains_says_only_who_scored_how_many(points: int) -> None:
    assert _score_after_backstop(points) == [f"CIN +{points}"]  # 6 could be a touchdown or two field goals


def test_a_try_owed_after_a_touchdown_is_left_to_its_own_row() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_home(16, "p1")), _scores(_home(17, "p1"))]
    feed.plays = [_page(_change("p0", 50, "D", yards_gained=4), _scoring("p1", 100))]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(minutes=30)))) == []


def test_plays_that_turn_up_after_their_score_was_shown_dont_open_a_new_gap() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_home(10, "p1")),
        _scores(_home(17, "p1")),
        _scores(_home(17, "p1")),
        _scores(_home(17, "p2")),
        _scores(_home(17, "p2")),
    ]
    feed.plays = [
        _page(_change("p1", 100, "D", yards_gained=4)),
        _page(),
        _page(_scoring("p2", 200, extra_point_result="good")),  # the skipped touchdown turns up, try and all
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    provider.poll(GAME, _at(T0 + timedelta(seconds=10)))
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(seconds=10) + SCORE_BACKSTOP))) == ["CIN +7"]
    late = provider.poll(GAME, _at(T0 + timedelta(minutes=5)))
    assert [event.summary.headline for event in late.events] == ["TOUCHDOWN CIN"]  # the real one, with its words
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(minutes=10)))) == []


def test_no_score_is_shown_until_the_plays_so_far_are_all_in() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_home(10, "p1")), _scores(_home(16, "p1"))]
    feed.plays = [_page(more=True, cursor="c")]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    assert _headlines(provider.poll(GAME, _at(T0 + timedelta(hours=1)))) == []


S10, S20, S30 = (timedelta(seconds=n) for n in (10, 20, 30))


def _reads(feed: FakeFeed) -> list[dict[str, Any]]:
    return [query for path, query in feed.requests if "/play-changes" in path]


def test_a_failed_play_read_is_retried_for_the_same_play() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0")), _scores(_synced("p1"))]
    feed.plays = [
        _page(_change("p0", 100, "C.Brown up the middle for 4 yards."), cursor="c1"),
        RuntimeError("timed out"),
        _page(_change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2), cursor="c2"),
        _page(cursor="c3"),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    failed = provider.poll(GAME, _at(T0 + S10))
    assert failed.state is not None and failed.events == () and not failed.caught_up  # the scoreboard still came
    assert [note.kind for note in failed.notes] == [FeedNoteKind.PLAY_READ_FAILED]
    retried = provider.poll(GAME, _at(T0 + S20))
    assert [event.summary.headline for event in retried.events] == ["2 YARD RUN"] and retried.caught_up
    provider.poll(GAME, _at(T0 + S30))  # read through for p1 now: not again
    assert _reads(feed) == [{"limit": 100}, {"limit": 100, "cursor": "c1"}, {"limit": 100, "cursor": "c1"}]


def test_a_read_that_fails_partway_keeps_its_pages_and_reads_on_from_there() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0")), _scores(_synced("p2"))]
    feed.plays = [
        _page(_change("p0", 100, "C.Brown up the middle for 4 yards."), cursor="c1"),
        _page(_change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2), more=True, cursor="c2"),
        RuntimeError("connection reset"),
        _page(_change("p2", 300, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion")),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    partway = provider.poll(GAME, _at(T0 + S10))
    assert [event.summary.headline for event in partway.events] == ["2 YARD RUN"]  # the page that came is kept
    assert not partway.caught_up  # but the rest of the feed isn't in
    rest = provider.poll(GAME, _at(T0 + S20))
    assert [event.summary.headline for event in rest.events] == ["INCOMPLETE"] and rest.caught_up
    assert _reads(feed)[1:] == [
        {"limit": 100, "cursor": "c1"},
        {"limit": 100, "cursor": "c2"},
        {"limit": 100, "cursor": "c2"},
    ]


def test_a_first_look_that_fails_partway_announces_nothing_until_the_game_is_all_in() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p2"))]
    feed.plays = [
        _page(_change("p0", 100, "C.Brown up the middle for 4 yards."), more=True, cursor="c1"),
        RuntimeError("connection reset"),
        _page(
            _change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2),
            _change("p2", 300, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion"),
        ),
    ]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0))
    assert first.events == () and not first.caught_up
    second = provider.poll(GAME, _at(T0 + S10))
    assert [event.summary.headline for event in second.events] == ["INCOMPLETE"]  # the latest, not a replay


def test_timeouts_and_challenges_stay_unknown_until_the_play_feed_is_all_in() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    feed.plays = [
        _page(_change("p0", 100, "C.Brown up the middle for 4 yards."), more=True, cursor="c1"),
        RuntimeError("connection reset"),
        _page(_change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2)),
    ]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0))
    assert first.state is not None and first.state.timeouts is None and first.state.challenges is None
    second = provider.poll(GAME, _at(T0 + S10))
    assert second.state is not None and second.state.timeouts is not None and second.state.challenges is not None


class _Gone(Exception):
    code = 410


def test_a_cursor_the_feed_refuses_starts_the_reading_over_without_replaying_the_game() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p0")), _scores(_synced("p1")), _scores(_synced("p2"))]
    run = _change("p0", 100, "C.Brown up the middle for 4 yards.")
    feed.plays = [
        _page(run, cursor="c1"),
        _Gone("cursor expired"),
        _page(
            run,
            _change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2),
            _change("p2", 300, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion"),
            cursor="c9",
        ),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    refused = provider.poll(GAME, _at(T0 + S10))
    assert refused.events == () and [note.kind for note in refused.notes] == [FeedNoteKind.CURSOR_RESET]
    again = provider.poll(GAME, _at(T0 + S20))
    assert [event.summary.headline for event in again.events] == ["INCOMPLETE"]  # the latest only
    assert _reads(feed) == [{"limit": 100}, {"limit": 100, "cursor": "c1"}, {"limit": 100}]


def _final_row(**changes: Any) -> dict[str, Any]:
    return _row(
        phase="post",
        status_detail="Final",
        period=4,
        clock="0:00",
        play_sync={"status": "unknown", "last_play_id": None},
        **changes,
    )


def test_the_final_drains_the_feed_for_its_last_plays_then_settles() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", period=4, clock="0:20", status_detail="0:20 - 4th")), _scores(_final_row())]
    feed.plays = [
        _page(_change("p1", 100, "M.Rudolph pass short right to L.Nichols for 1 yard.", quarter=4), cursor="c1"),
        _page(_change("p2", 200, "M.Rudolph pass incomplete.", play_type="pass_incompletion", quarter=4)),
        _page(_change("p3", 300, "END GAME", play_type="end_of_game", quarter=4)),
        _page(),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    final = provider.poll(GAME, _at(T0 + S10))
    assert [event.summary.headline for event in final.events] == ["INCOMPLETE"]  # read though nothing named it
    assert not final.caught_up and not final.settled
    ended = provider.poll(GAME, _at(T0 + S20))
    assert ended.caught_up and not ended.settled  # the feed wrote the game's end; fixes may still come
    assert provider.poll(GAME, _at(T0 + S10 + FINAL_DRAIN)).settled
    reads = feed.play_reads()
    provider.poll(GAME, _at(T0 + S20 + FINAL_DRAIN))
    assert feed.play_reads() == reads  # the drain is over


def test_halftime_drains_the_feed_until_its_hold_begins() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1")), _scores(_synced("p1", status="unknown", status_detail="Halftime"))]
    feed.plays = [
        _page(_change("p1", 100, "C.Brown up the middle for 4 yards."), cursor="c1"),
        _page(_change("p2", 200, "A.Rodgers kneels to PIT 21 for -1 yards.")),
        _page(),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    half = provider.poll(GAME, _at(T0 + S10))
    assert [event.summary.headline for event in half.events] == ["KNEEL"] and not half.caught_up
    assert not provider.holding(GAME, _at(T0 + S20))
    held = provider.poll(GAME, _at(T0 + S10 + HALFTIME_GRACE))
    assert provider.holding(GAME, _at(T0 + S20 + HALFTIME_GRACE))
    assert held.break_ends_at == T0 + S10 + HALFTIME_HOLD  # from when the break began, not when the drain ended


def test_a_play_first_seen_after_a_later_one_is_counted_but_not_shown() -> None:
    feed = FakeFeed()
    feed.scores = [
        _scores(_home(24, "p1")),
        _scores(_home(31, "p3")),
        _scores(_home(31, "p4")),
        _scores(_home(31, "p5")),
    ]
    kickoff = "E.McPherson kicks 60 yards from CIN 35 to PIT 5. L.Nichols to PIT 30 for 25 yards (R.Yeast)."
    feed.plays = [
        _page(_change("p1", 100, "C.Brown up the middle for 4 yards.", yards_gained=4), cursor="c1"),
        _page(_change("p3", 300, kickoff, play_type="kickoff", posteam_abbr="PIT"), cursor="c2"),
        _page(
            _scoring("p2", 200),
            _change("p4", 400, "A.Rodgers pass incomplete short right.", play_type="pass_incompletion"),
            cursor="c3",
        ),
        _page(
            _scoring("p2", 200, revision=2, extra_point_result="good"),
            _change("p5", 500, "C.Brown up the middle for 3 yards.", yards_gained=3),
            cursor="c4",
        ),
    ]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    kicked = provider.poll(GAME, _at(T0 + S10))
    assert [event.summary.headline for event in kicked.events] == ["KICKOFF"]  # the score's play may yet come
    late = provider.poll(GAME, _at(T0 + S20))
    # The touchdown turns up behind the kickoff: its points are the scoreboard's to show, not its words.
    assert [event.summary.headline for event in late.events] == ["CIN +7", "INCOMPLETE"]
    assert [(note.kind, note.detail.split(" | ")[0]) for note in late.notes] == [
        (FeedNoteKind.LATE_PLAY, "TOUCHDOWN CIN")
    ]
    fixed = provider.poll(GAME, _at(T0 + S30))
    assert [event.summary.headline for event in fixed.events] == ["3 YARD RUN"]  # no score again, and no try:
    assert [note.kind for note in fixed.notes] == [FeedNoteKind.LATE_PLAY]  # its try stays as unseen as it


def test_reading_the_play_feed_every_poll_finds_a_play_nothing_points_at() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", "awaiting_play"))]
    feed.plays = [
        _page(cursor="c1"),
        _page(_change("p1", 100, "C.Brown up the middle for 4 yards.", yards_gained=4), cursor="c2"),
    ]
    eager = _provider(feed, play_feed=PlayFeedPace.EVERY_POLL)
    eager.poll(GAME, _at(T0))
    assert [event.summary.headline for event in eager.poll(GAME, _at(T0 + S10)).events] == ["4 YARD RUN"]
    assert PlayFeedPace("every-poll") is PlayFeedPace.EVERY_POLL


def _overtime(period: int, week: int, status_detail: str = "10:00 - OT", **changes: Any) -> Any:
    feed = FakeFeed()
    feed.scores = [_scores(_row(period=period, week=week, status_detail=status_detail, **changes))]
    return _provider(feed).poll(GAME, _at(T0)).state


def test_overtime_shows_no_challenges_and_postseason_overtime_no_timeouts_yet() -> None:
    regular = _overtime(5, week=11)
    assert regular is not None and regular.challenges is None  # every overtime review is the booth's
    assert regular.timeouts == TeamTimeouts(away=2, home=2)
    postseason = _overtime(5, week=19)
    assert postseason is not None and (postseason.timeouts, postseason.challenges) == (None, None)


def test_only_regulations_end_waits_on_a_toss_and_a_later_overtime_period_carries_on() -> None:
    carried = _overtime(5, week=20, status_detail="End of 1st Overtime")
    assert carried is not None and carried.interval is FootballInterval.END_OF_QUARTER
    assert carried.possession is HomeAway.HOME  # the drive goes on into the next period
    second = _overtime(6, week=20, status_detail="End of 2nd Overtime")
    assert second is not None and second.interval is FootballInterval.END_OF_QUARTER  # no halftime in overtime
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", period=6, week=20, situation=None, status_detail="15:00 - 2nd OT"))]
    state = _provider(feed).poll(GAME, _at(T0)).state
    assert state is not None and state.set_piece is None  # no kickoff opens it


def _final_drain(feed: FakeFeed) -> NflMetaLiveProvider:
    """A game that goes final at +10 s, its play feed read through, then drained at 10 s polls to +120 s."""
    feed.scores = [_scores(_synced("p1", period=4, clock="0:03", status_detail="0:03 - 4th")), _scores(_final_row())]
    feed.plays = [_page(_change("p1", 100, "M.Rudolph pass incomplete.", quarter=4), cursor="c1"), _page(cursor="c1")]
    provider = _provider(feed)
    provider.poll(GAME, _at(T0))
    for second in range(10, 130, 10):
        assert not provider.poll(GAME, _at(T0 + timedelta(seconds=second))).settled
    return provider


def test_the_final_drain_ends_with_a_read_on_its_boundary_that_finds_a_late_decisive_row() -> None:
    feed = FakeFeed()
    provider = _final_drain(feed)
    walk_off = _change(
        "p2",
        200,
        "C.Boswell 45 yard field goal is GOOD, Center-C.Kuntz.",
        play_type="field_goal_good",
        field_goal_result="made",
        posteam_abbr="PIT",
        quarter=4,
    )
    end = _change("p3", 300, "END GAME", play_type="end_of_game", quarter=4)
    feed.plays = [_page(walk_off, end, cursor="c2")]  # its row lands at +129 s, the drain's 119th second
    last = provider.poll(GAME, _at(T0 + timedelta(seconds=10) + FINAL_DRAIN))  # on the drain's boundary
    assert [event.summary.headline for event in last.events] == ["FIELD GOAL GOOD"]
    assert last.settled and last.caught_up and last.notes == ()
    reads = feed.play_reads()
    assert provider.poll(GAME, _at(T0 + timedelta(seconds=140))).settled and feed.play_reads() == reads  # parked


def test_a_final_draining_says_how_long_is_left_and_a_last_read_without_the_games_end_notes_the_gap() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1", period=4, clock="0:03", status_detail="0:03 - 4th")), _scores(_final_row())]
    feed.plays = [_page(_change("p1", 100, "M.Rudolph pass incomplete.", quarter=4), cursor="c1"), _page(cursor="c1")]
    provider = _provider(feed)
    assert provider.poll(GAME, _at(T0)).drain_left is None  # still live
    assert provider.poll(GAME, _at(T0 + S10)).drain_left == FINAL_DRAIN  # final: the drain starts
    assert provider.poll(GAME, _at(T0 + S30)).drain_left == FINAL_DRAIN - S20
    last = provider.poll(GAME, _at(T0 + S10 + FINAL_DRAIN))  # its pages complete, and empty
    assert last.settled and last.drain_left is None and not last.caught_up
    assert [(note.kind, note.detail) for note in last.notes] == [
        (FeedNoteKind.FINAL_INCOMPLETE, "no end of game in the feed")
    ]


def test_the_halftime_hold_and_the_final_drain_run_their_length_whatever_the_wall_clock_does() -> None:
    feed = FakeFeed()
    halftime = _scores(_synced("p0", status_detail="Halftime", clock="0:00"))
    feed.scores = [_scores(_row()), halftime, _scores(_final_row())]
    feed.plays = [_page(_change("p0", 100, "A.Rodgers kneels to PIT 21 for -1 yards."))]
    provider = _provider(feed)
    clock = FakeClock(T0)
    provider.poll(GAME, clock.read())
    clock.advance(S10)
    assert provider.poll(GAME, clock.read()).break_ends_at == T0 + S10 + HALFTIME_HOLD  # dated on the wall clock
    clock.jump(-timedelta(minutes=10))  # the wall clock is set back ten minutes
    second = timedelta(seconds=1)
    clock.advance(HALFTIME_HOLD - second)
    assert provider.holding(GAME, clock.read())
    clock.advance(second)
    assert not provider.holding(GAME, clock.read())  # thirteen minutes after the break began, not 23
    provider.poll(GAME, clock.read())  # the final
    clock.jump(timedelta(hours=1))  # and now ahead an hour
    clock.advance(FINAL_DRAIN - second)
    assert not provider.poll(GAME, clock.read()).settled  # still draining
    clock.advance(second)
    assert provider.poll(GAME, clock.read()).settled  # two minutes after the final, by the monotonic clock


@pytest.mark.parametrize("answer", [RuntimeError("timed out"), _page(more=True, cursor="c9")])
def test_a_final_whose_last_read_doesnt_finish_notes_the_gap_then_settles(answer: Any) -> None:
    feed = FakeFeed()
    provider = _final_drain(feed)
    feed.plays = [answer]
    last = provider.poll(GAME, _at(T0 + timedelta(seconds=10) + FINAL_DRAIN))
    assert last.settled and [note.kind for note in last.notes][-1] is FeedNoteKind.FINAL_INCOMPLETE
    reads = feed.play_reads()
    provider.poll(GAME, _at(T0 + timedelta(seconds=140)))
    assert feed.play_reads() == reads  # parked all the same


def test_a_matched_play_a_complete_read_lacks_is_read_for_again_for_a_while() -> None:
    feed = FakeFeed()
    feed.scores = [_scores(_synced("p1"))]
    run = _change("p1", 200, "S.Perine right guard for 2 yards.", yards_gained=2)
    feed.plays = [_page(_change("p0", 100, "C.Brown up the middle for 4 yards."), cursor="c1"), _page(), _page(run)]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0))  # the scoreboard names p1; the feed, read to its end, doesn't have it
    assert [note.kind for note in first.notes] == [FeedNoteKind.PLAY_MISSING] and not first.caught_up
    assert provider.poll(GAME, _at(T0 + S10)).notes == ()  # read again: still not there
    found = provider.poll(GAME, _at(T0 + S20))  # read again: there it is
    assert [event.summary.headline for event in found.events] == ["2 YARD RUN"] and found.caught_up
    provider.poll(GAME, _at(T0 + S30))
    assert feed.play_reads() == 3  # read through for p1 now: not again

    feed = FakeFeed()
    feed.scores = [_scores(_synced("p9"))]
    provider = _provider(feed)
    for second in range(0, 30, 10):
        provider.poll(GAME, _at(T0 + timedelta(seconds=second)))
    gave_up = provider.poll(GAME, _at(T0 + MISMATCH_RETRY))
    assert [note.detail for note in gave_up.notes] == ["still not in the feed: waiting for the next play"]
    provider.poll(GAME, _at(T0 + MISMATCH_RETRY + S10))
    assert feed.play_reads() == 4  # three polls more for it, and then no more


def test_a_board_starting_mid_break_whose_first_read_fails_drains_before_its_hold() -> None:
    written = T0 - timedelta(minutes=4)
    feed = _half_ended(written)
    feed.plays = [RuntimeError("timed out"), *feed.plays]
    provider = _provider(feed)
    first = provider.poll(GAME, _at(T0))
    assert not provider.holding(GAME, _at(T0 + S10)) and first.break_ends_at is None  # a short drain first
    second = provider.poll(GAME, _at(T0 + S10))  # read through now
    assert provider.holding(GAME, _at(T0 + S20))
    assert second.break_ends_at == written + HALFTIME_HOLD  # the break began when the half ended
    stuck = _half_ended(written)
    stuck.plays = [RuntimeError("timed out")]
    failing = _provider(stuck)
    failing.poll(GAME, _at(T0))
    failing.poll(GAME, _at(T0 + S10))
    assert not failing.holding(GAME, _at(T0 + S20))
    late = failing.poll(GAME, _at(T0 + HALFTIME_GRACE))  # the grace is up: hold, with no countdown to show
    assert failing.holding(GAME, _at(T0 + HALFTIME_GRACE + S10)) and late.break_ends_at is None
