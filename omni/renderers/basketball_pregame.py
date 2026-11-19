"""Renderer for the NBA pregame card, scheduled or postponed, on the quad and stack boards (the single follows).

A pregame card names the clubs (their marks and codes), their records, and when the game tips. The tip reads as
a label and what it's about: ``TIP`` and the time with a countdown under it (``6:00 PM`` over ``1h 23m``, ``SOON``
inside a minute), or on another day the date over the time (``11/20`` over ``6:00 PM``). A postponed game reads
``PPD`` in the label's place, over its new tip, or over ``TIP TBD`` until one is set. Times are drawn with a
three-pixel space, a pixel narrower than the faces' own. The countdown runs off the render clock, so one card
stays right as it ticks.

- quad_128x64 : tiles with each club's record (6x10) and standing (``11th West, 4th NW``, teal), the top club's
  standing over its record and the bottom's under it, so the records flank the odds row (``MIN -265 (-6.5)``,
  green). At right, a column: the label (9x18B, gold) on the top record's line, the time or date (6x13) beside
  the odds, and the countdown, or the time under a date, (6x10) on the bottom record's line. COMPROMISE: when
  the odds would leave no blank column before the tip time, date or ``TIP TBD`` beside them, they drop their
  spread.
- stack_64x64 : tiles, each club's code (white) over its record (teal), both in 6x10, the odds row between; then
  the tip in two lines: the label and what it's about in 5x7 (``TIP 6:00 PM``), the countdown or the time
  under it in 4x6. COMPROMISE: no standings; odds too wide for the board drop their spread.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

from omni.cards.base import ScoreboardCard
from omni.cards.basketball import BasketballPregameCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.basketball import BasketballPhase, FavoriteLine, StandingRanks
from omni.domain.contest import TeamGame
from omni.domain.teams import BasketballTeam, Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.font import rasterize
from omni.renderers.pregame import start_time_label
from omni.renderers.team_row import draw_matchup_marks
from omni.renderers.text import draw_centered, text_width

__all__ = ["BasketballPregameRenderer", "TipLines", "Tip", "tip_lines"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_GOLD = RGBColor(255, 215, 0)
_ODDS_GREEN = RGBColor(50, 180, 50)  # the MLB pregame's odds
_STATS_TEAL = RGBColor(50, 180, 180)  # the MLB pregame's stat lines

_TIME_SPACE = 3  # between a time's clock and its AM or PM

# The Atlantic keeps mixed case and a period so it can't be read as Atlanta's code.
_DIVISION = {
    "Atlantic": "Atl.",
    "Central": "CEN",
    "Southeast": "SE",
    "Northwest": "NW",
    "Pacific": "PAC",
    "Southwest": "SW",
}


@dataclass(frozen=True, slots=True)
class Tip:
    """One line of a tip: its text, and whether it's a clock time (drawn with a narrow space before AM or PM)."""

    text: str
    time: bool = False


@dataclass(frozen=True, slots=True)
class TipLines:
    """What a set tip shows: an upper line (the time, or the date on another day) over a lower one (the countdown,
    or the time under a date)."""

    upper: Tip
    lower: Tip


def tip_lines(now: datetime, tip: datetime, zone: tzinfo | None) -> TipLines:
    """A tip as the board reads it at ``now``, in ``zone``.

    Later today: the time over the countdown (``1h 23m``, ``45m``, ``SOON`` inside a minute or once it's passed).
    On another day: the date (``11/20``) where the time was, over the time where the countdown was.
    """
    clock = Tip(start_time_label(tip, zone), time=True)
    local = tip.astimezone(zone)
    if local.date() != now.astimezone(zone).date():
        return TipLines(Tip(f"{local.month}/{local.day}"), clock)
    minutes = int((tip - now).total_seconds() // 60)
    if minutes < 1:
        countdown = "SOON"
    elif minutes < 60:
        countdown = f"{minutes}m"
    else:
        countdown = "{}h {}m".format(*divmod(minutes, 60))
    return TipLines(clock, Tip(countdown))


def _ink_left(font: str, text: str) -> int:
    """The first column ``text``'s ink reaches in ``font``."""
    return min(x for row in rasterize(font, text) for x, on in enumerate(row) if on)


def _ink_right(font: str, text: str) -> int:
    """The last column ``text``'s ink reaches in ``font``."""
    return max(x for row in rasterize(font, text) for x, on in enumerate(row) if on)


def _tip_width(tip: Tip, font: str) -> int:
    if not tip.time:
        return text_width(tip.text, font)
    clock, _, meridiem = tip.text.partition(" ")
    return text_width(clock, font) + _TIME_SPACE + text_width(meridiem, font)


def _draw_tip(canvas: Canvas, x: int, y: int, tip: Tip, color: RGBColor, font: str) -> None:
    if not tip.time:
        canvas.text(x, y, tip.text, color, font=font)
        return
    clock, _, meridiem = tip.text.partition(" ")
    canvas.text(x, y, clock, color, font=font)
    canvas.text(x + text_width(clock, font) + _TIME_SPACE, y, meridiem, color, font=font)


def _first_ink(tip: Tip, font: str) -> int:
    """The first inked column of a tip line, from where it's drawn: a time draws its clock part first."""
    return _ink_left(font, tip.text.partition(" ")[0] if tip.time else tip.text)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _standing_text(team: Team, ranks: StandingRanks) -> str:
    """``11th West, 4th NW``: the conference rank, then the division's when the club's division is known."""
    conference = team.conference if isinstance(team, BasketballTeam) else None
    division = team.division if isinstance(team, BasketballTeam) else None
    text = _ordinal(ranks.conference) + (f" {conference}" if conference else "")
    short = _DIVISION.get(division) if division is not None else None
    return f"{text}, {_ordinal(ranks.division)} {short}" if short is not None else text


def _odds_text(game: TeamGame, line: FavoriteLine, *, spread: bool = True) -> str:
    """The favorite's line the way the MLB pregame writes one: the moneyline, then the spread in parentheses."""
    favorite = (game.away if line.favorite is HomeAway.AWAY else game.home).abbreviation
    moneyline = f"{favorite} {line.moneyline:+d}"
    return f"{moneyline} ({line.spread:+g})" if spread else moneyline


@dataclass(frozen=True, slots=True)
class _Pregame:
    """One frame's reading of the card: its clubs, payload, label and tip lines (None with no tip set)."""

    game: TeamGame
    payload: BasketballPregameCardPayload
    tip: TipLines | None

    @property
    def label(self) -> str:
        return "TIP" if self.payload.phase is BasketballPhase.SCHEDULED else "PPD"

    def team(self, which: HomeAway) -> Team:
        return self.game.away if which is HomeAway.AWAY else self.game.home


# --- quad 128x64 ------------------------------------------------------------------------------------------

# The tip column's three lines, as (face, cell top). The label and what it's about are the large pair; the line
# under them stays 6x10. Their ink sits on rows 16-25, 28-36 (a colon's tail on 37) and 40-46, two blank rows
# apart, with three between the top standing's ink and the label.
_Q_TIP_LABEL = ("9x18B", 12)  # TIP or PPD, its ink's top level with the top record's
_Q_TIP_UPPER = ("6x13", 26)  # the time, or the date on another day, beside the odds
_Q_TIP_LOWER = ("6x10", 39)  # the countdown, or the time under a date, on the bottom record's line
_Q_TBD_Y = 27
_Q_COLUMN_CENTER = 107
_Q_TEXT_X = 24
_Q_ODDS_Y = 29
_TBD = "TIP TBD"


def _quad_column_x(width: int) -> int:
    """Where a right-column item starts: centered on the column, pulled left if it would run off the board."""
    return min(_Q_COLUMN_CENTER - width // 2, 128 - width)


def _quad(canvas: Canvas, context: RenderContext, card: _Pregame) -> None:
    """Marks with records and standings, the records mirrored about the odds row; the tip column at right.

    The label is ``TIP`` or ``PPD``: 27 px in 9x18B, the largest face whose capitals (10 rows) fit between the top
    standing and the time with at least two blank rows each side. The time keeps six pixels a character:
    ``12:00 PM`` is 45 px in any 6-wide face, so right-aligned it leaves one blank column after an odds line like
    ``MIN -265 (-6.5)``. 6x13 is the tallest 6-wide face whose digits keep a blank column between them.
    """
    game, payload = card.game, card.payload
    draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=32)  # tiles land at y6 and y38
    for which, record_y, standing_y in ((HomeAway.AWAY, 15, 7), (HomeAway.HOME, 39, 50)):
        record, ranks = payload.records[which], payload.ranks[which]
        if record is not None:
            canvas.text(_Q_TEXT_X, record_y, str(record), _WHITE, font="6x10")
        if ranks is not None:  # the longest standing, 19 characters, is 95 px in 5x7: it always fits
            canvas.text(_Q_TEXT_X, standing_y, _standing_text(card.team(which), ranks), _STATS_TEAL, font="5x7")
    label_font, label_y = _Q_TIP_LABEL
    canvas.text(_quad_column_x(text_width(card.label, label_font)), label_y, card.label, _GOLD, font=label_font)
    tip = card.tip
    if tip is None:
        tbd_x = _quad_column_x(text_width(_TBD, "6x10"))
        if payload.line is not None:
            _quad_odds(canvas, game, payload.line, tbd_x + _ink_left("6x10", _TBD))
        canvas.text(tbd_x, _Q_TBD_Y, _TBD, _WHITE, font="6x10")
        return
    upper_font, _ = _Q_TIP_UPPER
    upper_x = _quad_column_x(_tip_width(tip.upper, upper_font))
    if payload.line is not None:
        _quad_odds(canvas, game, payload.line, upper_x + _first_ink(tip.upper, upper_font))
    for line, (font, y) in ((tip.upper, _Q_TIP_UPPER), (tip.lower, _Q_TIP_LOWER)):
        _draw_tip(canvas, _quad_column_x(_tip_width(line, font)), y, line, _WHITE, font)


def _quad_odds(canvas: Canvas, game: TeamGame, line: FavoriteLine, beside: int) -> None:
    """The odds row, without its spread when the spread would leave no blank column before ``beside``.

    ``beside`` is the first inked column of what shares the row at right: the tip time, a date, or ``TIP TBD``.
    A long line (``MIN -650 (-12.5)``) against a two-digit-hour time is the case this drops; the moneyline alone
    always fits.
    """
    full = _odds_text(game, line)
    fits = _Q_TEXT_X + _ink_right("4x6", full) + 1 < beside
    text = full if fits else _odds_text(game, line, spread=False)
    canvas.text(_Q_TEXT_X, _Q_ODDS_Y, text, _ODDS_GREEN, font="4x6")


# --- stack 64x64 ------------------------------------------------------------------------------------------

_S_TEXT_X = 24
_S_HOME_TOP = 27
_S_ODDS_Y = 21
_S_UPPER_Y = 48  # cell tops under the home record: the label line's ink on rows 48-53
_S_LOWER_Y = 56  # and the countdown's (or the time's) on rows 56-60


def _stack(canvas: Canvas, context: RenderContext, card: _Pregame) -> None:
    """Marks, each club's code over its record the same way; the odds row between; then the tip, in two lines.

    The top line is the label and what it's about, in 5x7: ``TIP 6:00 PM`` (or ``TIP 11/20`` on another day),
    ``PPD 11/17`` for a postponed game with a new tip, ``PPD`` without one. Its widest case, ``TIP 12:00 PM``, is
    58 px in 5x7, 3 px to spare each side. Under it, in 4x6: the countdown, the time when the top line has the
    date, or ``TIP TBD``.
    """
    game, payload = card.game, card.payload
    draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=_S_HOME_TOP)
    for which, top in ((HomeAway.AWAY, 0), (HomeAway.HOME, _S_HOME_TOP)):
        canvas.text(_S_TEXT_X, top, card.team(which).abbreviation, _WHITE, font="6x10")
        record = payload.records[which]
        if record is not None:
            canvas.text(_S_TEXT_X, top + 10, str(record), _STATS_TEAL, font="6x10")
    if payload.line is not None:
        odds = _odds_text(game, payload.line)
        if text_width(odds, "4x6") > 64:
            odds = _odds_text(game, payload.line, spread=False)
        draw_centered(canvas, 0, 64, _S_ODDS_Y, odds, _ODDS_GREEN, "4x6")
    upper: list[tuple[Tip, RGBColor]] = [(Tip(card.label), _GOLD)]
    if card.tip is None:
        lower = Tip(_TBD)
    else:
        upper.append((card.tip.upper, _WHITE))
        lower = card.tip.lower
    _draw_line_parts(canvas, _S_UPPER_Y, upper, "5x7")
    _draw_line_parts(canvas, _S_LOWER_Y, [(lower, _WHITE)], "4x6")


def _draw_line_parts(canvas: Canvas, y: int, parts: Sequence[tuple[Tip, RGBColor]], font: str) -> None:
    """One line's parts, a space apart and centered across the board, each in its own color."""
    space = text_width(" ", font)
    total = sum(_tip_width(tip, font) for tip, _ in parts) + space * (len(parts) - 1)
    x = (64 - total) // 2
    for tip, color in parts:
        _draw_tip(canvas, x, y, tip, color, font)
        x += _tip_width(tip, font) + space


# --- the renderer -----------------------------------------------------------------------------------------

_DRAW: dict[PanelProfile, Callable[[Canvas, RenderContext, _Pregame], None]] = {
    PanelProfile.QUAD_128X64: _quad,
    PanelProfile.STACK_64X64: _stack,
}


class BasketballPregameRenderer:
    """Draws an NBA pregame card, scheduled or postponed, onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(_DRAW)

    def render(
        self, card: ScoreboardCard[BasketballPregameCardPayload], context: RenderContext, canvas: Canvas
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("an NBA pregame card requires a TeamGame contest")
        payload = card.payload
        zone = context.timezone or context.now.tzinfo
        tip = tip_lines(context.now, payload.tip, zone) if payload.tip is not None else None
        canvas.fill(_BLACK)
        _DRAW[context.profile](canvas, context, _Pregame(game, payload, tip))
