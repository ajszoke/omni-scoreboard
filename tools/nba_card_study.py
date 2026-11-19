"""Draw the NBA cards' third design pass: native mock cards on all three profiles, and contact sheets.

    python tools/nba_card_study.py --out <dir> [--sample <BALLDONTLIE sample json>]

A design study, not a renderer. It draws with the repo's own drawing stack (`PillowCanvas`, the patched BDF
faces, the matchup treatment and logo store, the win meter, text lanes) from the small typed mock inputs
defined here, so what the sheets show is what the board's own pixels would be. It writes
``native/<profile>/<card>.png`` (1 LED = 1 px) and ``nba-v3-contact-<profile>.png`` (enlarged, with a faint
pixel grid, a title per card and whether the card's data is recorded or synthetic) under ``--out``.

A live card's 12 s turn has two 6 s halves: the first names each team (its code over its record), the second
shows what each team has left (its timeouts over the bonus). The sheets set each pair side by side.

The recorded specimens were transcribed from a BALLDONTLIE Free recording of the 2026-11-16 slate. Given
``--sample``, the tool checks each one against the state it names before drawing anything. Player names,
stat lines, records, odds and every win-probability value are synthetic.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from omni.providers.nba_teams import NbaTeamRegistry  # noqa: E402
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

_PASS = "v3"

# --- colors: the house set, the MLB pregame's odds and stats colors, and NFL's play tones ---------------

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_GOLD = RGBColor(255, 215, 0)
_DIM_GOLD = RGBColor(110, 92, 0)  # an unbegun quarter's pip ring, and a final's losing record
_SECONDARY = RGBColor(180, 180, 180)
_LOSER = RGBColor(110, 110, 110)
_DIVIDER = RGBColor(55, 55, 55)
_RED = RGBColor(255, 50, 50)  # NFL's turnover red, here also the bonus box
_GREEN = RGBColor(60, 220, 90)  # NFL's scoring-play green
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
    HIGHLIGHTS = "highlights"


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
    """One play, put into a board's words: a headline in the play's tone over a detail line."""

    tone: Tone
    headline: str
    detail: str
    team: HomeAway  # whose play it was


@dataclass(frozen=True, slots=True)
class TeamBox:
    """A team's box-score line."""

    fg_pct: int
    threes_made: int
    threes_attempted: int
    rebounds: int
    assists: int
    turnovers: int


@dataclass(frozen=True, slots=True)
class Leader:
    """One player's line in the highlights: points, then shooting as makes over attempts."""

    name: str
    points: int
    shooting: str  # "2/5 3PT 6/11 FG"

    def stats(self) -> str:
        return f"{self.points} PTS {self.shooting}"


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
    box: TeamBox | None = None
    leader: Leader | None = None


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

    def side(self, which: HomeAway) -> Side:
        return self.away if which is HomeAway.AWAY else self.home


@dataclass(frozen=True, slots=True)
class StateRef:
    """A state in the recording: its game and state index (both zero-based)."""

    game: int
    state: int


_ALL = frozenset(PanelProfile)
_NO_SINGLE = frozenset({PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64})


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
) -> tuple[Specimen, ...]:
    """One live turn as its two 6 s halves: the teams named, then what they have left."""
    halves = (("a", "first 6 s", Half.NAMES, scroll[0]), ("b", "second 6 s", Half.COUNTS, scroll[1]))
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

_POR_BOX = TeamBox(fg_pct=44, threes_made=7, threes_attempted=21, rebounds=24, assists=13, turnovers=9)
_MIN_BOX = TeamBox(fg_pct=52, threes_made=9, threes_attempted=22, rebounds=29, assists=18, turnovers=6)

# POR at MIN, game 0 state 160: Q3 6:07, the base for the three rotation panels.
_Q3_AWAY = Side("POR", 58, 4, False, (26, 25, 7), _POR, box=_POR_BOX, leader=Leader("D.Avdija", 16, "2/5 3PT 6/11 FG"))
_Q3_HOME = Side("MIN", 68, 5, False, (26, 33, 9), _MIN, box=_MIN_BOX, leader=Leader("A.Edwards", 22, "4/9 3PT 8/15 FG"))
_Q3_NOW = _utc("2026-11-17T02:39:42Z")


def _q3(bottom: Bottom) -> Game:
    return Game(_Q3_AWAY, _Q3_HOME, Phase.LIVE, 3, GameClock(3670), home_win_pct=84.0, bottom=bottom)


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
        "Recorded read (both teams scored) · synthetic plays" if first else "Synthetic second read at Q3 2:05 · plays",
        True,
        game,
        _utc("2026-11-17T02:52:08Z") + timedelta(seconds=6 * (i - 1)),
        source=StateRef(0, 181) if first else None,
        half=half,
        pair="live-queue-12" if i <= 2 else "live-queue-34",
    )


_LATE_PERIODS = ((26, 25, 22, 25), (26, 33, 28, 11))  # POR at MIN, tied after four (synthetic Q4)

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
        "Line score",
        "Recorded · POR at MIN, Q3 6:07",
        False,
        _q3(Bottom.LINE_SCORE),
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
        "live-box-score",
        "Team box score",
        "Recorded state · synthetic box score",
        True,
        _q3(Bottom.BOX_SCORE),
        _Q3_NOW,
        source=StateRef(0, 160),
        profiles=_NO_SINGLE,
    ),
    *_pair(
        "live-highlights",
        "Player highlights",
        "Recorded state · synthetic players",
        True,
        _q3(Bottom.HIGHLIGHTS),
        _Q3_NOW,
        source=StateRef(0, 160),
        scroll=(1.0, 10.0),  # the head, then the tail
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
        "Overtime",
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
        "Double overtime",
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
    Specimen(
        "final",
        "Final · teams named all turn",
        "Recorded · POR at MIN",
        False,
        Game(
            Side("POR", 98, 2, True, (26, 25, 22, 25), Record(6, 9)),
            Side("MIN", 106, 1, True, (26, 33, 28, 19), Record(10, 5)),
            Phase.FINAL,
            4,
        ),
        now=_utc("2026-11-17T03:56:15Z"),
        source=StateRef(0, 212),
    ),
    Specimen(
        "final-overtime",
        "Final · double overtime",
        "Synthetic · DET at GSW color clash",
        True,
        Game(
            Side("DET", 117, None, False, (29, 24, 29, 22, 7, 6), Record(10, 5)),
            Side("GSW", 119, None, False, (25, 28, 25, 26, 7, 8), Record(9, 7)),
            Phase.FINAL,
            6,
        ),
        now=_utc("2026-11-17T04:40:00Z"),
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


def _draw_inverted(canvas: Canvas, x: int, y: int, text: str, font: str) -> None:
    """``text`` inverted, black letters on a red box with a pixel of red around them; the box's top-left at (x, y)."""
    width = text_width(text, font)  # the last glyph's blank column is the box's right margin
    canvas.fill_rect(x, y, width + 1, _ink_height(font) + 2, _RED)
    canvas.text(x + 1, y + 1, text, _BLACK, font=font)


def _timeouts_text(timeouts: int | None) -> str:
    """Timeouts left, the count first: ``4×TO``."""
    return f"{'-' if timeouts is None else timeouts}{_TIMES}TO"


def _score_text(score: int | None) -> str:
    return "-" if score is None else str(score)


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


def _time_to_tip(now: datetime, tip: datetime) -> str:
    """How long until tip: ``1h 23m``, ``45m``, ``1d 2h`` a day or more out, ``SOON`` inside a minute."""
    minutes = int((tip - now).total_seconds() // 60)
    if minutes < 1:
        return "SOON"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


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


def _shows_counts(spec: Specimen) -> bool:
    """Whether a frame shows timeouts and bonus: a live or end-of-period card's second 6 s.

    At halftime and a final the counts no longer matter, so the teams stay named for the whole turn.
    """
    return spec.half is Half.COUNTS and spec.game.phase in (Phase.LIVE, Phase.END_PERIOD)


def _names(canvas: Canvas, game: Game, which: HomeAway, x: int, ys: tuple[int, int], font: str) -> None:
    """A team's code (white) over its record (gold); a final's loser dimmed."""
    side = game.side(which)
    lost = _lost(game, which)
    canvas.text(x, ys[0], side.abbreviation, _LOSER if lost else _WHITE, font=font)
    if side.record is not None:
        canvas.text(x, ys[1], side.record.text(), _DIM_GOLD if lost else _GOLD, font=font)


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


# --- quad 128x64 ------------------------------------------------------------------------------------------

_Q_ROW = 20
_Q_LANE_X = 23
_Q_SCORE_RIGHT = 80
_Q_MODULE = (82, 128)
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
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _Q_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)
        if _shows_counts(spec):
            canvas.text(_Q_LANE_X, top + 3, _timeouts_text(side.timeouts), _GOLD, font="5x7")
            if game.phase is Phase.LIVE and side.in_bonus:
                _draw_inverted(canvas, _Q_LANE_X, top + 11, "BONUS", "5x7")
        else:
            _names(canvas, game, which, _Q_LANE_X, (top + 3, top + 12), "5x7")
        draw_right_aligned(canvas, _Q_SCORE_RIGHT, top + 1, _score_text(side.score), _text_color(game, which), "9x18B")
    _meter_if_any(canvas, treatment, game)
    _quad_module(canvas, spec)
    canvas.fill_rect(0, _Q_DIVIDER_Y, 128, 1, _DIVIDER)
    if game.bottom is Bottom.CAPTION and game.caption is not None:
        _quad_caption(canvas, spec, game.caption)
    elif game.bottom is Bottom.BOX_SCORE:
        _quad_box(canvas, game)
    elif game.bottom is Bottom.HIGHLIGHTS:
        _quad_highlights(canvas, spec)
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
    for word, y in rows:
        draw_centered(canvas, left, right, y, word, _GOLD, "8x13")


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
        if side.box is None:
            continue
        for (_, right), value in zip(_BOX_COLUMNS, _box_values(side.box)):
            draw_right_aligned(canvas, right, y, value, _WHITE, "4x6")


def _quad_highlights(canvas: Canvas, spec: Specimen) -> None:
    """Each team's leader, away over home: the code, the name held still, and the stats scrolling beside it.

    The names stay put while the stats scroll, as the house's name-with-stats lines do. Both stat lines start in
    one column past the longer name, so like-for-like stats line up and the two lanes move together.
    """
    game = spec.game
    now = _lane_now(spec)
    leaders = [(which, game.side(which)) for which in (HomeAway.AWAY, HomeAway.HOME)]
    names = [side.leader.name for _, side in leaders if side.leader is not None]
    stats_x = 20 + max((text_width(f"{name} ", "5x7") for name in names), default=0)
    for (which, side), y in zip(leaders, (44, 55)):
        canvas.text(4, y + 1, side.abbreviation, _SECONDARY, font="4x6")
        if side.leader is None:
            continue
        canvas.text(20, y, side.leader.name, _WHITE, font="5x7")
        lane = TextLane(x=stats_x, y=y, width=128 - stats_x, font="5x7")
        draw_text_lane(canvas, lane, side.leader.stats(), _STATS_TEAL, now=now)


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
    clock = start_time_label(game.tip, context.timezone)
    _draw_time(canvas, _quad_column_x(_time_width(clock, "6x10")), 27, clock, _WHITE, "6x10")
    _quad_column(canvas, 39, _time_to_tip(spec.now, game.tip), _WHITE)


_Q_COLUMN_CENTER = 107


def _quad_column_x(width: int) -> int:
    """Where a right-column item starts: centered on the column, pulled left if it would run off the board."""
    return min(_Q_COLUMN_CENTER - width // 2, 128 - width)


def _quad_column(canvas: Canvas, y: int, text: str, color: RGBColor) -> None:
    canvas.text(_quad_column_x(text_width(text, "6x10")), y, text, color, font="6x10")


# --- stack 64x64 ------------------------------------------------------------------------------------------

_S_ROW = 20
_S_LANE_X = 23
_S_CLOCK_Y = 40
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
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _S_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)
        if _shows_counts(spec):
            canvas.text(_S_LANE_X, top + 4, _timeouts_text(side.timeouts), _GOLD, font="4x6")
            if game.phase is Phase.LIVE and side.in_bonus:
                _draw_inverted(canvas, _S_LANE_X, top + 13, "BONUS", "4x6")
        else:
            _names(canvas, game, which, _S_LANE_X, (top + 4, top + 14), "4x6")
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
    """The clock beside the quarter pips (or the overtime), centered; at a break, the break's words."""
    game = spec.game
    font, y = "6x10", _S_CLOCK_Y
    if game.phase is Phase.LIVE:
        clock = game.clock.text() if game.clock is not None else "--:--"
        clock_w = text_width(clock, font)
        if game.period <= _REGULATION:
            tail_w = _pips_width(_PIP_STACK, _S_PIP_GAP)
        else:
            tail_w = text_width(_ot_label(game.period), font)
        x = (64 - clock_w - 4 - tail_w) // 2
        canvas.text(x, y, clock, _GOLD, font=font)
        tail_x = x + clock_w + 4
        if game.period <= _REGULATION:
            _draw_pips(canvas, tail_x, y + 2, _PIP_STACK, _S_PIP_GAP, game.period)
        else:
            canvas.text(tail_x, y, _ot_label(game.period), _GOLD, font=font)
        return
    if game.phase is Phase.END_PERIOD:
        label = f"END Q{game.period}" if game.period <= _REGULATION else f"END {_ot_label(game.period)}"
    elif game.phase is Phase.HALFTIME:
        ends = game.break_ends
        label = f"HALF {_countdown(ends - spec.now)}" if ends is not None else "HALFTIME"
    else:
        label = f"FINAL {_ot_label(game.period)}" if game.period > _REGULATION else "FINAL"
    draw_centered(canvas, 0, 64, y, label, _GOLD, font)


def _stack_bottom(canvas: Canvas, spec: Specimen) -> None:
    """Two small lines under the divider: the play, the rotation's panel, or a run while one is on."""
    game = spec.game
    first, second = _S_LINES_Y
    now = _lane_now(spec)
    if game.phase is Phase.LIVE and game.caption is not None and game.bottom is Bottom.CAPTION:
        caption = game.caption
        lane = TextLane(x=0, y=first, width=64, font="4x6", anchor=LaneAnchor.CENTER)
        draw_text_lane(canvas, lane, caption.headline, _TONE_COLOR[caption.tone], now=now)
        lane = TextLane(x=0, y=second, width=64, font="4x6", anchor=LaneAnchor.CENTER)
        draw_text_lane(canvas, lane, caption.detail, _WHITE, now=now)
        return
    if game.phase is Phase.LIVE and game.run is not None:
        run = game.run
        draw_centered(canvas, 0, 64, first, f"{game.side(run.team).abbreviation} {run.points}-0", _WHITE, "4x6")
        draw_centered(canvas, 0, 64, second, f"LAST {_span_text(run.span_seconds)}", _SECONDARY, "4x6")
        return
    if game.bottom is Bottom.BOX_SCORE and game.away.box is not None and game.home.box is not None:
        lines = (
            ("FG%", f"{game.away.box.fg_pct}-{game.home.box.fg_pct}"),
            ("REB", f"{game.away.box.rebounds}-{game.home.box.rebounds}"),
        )
        for (label, values), y in zip(lines, _S_LINES_Y):
            _stack_labeled(canvas, y, label, _SECONDARY, values)
        return
    if game.bottom is Bottom.HIGHLIGHTS:
        # One leader a half, away first, as the tiles above sit: the name and points, then the shooting.
        leader = (game.away if spec.half is Half.NAMES else game.home).leader
        if leader is not None:
            name, points = f"{leader.name} ", f"{leader.points} PTS"
            x = (64 - text_width(name + points, "4x6")) // 2
            canvas.text(x, first, name, _WHITE, font="4x6")
            canvas.text(x + text_width(name, "4x6"), first, points, _STATS_TEAL, font="4x6")
            lane = TextLane(x=0, y=second, width=64, font="4x6", anchor=LaneAnchor.CENTER)
            draw_text_lane(canvas, lane, leader.shooting, _STATS_TEAL, now=now)
        return
    _stack_line_score(canvas, game)


def _stack_labeled(canvas: Canvas, y: int, label: str, label_color: RGBColor, values: str) -> None:
    """``label away-home``, centered."""
    total = text_width(f"{label} {values}", "4x6")
    x = (64 - total) // 2
    canvas.text(x, y, label, label_color, font="4x6")
    canvas.text(x + text_width(f"{label} ", "4x6"), y, values, _WHITE, font="4x6")


def _stack_line_score(canvas: Canvas, game: Game) -> None:
    """The two latest periods, one per line, ``Q2 25-33``: the one under way gold, a finished one gray."""
    entered = min(len(game.away.periods), len(game.home.periods))
    headers = _period_headers(entered)
    start = max(0, entered - 2)
    for line, i in enumerate(range(start, entered)):
        live = game.phase is Phase.LIVE and i == game.period - 1
        values = f"{game.away.periods[i]}-{game.home.periods[i]}"
        _stack_labeled(canvas, _S_LINES_Y[line], headers[i], _GOLD if live else _SECONDARY, values)


def _stack_pregame(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    """Marks with the records mirrored about the odds row, each with its conference rank; then tip and countdown."""
    game = spec.game
    away, home = _teams(game)
    draw_matchup_marks(canvas, context, away, home, away_top=0, home_top=27)
    rows = ((away, game.away, 10, 3), (home, game.home, 28, 39))  # (team, side, record y, standing y)
    for team, side, record_y, standing_y in rows:
        if side.record is not None:
            canvas.text(23, record_y, side.record.text(), _WHITE, font="6x10")
        if side.ranks is not None:
            canvas.text(23, standing_y, _standing_text(team, side.ranks, division=False), _STATS_TEAL, font="4x6")
    if game.line is not None:
        draw_centered(canvas, 0, 64, 21, _odds_text(game, game.line), _ODDS_GREEN, "4x6")
    if game.phase is Phase.PREGAME and game.tip is not None:
        clock = start_time_label(game.tip, context.timezone)
        label_w = text_width("TIPOFF ", "4x6")
        x = (64 - label_w - _time_width(clock, "4x6")) // 2
        canvas.text(x, 48, "TIPOFF", _GOLD, font="4x6")
        _draw_time(canvas, x + label_w, 48, clock, _WHITE, "4x6")
    else:
        draw_centered(canvas, 0, 64, 48, "POSTPONED", _GOLD, "4x6")
    big = _time_to_tip(spec.now, game.tip) if game.tip is not None else "TIP TBD"
    draw_centered(canvas, 0, 64, 53, big, _WHITE, "6x10")


# --- single 64x32 -----------------------------------------------------------------------------------------

_I_ROW = 16
_I_CODE_X = 5
_I_SCORE_RIGHT = 42
_I_MODULE = (44, 64)
_I_CLOCK_Y = 10  # the clock and pips together, centered in the module
_I_PIP_Y = 18
_I_PIP_GAP = 1
_I_WORDS_Y = (7, 16)


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
    # A three-digit score reaches back to the lane, so the lane drops a size, as the NFL single board's code does.
    scores = [score for score in (game.away.score, game.home.score) if score is not None]
    font, drop = ("4x6", 1) if scores and max(scores) >= 100 else ("5x7", 0)
    rows = ((HomeAway.AWAY, away, treatment.away, 0), (HomeAway.HOME, home, treatment.home, _I_ROW))
    for which, team, mark, top in rows:
        side = game.side(which)
        draw_treated_mark(canvas, context, team, mark)  # a color bar: a 20px tile can't fit a 16px row
        color = _text_color(game, which)
        y = top + 4 + drop
        if _shows_counts(spec):
            counts = f"{'-' if side.timeouts is None else side.timeouts}TO"
            canvas.text(_I_CODE_X, y, counts, _GOLD, font=font)
            if game.phase is Phase.LIVE and side.in_bonus:  # the trailing blank column is the gap before the box
                _draw_inverted(canvas, _I_CODE_X + text_width(counts, font), y - 1, "B", font)
        else:
            canvas.text(_I_CODE_X, y, team.abbreviation, color, font=font)
        score_color = _GREEN if which is scorer else color
        draw_right_aligned(canvas, _I_SCORE_RIGHT, top + 3, _score_text(side.score), score_color, "6x10")
    _meter_if_any(canvas, treatment, game)
    left, right = _I_MODULE
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
        if game.period > _REGULATION:
            draw_centered(canvas, left, right, 8, "FIN", _GOLD, "5x8")
            draw_centered(canvas, left, right, 18, _ot_label(game.period), _GOLD, "4x6")
        else:
            draw_centered(canvas, left, right, 12, "FIN", _GOLD, "5x8")
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


def _single_pregame(canvas: Canvas, context: RenderContext, spec: Specimen) -> None:
    """Bars and codes; at right the tip time (or the status) over the countdown."""
    game = spec.game
    away, home = _teams(game)
    draw_single_bars(canvas, context, away, home)
    canvas.text(_I_CODE_X, 4, away.abbreviation, _WHITE, font="5x7")
    canvas.text(_I_CODE_X, 20, home.abbreviation, _WHITE, font="5x7")
    left, right = 24, 64  # three clear columns past the widest code
    if game.phase is Phase.PREGAME and game.tip is not None:
        clock = start_time_label(game.tip, context.timezone)
        width = _time_width(clock, "5x7")
        _draw_time(canvas, left + (right - left - width) // 2, 4, clock, _GOLD, "5x7")
    else:
        draw_centered(canvas, left, right, 5, "POSTPONED", _GOLD, "4x6")
    if game.tip is None:
        draw_centered(canvas, left, right, 20, "TIP TBD", _WHITE, "5x7")
        return
    countdown = _time_to_tip(spec.now, game.tip)
    if text_width(countdown, "6x10") <= right - left:
        draw_centered(canvas, left, right, 17, countdown, _WHITE, "6x10")
    else:  # ten hours or more out: a size down rather than into the code
        draw_centered(canvas, left, right, 20, countdown, _WHITE, "5x7")


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
    header_h, footer_h = 92, 64
    sheet_w = _COLUMNS * card_w + (_COLUMNS + 1) * _GAP
    sheet_h = header_h + rows * card_h + (rows - 1) * _GAP + footer_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), _SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((_GAP + 4, 18), f"Omni · NBA cards, third pass   {_SHEET_NAME[profile]}", fill=_INK, font=head_font)
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
    draw.text(
        (_GAP + 4, sheet_h - footer_h + 12),
        "Away top · home bottom    first 6 s: code over record    second 6 s: n×TO over BONUS    "
        "gold pips = quarters begun    LAST m:ss = game-clock span of the run",
        fill=_MUTED,
        font=sub_font,
    )
    draw.text(
        (_GAP + 4, sheet_h - footer_h + 34),
        "i/N = play i of the N that have queued while more than one waits; each shows 6 s, and N grows as plays arrive",
        fill=_MUTED,
        font=sub_font,
    )
    return sheet


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
    parser = argparse.ArgumentParser(description="Draw the NBA cards' third design pass.")
    parser.add_argument("--out", type=Path, required=True, help="where native/ and the contact sheets go")
    parser.add_argument("--sample", type=Path, help="the BALLDONTLIE recording to check the recorded specimens against")
    args = parser.parse_args()
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
