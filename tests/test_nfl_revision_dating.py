"""NFLMeta's revised plays through the shared NFL pipeline: an entry's hold is shortened only by the write of the
revision it announces.

Each test reads a game through the real NFLMeta provider over a fake feed, hands its answers to
`LiveFootballPipeline` as the shared board does, and watches when the entry's caption clears the 45 s delay.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from omni.app.nfl_feeds import NflReading
from omni.core.clock import ClockReading
from omni.core.time import DurationSeconds
from omni.domain.contest import TeamGame
from omni.providers.nflmeta import NflLiveSnapshot
from omni.queue.delay_policy import Arrival, DelayPolicy
from tests.test_app_nfl_pipeline import LAG, LONG, Board, T
from tests.test_nflmeta import FakeFeed, _change, _page, _provider, _row, _scores, _synced

GAME = 22920  # PIT at CIN
_TOUCHDOWN = "J.Burrow pass short right to J.Chase for 12 yards, TOUCHDOWN."


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _written(change: dict[str, Any], seconds: float) -> dict[str, Any]:
    return {**change, "changed_at": _iso(T + timedelta(seconds=seconds))}


def _touchdown(revision: int, extra: str = "", **play: Any) -> dict[str, Any]:
    fields = {"play_type": "pass_reception", "posteam_abbr": "CIN", "touchdown": True, **play}
    return _change("p1", 1, _TOUCHDOWN + extra, revision=revision, **fields)


class _Shared:
    """The provider reading the game, and the shared pipeline its answers go to."""

    def __init__(self, feed: FakeFeed) -> None:
        self.provider = _provider(feed)
        self.board = Board()
        self.game: TeamGame | None = None
        self.read_at: list[tuple[ClockReading, NflLiveSnapshot]] = []

    def read(self, seconds: float) -> None:
        """A poll at ``seconds`` past ``T``, handed to the pipeline as it arrived."""
        reading = self.board.to(seconds)
        snapshot = self.provider.poll(GAME, reading)
        self.game = snapshot.game
        self.read_at.append((reading, snapshot))
        answer = NflReading(snapshot, reading, LONG)
        self.board.feeds.waiting[snapshot.game.id].append(answer)
        self.board.feeds.newest[snapshot.game.id] = answer
        self.board.refresh(snapshot.game)

    def caption(self, seconds: float) -> str | None:
        assert self.game is not None
        self.board.at(seconds, self.game)
        card = self.board.card(self.game)
        return None if card is None else self.board.caption(self.game)


def _scored(observed: float, cin: int) -> dict[str, Any]:
    score = {"abbr": "CIN", "score": cin}
    return _synced("p1", home_team=score, observed_at=_iso(T + timedelta(seconds=observed)))


def test_a_touchdown_read_with_its_try_waits_the_full_delay_not_its_snap_s_age() -> None:
    """The snap was written 100 s before the read at 50 and its try 2 s before: the entry announces the try's
    revision, so it can't take the snap's 100 s off its hold. It clears the delay at 95, never at 50."""
    feed = FakeFeed()
    feed.scores = [
        _scores(_row(home_team={"abbr": "CIN", "score": 0}, observed_at=_iso(T))),
        _scores(_scored(50, 7)),
    ]
    feed.plays = [
        _page(),
        _page(
            _written(_touchdown(1), -50),
            _written(_touchdown(2, " The extra point is GOOD.", extra_point_result="good"), 48),
        ),
    ]
    shared = _Shared(feed)
    shared.read(0)
    assert shared.caption(45) is None  # the field is up, with no caption
    shared.read(50)
    assert shared.caption(94.9) is None
    assert shared.caption(95) is not None


def test_a_try_read_on_its_own_is_dated_by_its_own_write() -> None:
    """The touchdown came in an earlier read; the try's change, written 10 s before the read at 60, is the only one
    of its play there, so it's dated by it: released 10 s sooner than the full delay. Its caption still waits for a
    field that agrees with it, its own answer's, which has no occurrence to date it and clears the full delay."""
    feed = FakeFeed()
    feed.scores = [
        _scores(_row(home_team={"abbr": "CIN", "score": 0}, observed_at=_iso(T))),
        _scores(_scored(30, 6)),
        _scores(_scored(60, 7)),
    ]
    feed.plays = [
        _page(),
        _page(_written(_touchdown(1), 28)),
        _page(_written(_touchdown(2, " The extra point is GOOD.", extra_point_result="good"), 50)),
    ]
    shared = _Shared(feed)
    for seconds in (0, 30, 60):
        shared.read(seconds)
    policy = DelayPolicy(broadcast_lag=DurationSeconds(LAG))
    releases = [
        policy.release(Arrival(received=reading, occurred=event.written_at, as_of=snapshot.observed_at), reading)
        for reading, snapshot in shared.read_at
        for event in snapshot.events
    ]
    assert [release.seconds for release in releases] == [73.0, 95.0]  # 2 s old at 30; the try 10 s old at 60
    assert shared.caption(74.9) is None
    touchdown = shared.caption(75)  # with the field its answer showed
    assert touchdown is not None
    assert shared.caption(94.9) == touchdown
    assert shared.caption(95) is None  # the try is out, but not the field that scored it: the field alone
    assert shared.caption(105) not in (None, touchdown)  # the try, beside the field that scored it
