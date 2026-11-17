"""Clock jumps through the board's real release seam: `build_loop`, its refresh, the pipeline and its holds.

Each held item's release is pinned when it's admitted, on the monotonic clock: a wall-clock jump afterward
neither releases it early nor holds it longer, and anything received while the wall clock is suspect waits
its full delay from receipt. A play's age is told on the source's own clock, so a wall clock that has run off
since boot can't bring it forward either. The source's clock is right throughout; ours is the one that's off.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, Refresh
from omni.app.runner import build_loop
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.mlb_statsapi import MlbStatsApiProvider
from tests.test_app_loop import _REG, SOURCE, T, _game, _state
from tests.test_app_pipeline import _event

S = timedelta(seconds=1)
LAG = DurationSeconds(45)


class _Rig:
    """A slate and its games' feed answers, each answer received when the test says so, over a fake clock
    whose wall clock the test can jump."""

    source = SOURCE
    league = League.MLB

    def __init__(self, clock: FakeClock, *games: TeamGame) -> None:
        self.clock = clock
        self.start = clock.monotonic()
        self.games = {game.id.raw: game for game in games}
        self.answers: dict[str, LiveBaseballFeed] = {}
        self.failing: set[str] = set()
        self.loop: AppLoop = build_loop(
            self, self.feed, RecordingDisplaySink(PanelProfile.QUAD_128X64), broadcast_lag=LAG
        )

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=tuple(self.games.values()))

    def feed(self, game: TeamGame, now: datetime) -> LiveBaseballFeed:
        if game.id.raw in self.failing:
            raise ProviderError("game feed down")
        return self.answers[game.id.raw]

    def answer(
        self, raw: str, *, at: float, away: int = 0, home: int = 0, events: tuple[BaseballGameEvent, ...] = ()
    ) -> None:
        """A feed answer for ``raw`` arrives at ``at`` seconds, saying what the schedule says of its game. It
        stood as it arrived, by the source's clock."""
        self._run_to(at)
        status = self.games[raw].status
        state = _state(away=away, home=home)
        received, as_of = self.clock.read(), T + timedelta(seconds=at)
        self.answers[raw] = LiveBaseballFeed(state=state, events=events, status=status, received=received, as_of=as_of)

    def answer_from(self, raw: str, payload: dict[str, Any], *, at: float) -> None:
        """StatsAPI's ``payload`` for ``raw`` arrives at ``at`` seconds, read by the real provider."""
        self._run_to(at)
        provider = MlbStatsApiProvider(_REG, fetch_game=lambda game_pk: payload)
        feed = provider.fetch_live_feed(self.games[raw], now=self.clock.now())
        self.answers[raw] = dataclasses.replace(feed, received=self.clock.read())

    def at(self, seconds: float) -> Refresh:
        """A refresh at ``seconds``."""
        self._run_to(seconds)
        return self.loop.refresh(self.clock.read())

    def _run_to(self, seconds: float) -> None:
        """Let time run to ``seconds`` on the monotonic clock, the wall clock alongside."""
        self.clock.advance(timedelta(seconds=seconds) - self.clock.monotonic().since(self.start))


def _plays(refresh: Refresh) -> list[str]:
    return [card.raw for card in refresh.pipeline.big_plays]


def _cards(refresh: Refresh) -> list[str]:
    return sorted(
        card.raw for card in (*refresh.pipeline.ingested, *refresh.pipeline.big_plays, *refresh.pipeline.finals)
    )


def _released_and_aired(rig: _Rig, card: str, *, start: int, until: int) -> tuple[int | None, int | None]:
    """The first second from ``start`` through ``until`` at which big play ``card`` clears the delay, and the
    first at which the board paints it: a refresh and a paint each second."""
    released: int | None = None
    aired: int | None = None
    for second in range(start, until + 1):
        refresh = rig.at(second)
        if released is None and card in _plays(refresh):
            released = second
        painted = rig.loop.paint(rig.clock.read())
        if aired is None and painted.shown is not None and painted.shown.raw == card:
            aired = second
    return released, aired


def _stamp(second: float) -> str:
    """StatsAPI's `metaData.timeStamp` for ``second`` seconds on the source's clock."""
    return (T + timedelta(seconds=second)).strftime("%Y%m%d_%H%M%S")


def _iso(second: float) -> str:
    """A StatsAPI play time for ``second`` seconds on the source's clock."""
    return (T + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _statsapi(at: float, *plays: dict[str, Any], home: int = 0) -> dict[str, Any]:
    """StatsAPI's game feed as it stood at ``at`` seconds: the bottom of the 5th, and its plays so far."""
    return {
        "gameData": {"status": {"detailedState": "In Progress"}},
        "metaData": {"timeStamp": _stamp(at)},
        "liveData": {
            "linescore": {
                "currentInning": 5,
                "inningState": "Bottom",
                "teams": {"away": {"runs": 0, "hits": 3, "errors": 0}, "home": {"runs": home, "hits": 4, "errors": 0}},
            },
            "plays": {"allPlays": list(plays)},
        },
    }


def _home_run(*, started: float, ended: float | None) -> dict[str, Any]:
    """A home run whose at-bat started at ``started`` seconds and ended at ``ended``, if the feed says when."""
    about: dict[str, Any] = {"atBatIndex": 40, "halfInning": "bottom", "inning": 5, "startTime": _iso(started)}
    if ended is not None:
        about["endTime"] = _iso(ended)
    result = {"eventType": "home_run", "description": "homers", "rbi": 1, "awayScore": 0, "homeScore": 1}
    return {"atBatIndex": 40, "result": result, "about": about}


def test_a_plausible_source_time_releases_a_play_the_delay_after_it_happened() -> None:
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)  # first sight: plays from before it are the backlog
    rig.answer("g1", at=10, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))  # it reached us 5 s late
    rig.at(10)
    assert _plays(rig.at(49)) == []
    assert _plays(rig.at(50)) == ["g1:bigplay:g1:ab:1"]  # it happened at 5: 45 s on, not 45 s after receipt


def test_a_forward_wall_jump_while_holding_releases_nothing_early() -> None:
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"), _game("g2", GameStatus.FINAL))
    rig.answer("g1", at=0)
    rig.answer("g2", at=0, away=4, home=1)  # g2's feed says final at 0: its result shows at 45
    rig.at(0)
    rig.answer("g1", at=5, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))
    rig.at(5)  # held: g1's first state until 45, the home run and the state after it until 50
    clock.jump(60 * S)  # the wall clock jumps a minute ahead: every UTC deadline above now looks past
    assert _cards(rig.at(10)) == []  # nothing held is released early
    # Received while the wall clock is suspect, a play that looks plausible against it (62 s before receipt
    # on the jumped clock) still waits its full delay from receipt.
    plays = (_event("g1:ab:1", source_time=T + 5 * S), _event("g1:ab:2", source_time=T + 18 * S))
    rig.answer("g1", at=20, away=2, events=plays)
    assert _cards(rig.at(20)) == []
    assert _cards(rig.at(44)) == []
    assert _cards(rig.at(45)) == ["g1:live", "g2:final"]
    assert _cards(rig.at(50)) == ["g1:bigplay:g1:ab:1", "g1:live"]
    assert _cards(rig.at(64)) == []
    assert _cards(rig.at(65)) == ["g1:bigplay:g1:ab:2", "g1:live"]


def test_a_backward_wall_jump_holds_nothing_longer_and_brings_nothing_back() -> None:
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)
    rig.answer("g1", at=5, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))
    rig.at(5)
    clock.jump(-10 * 60 * S)  # ten minutes back: every UTC deadline above looks ten minutes off
    assert _cards(rig.at(10)) == []
    plays = (_event("g1:ab:1", source_time=T + 5 * S), _event("g1:ab:2", source_time=T + 18 * S))
    rig.answer("g1", at=20, away=2, events=plays)  # its play's time is ten minutes ahead of our wall clock now
    rig.at(20)
    assert _cards(rig.at(45)) == ["g1:live"]  # on time, not ten minutes late
    assert _cards(rig.at(50)) == ["g1:bigplay:g1:ab:1", "g1:live"]
    assert _cards(rig.at(64)) == []
    assert _cards(rig.at(65)) == ["g1:bigplay:g1:ab:2", "g1:live"]  # it waited from receipt
    rig.failing.add("g1")
    assert rig.at(70).pipeline.stale == (_game("g1").id,)  # its feed stops answering: stale from 70
    clock.jump(-10 * 60 * S)  # and the wall clock jumps back again
    assert rig.at(189).pipeline.removed == () and rig.loop._queue.card("g1:live") is not None
    rig.at(190)
    assert rig.loop._queue.card("g1:live") is None  # two minutes after it went stale, as measured, not twelve


def test_a_board_that_booted_without_the_right_time_waits_the_full_delay_from_receipt() -> None:
    # The board boots five months behind (no clock kept time while it was off); the source's times are right,
    # so each looks far in the future. Then the network sets the clock: a jump of five months.
    clock = FakeClock(T - timedelta(days=150))
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)
    rig.answer("g1", at=10, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))  # it happened just now
    rig.at(10)
    clock.set(T + 20 * S)  # the clock is set right
    assert _cards(rig.at(20)) == []
    assert _cards(rig.at(45)) == ["g1:live"]
    assert _plays(rig.at(54)) == []
    assert _plays(rig.at(55)) == ["g1:bigplay:g1:ab:1"]  # its receipt (10) + the delay: not months away, not early


@pytest.mark.parametrize("off, released, aired", [(60, 50, 53), (-60, 55, 55)])
def test_a_wall_clock_off_since_boot_never_airs_a_play_sooner_than_the_delay_after_it(
    off: int, released: int, aired: int
) -> None:
    # The board's wall clock has read a minute off since it booted, with no jump to notice. A home run happens at
    # 5 and the answer reporting it reaches the board at 10: by the source's clock, it is 5 s old. With our clock
    # ahead, it clears the delay 45 s after it happened, not at its receipt (and airs once the live card, up since
    # 45, has had its minimum turn). With our clock behind, the source's times look like our future: one clock is
    # wrong, so it waits the full delay from receipt, and no longer.
    clock = FakeClock(T + off * S)
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)
    rig.answer("g1", at=10, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))
    assert _released_and_aired(rig, "g1:bigplay:g1:ab:1", start=10, until=60) == (released, aired)


@pytest.mark.parametrize("jump", [120, -120])
def test_a_two_minute_wall_jump_while_a_play_is_held_moves_its_release_neither_way(jump: int) -> None:
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)
    rig.answer("g1", at=10, away=1, events=(_event("g1:ab:1", source_time=T + 5 * S),))
    rig.at(10)  # held: 5 s old by the source's clock, it may clear the delay at 50
    rig.at(20)
    clock.jump(jump * S)
    released, aired = _released_and_aired(rig, "g1:bigplay:g1:ab:1", start=21, until=70)
    assert released == 50 and aired == 53  # as without the jump: the live card, up since 45, has its minimum turn


@pytest.mark.parametrize("ended, aired", [(90, 135), (None, 145)])
def test_a_play_without_its_end_time_waits_the_full_delay_from_receipt(ended: int | None, aired: int) -> None:
    # A 90 s at-bat starts at 0 and ends at 90 with a home run; the answer reporting it, as of 100, reaches the board
    # then. With its end time the play airs 45 s after it. Without one it waits the full delay from receipt: dated
    # by its at-bat's start, it would have aired at once.
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"))
    rig.answer_from("g1", _statsapi(0), at=0)
    rig.at(0)
    rig.answer_from("g1", _statsapi(100, _home_run(started=0, ended=ended), home=1), at=100)
    assert _released_and_aired(rig, "g1:bigplay:g1:ab:40", start=100, until=150) == (aired, aired)


@pytest.mark.parametrize("jump, aired", [(0, 30), (60, 75)])
def test_a_late_play_received_while_the_wall_clock_is_suspect_waits_the_full_delay_from_receipt(
    jump: int, aired: int
) -> None:
    # At 30 an answer reports a play that happened 50 s before it: past its 45 s delay already, so on a trusted
    # clock it airs at once. Received ten seconds after the wall clock jumped a minute ahead, it waits the full
    # delay from receipt, though its time looks plausible against the jumped clock.
    clock = FakeClock(T)
    rig = _Rig(clock, _game("g1"))
    rig.answer("g1", at=0)
    rig.at(0)
    rig.at(20)
    clock.jump(jump * S)
    rig.at(21)
    rig.answer("g1", at=30, away=1, events=(_event("g1:ab:1", source_time=T - 20 * S),))
    assert _released_and_aired(rig, "g1:bigplay:g1:ab:1", start=30, until=80) == (aired, aired)
