"""A representative MLB evening for replaying the board: six games over 55 minutes, behind a 45 s delay.

What happens, in minutes from T0 (each shows on the board 45 s later):
- **col-lad** (a favorite): back-to-back COL home runs at 4:00 and 4:20, a LAD run at 15:00, a LAD
  walk-off home run at 30:00, FINAL at 30:15.
- **atl-nym** (a favorite): an ATL go-ahead double at 6:00; FINAL at 30:25, ten seconds after col-lad's.
- **bos-nyy:** pregame until first pitch, live from 20:30, a NYY home run at 35:00.
- **chc-stl:** a rain delay from 10:00 to 22:00, a CHC triple at 35:12.
- **sf-sd:** SD's no-hitter, broken by a single at 16:00.
- **hou-tex:** home runs at 35:05 and 35:20, so four big plays clear the delay within 20 s.
- **An outage** from 40:00 to 52:00: the schedule and every feed fail.

The state of each live game is refreshed every minute, and on each play.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from omni.core.enum import GameStatus, League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    InningPhase,
    PitchingDecisions,
    scoring_impact,
)
from omni.domain.contest import TeamGame
from omni.events.baseball import BaseballGameEvent, BaseballGameEventType, BaseballPlayPayload
from omni.providers.mlb_statsapi import _event_importance
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.preferences.favorites import Favorites
from omni.replay.timeline import GameFrame, Outage, Timeline

T0 = datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc)
END = T0 + timedelta(minutes=55)
LAG = DurationSeconds(45)
FAVORITES = Favorites({League.MLB: ("ATL", "COL")})
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")

_REG = MlbTeamRegistry.from_color_file()
_TOP, _BOTTOM = InningPhase.TOP, InningPhase.BOTTOM


@dataclass(frozen=True, slots=True)
class _Line:
    """A live game's situation: inning, half, score, hits, outs and runners."""

    inning: int
    phase: InningPhase
    away: int
    home: int
    away_hits: int = 5
    home_hits: int = 5
    outs: int = 0
    first: bool = False

    def state(self) -> BaseballGameState:
        return BaseballGameState(
            away_score=self.away,
            home_score=self.home,
            inning=self.inning,
            phase=self.phase,
            count=BaseballCount(balls=1, strikes=1, outs=self.outs),
            bases=BaseballBaseState(first=self.first),
            away_hits=self.away_hits,
            home_hits=self.home_hits,
        )


@dataclass
class _Game:
    """One game's frames, written in order: its status and state at each moment, and its plays so far."""

    raw: str
    away: int
    home: int
    start: datetime = T0
    frames: list[GameFrame] = field(default_factory=list)
    plays: list[BaseballGameEvent] = field(default_factory=list)
    line: _Line | None = None

    def team_game(self, status: GameStatus) -> TeamGame:
        return TeamGame(
            id=LeagueScopedId(League.MLB, SOURCE, self.raw),
            league=League.MLB,
            status=status,
            scheduled_start=self.start,
            away=_REG.resolve(self.away),
            home=_REG.resolve(self.home),
        )

    def pregame(self, second: int) -> None:
        self.frames.append(GameFrame(at=_at(second), game=self.team_game(GameStatus.PREGAME)))

    def delayed(self, second: int) -> None:
        self.frames.append(GameFrame(at=_at(second), game=self.team_game(GameStatus.DELAYED)))

    def live(self, second: int, line: _Line | None = None) -> None:
        self.line = line if line is not None else self.line
        assert self.line is not None
        self.frames.append(
            GameFrame(
                at=_at(second),
                game=self.team_game(GameStatus.LIVE),
                state=self.line.state(),
                events=tuple(self.plays),
            )
        )

    def every_minute(self, first: int, last: int) -> None:
        """A refreshed state each minute in [first, last), the outs turning over."""
        for second in range(first, last, 60):
            assert self.line is not None
            self.live(second, _replace_outs(self.line, (second // 60) % 3))

    def play(self, second: int, kind: BaseballGameEventType, line: _Line, description: str, *, rbi: int) -> None:
        """A play at ``second``, leaving the game at ``line``."""
        impact = scoring_impact(
            phase=line.phase, inning=line.inning, rbi=rbi, away_score=line.away, home_score=line.home
        )
        self.plays.append(
            BaseballGameEvent(
                id=LeagueScopedId(League.MLB, SOURCE, f"{self.raw}:ab:{len(self.plays) + 1}"),
                contest=self.team_game(GameStatus.LIVE),
                event_type=kind,
                source=SOURCE,
                source_time=_at(second),
                observed_at=_at(second),
                importance=_event_importance(kind, impact),
                payload=BaseballPlayPayload(
                    inning=line.inning,
                    phase=line.phase,
                    description=description,
                    rbi=rbi,
                    away_score=line.away,
                    home_score=line.home,
                    scoring_impact=impact,
                ),
            )
        )
        self.live(second, line)

    def final(self, second: int, line: _Line, decisions: PitchingDecisions) -> None:
        self.frames.append(
            GameFrame(
                at=_at(second),
                game=self.team_game(GameStatus.FINAL),
                state=line.state(),
                events=tuple(self.plays),
                decisions=decisions,
            )
        )


def _at(second: int) -> datetime:
    return T0 + timedelta(seconds=second)


def _replace_outs(line: _Line, outs: int) -> _Line:
    return _Line(line.inning, line.phase, line.away, line.home, line.away_hits, line.home_hits, outs, line.first)


_HR = BaseballGameEventType.HOME_RUN


def _col_lad() -> _Game:
    game = _Game("col-lad", away=115, home=119)
    game.live(0, _Line(7, _TOP, 2, 2, 6, 7))
    game.every_minute(60, 240)
    game.play(240, _HR, _Line(7, _TOP, 3, 2, 7, 7), "Tovar homers to left field.", rbi=1)
    game.play(260, _HR, _Line(7, _TOP, 4, 2, 8, 7), "Goodman homers to center field.", rbi=1)
    game.every_minute(300, 900)
    game.play(
        900, BaseballGameEventType.SINGLE, _Line(8, _BOTTOM, 4, 3, 8, 8, 1), "Betts singles; Ohtani scores.", rbi=1
    )
    game.every_minute(960, 1740)
    game.live(1740, _Line(9, _BOTTOM, 4, 3, 8, 9, 1, first=True))
    game.play(1800, _HR, _Line(9, _BOTTOM, 4, 5, 8, 10, 1), "Freeman homers to right field. Walk-off!", rbi=2)
    game.final(1815, _Line(9, _BOTTOM, 4, 5, 8, 10, 1), PitchingDecisions(winner="Evan Phillips", loser="Tyler Kinley"))
    return game


def _atl_nym() -> _Game:
    game = _Game("atl-nym", away=144, home=121)
    game.live(0, _Line(8, _TOP, 1, 1, 5, 4))
    game.every_minute(60, 360)
    game.play(360, BaseballGameEventType.DOUBLE, _Line(8, _TOP, 2, 1, 6, 4, 1), "Riley doubles; Olson scores.", rbi=1)
    game.every_minute(420, 1200)
    game.live(1200, _Line(9, _BOTTOM, 2, 1, 6, 5))
    game.every_minute(1260, 1825)
    game.final(
        1825,
        _Line(9, _BOTTOM, 2, 1, 6, 5, 3),
        PitchingDecisions(winner="Chris Sale", loser="Kodai Senga", save="Raisel Iglesias"),
    )
    return game


def _bos_nyy() -> _Game:
    game = _Game("bos-nyy", away=111, home=147, start=_at(1200))
    game.pregame(0)
    game.live(1230, _Line(1, _TOP, 0, 0, 1, 1))
    game.every_minute(1260, 2100)
    game.play(2100, _HR, _Line(2, _BOTTOM, 0, 2, 2, 3), "Judge homers to left field.", rbi=2)
    game.every_minute(2160, 3300)
    return game


def _chc_stl() -> _Game:
    game = _Game("chc-stl", away=112, home=138)
    game.live(0, _Line(3, _TOP, 1, 0, 2, 3))
    game.every_minute(60, 600)
    game.delayed(600)
    game.live(1320, _Line(3, _BOTTOM, 1, 0, 2, 3))
    game.every_minute(1380, 2112)
    game.play(2112, BaseballGameEventType.TRIPLE, _Line(4, _TOP, 2, 0, 3, 3), "Happ triples; Suzuki scores.", rbi=1)
    game.every_minute(2160, 3300)
    return game


def _sf_sd() -> _Game:
    game = _Game("sf-sd", away=137, home=135)
    game.live(0, _Line(7, _TOP, 0, 2, 0, 6))
    game.every_minute(60, 960)
    game.play(960, BaseballGameEventType.SINGLE, _Line(8, _TOP, 0, 2, 1, 6, 1), "Chapman singles.", rbi=0)
    game.every_minute(1020, 3300)
    return game


def _hou_tex() -> _Game:
    game = _Game("hou-tex", away=117, home=140)
    game.live(0, _Line(2, _TOP, 0, 0, 1, 1))
    game.every_minute(60, 2105)
    game.play(2105, _HR, _Line(3, _TOP, 1, 0, 2, 1), "Alvarez homers to right field.", rbi=1)
    game.play(2120, _HR, _Line(3, _BOTTOM, 1, 1, 2, 2), "Seager homers to center field.", rbi=1)
    game.every_minute(2160, 3300)
    return game


def slate() -> Timeline:
    """The evening, as a replay timeline."""
    games = (_col_lad(), _atl_nym(), _bos_nyy(), _chc_stl(), _sf_sd(), _hou_tex())
    frames = tuple(frame for game in games for frame in game.frames)
    return Timeline(frames=frames, outages=(Outage(start=_at(2400), end=_at(3120)),))
