"""The mixed Sunday under `omni-board-policy/2`, with the user's favorites playing: `mlb:ATL`, `mlb:COL` and `nfl:CAR`.

It's the mixed day of `tests.test_replay_mixed` (every game and every fault of it: the midnight game both dates list,
Saturday's partial slate, NFL's outage from 11:55 to 11:59, the corrected final and the wall clock's step back at
15:00) with three games of the favorites added. All times are local, Mountain time:

- **NO@CAR** (NFL ``404``, the Panthers at home) kicks off at 11:00 beside DAL@PHI and MIN@GB. Its feed writes an
  official timeout's row at 11:24 (play resumes at 11:27:30), the first quarter ends at 11:38, a Carolina timeout's
  row at 11:52, halftime from 12:31 (the hold ending 12:44), the two-minute warning's row at 13:49, and Carolina's
  go-ahead touchdown at 13:55, 24-21. Final at 14:10.
- **NYM@ATL** (MLB ``776205``, the Braves at home) from 11:20: each half runs six minutes, then a two-minute inning
  break the feed states as 120 s, timed by the plate appearance that ended the half. Its answers say when they stood,
  so each break's onset is credible. A mound visit in the top of the 7th; the Braves' two-run homer in the bottom of
  the 8th wins it 3-2, final at 13:44.
- **COL@AZ** (MLB ``776206``, the Rockies away) from 12:40, its state every twelve minutes with an inning break the
  feed doesn't time in each, final 5-3 at 14:50.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omni.core.enum import GameStatus, HomeAway, League
from omni.domain.baseball import (
    BaseballAction,
    BaseballBaseState,
    BaseballBreakSignals,
    BaseballCount,
    BaseballFinishedPlay,
    BaseballGameState,
    BaseballStoppage,
    BaseballStoppageKind,
    InningPhase,
    scoring_impact,
)
from omni.domain.contest import TeamGame
from omni.domain.football import FootballGameState, FootballInterval, PlaySummary, PlayTone
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.events.football import FootballEvent, FootballEventKind
from omni.preferences.favorites import Favorites
from omni.providers.mlb_statsapi import _event_importance
from omni.replay.mixed import MLB_STATSAPI, MixedTimeline, SlateFrame
from omni.replay.timeline import GameFrame
from tests.test_replay_mixed import (
    _MLB,
    _NFL,
    SUN,
    _at,
    _half,
    _kickoff,
    _mlb_id,
    _nfl_id,
    _pass,
    _run,
    _snap,
    mixed_slate,
)

__all__ = ["ATL", "CAR", "COL", "FAVORITES", "mixed_slate_v2"]

FAVORITES = Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",)})
CAR, ATL, COL = _nfl_id("404"), _mlb_id("776205"), _mlb_id("776206")

_TOP, _MIDDLE, _BOTTOM, _END = InningPhase.TOP, InningPhase.MIDDLE, InningPhase.BOTTOM, InningPhase.END
_BREAK_AFTER = {_TOP: _MIDDLE, _BOTTOM: _END}
_HALF = timedelta(minutes=6)  # a half's play
_INNING_BREAK = timedelta(minutes=2)
_STATED = timedelta(seconds=120)  # the inning break the feed states: a regional broadcast's


@dataclass
class _Braves:
    """NYM@ATL's frames, half by half: each answer dated, each break timed by the plate appearance that ended it."""

    start: datetime
    frames: list[GameFrame] = field(default_factory=list)
    plays: list[BaseballGameEvent] = field(default_factory=list)
    at_bat: int = 0

    def team_game(self, status: GameStatus) -> TeamGame:
        return TeamGame(
            id=ATL,
            league=League.MLB,
            status=status,
            scheduled_start=self.start,
            provider_date=SUN,
            away=_MLB.resolve(121),
            home=_MLB.resolve(144),
        )

    def frame(self, at: datetime, state: BaseballGameState, status: GameStatus = GameStatus.LIVE) -> None:
        self.frames.append(
            GameFrame(at=at, game=self.team_game(status), state=state, events=tuple(self.plays), dated=True)
        )

    def build(self) -> list[GameFrame]:
        self.frames.append(GameFrame(at=self.start - timedelta(minutes=10), game=self.team_game(GameStatus.PREGAME)))
        away = home = 0
        hits = [0, 0]
        at = self.start
        for inning in range(1, 10):
            for phase in (_TOP, _BOTTOM):
                batting = 0 if phase is _TOP else 1
                last_pitch = BaseballAction(at_bat=self.at_bat + 1, pitch=1, at=at)
                self.frame(at, self._state(inning, phase, away, home, hits, outs=0, action=last_pitch))
                hits[batting] += 1
                self.at_bat += 2
                middle = at + _HALF / 2
                if inning == 7 and phase is _TOP:
                    visit = BaseballStoppage(
                        kind=BaseballStoppageKind.MOUND_VISIT, at_bat=self.at_bat, event=2, began=middle
                    )
                    self.frame(middle, self._state(inning, phase, away, home, hits, outs=1, stoppages=(visit,)))
                    resumed = BaseballAction(at_bat=self.at_bat, pitch=3, at=middle + timedelta(seconds=40))
                    self.frame(
                        middle + timedelta(seconds=40),
                        self._state(inning, phase, away, home, hits, outs=1, action=resumed),
                    )
                else:
                    self.frame(middle, self._state(inning, phase, away, home, hits, outs=1))
                if inning == 1 and phase is _TOP:
                    away = 1
                if inning == 4 and phase is _TOP:
                    away = 2
                if inning == 8 and phase is _BOTTOM:
                    home = 3
                    self._home_run(at + _HALF - timedelta(seconds=30), inning, phase, away, home, hits)
                ended = at + _HALF
                if inning == 9 and phase is _TOP:  # the Braves lead: no bottom of the 9th
                    final = self._state(inning, _TOP, away, home, hits, outs=3)
                    self.frame(ended, final, GameStatus.FINAL)
                    return self.frames
                done = BaseballFinishedPlay(at_bat=self.at_bat, inning=inning, phase=phase, ended=ended)
                self.frame(ended, self._state(inning, _BREAK_AFTER[phase], away, home, hits, outs=3, finished=done))
                at = ended + _INNING_BREAK
        raise AssertionError("the game ends in the 9th")  # pragma: no cover - the loop always returns

    def _home_run(self, at: datetime, inning: int, phase: InningPhase, away: int, home: int, hits: list[int]) -> None:
        impact = scoring_impact(phase=phase, inning=inning, rbi=2, away_score=away, home_score=home)
        hits[1] += 1
        self.plays.append(
            BaseballGameEvent(
                id=_mlb_id(f"776205:ab:{self.at_bat}"),
                contest=self.team_game(GameStatus.LIVE),
                event_type=BaseballGameEventType.HOME_RUN,
                source=MLB_STATSAPI,
                source_time=at,
                observed_at=at,
                importance=_event_importance(BaseballGameEventType.HOME_RUN, impact),
                payload=BaseballPlayPayload(
                    inning=inning,
                    phase=phase,
                    description="A synthetic two-run homer.",
                    rbi=2,
                    away_score=away,
                    home_score=home,
                    scoring_impact=impact,
                ),
            )
        )
        done = BaseballAction(at_bat=self.at_bat, at=at)
        self.frame(at, self._state(inning, phase, away, home, hits, outs=2, action=done))

    def _state(
        self,
        inning: int,
        phase: InningPhase,
        away: int,
        home: int,
        hits: list[int],
        *,
        outs: int,
        action: BaseballAction | None = None,
        finished: BaseballFinishedPlay | None = None,
        stoppages: tuple[BaseballStoppage, ...] = (),
    ) -> BaseballGameState:
        signals = BaseballBreakSignals(
            inning_break_length=_STATED, last_finished=finished, last_action=action, stoppages=stoppages
        )
        return BaseballGameState(
            away_score=away,
            home_score=home,
            inning=inning,
            phase=phase,
            count=BaseballCount(balls=0, strikes=1, outs=outs),
            bases=BaseballBaseState(),
            away_hits=hits[0],
            home_hits=hits[1],
            break_signals=signals,
        )


def _rockies() -> list[GameFrame]:
    """COL@AZ from 12:40: its state every twelve minutes, the last two of them an inning break the feed doesn't time
    (so its lease is provisional, its length the policy's for an unknown one), the next half's first pitch ending it.
    Final 5-3 at 14:50."""
    start = _at(SUN, 12, 40)

    def game(status: GameStatus) -> TeamGame:
        return TeamGame(
            id=COL,
            league=League.MLB,
            status=status,
            scheduled_start=start,
            provider_date=SUN,
            away=_MLB.resolve(115),
            home=_MLB.resolve(109),
        )

    def state(
        inning: int, phase: InningPhase, away: int, home: int, action: BaseballAction | None = None
    ) -> BaseballGameState:
        return BaseballGameState(
            away_score=away,
            home_score=home,
            inning=inning,
            phase=phase,
            count=BaseballCount(balls=1, strikes=0, outs=1),
            bases=BaseballBaseState(),
            away_hits=2 * inning,
            home_hits=2 * inning - 1,
            break_signals=BaseballBreakSignals(last_action=action),
        )

    lines = [(1, _TOP, 0, 0), (2, _BOTTOM, 1, 0), (3, _TOP, 1, 1), (4, _BOTTOM, 2, 2), (5, _TOP, 3, 2)]
    lines += [(6, _BOTTOM, 3, 3), (7, _TOP, 4, 3), (8, _BOTTOM, 5, 3), (9, _TOP, 5, 3)]
    frames = [GameFrame(at=start - timedelta(minutes=15), game=game(GameStatus.PREGAME))]
    for index, (inning, phase, away, home) in enumerate(lines):
        at = start + index * timedelta(minutes=12)
        pitch = BaseballAction(at_bat=4 * index + 1, pitch=1)
        frames.append(GameFrame(at=at, game=game(GameStatus.LIVE), state=state(inning, phase, away, home, pitch)))
        between = state(inning, _BREAK_AFTER[phase], away, home)
        frames.append(GameFrame(at=at + timedelta(minutes=10), game=game(GameStatus.LIVE), state=between))
    frames.append(GameFrame(at=_at(SUN, 14, 50), game=game(GameStatus.FINAL), state=state(9, _BOTTOM, 5, 3)))
    return frames


@dataclass
class _Panthers:
    """NO@CAR's frames: its slate entry, then each answer its feed gives, with the plays and stoppage rows written."""

    kickoff: datetime
    frames: list[SlateFrame] = field(default_factory=list)
    plays: int = 0

    def team_game(self, status: GameStatus) -> TeamGame:
        return TeamGame(
            id=CAR,
            league=League.NFL,
            status=status,
            scheduled_start=self.kickoff,
            provider_date=SUN,
            away=_NFL.resolve("NO"),
            home=_NFL.resolve("CAR"),
        )

    def live(
        self,
        at: datetime,
        state: FootballGameState,
        *rows: tuple[FootballEventKind, PlaySummary],
        break_ends_at: datetime | None = None,
    ) -> _Panthers:
        events = []
        for kind, summary in rows:
            self.plays += 1
            events.append(
                FootballEvent(key=f"404:{self.plays}", kind=kind, summary=summary, play=self.plays, written_at=at)
            )
        self.frames.append(
            SlateFrame(
                at=at,
                game=self.team_game(GameStatus.LIVE),
                state=state,
                plays=tuple(events),
                break_ends_at=break_ends_at,
            )
        )
        return self

    def build(self, listed: datetime) -> list[SlateFrame]:
        self.frames.append(SlateFrame(at=listed, game=self.team_game(GameStatus.SCHEDULED)))
        home, away = HomeAway.HOME, HomeAway.AWAY
        play = FootballEventKind.PLAY
        self.live(self.kickoff, _kickoff())
        drive = _snap(1, 520, 0, 0, away, 2, 7, 48)
        self.live(_at(SUN, 11, 12, 10), drive).live(_at(SUN, 11, 12, 40), drive, (play, _run(4)))
        stopped = _snap(1, 300, 0, 3, home, 1, 10, 66)
        self.live(_at(SUN, 11, 24), stopped, (FootballEventKind.OFFICIAL_TIMEOUT, _row("OFFICIAL TIMEOUT")))
        resumed = _snap(1, 290, 0, 3, home, 2, 4, 60)
        self.live(_at(SUN, 11, 27, 30), resumed, (play, _pass(6)))
        quarter = FootballGameState(period=1, clock_seconds=0, away_score=0, home_score=3, interval=_QUARTER)
        self.live(_at(SUN, 11, 38), quarter)
        second = _snap(2, 880, 0, 3, home, 1, 10, 40)
        self.live(_at(SUN, 11, 41), second, (play, _run(7)))
        called = _snap(2, 640, 7, 3, home, 3, 2, 22)
        self.live(_at(SUN, 11, 52), called, (FootballEventKind.TIMEOUT, _row("TIMEOUT CAR")))
        self.live(_at(SUN, 11, 54, 30), _snap(2, 630, 7, 3, home, 3, 2, 22), (play, _run(3)))
        self.live(_at(SUN, 12, 31), _half(7, 10), break_ends_at=_at(SUN, 12, 44))
        self.live(_at(SUN, 12, 46), _snap(3, 890, 7, 10, away, 1, 10, 70), (play, _pass(9)))
        self.live(_at(SUN, 13, 20), _snap(4, 600, 14, 17, away, 2, 5, 35), (play, _run(5)))
        warning = _snap(4, 120, 21, 17, home, 1, 10, 75)
        self.live(_at(SUN, 13, 49), warning, (FootballEventKind.TWO_MINUTE_WARNING, _row("TWO-MINUTE WARNING")))
        self.live(_at(SUN, 13, 51), _snap(4, 110, 21, 17, home, 2, 3, 68), (play, _pass(7)))
        scored = _kickoff(21, 24, period=4, clock=62)
        touchdown = PlaySummary(tone=PlayTone.SCORE, headline="TOUCHDOWN", detail="A synthetic go-ahead score.")
        self.live(_at(SUN, 13, 55), scored).live(_at(SUN, 13, 55, 30), scored, (play, touchdown))
        final = FootballGameState(period=4, clock_seconds=0, away_score=21, home_score=24, interval=_FINAL)
        self.frames.append(SlateFrame(at=_at(SUN, 14, 10), game=self.team_game(GameStatus.FINAL), state=final))
        return self.frames


_QUARTER, _FINAL = FootballInterval.END_OF_QUARTER, FootballInterval.FINAL


def _row(headline: str) -> PlaySummary:
    return PlaySummary(tone=PlayTone.STANDARD, headline=headline)


def mixed_slate_v2() -> MixedTimeline:
    """The mixed day with the favorites' three games added."""
    day = mixed_slate()
    braves = _Braves(_at(SUN, 11, 20)).build()
    panthers = _Panthers(_at(SUN, 11, 0)).build(day.start)
    return MixedTimeline(
        zone=day.zone,
        start=day.start,
        end=day.end,
        mlb=day.mlb + tuple(braves) + tuple(_rockies()),
        nfl=day.nfl + tuple(panthers),
        also_listed=day.also_listed,
        faults=day.faults,
    )
