"""Draw the NBA cards' sixth design pass: native mock cards on all three profiles, and contact sheets.

    python tools/nba_card_study.py --out <dir> [--sample <BALLDONTLIE sample json>]

A design study, not a renderer. It draws with the repo's own drawing stack (`PillowCanvas`, the patched BDF
faces, the matchup treatment and logo store, the win meter, text lanes) from the small typed mock inputs
defined here, so what the sheets show is what the board's own pixels would be. It writes
``native/<profile>/<card>.png`` (1 LED = 1 px) and ``nba-v6-contact-<profile>.png`` (enlarged, with a faint
pixel grid, a title per card and whether the card's data is recorded or synthetic) under ``--out``.

A live card's 12 s turn has two 6 s halves: the first names each team (its code over its record), the second
shows what each team has left (its timeouts over the bonus). The sheets set each pair side by side.

The recorded specimens were transcribed from a BALLDONTLIE Free recording of the 2026-11-16 slate. Given
``--sample``, the tool checks each one against the state it names before drawing anything. Player names,
stat lines, plus-minus values, records, odds and every win-probability value are synthetic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum
from pathlib import Path
from typing import Any, assert_never
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw, ImageFont

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.core.colors import RGBColor  # noqa: E402
from omni.core.enum import HomeAway, PanelProfile  # noqa: E402
from omni.domain.baseball import WinProbability  # noqa: E402
from omni.domain.teams import BasketballTeam  # noqa: E402
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry  # noqa: E402
from omni.renderers.canvas import Canvas  # noqa: E402
from omni.renderers.context import RenderContext  # noqa: E402
from omni.renderers.font import rasterize  # noqa: E402
from omni.renderers.image import LogoStore  # noqa: E402
from omni.renderers.pillow_canvas import PillowCanvas  # noqa: E402
from omni.renderers.pregame import start_time_label  # noqa: E402
from omni.renderers.team_row import draw_matchup_marks, draw_single_bars, draw_treated_mark  # noqa: E402
from omni.renderers.text import draw_centered, draw_right_aligned, text_width  # noqa: E402
from omni.renderers.text_lane import LaneAnchor, TextLane, draw_text_lane  # noqa: E402
from omni.renderers.visual_treatment import MatchupVisualTreatment, resolve_matchup_treatment  # noqa: E402
from omni.renderers.win_meter import draw_win_meter  # noqa: E402

_PASS = "v6"

# --- colors: the house set, the MLB pregame's odds and stats colors, and NFL's play tones ---------------

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_GOLD = RGBColor(255, 215, 0)
_DIM_GOLD = RGBColor(110, 92, 0)  # an unbegun quarter's pip ring, and a final's losing record
_SECONDARY = RGBColor(180, 180, 180)
_LOSER = RGBColor(110, 110, 110)
_DIVIDER = RGBColor(55, 55, 55)
_RED = RGBColor(255, 50, 50)  # NFL's turnover red, here also the bonus box and a minus
_GREEN = RGBColor(60, 220, 90)  # NFL's scoring-play green, here also a plus
_ODDS_GREEN = RGBColor(50, 180, 50)  # the MLB pregame's odds
_STATS_TEAL = RGBColor(50, 180, 180)  # the MLB pregame's stat lines

_REGULATION = 4
_HALFTIME = timedelta(minutes=15)
_ZONE = ZoneInfo("America/Denver")
_TIMES = "×"  # the multiplication sign every face used here carries


# --- typed mock inputs ------------------------------------------------------------------------------------


class Phase(Enum):
    """Where the game stands, as far as a card is concerned."""

    PREGAME = "pregame"
    POSTPONED = "postponed"
    LIVE = "live"
    END_PERIOD = "end_period"
    HALFTIME = "halftime"
    FINAL = "final"


class Half(Enum):
    """Which 6 s of a live card's 12 s turn a frame is: the teams named, or what they have left."""

    NAMES = "names"  # the code over the record
    COUNTS = "counts"  # the timeouts over the bonus


class Tone(Enum):
    """How a caption reads at a glance; its headline takes the tone's color, as on the NFL strip."""

    SCORE = "score"
    ALERT = "alert"
    TURNOVER = "turnover"
    STANDARD = "standard"


_TONE_COLOR = {Tone.SCORE: _GREEN, Tone.ALERT: _GOLD, Tone.TURNOVER: _RED, Tone.STANDARD: _WHITE}


class Bottom(Enum):
    """What the panel under the divider shows."""

    LINE_SCORE = "line_score"
    CAPTION = "caption"
    BOX_SCORE = "box_score"
    PLAYER_LINES = "player_lines"


class TimeoutLength(Enum):
    """How long a timeout's break should run, for break planning only: no caption says which.

    The first and second timeouts of a regulation quarter are the long ones (play resumes about 3.5 minutes
    after the clock stopped); the third and later, and every overtime timeout, are short (1.5 to 2 minutes).
    The data can't tell a called timeout from a mandatory one charged to a team, so the caption never says TV.
    """

    LONG = "long"
    SHORT = "short"


def timeout_length(period: int, nth_in_period: int) -> TimeoutLength:
    """The length class of the period's ``nth_in_period`` timeout (1 for the first)."""
    if period <= _REGULATION and nth_in_period <= 2:
        return TimeoutLength.LONG
    return TimeoutLength.SHORT


class CallChange(Enum):
    """How a call taken back shows in one player's line."""

    FOUL_OFF = "foul_off"  # -1 PF
    FOUL_TO_BLOCK = "foul_to_block"  # +1 BLK, -1 PF
    OFFENSIVE_FOUL_OFF = "offensive_foul_off"  # -1 TOV, -1 PF


# The quad's detail line for each change, and the short words the stack puts over the player's name.
_CHANGE_DETAIL = {
    CallChange.FOUL_OFF: "FOUL OFF {name}",
    CallChange.FOUL_TO_BLOCK: "BLOCK, NOT FOUL: {name}",
    CallChange.OFFENSIVE_FOUL_OFF: "OFFENSIVE FOUL OFF: {name}",
}
_CHANGE_WORDS = {
    CallChange.FOUL_OFF: "FOUL OFF",
    CallChange.FOUL_TO_BLOCK: "BLOCK, NOT FOUL",
    CallChange.OFFENSIVE_FOUL_OFF: "OFFENSIVE FOUL OFF",
}


@dataclass(frozen=True, slots=True)
class Record:
    """A club's wins and losses."""

    wins: int
    losses: int

    def text(self) -> str:
        return f"{self.wins}-{self.losses}"


@dataclass(frozen=True, slots=True)
class Ranks:
    """Where a club ranks in its conference and its division."""

    conference: int
    division: int


@dataclass(frozen=True, slots=True)
class BettingLine:
    """The favorite's moneyline and point spread."""

    favorite: HomeAway
    moneyline: int
    spread: float


@dataclass(frozen=True, slots=True)
class Caption:
    """One play or ruling, put into a board's words: a headline in the play's tone over a detail line.

    ``halves`` is the stack's wording when the quad's lines don't fit its 16-character lane: one
    (headline, detail) pair for each 6 s half of the turn.
    """

    tone: Tone
    headline: str
    detail: str
    team: HomeAway | None  # whose play it was; None when the data can't say
    halves: tuple[tuple[str, str], ...] = ()


def timeout_caption(team: HomeAway, code: str, left: int) -> Caption:
    """A team's count dropped by one: ``TIMEOUT POR`` over ``4 LEFT``, whatever its length class."""
    return Caption(Tone.ALERT, f"TIMEOUT {code}", f"{left} LEFT", team)


def challenge_caption(team: HomeAway, code: str, change: tuple[CallChange, str] | None) -> Caption:
    """A team's count came back with the clock still stopped: its challenge won, and the call reversed."""
    headline = f"{code} CHALLENGE WON"
    first = (f"{code} CHALLENGE", "CALL REVERSED")
    if change is None:
        return Caption(Tone.ALERT, headline, "CALL REVERSED", team, halves=(first, first))
    kind, name = change
    detail = _CHANGE_DETAIL[kind].format(name=name)
    return Caption(Tone.ALERT, headline, detail, team, halves=(first, (_CHANGE_WORDS[kind], name)))


def reversal_caption(kind: CallChange, name: str) -> Caption:
    """A player's line took a call back with no challenge signal; nobody is named as having asked."""
    detail = _CHANGE_DETAIL[kind].format(name=name)
    halves = (("CALL REVERSED", _CHANGE_WORDS[kind]), ("CALL REVERSED", name))
    return Caption(Tone.ALERT, "CALL REVERSED", detail, None, halves=halves)


@dataclass(frozen=True, slots=True)
class PlayerLine:
    """One player's box-score line, as BALLDONTLIE's stat rows carry it."""

    name: str
    plus_minus: int
    fgm: int
    fga: int
    tpm: int  # threes made
    tpa: int
    ftm: int
    fta: int
    oreb: int
    dreb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int

    @property
    def points(self) -> int:
        return 2 * self.fgm + self.tpm + self.ftm

    @property
    def rebounds(self) -> int:
        return self.oreb + self.dreb

    def plus_minus_text(self) -> str:
        return f"{self.plus_minus:+d}" if self.plus_minus else "0"

    def shooting(self) -> str:
        """Makes over attempts, threes first; a player who hasn't tried a three shows only his field goals."""
        field_goals = f"{self.fgm}/{self.fga} FG"
        return f"{self.tpm}/{self.tpa} 3PT {field_goals}" if self.tpa else field_goals

    def stats(self) -> str:
        """Points, then shooting as makes over attempts: ``16 PTS 2/5 3PT 6/11 FG``."""
        return f"{self.points} PTS {self.shooting()}"

    def counting(self) -> tuple[str, ...]:
        """The line's other numbers worth a mention, biggest first: ``11 AST``, ``8 REB``, ``3 BLK``.

        Rebounds and assists count from one; blocks and steals from two. A tie keeps that order.
        """
        found = [(self.rebounds, "REB", 1), (self.ast, "AST", 1), (self.blk, "BLK", 2), (self.stl, "STL", 2)]
        kept = [(value, label) for value, label, least in found if value >= least]
        return tuple(f"{value} {label}" for value, label in sorted(kept, key=lambda item: -item[0]))


@dataclass(frozen=True, slots=True)
class TeamBox:
    """A team's box-score line, summed from its players."""

    fg_pct: int
    threes_made: int
    threes_attempted: int
    rebounds: int
    assists: int
    turnovers: int

    @classmethod
    def of(cls, players: Sequence[PlayerLine]) -> TeamBox:
        fga = sum(p.fga for p in players)
        return cls(
            fg_pct=round(100 * sum(p.fgm for p in players) / fga) if fga else 0,
            threes_made=sum(p.tpm for p in players),
            threes_attempted=sum(p.tpa for p in players),
            rebounds=sum(p.rebounds for p in players),
            assists=sum(p.ast for p in players),
            turnovers=sum(p.tov for p in players),
        )


@dataclass(frozen=True, slots=True)
class ScoringRun:
    """An unanswered run: who, how many points, and the game-clock span it has covered so far."""

    team: HomeAway
    points: int
    span_seconds: int


@dataclass(frozen=True, slots=True)
class GameClock:
    """The sampled game clock, in tenths of a second left in the period."""

    tenths: int

    def text(self) -> str:
        """``10:13`` at a minute or more; ``:48.2`` under one, with the tenths the feed reports."""
        seconds, tenth = divmod(self.tenths, 10)
        if seconds < 60:
            return f":{seconds:02d}.{tenth}"
        return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True, slots=True)
class Side:
    """One club's half of a game: its score, timeouts, bonus and points by period (Q1 first, then OTs)."""

    abbreviation: str
    score: int | None = None
    timeouts: int | None = None
    in_bonus: bool = False
    periods: tuple[int, ...] = ()
    record: Record | None = None  # as of tip; a final's includes the result
    ranks: Ranks | None = None
    players: tuple[PlayerLine, ...] = ()

    def by_plus_minus(self) -> tuple[PlayerLine, ...]:
        """The players in plus-minus order, points breaking a tie: the player-lines panel walks it, and a
        final's best player heads it."""
        return tuple(sorted(self.players, key=lambda p: (-p.plus_minus, -p.points)))

    def best(self) -> PlayerLine | None:
        """The player with the best plus-minus (the higher points on a tie), if any are known."""
        order = self.by_plus_minus()
        return order[0] if order else None


@dataclass(frozen=True, slots=True)
class Game:
    """Everything a mock card draws from."""

    away: Side
    home: Side
    phase: Phase
    period: int = 0
    clock: GameClock | None = None
    home_win_pct: float | None = None  # the market's reading for the home side
    tip: datetime | None = None
    line: BettingLine | None = None
    run: ScoringRun | None = None
    bottom: Bottom = Bottom.LINE_SCORE
    caption: Caption | None = None
    caption_of: tuple[int, int] | None = None  # (i, n) while more than one play is waiting
    break_ends: datetime | None = None
    timeout: HomeAway | None = None  # whose timeout the game is stopped for
    rank: int = 0  # which pair of players the player-lines panel shows, 0 for the top of each team's order

    def side(self, which: HomeAway) -> Side:
        return self.away if which is HomeAway.AWAY else self.home


@dataclass(frozen=True, slots=True)
class StateRef:
    """A state in the recording: its game and state index (both zero-based)."""

    game: int
    state: int


_ALL = frozenset(PanelProfile)
_NO_SINGLE = frozenset({PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64})
_STACK_ONLY = frozenset({PanelProfile.STACK_64X64})
_NO_STACK = frozenset({PanelProfile.QUAD_128X64, PanelProfile.SINGLE_64X32})


@dataclass(frozen=True, slots=True)
class Specimen:
    """One mock card: its file name, its title and provenance on the sheet, and what it draws."""

    key: str
    title: str
    provenance: str
    synthetic: bool
    game: Game
    now: datetime
    source: StateRef | None = None
    scroll_s: float = 0.0  # seconds into a scrolling line's cycle, for a frame mid-scroll
    profiles: frozenset[PanelProfile] = field(default=_ALL)
    half: Half = Half.NAMES
    pair: str = ""  # frames sharing a pair key sit side by side on the sheet


# --- specimens --------------------------------------------------------------------------------------------


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _pair(
    key: str,
    title: str,
    provenance: str,
    synthetic: bool,
    game: Game,
    now: datetime,
    *,
    source: StateRef | None = None,
    scroll: tuple[float, float] = (0.0, 0.0),
    profiles: frozenset[PanelProfile] = _ALL,
    whens: tuple[str, str] = ("first 6 s", "second 6 s"),
) -> tuple[Specimen, ...]:
    """One live turn as its two 6 s halves: the teams named, then what they have left."""
    halves = (("a", whens[0], Half.NAMES, scroll[0]), ("b", whens[1], Half.COUNTS, scroll[1]))
    return tuple(
        Specimen(
            f"{key}-{suffix}",
            f"{title} · {when}",
            provenance,
            synthetic,
            game,
            now,
            source=source,
            scroll_s=scroll_s,
            profiles=profiles,
            half=half,
            pair=key,
        )
        for suffix, when, half, scroll_s in halves
    )


_POR, _MIN = Record(6, 8), Record(9, 5)
_ORL, _BOS = Record(8, 6), Record(7, 7)
_DET, _GSW = Record(10, 4), Record(8, 7)
_DEN_LATE, _LAL_LATE = Record(41, 30), Record(44, 27)  # five characters each, as most of a season's records are

_P = PlayerLine  # fields: name, +/-, FGM, FGA, 3PM, 3PA, FTM, FTA, OREB, DREB, AST, STL, BLK, TOV, PF

# POR at MIN through Q3 7:18 (58-68): every point accounted for. Synthetic.
_POR_Q3 = (
    _P("T.Camara", 3, 3, 5, 2, 3, 0, 0, 1, 3, 1, 2, 1, 0, 2),
    _P("D.Avdija", -4, 6, 12, 2, 5, 2, 2, 1, 5, 4, 1, 0, 2, 2),
    _P("D.Clingan", -6, 3, 5, 0, 0, 0, 2, 3, 6, 1, 0, 2, 1, 3),
    _P("J.Grant", -8, 2, 7, 1, 3, 2, 2, 0, 2, 1, 0, 0, 1, 1),
    _P("S.Sharpe", -9, 5, 13, 2, 6, 2, 3, 0, 2, 1, 1, 0, 3, 2),
    _P("J.Holiday", -11, 3, 8, 1, 3, 0, 0, 1, 1, 5, 1, 0, 1, 2),
)
_MIN_Q3 = (
    _P("A.Edwards", 15, 8, 15, 4, 9, 2, 2, 0, 4, 3, 1, 0, 2, 1),
    _P("R.Gobert", 12, 4, 5, 0, 0, 0, 1, 3, 7, 1, 0, 2, 1, 2),
    _P("J.Randle", 9, 4, 9, 1, 3, 3, 4, 1, 4, 4, 0, 0, 2, 3),
    _P("J.McDaniels", 7, 4, 7, 1, 2, 0, 0, 1, 2, 1, 2, 1, 0, 2),
    _P("N.Reid", 4, 4, 8, 2, 4, 1, 2, 1, 3, 2, 0, 1, 1, 1),
    _P("M.Conley", 3, 2, 4, 2, 3, 0, 0, 0, 1, 6, 1, 0, 0, 1),
)
# The final, 98-106. Plus-minus picks T.Camara (+1) for POR, not the top scorer, S.Sharpe (26). Synthetic.
_POR_FINAL = (
    _P("D.Avdija", -6, 8, 13, 2, 5, 4, 5, 2, 7, 7, 1, 0, 3, 3),
    _P("S.Sharpe", -12, 9, 24, 4, 11, 4, 4, 0, 3, 2, 1, 0, 4, 2),
    _P("J.Holiday", -10, 6, 12, 3, 7, 0, 0, 0, 3, 6, 1, 0, 2, 2),
    _P("J.Grant", -9, 5, 11, 2, 6, 2, 2, 0, 4, 1, 0, 1, 1, 3),
    _P("D.Clingan", -2, 5, 7, 0, 0, 0, 2, 4, 8, 1, 0, 3, 1, 4),
    _P("T.Camara", 1, 4, 8, 3, 6, 0, 0, 1, 4, 2, 2, 1, 1, 3),
)
_MIN_FINAL = (
    _P("A.Edwards", 11, 11, 20, 4, 9, 5, 6, 1, 6, 5, 2, 0, 3, 2),
    _P("R.Gobert", 14, 6, 7, 0, 0, 2, 3, 5, 10, 1, 1, 4, 1, 3),
    _P("J.Randle", 6, 6, 14, 1, 4, 5, 6, 1, 6, 5, 0, 0, 3, 3),
    _P("N.Reid", 4, 6, 11, 3, 6, 0, 0, 1, 4, 1, 1, 1, 1, 2),
    _P("J.McDaniels", 9, 5, 9, 2, 4, 0, 0, 1, 3, 2, 2, 1, 0, 3),
    _P("M.Conley", 8, 5, 9, 4, 7, 2, 2, 0, 2, 6, 1, 0, 1, 1),
)
# DET at GSW, 117-119 in double overtime. Synthetic.
_DET_FINAL = (
    _P("C.Cunningham", -3, 12, 28, 3, 9, 7, 8, 1, 7, 11, 1, 0, 5, 3),
    _P("J.Duren", 2, 8, 10, 0, 0, 4, 6, 6, 11, 2, 1, 3, 2, 4),
    _P("A.Thompson", -1, 5, 9, 0, 1, 2, 4, 2, 5, 3, 3, 1, 1, 4),
    _P("T.Harris", -4, 6, 13, 2, 5, 2, 2, 1, 5, 2, 0, 0, 1, 2),
    _P("J.Ivey", -5, 7, 15, 2, 6, 2, 3, 0, 3, 3, 1, 0, 3, 3),
    _P("D.Robinson", 1, 6, 11, 5, 9, 0, 0, 0, 2, 1, 0, 0, 0, 2),
)
_GSW_FINAL = (
    _P("S.Curry", 5, 13, 27, 7, 15, 5, 5, 0, 5, 6, 2, 0, 4, 2),
    _P("J.Butler", 7, 7, 13, 0, 1, 9, 11, 2, 6, 7, 3, 0, 2, 3),
    _P("D.Green", 4, 4, 8, 1, 4, 0, 0, 1, 8, 9, 2, 1, 3, 5),
    _P("B.Podziemski", -2, 5, 11, 2, 5, 2, 2, 1, 5, 3, 1, 0, 1, 2),
    _P("J.Kuminga", -1, 7, 14, 1, 3, 2, 4, 2, 4, 1, 0, 1, 2, 3),
    _P("M.Moody", 3, 6, 10, 4, 7, 2, 2, 0, 3, 1, 1, 0, 0, 2),
)

# POR at MIN, game 0 state 155: POR's count drops 5 to 4 with the clock stopped at Q3 7:18, the quarter's first
# timeout (the long class). Its first turn is the caption; the stat panels rotate through the rest of the break.
_TO_AWAY = Side("POR", 58, 4, False, (26, 25, 7), _POR, players=_POR_Q3)
_TO_HOME = Side("MIN", 68, 4, False, (26, 33, 9), _MIN, players=_MIN_Q3)
_TO_NOW = _utc("2026-11-17T02:35:30Z")


def _timeout_game(bottom: Bottom, rank: int = 0) -> Game:
    caption = timeout_caption(HomeAway.AWAY, "POR", 4) if bottom is Bottom.CAPTION else None
    return Game(
        _TO_AWAY,
        _TO_HOME,
        Phase.LIVE,
        3,
        GameClock(4380),
        home_win_pct=83.0,
        bottom=bottom,
        caption=caption,
        timeout=HomeAway.AWAY,
        rank=rank,
    )


# POR at MIN, game 0 state 160: Q3 6:07, play back on, so the bottom panel is back to the line score.
_Q3_AWAY = Side("POR", 58, 4, False, (26, 25, 7), _POR)
_Q3_HOME = Side("MIN", 68, 5, False, (26, 33, 9), _MIN)
_Q3_NOW = _utc("2026-11-17T02:39:42Z")

# The play queue. The first read (game 0 state 181) had both teams scoring; a second, synthetic read lands
# during the first play's 6 s with a foul and the free throws it gave. Plays within a read go away, then home.
_QUEUE_PLAYS = (
    Caption(Tone.SCORE, "3-POINT FG GOOD", "J.Grant (J.Holiday asst.)", HomeAway.AWAY),
    Caption(Tone.SCORE, "2-POINT FG GOOD", "A.Edwards (N.Reid asst.)", HomeAway.HOME),
    Caption(Tone.ALERT, "PERSONAL FOUL", "D.Clingan (4th foul)", HomeAway.AWAY),
    Caption(Tone.SCORE, "FREE THROWS GOOD", "A.Edwards (6/6 FT)", HomeAway.HOME),
)
_QUEUE_READS = (
    (Side("POR", 68, 3, False, (26, 25, 17), _POR), Side("MIN", 79, 4, True, (26, 33, 20), _MIN), GameClock(1300)),
    (Side("POR", 68, 3, False, (26, 25, 17), _POR), Side("MIN", 81, 4, True, (26, 33, 22), _MIN), GameClock(1250)),
)


def _queue_frame(i: int) -> Specimen:
    """Play ``i`` of the queue: 1/2 as the first read leaves it, then 2/4, 3/4, 4/4 once the second lands."""
    first = i == 1
    away, home, clock = _QUEUE_READS[0 if first else 1]
    waiting = 2 if first else 4
    game = Game(
        away,
        home,
        Phase.LIVE,
        3,
        clock,
        home_win_pct=88.0 if first else 89.0,
        bottom=Bottom.CAPTION,
        caption=_QUEUE_PLAYS[i - 1],
        caption_of=(i, waiting),
    )
    half = Half.NAMES if i % 2 else Half.COUNTS
    return Specimen(
        f"live-queue-{i}",
        f"Play queue {i}/{waiting} · {'first' if half is Half.NAMES else 'second'} 6 s",
        (
            "Recorded read, both scored · synthetic plays, ingest order"
            if first
            else "Synthetic second read · ingest order"
        ),
        True,
        game,
        _utc("2026-11-17T02:52:08Z") + timedelta(seconds=6 * (i - 1)),
        source=StateRef(0, 181) if first else None,
        half=half,
        pair="live-queue-12" if i <= 2 else "live-queue-34",
    )


_LATE_PERIODS = ((26, 25, 22, 25), (26, 33, 28, 11))  # POR at MIN, tied after four (synthetic Q4)

_FINAL = Game(
    Side("POR", 98, 2, True, (26, 25, 22, 25), Record(6, 9), players=_POR_FINAL),
    Side("MIN", 106, 1, True, (26, 33, 28, 19), Record(10, 5), players=_MIN_FINAL),
    Phase.FINAL,
    4,
)
_FINAL_OT = Game(
    Side("DET", 117, None, False, (29, 24, 29, 22, 7, 6), Record(10, 5), players=_DET_FINAL),
    Side("GSW", 119, None, False, (25, 28, 25, 26, 7, 8), Record(9, 7), players=_GSW_FINAL),
    Phase.FINAL,
    6,
)
_BEST = ("winner's best player", "loser's best player")

SPECIMENS: tuple[Specimen, ...] = (
    Specimen(
        "pregame",
        "Pregame",
        "Recorded tip, POR at MIN · synthetic records and odds",
        True,
        Game(
            Side("POR", record=_POR, ranks=Ranks(11, 4)),
            Side("MIN", record=_MIN, ranks=Ranks(5, 2)),
            Phase.PREGAME,
            tip=_utc("2026-11-17T01:00:00Z"),
            line=BettingLine(HomeAway.HOME, -265, -6.5),
        ),
        now=_utc("2026-11-17T01:00:00Z") - timedelta(hours=1, minutes=23),
        source=StateRef(0, 0),
    ),
    Specimen(
        "pregame-postponed-new-tip",
        "Postponed · new tip known",
        "Synthetic · ORL at BOS moved to tomorrow noon",
        True,
        Game(
            Side("ORL", record=_ORL, ranks=Ranks(6, 2)),
            Side("BOS", record=_BOS, ranks=Ranks(9, 3)),
            Phase.POSTPONED,
            tip=_utc("2026-11-17T19:00:00Z"),
        ),
        now=_utc("2026-11-16T23:37:00Z"),
    ),
    Specimen(
        "postponed-tbd",
        "Postponed · tip TBD",
        "Synthetic · ORL at BOS, no new tip",
        True,
        Game(
            Side("ORL", record=_ORL, ranks=Ranks(6, 2)),
            Side("BOS", record=_BOS, ranks=Ranks(9, 3)),
            Phase.POSTPONED,
        ),
        now=_utc("2026-11-16T23:37:00Z"),
    ),
    *_pair(
        "live-line-score",
        "Line score (two-digit scores)",
        "Recorded · POR at MIN, Q3 6:07",
        False,
        Game(_Q3_AWAY, _Q3_HOME, Phase.LIVE, 3, GameClock(3670), home_win_pct=84.0),
        _Q3_NOW,
        source=StateRef(0, 160),
    ),
    *_pair(
        "live-caption",
        "One play",
        "Recorded score (POR +3) · synthetic play",
        True,
        Game(
            Side("POR", 54, 5, False, (26, 25, 3), _POR),
            Side("MIN", 62, 4, False, (26, 33, 3), _MIN),
            Phase.LIVE,
            3,
            GameClock(5820),
            home_win_pct=79.0,
            bottom=Bottom.CAPTION,
            caption=Caption(Tone.SCORE, "3-POINT FG GOOD", "S.Sharpe (D.Avdija asst.)", HomeAway.AWAY),
        ),
        _utc("2026-11-17T02:30:29Z"),
        source=StateRef(0, 141),
    ),
    *(_queue_frame(i) for i in range(1, 5)),
    *_pair(
        "live-timeout",
        "Timeout, turn 1 · caption",
        "Recorded · POR 5 to 4 at Q3 7:18, the quarter's 1st (long class)",
        False,
        _timeout_game(Bottom.CAPTION),
        _TO_NOW,
        source=StateRef(0, 155),
    ),
    *_pair(
        "live-box-score",
        "Timeout, turn 2 · box score",
        "Recorded state (Q3 7:18) · synthetic box score",
        True,
        _timeout_game(Bottom.BOX_SCORE),
        _TO_NOW + timedelta(seconds=12),
        source=StateRef(0, 155),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-player-lines",
        "Timeout, turn 3 · player lines",
        "Recorded state · synthetic players and +/-, each team's top",
        True,
        _timeout_game(Bottom.PLAYER_LINES),
        _TO_NOW + timedelta(seconds=24),
        source=StateRef(0, 155),
        scroll=(1.0, 11.5),  # the head, then the tail
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-player-lines-next",
        "Timeout, turn 4 · player lines",
        "Recorded state · synthetic players and +/-, each team's 2nd",
        True,
        _timeout_game(Bottom.PLAYER_LINES, rank=1),
        _TO_NOW + timedelta(seconds=36),
        source=StateRef(0, 155),
        scroll=(1.0, 11.5),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-challenge",
        "Challenge won, stat change",
        "Synthetic · POR TO 3 back to 4, Q3 5:06 stopped; +1 BLK -1 PF",
        True,
        Game(
            Side("POR", 58, 4, False, (26, 25, 7), _POR),
            Side("MIN", 73, 5, False, (26, 33, 14), _MIN),
            Phase.LIVE,
            3,
            GameClock(3060),
            home_win_pct=88.0,
            bottom=Bottom.CAPTION,
            caption=challenge_caption(HomeAway.AWAY, "POR", (CallChange.FOUL_TO_BLOCK, "T.Camara")),
        ),
        _utc("2026-11-17T02:44:30Z"),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-reversal",
        "Call reversed, no challenge",
        "Synthetic · J.Randle -1 PF, no count change, Q2 3:40",
        True,
        Game(
            Side("POR", 47, 5, False, (26, 21), _POR),
            Side("MIN", 50, 4, False, (26, 24), _MIN),
            Phase.LIVE,
            2,
            GameClock(2200),
            home_win_pct=61.0,
            bottom=Bottom.CAPTION,
            caption=reversal_caption(CallChange.FOUL_OFF, "J.Randle"),
        ),
        _utc("2026-11-17T02:05:40Z"),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-run",
        "Scoring run",
        "Recorded · MIN 41 to 48 while POR held 45, Q2 6:21 to 4:53",
        False,
        Game(
            Side("POR", 45, 5, False, (26, 19), _POR),
            Side("MIN", 48, 4, False, (26, 22), _MIN),
            Phase.LIVE,
            2,
            GameClock(2930),
            home_win_pct=58.0,
            run=ScoringRun(HomeAway.HOME, 7, 88),
        ),
        _utc("2026-11-17T02:01:04Z"),
        source=StateRef(0, 108),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-bonus",
        "A team in the bonus",
        "Recorded · MIN in the bonus, Q2 2:20",
        False,
        Game(
            Side("POR", 49, 5, False, (26, 23), _POR),
            Side("MIN", 52, 4, True, (26, 26), _MIN),
            Phase.LIVE,
            2,
            GameClock(1400),
            home_win_pct=63.0,
        ),
        _utc("2026-11-17T02:08:09Z"),
        source=StateRef(0, 119),
    ),
    *_pair(
        "live-late-close",
        "Late and close",
        "Synthetic · tied with 48.2 s left, both in the bonus",
        True,
        Game(
            Side("POR", 98, 1, True, _LATE_PERIODS[0], _POR),
            Side("MIN", 98, 2, True, _LATE_PERIODS[1], _MIN),
            Phase.LIVE,
            4,
            GameClock(482),
            home_win_pct=56.0,
        ),
        _utc("2026-11-17T03:40:00Z"),
    ),
    *_pair(
        "late-season-two-digits",
        "Late season, two digits",
        "Synthetic · five-character records, the widest a season gives",
        True,
        Game(
            Side("DEN", 74, 3, False, (28, 24, 22), _DEN_LATE),
            Side("LAL", 71, 4, True, (25, 27, 19), _LAL_LATE),
            Phase.LIVE,
            3,
            GameClock(2520),
            home_win_pct=48.0,
        ),
        _utc("2026-03-20T03:31:00Z"),
    ),
    *_pair(
        "late-season-three-digits",
        "Late season, three digits",
        "Synthetic · five-character records against 1xx scores",
        True,
        Game(
            Side("DEN", 104, 1, True, (28, 24, 26, 26), _DEN_LATE),
            Side("LAL", 101, 2, True, (25, 27, 24, 25), _LAL_LATE),
            Phase.LIVE,
            4,
            GameClock(620),
            home_win_pct=39.0,
        ),
        _utc("2026-03-20T04:12:00Z"),
    ),
    *_pair(
        "end-q1",
        "End of the first",
        "Recorded · ORL at BOS",
        False,
        Game(
            Side("ORL", 20, 6, True, (20,), _ORL),
            Side("BOS", 17, 6, False, (17,), _BOS),
            Phase.END_PERIOD,
            1,
            home_win_pct=41.0,
        ),
        _utc("2026-11-17T00:36:46Z"),
        source=StateRef(1, 60),
    ),
    *_pair(
        "overtime",
        "Overtime (three digits)",
        "Synthetic · first OT",
        True,
        Game(
            Side("POR", 105, 2, False, (*_LATE_PERIODS[0], 7), _POR),
            Side("MIN", 107, 1, True, (*_LATE_PERIODS[1], 9), _MIN),
            Phase.LIVE,
            5,
            GameClock(1360),
            home_win_pct=71.0,
        ),
        _utc("2026-11-17T03:52:00Z"),
    ),
    *_pair(
        "double-overtime",
        "Double overtime (three digits)",
        "Synthetic · DET at GSW color clash",
        True,
        Game(
            Side("DET", 113, 1, True, (29, 24, 29, 22, 7, 2), _DET),
            Side("GSW", 115, 0, True, (25, 28, 25, 26, 7, 4), _GSW),
            Phase.LIVE,
            6,
            GameClock(650),
            home_win_pct=66.0,
        ),
        _utc("2026-11-17T04:30:00Z"),
    ),
    Specimen(
        "halftime",
        "Halftime · teams named all turn",
        "Recorded · POR at MIN · countdown from 15:00 at the half",
        False,
        Game(
            Side("POR", 51, 5, False, (26, 25), _POR),
            Side("MIN", 59, 4, True, (26, 33), _MIN),
            Phase.HALFTIME,
            2,
            home_win_pct=74.0,
            break_ends=_utc("2026-11-17T02:11:56Z") + _HALFTIME,
        ),
        now=_utc("2026-11-17T02:14:21Z"),
        source=StateRef(0, 130),
    ),
    *_pair(
        "final",
        "Final",
        "Recorded score · synthetic players · best by plus-minus",
        True,
        _FINAL,
        _utc("2026-11-17T03:56:15Z"),
        source=StateRef(0, 212),
        profiles=_STACK_ONLY,
        whens=_BEST,
    ),
    Specimen(
        "final",
        "Final · teams named all turn",
        "Recorded · POR at MIN",
        False,
        _FINAL,
        now=_utc("2026-11-17T03:56:15Z"),
        source=StateRef(0, 212),
        profiles=_NO_STACK,
    ),
    *_pair(
        "final-overtime",
        "Final, 2OT",
        "Synthetic · DET at GSW · best by plus-minus",
        True,
        _FINAL_OT,
        _utc("2026-11-17T04:40:00Z"),
        profiles=_STACK_ONLY,
        whens=_BEST,
    ),
    Specimen(
        "final-overtime",
        "Final · double overtime",
        "Synthetic · DET at GSW color clash",
        True,
        _FINAL_OT,
        now=_utc("2026-11-17T04:40:00Z"),
        profiles=_NO_STACK,
    ),
)


# --- shared pieces ----------------------------------------------------------------------------------------

_REGISTRY = NbaTeamRegistry.default()
_LOGOS = LogoStore(_ROOT)

# The Atlantic keeps mixed case and a period so it can't be read as Atlanta's code.
_DIVISION = {
    "Atlantic": "Atl.",
    "Central": "CEN",
    "Southeast": "SE",
    "Northwest": "NW",
    "Pacific": "PAC",
    "Southwest": "SW",
}

# Quarter pips: filled once the quarter has begun, a ring before.
_PIP_QUAD = ("..XXX..", ".XXXXX.", "XXXXXXX", "XXXXXXX", "XXXXXXX", ".XXXXX.", "..XXX..")
_PIP_STACK = (".XXX.", "XXXXX", "XXXXX", "XXXXX", ".XXX.")
_PIP_SINGLE = (".XX.", "XXXX", "XXXX", ".XX.")


def _ring(sprite: tuple[str, ...]) -> tuple[str, ...]:
    """The outline of a filled sprite: each lit cell with an unlit (or off-sprite) neighbor."""

    def lit(x: int, y: int) -> bool:
        return 0 <= y < len(sprite) and 0 <= x < len(sprite[y]) and sprite[y][x] == "X"

    return tuple(
        "".join(
            "X" if lit(x, y) and not all(lit(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))) else "."
            for x in range(len(row))
        )
        for y, row in enumerate(sprite)
    )


def _sprite(canvas: Canvas, x: int, y: int, rows: tuple[str, ...], color: RGBColor) -> None:
    for dy, row in enumerate(rows):
        for dx, cell in enumerate(row):
            if cell == "X":
                canvas.set_pixel(x + dx, y + dy, color)


def _pips_width(sprite: tuple[str, ...], gap: int) -> int:
    return _REGULATION * len(sprite[0]) + (_REGULATION - 1) * gap


def _draw_pips(canvas: Canvas, x: int, y: int, sprite: tuple[str, ...], gap: int, begun: int) -> None:
    """Four quarter pips from ``x``: the first ``begun`` filled gold, the rest dim gold rings."""
    ring = _ring(sprite)
    for i in range(_REGULATION):
        px = x + i * (len(sprite[0]) + gap)
        if i < begun:
            _sprite(canvas, px, y, sprite, _GOLD)
        else:
            _sprite(canvas, px, y, ring, _DIM_GOLD)


def _ink_height(font: str) -> int:
    """Rows a capital's ink spans in ``font``, from the cell top."""
    rows = rasterize(font, "BONUS")
    return max(i for i, row in enumerate(rows) if any(row)) + 1


def _box_height(font: str) -> int:
    """Rows an inverted box takes: the capitals with a red row above and below."""
    return _ink_height(font) + 2


def _draw_inverted(canvas: Canvas, x: int, y: int, text: str, font: str) -> None:
    """``text`` inverted, black letters on a red box with a pixel of red around them; the box's top-left at (x, y)."""
    width = text_width(text, font)  # the last glyph's blank column is the box's right margin
    canvas.fill_rect(x, y, width + 1, _box_height(font), _RED)
    canvas.text(x + 1, y + 1, text, _BLACK, font=font)


def _timeouts_text(timeouts: int | None) -> str:
    """Timeouts left, the count first: ``4×TO``."""
    return f"{'-' if timeouts is None else timeouts}{_TIMES}TO"


def _score_text(score: int | None) -> str:
    return "-" if score is None else str(score)


def _three_digits(game: Game) -> bool:
    """Whether either team has reached 100, which shrinks every board's codes and the quad's and stack's records
    and counts."""
    return any(score is not None and score >= 100 for score in (game.away.score, game.home.score))


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _ot_label(period: int) -> str:
    """An overtime as the state module names it: ``OT``, then ``2OT``, ``3OT``."""
    extra = period - _REGULATION
    return "OT" if extra == 1 else f"{extra}OT"


def _period_headers(periods_entered: int) -> list[str]:
    """Line-score headers: the four quarters, then ``OT`` for a lone overtime, or ``OT1`` ``OT2`` once there's a second."""
    overtimes = max(0, periods_entered - _REGULATION)
    quarters = [f"Q{i}" for i in range(1, _REGULATION + 1)]
    if overtimes == 1:
        return [*quarters, "OT"]
    return [*quarters, *(f"OT{i}" for i in range(1, overtimes + 1))]


def _countdown(left: timedelta) -> str:
    seconds = max(0, round(left.total_seconds()))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _tip_lines(now: datetime, tip: datetime, zone: tzinfo | None) -> tuple[str, str, str]:
    """What a scheduled tip shows, upper line first, and its time.

    Later today: the time over the countdown (``1h 23m``, ``45m``, ``SOON`` inside a minute). On another day: the
    date (``11/20``) where the time was, over the time where the countdown was.
    """
    clock = start_time_label(tip, zone)
    local = tip.astimezone(zone)
    if local.date() != now.astimezone(zone).date():
        return f"{local.month}/{local.day}", clock, clock
    minutes = int((tip - now).total_seconds() // 60)
    if minutes < 1:
        countdown = "SOON"
    elif minutes < 60:
        countdown = f"{minutes}m"
    else:
        countdown = "{}h {}m".format(*divmod(minutes, 60))
    return clock, countdown, clock


def _when_width(text: str, clock: str, font: str) -> int:
    """How wide a tip line is; the tip time takes its narrow space."""
    return _time_width(text, font) if text == clock else text_width(text, font)


def _draw_when(canvas: Canvas, x: int, y: int, text: str, clock: str, color: RGBColor, font: str) -> None:
    if text == clock:
        _draw_time(canvas, x, y, text, color, font)
    else:
        canvas.text(x, y, text, color, font=font)


def _span_text(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _winner(game: Game) -> HomeAway | None:
    away, home = game.away.score, game.home.score
    if game.phase is not Phase.FINAL or away is None or home is None or away == home:
        return None
    return HomeAway.AWAY if away > home else HomeAway.HOME


def _meter(game: Game) -> WinProbability | None:
    """The market's reading while the game goes on; at a final, full for the winner."""
    if game.phase is Phase.FINAL:
        winner = _winner(game)
        if winner is None:
            return WinProbability(home=50.0, away=50.0)
        won = 100.0 if winner is HomeAway.HOME else 0.0
        return WinProbability(home=won, away=100.0 - won)
    if game.home_win_pct is None:
        return None
    return WinProbability(home=game.home_win_pct, away=100.0 - game.home_win_pct)


def _lost(game: Game, side: HomeAway) -> bool:
    winner = _winner(game)
    return winner is not None and side is not winner


def _text_color(game: Game, side: HomeAway) -> RGBColor:
    return _LOSER if _lost(game, side) else _WHITE


def _plus_minus_color(player: PlayerLine) -> RGBColor:
    if player.plus_minus > 0:
        return _GREEN
    return _RED if player.plus_minus < 0 else _SECONDARY


def _shows_counts(spec: Specimen) -> bool:
    """Whether a frame shows timeouts and bonus: a live or end-of-period card's second 6 s.

    At halftime and a final the counts no longer matter, so the teams stay named for the whole turn.
    """
    return spec.half is Half.COUNTS and spec.game.phase in (Phase.LIVE, Phase.END_PERIOD)


def _names(canvas: Canvas, game: Game, which: HomeAway, x: int, ys: tuple[int, int], fonts: tuple[str, str]) -> None:
    """A team's code (white) over its record (gold), each at its own cell row and face; a final's loser dimmed."""
    side = game.side(which)
    lost = _lost(game, which)
    canvas.text(x, ys[0], side.abbreviation, _LOSER if lost else _WHITE, font=fonts[0])
    if side.record is not None:
        canvas.text(x, ys[1], side.record.text(), _DIM_GOLD if lost else _GOLD, font=fonts[1])


def _teams(game: Game) -> tuple[BasketballTeam, BasketballTeam]:
    return _REGISTRY.resolve(game.away.abbreviation), _REGISTRY.resolve(game.home.abbreviation)


def _lane_now(spec: Specimen) -> datetime:
    """The instant a scrolling line reads its cycle from: ``scroll_s`` into the cycle."""
    return datetime.fromtimestamp(spec.scroll_s, tz=UTC)


def _standing_text(team: BasketballTeam, ranks: Ranks, *, division: bool) -> str:
    conference = team.conference or ""
    text = f"{_ordinal(ranks.conference)} {conference}"
    if division and team.division is not None:
        text += f", {_ordinal(ranks.division)} {_DIVISION[team.division]}"
    return text


def _odds_text(game: Game, line: BettingLine) -> str:
    """The favorite's line the way the MLB pregame writes one: moneyline, then the spread in parentheses."""
    favorite = game.side(line.favorite).abbreviation
    return f"{favorite} {line.moneyline:+d} ({line.spread:+g})"


def _draw_time(canvas: Canvas, x: int, y: int, label: str, color: RGBColor, font: str) -> int:
    """A clock time like ``6:00 PM`` with a three-pixel space, a pixel narrower than the face's own; returns its width."""
    clock, _, meridiem = label.partition(" ")
    canvas.text(x, y, clock, color, font=font)
    canvas.text(x + text_width(clock, font) + 3, y, meridiem, color, font=font)
    return text_width(clock, font) + 3 + text_width(meridiem, font)


def _time_width(label: str, font: str) -> int:
    clock, _, meridiem = label.partition(" ")
    return text_width(clock, font) + 3 + text_width(meridiem, font)


def _ranked(side: Side, rank: int) -> PlayerLine | None:
    order = side.by_plus_minus()
    return order[rank] if rank < len(order) else None


def _caption_lines(caption: Caption, half: Half) -> tuple[str, str]:
    """The stack's two lines for a caption in one half of its turn."""
    if not caption.halves:
        return caption.headline, caption.detail
    return caption.halves[0 if half is Half.NAMES else min(1, len(caption.halves) - 1)]


# --- quad 128x64 ------------------------------------------------------------------------------------------

_Q_ROW = 20
_Q_LANE_X = 23
_Q_SCORE_RIGHT = 78
_Q_MODULE = (80, 128)  # the clock, pips and break words center in what's right of the scores
# The code and the timeouts are 6x10 at any score. The record is 6x10 while both scores have two digits and 5x7
# once either reaches 100, where a five-character record in 6x10 would reach the score's first column. BONUS keeps
# 5x7. Each is (face, cell row below the team row's top).
_Q_CODE = ("6x10", 1)
_Q_RECORD = {False: ("6x10", 10), True: ("5x7", 11)}
_Q_COUNTS = ("6x10", 1)
_Q_BONUS_ROW = 11
# The clock over the pips, centered in the 40 rows above the divider; raised to make room while a run is on.
_Q_CLOCK_Y = {False: 8, True: 2}
_Q_PIP_DROP = 15  # pips' top below the clock's cell top
_Q_PIP_GAP = 4
_Q_RUN_Y = (27, 34)
_Q_WORDS_Y = (5, 21)
_Q_DIVIDER_Y = 40
_Q_LINE_CENTERS = (30, 45, 60, 75, 90, 105, 120)


def _quad_scored(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    game = spec.game
    away, home = _teams(game)
    treatment = resolve_matchup_treatment(
        away,
        home,
        profile=PanelProfile.QUAD_128X64,
        logos=context.logos,
        policy=context.contrast,
        away_top=0,
        home_top=_Q_ROW,
    )
    (code_font, code_row), (record_font, record_row) = _Q_CODE, _Q_RECORD[_three_digits(game)]
    counts_font, counts_row = _Q_COUNTS
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _Q_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)
        if _shows_counts(spec):
            canvas.text(_Q_LANE_X, top + counts_row, _timeouts_text(side.timeouts), _GOLD, font=counts_font)
            if game.phase is Phase.LIVE and side.in_bonus:
                _draw_inverted(canvas, _Q_LANE_X, top + _Q_BONUS_ROW, "BONUS", "5x7")
        else:
            ys = (top + code_row, top + record_row)
            _names(canvas, game, which, _Q_LANE_X, ys, (code_font, record_font))
        draw_right_aligned(canvas, _Q_SCORE_RIGHT, top + 1, _score_text(side.score), _text_color(game, which), "9x18B")
    _meter_if_any(canvas, treatment, game)
    _quad_module(canvas, spec)
    canvas.fill_rect(0, _Q_DIVIDER_Y, 128, 1, _DIVIDER)
    if game.bottom is Bottom.CAPTION and game.caption is not None:
        _quad_caption(canvas, spec, game.caption)
    elif game.bottom is Bottom.BOX_SCORE:
        _quad_box(canvas, game)
    elif game.bottom is Bottom.PLAYER_LINES:
        _quad_player_lines(canvas, spec)
    else:
        _quad_line_score(canvas, game)


def _meter_if_any(canvas: Canvas, treatment: MatchupVisualTreatment, game: Game) -> None:
    meter = _meter(game)
    if meter is not None:
        draw_win_meter(canvas, treatment, meter)


def _quad_module(canvas: Canvas, spec: Specimen) -> None:
    """Right of the scores: the clock over the quarter pips (or the overtime), raised over a run when one is on."""
    game = spec.game
    left, right = _Q_MODULE
    if game.phase is Phase.LIVE:
        clock_y = _Q_CLOCK_Y[game.run is not None]
        clock = game.clock.text() if game.clock is not None else "--:--"
        draw_centered(canvas, left, right, clock_y, clock, _GOLD, "8x13B")
        pip_y = clock_y + _Q_PIP_DROP
        if game.period <= _REGULATION:
            width = _pips_width(_PIP_QUAD, _Q_PIP_GAP)
            _draw_pips(canvas, left + (right - left - width) // 2, pip_y, _PIP_QUAD, _Q_PIP_GAP, game.period)
        else:  # the label's ink rows match the pips'
            draw_centered(canvas, left, right, pip_y - 1, _ot_label(game.period), _GOLD, "6x10")
        if game.run is not None:
            run = game.run
            team = game.side(run.team).abbreviation
            draw_centered(canvas, left, right, _Q_RUN_Y[0], f"{team} {run.points}-0", _WHITE, "5x7")
            draw_centered(canvas, left, right, _Q_RUN_Y[1], f"LAST {_span_text(run.span_seconds)}", _SECONDARY, "4x6")
        return
    words: tuple[str, ...]
    if game.phase is Phase.END_PERIOD:
        words = ("END", f"Q{game.period}" if game.period <= _REGULATION else _ot_label(game.period))
    elif game.phase is Phase.HALFTIME:
        ends = game.break_ends
        words = ("HALF", _countdown(ends - spec.now)) if ends is not None else ("HALF",)
    else:
        words = ("FINAL", _ot_label(game.period)) if game.period > _REGULATION else ("FINAL",)
    rows = [(words[0], sum(_Q_WORDS_Y) // 2)] if len(words) == 1 else list(zip(words, _Q_WORDS_Y))
    for i, (word, y) in enumerate(rows):
        bold = game.phase is Phase.FINAL and i == 0  # FINAL in the bold face of the single's F; its OT regular
        draw_centered(canvas, left, right, y, word, _GOLD, "8x13B" if bold else "8x13")


def _quad_line_score(canvas: Canvas, game: Game) -> None:
    """Quarter by quarter under the divider: headers, then the away and home rows."""
    entered = max(len(game.away.periods), len(game.home.periods), game.period)
    headers = _period_headers(entered)
    current = game.period - 1  # the period under way, or the one just finished
    for i, header in enumerate(headers):
        color = _GOLD if i == current else _SECONDARY
        draw_centered(canvas, _Q_LINE_CENTERS[i] - 7, _Q_LINE_CENTERS[i] + 7, 42, header, color, "4x6")
    for which, y in ((HomeAway.AWAY, 49), (HomeAway.HOME, 56)):
        side = game.side(which)
        color = _text_color(game, which)
        canvas.text(4, y, side.abbreviation, color, font="4x6")
        for i in range(len(headers)):
            known = i < len(side.periods)
            value, value_color = (str(side.periods[i]), color) if known else ("-", _SECONDARY)
            draw_centered(canvas, _Q_LINE_CENTERS[i] - 7, _Q_LINE_CENTERS[i] + 7, y, value, value_color, "4x6")


def _quad_caption(canvas: Canvas, spec: Specimen, caption: Caption) -> None:
    """The play as it happens, NFL's way: a headline in the play's tone, a detail line a size down in white.

    While more than one play waits, each shows 6 s, marked ``i/N``; N grows as plays arrive.
    """
    now = _lane_now(spec)
    width = 128
    if spec.game.caption_of is not None:
        i, waiting = spec.game.caption_of
        draw_right_aligned(canvas, 127, 42, f"{i}/{waiting}", _SECONDARY, "4x6")
        width = 114
    headline = TextLane(x=0, y=42, width=width, font="6x10")
    draw_text_lane(canvas, headline, caption.headline, _TONE_COLOR[caption.tone], now=now)
    detail = TextLane(x=0, y=55, width=128, font="5x7")
    draw_text_lane(canvas, detail, caption.detail, _WHITE, now=now)


_BOX_COLUMNS = (("FG%", 44), ("3PT", 72), ("REB", 90), ("AST", 108), ("TOV", 126))


def _box_values(box: TeamBox) -> tuple[str, ...]:
    return (
        str(box.fg_pct),
        f"{box.threes_made}-{box.threes_attempted}",
        str(box.rebounds),
        str(box.assists),
        str(box.turnovers),
    )


def _quad_box(canvas: Canvas, game: Game) -> None:
    """The two teams' box-score lines as a table, laid out like the line score."""
    for label, right in _BOX_COLUMNS:
        draw_right_aligned(canvas, right, 42, label, _SECONDARY, "4x6")
    for which, y in ((HomeAway.AWAY, 49), (HomeAway.HOME, 56)):
        side = game.side(which)
        canvas.text(4, y, side.abbreviation, _WHITE, font="4x6")
        if not side.players:
            continue
        for (_, right), value in zip(_BOX_COLUMNS, _box_values(TeamBox.of(side.players))):
            draw_right_aligned(canvas, right, y, value, _WHITE, "4x6")


_Q_NAME_X = 18


def _quad_player_lines(canvas: Canvas, spec: Specimen) -> None:
    """One player a team, away over home, walking each team's plus-minus order a turn at a time.

    Each row runs on its own: the code, the name, a space, the plus-minus, a space, then the stats, which scroll
    when they overflow while everything before them holds still. The rows don't line up with each other.
    """
    game = spec.game
    now = _lane_now(spec)
    for which, y in ((HomeAway.AWAY, 44), (HomeAway.HOME, 55)):
        side = game.side(which)
        canvas.text(3, y + 1, side.abbreviation, _SECONDARY, font="4x6")
        player = _ranked(side, game.rank)
        if player is None:
            continue
        canvas.text(_Q_NAME_X, y, player.name, _WHITE, font="5x7")
        pm = player.plus_minus_text()
        pm_x = _Q_NAME_X + text_width(f"{player.name} ", "5x7")
        canvas.text(pm_x, y, pm, _plus_minus_color(player), font="5x7")
        stats_x = pm_x + text_width(f"{pm} ", "5x7")
        lane = TextLane(x=stats_x, y=y, width=128 - stats_x, font="5x7")
        draw_text_lane(canvas, lane, player.stats(), _STATS_TEAL, now=now)


def _quad_pregame(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    """Marks with records and standings, the records mirrored about the odds row; the tip column at right.

    The top team's standing sits over its record, the bottom team's under it, so the two records flank the
    odds. The tip label rides on the top record's line, directly over the time.
    """
    game = spec.game
    away, home = _teams(game)
    draw_matchup_marks(canvas, context, away, home, away_top=0, home_top=32)  # tiles land at y6 and y38
    rows = ((away, game.away, 15, 7), (home, game.home, 39, 50))  # (team, side, record y, standing y)
    for team, side, record_y, standing_y in rows:
        if side.record is not None:
            canvas.text(24, record_y, side.record.text(), _WHITE, font="6x10")
        if side.ranks is not None:
            full = _standing_text(team, side.ranks, division=True)
            font = "5x7" if 24 + text_width(full, "5x7") <= 128 else "4x6"
            canvas.text(24, standing_y, full, _STATS_TEAL, font=font)
    if game.line is not None:
        canvas.text(24, 29, _odds_text(game, game.line), _ODDS_GREEN, font="4x6")
    label = "TIPOFF" if game.phase is Phase.PREGAME else "POSTPONED"
    _quad_column(canvas, 15, label, _GOLD)
    if game.tip is None:
        _quad_column(canvas, 27, "TIP TBD", _WHITE)
        return
    upper, lower, clock = _tip_lines(spec.now, game.tip, context.timezone)
    for text, y in ((upper, 27), (lower, 39)):
        _draw_when(canvas, _quad_column_x(_when_width(text, clock, "6x10")), y, text, clock, _WHITE, "6x10")


_Q_COLUMN_CENTER = 107


def _quad_column_x(width: int) -> int:
    """Where a right-column item starts: centered on the column, pulled left if it would run off the board."""
    return min(_Q_COLUMN_CENTER - width // 2, 128 - width)


def _quad_column(canvas: Canvas, y: int, text: str, color: RGBColor) -> None:
    canvas.text(_quad_column_x(text_width(text, "6x10")), y, text, color, font="6x10")


# --- stack 64x64 ------------------------------------------------------------------------------------------

_S_ROW = 20
_S_LANE_X = 24  # the code, record, timeouts and bonus box all start here
# The code is 6x10 while both scores have two digits and 5x7 once either reaches 100, where a 6x10 code would
# touch the score's first column; its ink ends on the score's last row. The record is 4x6 at any score, with two
# blank rows under the score: it clears a two-digit score, and passes under a 1xx's first two columns, since no
# face fits a five-character record in the 16 columns left of one. Each is (face, cell row below the row's top).
_S_CODE = {False: ("6x10", 4), True: ("5x7", 6)}
_S_RECORD = ("4x6", 14)
_S_COUNTS = {False: "5x7", True: "4x6"}  # the timeouts; BONUS keeps 4x6
_S_COUNTS_ROW = 4
_S_BONUS_ROW = 13
_S_CLOCK_Y = 40
_S_CLOCK_MARGIN = 2  # the clock this far from the left edge, the pips or OT this far from the right
_S_PIP_GAP = 2
_S_DIVIDER_Y = 50
_S_LINES_Y = (52, 58)


def _stack_scored(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    game = spec.game
    away, home = _teams(game)
    treatment = resolve_matchup_treatment(
        away,
        home,
        profile=PanelProfile.STACK_64X64,
        logos=context.logos,
        policy=context.contrast,
        away_top=0,
        home_top=_S_ROW,
    )
    (code_font, code_row), (record_font, record_row) = _S_CODE[_three_digits(game)], _S_RECORD
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _S_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)
        if _shows_counts(spec):
            counts_font = _S_COUNTS[_three_digits(game)]
            canvas.text(_S_LANE_X, top + _S_COUNTS_ROW, _timeouts_text(side.timeouts), _GOLD, font=counts_font)
            if game.phase is Phase.LIVE and side.in_bonus:
                _draw_inverted(canvas, _S_LANE_X, top + _S_BONUS_ROW, "BONUS", "4x6")
        else:
            ys = (top + code_row, top + record_row)
            _names(canvas, game, which, _S_LANE_X, ys, (code_font, record_font))
        draw_right_aligned(canvas, 64, top + 1, _score_text(side.score), _text_color(game, which), "8x13B")
    _meter_if_any(canvas, treatment, game)
    _stack_clock_line(canvas, spec)
    _stack_divider(canvas, game)
    _stack_bottom(canvas, spec)


def _stack_divider(canvas: Canvas, game: Game) -> None:
    """The gray divider; while plays queue, cut into one segment per waiting play, the one showing gold.

    There's no room beside a 4x6 headline for an ``i/N`` count, so the divider carries it.
    """
    if game.caption_of is None:
        canvas.fill_rect(0, _S_DIVIDER_Y, 64, 1, _DIVIDER)
        return
    shown, waiting = game.caption_of
    for i in range(waiting):
        x0, x1 = i * 64 // waiting, (i + 1) * 64 // waiting
        gap = 0 if i == waiting - 1 else 1
        canvas.fill_rect(x0, _S_DIVIDER_Y, x1 - x0 - gap, 1, _GOLD if i == shown - 1 else _DIVIDER)


def _stack_clock_line(canvas: Canvas, spec: Specimen) -> None:
    """The clock pinned left and the quarter pips (or the overtime) pinned right; at a break, the break's words."""
    game = spec.game
    font, y = "6x10", _S_CLOCK_Y
    if game.phase is Phase.LIVE:
        clock = game.clock.text() if game.clock is not None else "--:--"
        canvas.text(_S_CLOCK_MARGIN, y, clock, _GOLD, font=font)
        right = 64 - _S_CLOCK_MARGIN
        if game.period <= _REGULATION:
            width = _pips_width(_PIP_STACK, _S_PIP_GAP)
            _draw_pips(canvas, right - width, y + 2, _PIP_STACK, _S_PIP_GAP, game.period)
        else:  # the face's trailing blank column stands in for one pixel of the margin
            draw_right_aligned(canvas, right + 1, y, _ot_label(game.period), _GOLD, font)
        return
    if game.phase is Phase.END_PERIOD:
        label = f"END Q{game.period}" if game.period <= _REGULATION else f"END {_ot_label(game.period)}"
    elif game.phase is Phase.HALFTIME:
        ends = game.break_ends
        label = f"HALF {_countdown(ends - spec.now)}" if ends is not None else "HALFTIME"
    else:
        label = f"FINAL/{_ot_label(game.period)}" if game.period > _REGULATION else "FINAL"
    draw_centered(canvas, 0, 64, y, label, _GOLD, font)


def _stack_bottom(canvas: Canvas, spec: Specimen) -> None:
    """Two small lines under the divider: the play or ruling, the rotation's panel, a run, or a final's best."""
    game = spec.game
    first, second = _S_LINES_Y
    now = _lane_now(spec)
    if game.phase is Phase.FINAL:
        _stack_best(canvas, game, spec.half)
        return
    if game.phase is Phase.LIVE and game.caption is not None and game.bottom is Bottom.CAPTION:
        caption = game.caption
        headline, detail = _caption_lines(caption, spec.half)
        lane = TextLane(x=0, y=first, width=64, font="4x6", anchor=LaneAnchor.CENTER)
        draw_text_lane(canvas, lane, headline, _TONE_COLOR[caption.tone], now=now)
        lane = TextLane(x=0, y=second, width=64, font="4x6", anchor=LaneAnchor.CENTER)
        draw_text_lane(canvas, lane, detail, _WHITE, now=now)
        return
    if game.phase is Phase.LIVE and game.run is not None:
        run = game.run
        draw_centered(canvas, 0, 64, first, f"{game.side(run.team).abbreviation} {run.points}-0", _WHITE, "4x6")
        draw_centered(canvas, 0, 64, second, f"LAST {_span_text(run.span_seconds)}", _SECONDARY, "4x6")
        return
    if game.bottom is Bottom.BOX_SCORE and game.away.players and game.home.players:
        away, home = TeamBox.of(game.away.players), TeamBox.of(game.home.players)
        lines = (("FG%", f"{away.fg_pct}-{home.fg_pct}"), ("REB", f"{away.rebounds}-{home.rebounds}"))
        for (label, values), y in zip(lines, _S_LINES_Y):
            _stack_labeled(canvas, y, label, _SECONDARY, values)
        return
    if game.bottom is Bottom.PLAYER_LINES:
        # One player a half, away first, as the tiles above sit: the name and plus-minus, then points and shooting.
        side = game.away if spec.half is Half.NAMES else game.home
        player = _ranked(side, game.rank)
        if player is not None:
            _stack_name_line(canvas, first, player)
            stats = f"{player.points} PTS {player.fgm}/{player.fga} FG"
            draw_centered(canvas, 0, 64, second, stats, _STATS_TEAL, "4x6")
        return
    _stack_line_score(canvas, game)


def _stack_name_line(canvas: Canvas, y: int, player: PlayerLine) -> None:
    """A player's name (white) and plus-minus (in its sign's color), centered."""
    name, pm = f"{player.name} ", player.plus_minus_text()
    x = (64 - text_width(name + pm, "4x6")) // 2
    canvas.text(x, y, name, _WHITE, font="4x6")
    canvas.text(x + text_width(name, "4x6"), y, pm, _plus_minus_color(player), font="4x6")


def _stack_best(canvas: Canvas, game: Game, half: Half) -> None:
    """A final's bottom panel: the winner's best player by plus-minus, then the loser's, with their numbers.

    The name and plus-minus fill the first line; the points and then the line's biggest other numbers follow on
    the second, as many as fit.
    """
    winner = _winner(game)
    if winner is None:
        return
    loser = HomeAway.HOME if winner is HomeAway.AWAY else HomeAway.AWAY
    player = game.side(winner if half is Half.NAMES else loser).best()
    if player is None:
        return
    first, second = _S_LINES_Y
    _stack_name_line(canvas, first, player)
    line = f"{player.points} PTS"
    for item in player.counting():
        trial = f"{line} {item}"
        if text_width(trial, "4x6") > 64:
            break
        line = trial
    draw_centered(canvas, 0, 64, second, line, _STATS_TEAL, "4x6")


def _stack_labeled(canvas: Canvas, y: int, label: str, label_color: RGBColor, values: str) -> None:
    """``label away-home``, centered."""
    total = text_width(f"{label} {values}", "4x6")
    x = (64 - total) // 2
    canvas.text(x, y, label, label_color, font="4x6")
    canvas.text(x + text_width(f"{label} ", "4x6"), y, values, _WHITE, font="4x6")


def _stack_line_rows(game: Game) -> list[tuple[str, int, int, bool]]:
    """The line score's two rows, (label, away, home, under way), latest last.

    Q1 alone after the first; Q1 over Q2 through the half; the first half's total (H1) over Q3; Q3 over Q4;
    in overtime the last two periods, ``Q4`` over ``OT`` or ``OT1`` over ``OT2``.
    """
    entered = min(len(game.away.periods), len(game.home.periods))
    headers = _period_headers(entered)
    away, home = game.away.periods, game.home.periods

    def row(i: int) -> tuple[str, int, int, bool]:
        return headers[i], away[i], home[i], game.phase is Phase.LIVE and i == game.period - 1

    if entered == 3:
        return [("H1", away[0] + away[1], home[0] + home[1], False), row(2)]
    return [row(i) for i in range(max(0, entered - 2), entered)]


def _stack_line_score(canvas: Canvas, game: Game) -> None:
    """The line score as two rows, ``Q2 25-33``: the period under way gold, a finished one gray."""
    for (label, away, home, live), y in zip(_stack_line_rows(game), _S_LINES_Y):
        _stack_labeled(canvas, y, label, _GOLD if live else _SECONDARY, f"{away}-{home}")


_S_PREGAME_BIG_Y = 47  # cell tops under the home record: the big line's ink on rows 48-54
_S_PREGAME_SMALL_Y = 57  # and the small line's on rows 57-61


def _stack_pregame(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    """Marks, each team's code over its record the same way; the odds row between; then the tip, in two lines.

    What carries the news goes big (6x10): the tip time later today, the date on another day, or ``POSTPONED``.
    The rest goes on a small line (4x6) under it: ``TIPOFF`` with the countdown, or with the time on another day;
    a postponed game's new date and time; or ``TIP TBD``. A full line such as ``TIPOFF 6:00 PM`` would be 87 px in
    6x10, wider than the board.
    """
    game = spec.game
    away, home = _teams(game)
    draw_matchup_marks(canvas, context, away, home, away_top=0, home_top=27)
    for side, top in ((game.away, 0), (game.home, 27)):
        canvas.text(_S_LANE_X, top, side.abbreviation, _WHITE, font="6x10")
        if side.record is not None:
            canvas.text(_S_LANE_X, top + 10, side.record.text(), _STATS_TEAL, font="6x10")
    if game.line is not None:
        draw_centered(canvas, 0, 64, 21, _odds_text(game, game.line), _ODDS_GREEN, "4x6")
    small: list[tuple[str, RGBColor]]
    clock = ""
    if game.tip is None:
        big, big_color = "POSTPONED", _GOLD
        small = [("TIP TBD", _WHITE)]
    else:
        upper, lower, clock = _tip_lines(spec.now, game.tip, context.timezone)
        if game.phase is Phase.PREGAME:
            big, big_color = upper, _WHITE
            small = [("TIPOFF", _GOLD), (lower, _WHITE)]
        else:  # a new tip later today shows its time alone: a postponed game's countdown matters less
            big, big_color = "POSTPONED", _GOLD
            small = [(upper, _WHITE), (lower, _WHITE)] if upper != clock else [(clock, _WHITE)]
    big_x = (64 - _when_width(big, clock, "6x10")) // 2
    _draw_when(canvas, big_x, _S_PREGAME_BIG_Y, big, clock, big_color, "6x10")
    _draw_line_parts(canvas, 64, _S_PREGAME_SMALL_Y, small, clock, "4x6")


def _draw_line_parts(
    canvas: Canvas, width: int, y: int, parts: Sequence[tuple[str, RGBColor]], clock: str, font: str
) -> None:
    """One line's parts, a space apart and centered across ``width``, each in its own color."""
    space = text_width(" ", font)
    total = sum(_when_width(text, clock, font) for text, _ in parts) + space * (len(parts) - 1)
    x = (width - total) // 2
    for text, color in parts:
        _draw_when(canvas, x, y, text, clock, color, font)
        x += _when_width(text, clock, font) + space


# --- single 64x32 -----------------------------------------------------------------------------------------

_I_ROW = 16
_I_CODE_X = 5
_I_SCORE_RIGHT = 42
_I_MODULE = (44, 64)
_I_CLOCK_Y = 10  # the clock and pips together, centered in the module
_I_PIP_Y = 18
_I_PIP_GAP = 1
_I_WORDS_Y = (7, 16)
_I_BONUS = "BON"
_I_STACK_GAP = 1  # rows between the timeouts and the bonus box under them
# The score's cell sits a row into its lane, so its ink is on rows 2-8. The code's ink ends on the score's last
# row: 6x10 while both scores have two digits, 5x7 once either reaches 100 (a 6x10 code two columns from a 6x10
# 1xx reads as one word). The record (5x7) takes rows 10-15 under both, on every card, so a five-character record
# may run beneath a three-digit score. The timeouts and the bonus box keep 5x7 throughout.
_I_SCORE_ROW = 1
_I_CODE = {False: ("6x10", 1), True: ("5x7", 3)}  # face and cell row
_I_RECORD = ("5x7", 10)
_I_COUNTS_FONT = "5x7"


def _single_counts(canvas: Canvas, side: Side, top: int, font: str, *, live: bool) -> None:
    """``5TO`` over the boxed ``BON``, both inside the team's lane, starting at ``top``."""
    counts = f"{'-' if side.timeouts is None else side.timeouts}TO"
    canvas.text(_I_CODE_X, top, counts, _GOLD, font=font)
    if live and side.in_bonus:
        _draw_inverted(canvas, _I_CODE_X, top + _ink_height(font) + _I_STACK_GAP, _I_BONUS, font)


def _single_block_height(font: str) -> int:
    return _ink_height(font) + _I_STACK_GAP + _box_height(font)


def _single_count_tops(font: str) -> dict[HomeAway, int]:
    """Where each team's timeouts-over-bonus block starts.

    With a row or less to spare, the away block hugs the top of its lane and the home block the bottom of its
    own, so the gap between the lanes is wider than the gap inside a block. With more, each centers in its lane.
    """
    slack = _I_ROW - _single_block_height(font)
    if slack < 0:
        raise ValueError(f"timeouts over the bonus box need {_single_block_height(font)} rows in {font}")
    if slack <= 1:
        return {HomeAway.AWAY: 0, HomeAway.HOME: _I_ROW + slack}
    return {HomeAway.AWAY: slack // 2, HomeAway.HOME: _I_ROW + slack // 2}


def _single_scored(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    game = spec.game
    away, home = _teams(game)
    treatment = resolve_matchup_treatment(
        away,
        home,
        profile=PanelProfile.SINGLE_64X32,
        logos=context.logos,
        policy=context.contrast,
        away_top=0,
        home_top=_I_ROW,
    )
    scorer = game.caption.team if game.caption is not None and game.caption.tone is Tone.SCORE else None
    count_tops = _single_count_tops(_I_COUNTS_FONT)
    code_font, code_row = _I_CODE[_three_digits(game)]
    record_font, record_row = _I_RECORD
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _I_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)  # a color bar: a 20px tile can't fit a 16px row
        color = _text_color(game, which)
        if _shows_counts(spec):
            _single_counts(canvas, side, count_tops[which], _I_COUNTS_FONT, live=game.phase is Phase.LIVE)
        else:
            canvas.text(_I_CODE_X, top + code_row, team.abbreviation, color, font=code_font)
            if side.record is not None:
                record_color = _DIM_GOLD if _lost(game, which) else _GOLD
                canvas.text(_I_CODE_X, top + record_row, side.record.text(), record_color, font=record_font)
        score_color = _GREEN if which is scorer else color
        draw_right_aligned(canvas, _I_SCORE_RIGHT, top + _I_SCORE_ROW, _score_text(side.score), score_color, "6x10")
    _meter_if_any(canvas, treatment, game)
    _single_module(canvas, spec)


def _single_module(canvas: Canvas, spec: Specimen) -> None:
    """Right of the scores: the clock and pips; ``TO`` over the code while a timeout lasts; a break's words."""
    game = spec.game
    left, right = _I_MODULE
    if game.phase is Phase.LIVE and game.timeout is not None:
        draw_centered(canvas, left, right, _I_WORDS_Y[0], "TO", _GOLD, "5x7")
        draw_centered(canvas, left, right, _I_WORDS_Y[1], game.side(game.timeout).abbreviation, _WHITE, "5x7")
        return
    if game.phase is Phase.LIVE:
        clock = game.clock.text() if game.clock is not None else "--:--"
        draw_centered(canvas, left, right, _I_CLOCK_Y, clock, _GOLD, "4x6")
        if game.period <= _REGULATION:
            width = _pips_width(_PIP_SINGLE, _I_PIP_GAP)
            _draw_pips(canvas, left + (right - left - width) // 2, _I_PIP_Y, _PIP_SINGLE, _I_PIP_GAP, game.period)
        else:
            draw_centered(canvas, left, right, _I_PIP_Y - 1, _ot_label(game.period), _GOLD, "4x6")
        return
    if game.phase is Phase.FINAL:
        # A big F; in overtime, over the overtime in the largest face that fits the module, on the home score's
        # rows (the score's face and cell row) with two blank rows under the F.
        if game.period > _REGULATION:
            ot_row = _I_ROW + _I_SCORE_ROW
            draw_centered(canvas, left, right, ot_row - 12, "F", _GOLD, "8x13B")
            draw_centered(canvas, left, right, ot_row, _ot_label(game.period), _GOLD, "6x10")
        else:
            draw_centered(canvas, left, right, 10, "F", _GOLD, "8x13B")
        return
    words: tuple[str, str]
    if game.phase is Phase.END_PERIOD:
        words = ("END", f"Q{game.period}" if game.period <= _REGULATION else _ot_label(game.period))
    else:
        ends = game.break_ends
        words = ("HALF", _countdown(ends - spec.now) if ends is not None else "TIME")
    for word, y in zip(words, _I_WORDS_Y):
        word_font = "5x7" if text_width(word, "5x7") <= right - left else "4x6"
        draw_centered(canvas, left, right, y + (1 if word_font == "4x6" else 0), word, _GOLD, word_font)


_I_PREGAME_COLUMN = (26, 64)  # two clear columns past the widest 4x6 record
_I_PREGAME_ROWS = (5, 13, 21)  # cell tops of the tip, the countdown and the favorite's line


def _single_pregame(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    """Bars, each team's code over its record; at right, the tip over the countdown over the favorite's line.

    The code is 6x10, as on the live cards. The record is 4x6 here: the right column needs the columns a 5x7
    record would take (a five-character one ends at x28). The column is 38 wide, so the tip can't take 6x10
    (``6:00 PM`` needs 39 and ``12:00 PM`` 45); the tip and the countdown share 5x7. A tip on another day shows
    its date over its time in their place; a postponed game puts ``POSTPONED`` first and moves those two down one.
    """
    game = spec.game
    away, home = _teams(game)
    draw_single_bars(canvas, context, away, home)
    for side, top in ((game.away, 0), (game.home, _I_ROW)):
        canvas.text(_I_CODE_X, top + 1, side.abbreviation, _WHITE, font="6x10")
        if side.record is not None:
            canvas.text(_I_CODE_X, top + 10, side.record.text(), _STATS_TEAL, font="4x6")
    left, right = _I_PREGAME_COLUMN
    tip_y, countdown_y, line_y = _I_PREGAME_ROWS
    if game.tip is None:
        draw_centered(canvas, left, right, tip_y + 1, "POSTPONED", _GOLD, "4x6")
        draw_centered(canvas, left, right, countdown_y, "TIP TBD", _WHITE, "5x7")
        return
    upper, lower, clock = _tip_lines(spec.now, game.tip, context.timezone)
    slots: tuple[tuple[str, int, RGBColor, str], ...]
    if game.phase is Phase.POSTPONED:  # the status takes the tip's slot, so the tip's two lines move down one
        draw_centered(canvas, left, right, tip_y + 1, "POSTPONED", _GOLD, "4x6")
        slots = ((upper, countdown_y, _WHITE, "5x7"), (lower, line_y, _WHITE, "4x6"))
    else:
        slots = ((upper, tip_y, _GOLD, "5x7"), (lower, countdown_y, _WHITE, "5x7"))
    for text, y, color, font in slots:
        _draw_when(canvas, left + (right - left - _when_width(text, clock, font)) // 2, y, text, clock, color, font)
    if game.phase is Phase.PREGAME and game.line is not None:
        favorite = game.side(game.line.favorite).abbreviation
        draw_centered(canvas, left, right, line_y, f"{favorite} {game.line.moneyline:+d}", _ODDS_GREEN, "4x6")


# --- drawing a card ---------------------------------------------------------------------------------------

_SIZE = {PanelProfile.QUAD_128X64: (128, 64), PanelProfile.STACK_64X64: (64, 64), PanelProfile.SINGLE_64X32: (64, 32)}


def draw_card(spec: Specimen, profile: PanelProfile) -> Image.Image:
    """One specimen on one profile, 1 LED = 1 px."""
    width, height = _SIZE[profile]
    canvas = PillowCanvas(width, height)
    context = RenderContext(profile=profile, now=spec.now, logos=_LOGOS, timezone=_ZONE)
    unstarted = spec.game.phase in (Phase.PREGAME, Phase.POSTPONED)
    if profile is PanelProfile.QUAD_128X64:
        (_quad_pregame if unstarted else _quad_scored)(canvas, context, spec)
    elif profile is PanelProfile.STACK_64X64:
        (_stack_pregame if unstarted else _stack_scored)(canvas, context, spec)
    elif profile is PanelProfile.SINGLE_64X32:
        (_single_pregame if unstarted else _single_scored)(canvas, context, spec)
    else:  # pragma: no cover - exhaustiveness guard
        assert_never(profile)
    return canvas.image()


# --- contact sheets ---------------------------------------------------------------------------------------

_SCALE = {PanelProfile.QUAD_128X64: 4, PanelProfile.STACK_64X64: 6, PanelProfile.SINGLE_64X32: 6}
_COLUMNS = 4  # two pairs to a row
_SHEET_NAME = {
    PanelProfile.QUAD_128X64: "128×64",
    PanelProfile.STACK_64X64: "64×64",
    PanelProfile.SINGLE_64X32: "64×32",
}
_LEGEND = {
    PanelProfile.QUAD_128X64: (
        "Away top · home bottom    first 6 s: code over record    second 6 s: n×TO over BONUS    code and n×TO "
        "6x10 at any score; the record 6x10, 5x7 once a score reaches 100",
        "i/N = play i of the N queued, in ingest order    TIMEOUT holds the timeout's first turn, then the stat "
        "panels rotate until play resumes",
        "Player lines walk each team's plus-minus order, one player per team per turn    +n / -n = plus-minus",
    ),
    PanelProfile.STACK_64X64: (
        "Away top · home bottom    first 6 s: code (6x10; 5x7 once a score reaches 100) over record (4x6 at any "
        "score)    second 6 s: n×TO over BONUS",
        "Segmented divider = play queue, in ingest order    TIMEOUT holds the timeout's first turn, then the stat "
        "panels rotate    a ruling splits across its halves",
        "Line score: Q1/Q2 to the half, H1/Q3, Q3/Q4    final: the winner's best player by plus-minus, then the "
        "loser's    +n / -n = plus-minus",
    ),
    PanelProfile.SINGLE_64X32: (
        "Away top · home bottom    first 6 s: code (6x10; 5x7 once a score reaches 100) over record (5x7)    "
        "second 6 s: nTO over a boxed BON",
        "TO over the code replaces the clock and pips while a timeout lasts    F = final    "
        "no stat panels, captions or rulings on the single",
        "Pregame: tip over countdown over the favorite's moneyline; a tip on another day shows its date (m/d)",
    ),
}
_SHEET_BG = (15, 20, 26)
_CARD_BG = (29, 38, 48)
_CARD_EDGE = (46, 58, 72)
_PAIR_BG = (44, 56, 70)
_INK = (236, 239, 243)
_MUTED = (160, 170, 182)
_SYNTHETIC_INK = (232, 196, 90)
_GRID = (26, 26, 26)
_PAD = 12
_GAP = 14

_FontType = ImageFont.FreeTypeFont | ImageFont.ImageFont


def _font(size: int, *, bold: bool = False) -> _FontType:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for folder in (Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu")):
        if (folder / name).is_file():
            return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size=size)


def enlarge(image: Image.Image, scale: int) -> Image.Image:
    """Nearest-neighbor enlargement with a faint grid between the LEDs."""
    big = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    dimmed = big.point([v * 6 // 10 for v in range(256)] * 3)
    gridded = ImageChops.lighter(dimmed, Image.new("RGB", big.size, _GRID))
    mask = Image.new("L", big.size, 0)
    lines = ImageDraw.Draw(mask)
    for x in range(0, big.width, scale):
        lines.line((x, 0, x, big.height), fill=255)
    for y in range(0, big.height, scale):
        lines.line((0, y, big.width, y), fill=255)
    return Image.composite(gridded, big, mask)


def _place(cards: Sequence[tuple[Specimen, Image.Image]]) -> list[tuple[int, int, Specimen, Image.Image]]:
    """Grid cells for the cards, a pair always side by side starting on an even column."""
    groups: list[list[tuple[Specimen, Image.Image]]] = []
    for spec, image in cards:
        if groups and spec.pair and groups[-1][0][0].pair == spec.pair:
            groups[-1].append((spec, image))
        else:
            groups.append([(spec, image)])
    placed: list[tuple[int, int, Specimen, Image.Image]] = []
    row = col = 0
    for group in groups:
        if len(group) > 1 and col % 2:
            col += 1
        if col + len(group) > _COLUMNS:
            row, col = row + 1, 0
        for spec, image in group:
            placed.append((row, col, spec, image))
            col += 1
    return placed


def contact_sheet(profile: PanelProfile, cards: Sequence[tuple[Specimen, Image.Image]]) -> Image.Image:
    """Every card for one profile, enlarged, titled and labeled recorded or synthetic; a turn's halves paired."""
    scale = _SCALE[profile]
    width, height = _SIZE[profile]
    title_font, label_font = _font(15, bold=True), _font(12)
    head_font, sub_font = _font(28, bold=True), _font(13)
    card_w = width * scale + 2 * _PAD
    card_h = height * scale + 2 * _PAD + 40
    placed = _place(cards)
    rows = max(row for row, _, _, _ in placed) + 1
    header_h, footer_h = 92, 86
    sheet_w = _COLUMNS * card_w + (_COLUMNS + 1) * _GAP
    sheet_h = header_h + rows * card_h + (rows - 1) * _GAP + footer_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), _SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((_GAP + 4, 18), f"Omni · NBA cards, sixth pass   {_SHEET_NAME[profile]}", fill=_INK, font=head_font)
    draw.text(
        (_GAP + 4, 58),
        f"{scale}× pixel grid  ·  recorded BALLDONTLIE Free states and labeled synthetic specimens  ·  "
        "every win-meter value is mocked  ·  a live turn's two 6 s halves sit side by side on one backdrop",
        fill=_MUTED,
        font=sub_font,
    )

    def origin(row: int, col: int) -> tuple[int, int]:
        return _GAP + col * (card_w + _GAP), header_h + row * (card_h + _GAP)

    for row, col, spec, _ in placed:
        if spec.pair and col % 2 == 0:
            partner = [c for r, c, s, _ in placed if r == row and c == col + 1 and s.pair == spec.pair]
            if partner:
                x, y = origin(row, col)
                draw.rounded_rectangle(
                    (x - 6, y - 6, x + 2 * card_w + _GAP + 5, y + card_h + 5), radius=9, fill=_PAIR_BG
                )
    for row, col, spec, image in placed:
        x, y = origin(row, col)
        draw.rounded_rectangle((x, y, x + card_w - 1, y + card_h - 1), radius=6, fill=_CARD_BG, outline=_CARD_EDGE)
        draw.text((x + _PAD, y + 8), spec.title, fill=_INK, font=title_font)
        tag = "Synthetic" if spec.synthetic else "Recorded"
        label = (
            spec.provenance if spec.provenance.startswith(("Recorded", "Synthetic")) else f"{tag} · {spec.provenance}"
        )
        draw.text((x + _PAD, y + 28), label, fill=_SYNTHETIC_INK if spec.synthetic else _MUTED, font=label_font)
        sheet.paste(enlarge(image, scale), (x + _PAD, y + 40 + _PAD - 4))
    for i, line in enumerate(_LEGEND[profile]):
        draw.text((_GAP + 4, sheet_h - footer_h + 12 + 22 * i), line, fill=_MUTED, font=sub_font)
    return sheet


# --- checking the team lanes against the scores ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaneFit:
    """One team-lane element at its widest, against the score in the same team row.

    Columns are board columns; rows are offsets below the team row's top.
    """

    profile: PanelProfile
    three_digits: bool
    element: str
    font: str
    text: str
    ends: int  # the last column its ink (or its box) reaches
    rows: tuple[int, int]
    score_starts: int  # the first column of the score's ink
    score_rows: tuple[int, int]

    @property
    def clear(self) -> int | None:
        """Blank columns between it and the score, or None when it sits wholly above or below the score's ink."""
        if self.rows[1] < self.score_rows[0] or self.rows[0] > self.score_rows[1]:
            return None
        return self.score_starts - self.ends - 1


def _ink_extent(font: str, text: str) -> tuple[int, int, int, int]:
    """The first and last columns, then the first and last rows, that ``text``'s ink reaches in ``font``."""
    lit = [(x, y) for y, row in enumerate(rasterize(font, text)) for x, on in enumerate(row) if on]
    xs, ys = [x for x, _ in lit], [y for _, y in lit]
    return min(xs), max(xs), min(ys), max(ys)


def _widest_code(font: str) -> str:
    return max(NBA_TEAMS, key=lambda code: _ink_extent(font, code)[1])


def _widest_record(font: str) -> str:
    """The widest record a season can show: five characters, ending in the digit whose ink reaches furthest."""
    digit = max("0123456789", key=lambda d: _ink_extent(font, d)[1])
    return f"{digit}{digit}-{digit}{digit}"


def _score_ink(font: str, right: int, row: int, three: bool) -> tuple[int, tuple[int, int]]:
    """Where the score's ink starts at its widest (a 1xx, or any two digits), and the rows it covers."""
    lead = "1" if three else "123456789"
    start = right - (3 if three else 2) * text_width("0", font) + min(_ink_extent(font, d)[0] for d in lead)
    _, _, top, bottom = _ink_extent(font, "0123456789")
    return start, (row + top, row + bottom)


def _fit(
    profile: PanelProfile,
    three: bool,
    element: str,
    font: str,
    text: str,
    x: int,
    row: int,
    score: tuple[str, int, int],
) -> LaneFit:
    left, right, top, bottom = _ink_extent(font, text)
    starts, rows = _score_ink(*score, three)
    return LaneFit(profile, three, element, font, text, x + right, (row + top, row + bottom), starts, rows)


def _box_fit(
    profile: PanelProfile, three: bool, text: str, font: str, x: int, row: int, score: tuple[str, int, int]
) -> LaneFit:
    starts, rows = _score_ink(*score, three)
    ends = x + text_width(text, font)  # the box's last column, as `_draw_inverted` fills it
    return LaneFit(profile, three, "bonus", font, text, ends, (row, row + _box_height(font) - 1), starts, rows)


def lane_fits() -> list[LaneFit]:
    """Every team-lane element at its widest, on every profile, with two-digit and with three-digit scores."""
    fits: list[LaneFit] = []
    quad = (PanelProfile.QUAD_128X64, ("9x18B", _Q_SCORE_RIGHT, 1))
    stack = (PanelProfile.STACK_64X64, ("8x13B", 64, 1))
    single = (PanelProfile.SINGLE_64X32, ("6x10", _I_SCORE_RIGHT, _I_SCORE_ROW))
    for three in (False, True):
        profile, score = quad
        (code_font, code_row), (record_font, record_row) = _Q_CODE, _Q_RECORD[three]
        counts_font, counts_row = _Q_COUNTS
        fits += [
            _fit(profile, three, "code", code_font, _widest_code(code_font), _Q_LANE_X, code_row, score),
            _fit(profile, three, "record", record_font, _widest_record(record_font), _Q_LANE_X, record_row, score),
            _fit(profile, three, "timeouts", counts_font, f"7{_TIMES}TO", _Q_LANE_X, counts_row, score),
            _box_fit(profile, three, "BONUS", "5x7", _Q_LANE_X, _Q_BONUS_ROW, score),
        ]
        profile, score = stack
        (code_font, code_row), (record_font, record_row) = _S_CODE[three], _S_RECORD
        counts_font = _S_COUNTS[three]
        fits += [
            _fit(profile, three, "code", code_font, _widest_code(code_font), _S_LANE_X, code_row, score),
            _fit(profile, three, "record", record_font, _widest_record(record_font), _S_LANE_X, record_row, score),
            _fit(profile, three, "timeouts", counts_font, f"7{_TIMES}TO", _S_LANE_X, _S_COUNTS_ROW, score),
            _box_fit(profile, three, "BONUS", "4x6", _S_LANE_X, _S_BONUS_ROW, score),
        ]
        profile, score = single
        code_font, code_row = _I_CODE[three]
        record_font, record_row = _I_RECORD
        top = _single_count_tops(_I_COUNTS_FONT)[HomeAway.AWAY]
        box_row = top + _ink_height(_I_COUNTS_FONT) + _I_STACK_GAP
        fits += [
            _fit(profile, three, "code", code_font, _widest_code(code_font), _I_CODE_X, code_row, score),
            _fit(profile, three, "record", record_font, _widest_record(record_font), _I_CODE_X, record_row, score),
            _fit(profile, three, "timeouts", _I_COUNTS_FONT, "7TO", _I_CODE_X, top, score),
            _box_fit(profile, three, _I_BONUS, _I_COUNTS_FONT, _I_CODE_X, box_row, score),
        ]
    return fits


def _lane_table(fits: Sequence[LaneFit]) -> str:
    lines = []
    for f in fits:
        digits = "1xx" if f.three_digits else "2-digit"
        if f.clear is None:
            under = f.ends - f.score_starts + 1
            where = "below the score" + (f", {under} columns under it" if under > 0 else "")
        else:
            where = f"{f.clear} blank before x{f.score_starts}"
        lines.append(
            f"{f.profile.value:13} {digits:7} {f.element:8} {f.font:5} {f.text:6} "
            f"ink x..{f.ends} rows {f.rows[0]}-{f.rows[1]}: {where}"
        )
    return "\n".join(lines)


# --- checking the recorded specimens ----------------------------------------------------------------------


def _sample_mismatches(spec: Specimen, row: dict[str, Any]) -> list[str]:
    """Where a recorded specimen disagrees with the feed row it was transcribed from."""
    problems: list[str] = []
    game = spec.game
    feed = {"away": "visitor", "home": "home"}
    for which in (HomeAway.AWAY, HomeAway.HOME):
        side, prefix = game.side(which), feed[which.value]
        if row[f"{prefix}_team"]["abbreviation"] != side.abbreviation:
            problems.append(f"{which.value} team")
        if game.phase is Phase.PREGAME:
            continue
        if row[f"{prefix}_team_score"] != side.score:
            problems.append(f"{which.value} score")
        if game.phase is not Phase.FINAL and row[f"{prefix}_timeouts_remaining"] != side.timeouts:
            problems.append(f"{which.value} timeouts")
        if game.phase is Phase.LIVE and bool(row[f"{prefix}_in_bonus"]) != side.in_bonus:
            problems.append(f"{which.value} bonus")
        recorded = [row[f"{prefix}_q{i}"] for i in range(1, 5)][: len(side.periods)]
        if tuple(recorded) != side.periods:
            problems.append(f"{which.value} periods")
    if game.phase is Phase.PREGAME:
        if game.tip is None or _utc(row["datetime"]) != game.tip:
            problems.append("tip")
        return problems
    if row["period"] != game.period:
        problems.append("period")
    if game.clock is not None and not str(row["time"]).endswith(game.clock.text()):
        problems.append("clock")
    return problems


def check_sample(path: Path) -> list[str]:
    """Every recorded specimen against its state in the recording; an empty list when all agree."""
    games = json.loads(path.read_text())["games"]
    problems: list[str] = []
    for spec in SPECIMENS:
        if spec.source is None:
            continue
        row = games[spec.source.game]["states"][spec.source.state]["row"]
        problems.extend(f"{spec.key}: {problem}" for problem in _sample_mismatches(spec, row))
    return problems


# --- entry point ------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Draw the NBA cards' sixth design pass.")
    parser.add_argument("--out", type=Path, required=True, help="where native/ and the contact sheets go")
    parser.add_argument("--sample", type=Path, help="the BALLDONTLIE recording to check the recorded specimens against")
    parser.add_argument("--fits", action="store_true", help="print each team-lane element's room beside the score")
    args = parser.parse_args()
    fits = lane_fits()
    if args.fits:
        print(_lane_table(fits))
    touching = [f for f in fits if f.clear is not None and f.clear < 1]
    if touching:
        print("team-lane elements touch the score:\n" + _lane_table(touching), file=sys.stderr)
        return 1
    if args.sample is not None:
        problems = check_sample(args.sample)
        if problems:
            print("recorded specimens disagree with the sample:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        print(f"checked {sum(1 for s in SPECIMENS if s.source is not None)} specimens against {args.sample}")
    out: Path = args.out
    for profile in PanelProfile:
        folder = out / "native" / profile.value
        folder.mkdir(parents=True, exist_ok=True)
        cards = []
        for spec in SPECIMENS:
            if profile not in spec.profiles:
                continue
            image = draw_card(spec, profile)
            image.save(folder / f"{spec.key}.png")
            cards.append((spec, image))
        contact_sheet(profile, cards).save(out / f"nba-{_PASS}-contact-{profile.value}.png")
        print(f"{profile.value}: {len(cards)} cards")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
