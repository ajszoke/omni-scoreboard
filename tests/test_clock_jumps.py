"""Clock jumps through the board's real release seam: `build_loop`, its refresh, the pipeline and its holds.

Each held item's release is pinned when it's admitted, on the monotonic clock: a wall-clock jump afterward
neither releases it early nor holds it longer, and anything received while the wall clock is suspect waits
its full delay from receipt. The source's clock is right throughout; ours is the one that jumps.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omni.app.display import RecordingDisplaySink
from omni.app.loop import AppLoop, Refresh
from omni.app.runner import build_loop
from omni.core.clock import FakeClock
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, LiveBaseballFeed
from omni.providers.base import ProviderError, ProviderUpdate
from tests.test_app_loop import SOURCE, T, _game, _state
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
        """A feed answer for ``raw`` arrives at ``at`` seconds, saying what the schedule says of its game."""
        self._run_to(at)
        status = self.games[raw].status
        state = _state(away=away, home=home)
        self.answers[raw] = LiveBaseballFeed(state=state, events=events, status=status, received=self.clock.read())

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
