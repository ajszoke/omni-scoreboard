"""Renderer for the NBA game card, live through final, on the quad board (the stack and single follow).

The card keeps the house frame: team rows down the left, each a logo tile (or a color bar) with the win meter
hugging it, a lane of the team's own words, and the score; the game's state at right; and, on the boards with
room, a panel under a divider.

A turn on the board is 12 s, in two 6 s halves. In the first, each team's lane names it: its code (white) over
its record (gold). In the second it shows what the team has left: its timeouts (``4×TO``, gold) over a red
``BONUS`` box while it's in the bonus. Which half is up follows how long the card has been on the board
(`turn_half`), never the payload. At halftime and at a final the counts no longer matter, and the teams stay
named all turn.

- quad_128x64 : 20-row team rows over a divider at row 40. The code and the timeouts are 6x10 at any score; the
  record is 6x10 while both scores have two digits and 5x7 once either reaches 100, where a five-character record
  in 6x10 would reach the score's first column. ``BONUS`` is 5x7. The 9x18B scores end at x78; right of them,
  the clock (8x13B) over four quarter pips (filled gold once begun, a dim ring before), or ``OT``/``2OT``; a run
  raises them and sits underneath (``MIN 7-0`` over ``LAST 1:28``); a break shows its words instead (``END Q1``,
  ``HALF 12:35``, ``FINAL 2OT``). Under the divider: the line score by default; a caption, NFL's way, a headline
  in the event's tone over a white detail line, marked ``i/N`` while plays queue; the two teams' box-score lines;
  or one player a team, away over home, from each team's plus-minus order, the stats scrolling when they overflow.

A final settles the meter, full for the winner, and dims the loser's code, record and score.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.cards.base import ScoreboardCard
from omni.cards.basketball import (
    BasketballGameCardPayload,
    CaptionPanel,
    LineScorePanel,
    PlayerLinesPanel,
    TeamBoxPanel,
)
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.baseball import WinProbability
from omni.domain.basketball import (
    REGULATION_PERIODS,
    BasketballGameState,
    BasketballPhase,
    BasketballSide,
    BySide,
    PlayerLine,
    period_label,
)
from omni.domain.contest import TeamGame
from omni.domain.football import PlayTone
from omni.domain.teams import Team
from omni.events.basketball import CaptionWords
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.font import rasterize
from omni.renderers.team_row import draw_treated_mark
from omni.renderers.text import draw_centered, draw_right_aligned, text_width
from omni.renderers.text_lane import TextLane, draw_text_lane
from omni.renderers.visual_treatment import MatchupVisualTreatment, resolve_matchup_treatment
from omni.renderers.win_meter import draw_win_meter

__all__ = ["LiveBasketballRenderer", "TurnHalf", "turn_half"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_GOLD = RGBColor(255, 215, 0)  # the house state color: clocks, pips, records, timeouts
_DIM_GOLD = RGBColor(110, 92, 0)  # an unbegun quarter's pip ring, and a final's losing record
_SECONDARY = RGBColor(180, 180, 180)
_LOSER = RGBColor(110, 110, 110)
_DIVIDER = RGBColor(55, 55, 55)
_RED = RGBColor(255, 50, 50)  # the bonus box, and a minus
_GREEN = RGBColor(60, 220, 90)  # a scoring play, and a plus
_STATS_TEAL = RGBColor(50, 180, 180)

_TONE_COLOR = {PlayTone.SCORE: _GREEN, PlayTone.ALERT: _GOLD, PlayTone.TURNOVER: _RED, PlayTone.STANDARD: _WHITE}
_TIMES = "×"  # the multiplication sign every face used here carries
_HALF_SECONDS = 6.0


class TurnHalf(Enum):
    """Which 6 s of a 12 s turn is up: the first names each team, the second shows what each has left."""

    FIRST = "first"
    SECOND = "second"


def turn_half(context: RenderContext) -> TurnHalf:
    """The half up for how long the card has been on the board, as its lane clock tells it.

    With no lane clock (a still preview) the halves run off the render clock itself, as a marquee does.
    """
    clock = context.marquee.clock
    up = clock.up_for() if clock is not None else context.now.timestamp()
    return TurnHalf.FIRST if int(up // _HALF_SECONDS) % 2 == 0 else TurnHalf.SECOND


@dataclass(frozen=True, slots=True)
class _Card:
    """One frame's reading of the card: its clubs, payload and state, the half up, and the clubs' codes."""

    game: TeamGame
    payload: BasketballGameCardPayload
    half: TurnHalf

    @property
    def state(self) -> BasketballGameState:
        return self.payload.state

    @property
    def codes(self) -> BySide[str]:
        return BySide(self.game.away.abbreviation, self.game.home.abbreviation)

    def team(self, which: HomeAway) -> Team:
        return self.game.away if which is HomeAway.AWAY else self.game.home

    @property
    def shows_counts(self) -> bool:
        """Whether the teams' lanes show their timeouts and bonus: a live or end-of-period card's second half."""
        phase = self.state.phase
        return self.half is TurnHalf.SECOND and phase in (BasketballPhase.LIVE, BasketballPhase.END_OF_PERIOD)

    @property
    def three_digits(self) -> bool:
        """Whether either score has reached 100, which shrinks the lanes' words on every board."""
        return max(self.state.away.score, self.state.home.score) >= 100

    def lost(self, which: HomeAway) -> bool:
        winner = self.state.winner
        return winner is not None and which is not winner

    def text_color(self, which: HomeAway) -> RGBColor:
        return _LOSER if self.lost(which) else _WHITE

    def treatment(self, context: RenderContext, profile: PanelProfile, home_top: int) -> MatchupVisualTreatment:
        return resolve_matchup_treatment(
            self.game.away,
            self.game.home,
            profile=profile,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=home_top,
        )


def _meter(card: _Card) -> WinProbability | None:
    """The market's reading while the game goes on; at a final, full for the winner."""
    winner = card.state.winner
    if winner is None:
        return card.payload.win_probability
    won = 100.0 if winner is HomeAway.HOME else 0.0
    return WinProbability(home=won, away=100.0 - won)


def _draw_meter(canvas: Canvas, treatment: MatchupVisualTreatment, card: _Card) -> None:
    meter = _meter(card)
    if meter is not None:
        draw_win_meter(canvas, treatment, meter)


def _timeouts_text(side: BasketballSide, sign: str = _TIMES) -> str:
    """Timeouts left, the count first: ``4×TO``; ``-`` for a count the feed hasn't given."""
    return f"{'-' if side.timeouts is None else side.timeouts}{sign}TO"


def _names(canvas: Canvas, card: _Card, which: HomeAway, x: int, ys: tuple[int, int], fonts: tuple[str, str]) -> None:
    """A team's code (white) over its record (gold), each at its own row and face; a final's loser dimmed."""
    lost = card.lost(which)
    canvas.text(x, ys[0], card.team(which).abbreviation, _LOSER if lost else _WHITE, font=fonts[0])
    record = card.payload.records[which]
    if record is not None:
        canvas.text(x, ys[1], str(record), _DIM_GOLD if lost else _GOLD, font=fonts[1])


# Quarter pips: filled once the quarter has begun, a ring before.
_PIP_QUAD = ("..XXX..", ".XXXXX.", "XXXXXXX", "XXXXXXX", "XXXXXXX", ".XXXXX.", "..XXX..")


@functools.cache
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
    return REGULATION_PERIODS * len(sprite[0]) + (REGULATION_PERIODS - 1) * gap


def _draw_pips(canvas: Canvas, x: int, y: int, sprite: tuple[str, ...], gap: int, begun: int) -> None:
    """Four quarter pips from ``x``: the first ``begun`` filled gold, the rest dim gold rings."""
    for i in range(REGULATION_PERIODS):
        px = x + i * (len(sprite[0]) + gap)
        if i < begun:
            _sprite(canvas, px, y, sprite, _GOLD)
        else:
            _sprite(canvas, px, y, _ring(sprite), _DIM_GOLD)


@functools.cache
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


def _period_headers(entered: int) -> list[str]:
    """Line-score headers: the four quarters, then ``OT`` for a lone overtime, or ``OT1`` ``OT2`` once there's a
    second."""
    overtimes = max(0, entered - REGULATION_PERIODS)
    quarters = [f"Q{i}" for i in range(1, REGULATION_PERIODS + 1)]
    if overtimes == 1:
        return [*quarters, "OT"]
    return [*quarters, *(f"OT{i}" for i in range(1, overtimes + 1))]


def _countdown(left: timedelta) -> str:
    """Time left in a break, ``12:35``; ``0:00`` once it's run out."""
    seconds = max(0, round(left.total_seconds()))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _span_text(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _run_lines(card: _Card) -> tuple[str, str] | None:
    """A run on, in two lines: ``MIN 7-0`` over ``LAST 1:28``; None with no run."""
    run = card.payload.run
    if run is None:
        return None
    return f"{card.codes[run.team]} {run.scored}-{run.allowed}", f"LAST {_span_text(run.span_seconds)}"


def _plus_minus_text(line: PlayerLine) -> str:
    return f"{line.plus_minus:+d}" if line.plus_minus else "0"


def _plus_minus_color(line: PlayerLine) -> RGBColor:
    if line.plus_minus > 0:
        return _GREEN
    return _RED if line.plus_minus < 0 else _SECONDARY


def _stats_text(line: PlayerLine) -> str:
    """Points, then shooting as makes over attempts, threes first: ``16 PTS 2/5 3PT 6/11 FG``; a player who hasn't
    tried a three shows only his field goals."""
    field_goals = f"{line.fg_made}/{line.fg_attempted} FG"
    shooting = f"{line.threes_made}/{line.threes_attempted} 3PT {field_goals}" if line.threes_attempted else field_goals
    return f"{line.points} PTS {shooting}"


def _words(panel: CaptionPanel, card: _Card) -> CaptionWords:
    return panel.caption.words(card.codes)


# --- quad 128x64 ------------------------------------------------------------------------------------------

_Q_ROW = 20
_Q_LANE_X = 23
_Q_SCORE_RIGHT = 78
_Q_MODULE = (80, 128)  # the clock, pips and break words center in what's right of the scores
_Q_CODE = ("6x10", 1)  # (face, cell row below the team row's top), at any score
_Q_RECORD = {False: ("6x10", 10), True: ("5x7", 11)}  # by whether a score has three digits
_Q_COUNTS = ("6x10", 1)
_Q_BONUS = ("5x7", 11)
_Q_CLOCK_Y = {False: 8, True: 2}  # the clock over the pips, centered above the divider; raised for a run
_Q_PIP_DROP = 15  # the pips' top below the clock's cell top
_Q_PIP_GAP = 4
_Q_RUN_Y = (27, 34)
_Q_WORDS_Y = (5, 21)
_Q_DIVIDER_Y = 40
_Q_LINE_CENTERS = (30, 45, 60, 75, 90, 105, 120)  # the line score's columns: Q1-Q4 and three overtimes
_Q_BOX_COLUMNS = (("FG%", 44), ("3PT", 72), ("REB", 90), ("AST", 108), ("TOV", 126))  # label, right edge
_Q_NAME_X = 18


def _quad(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    treatment = card.treatment(context, PanelProfile.QUAD_128X64, _Q_ROW)
    record_font, record_row = _Q_RECORD[card.three_digits]
    code_font, code_row = _Q_CODE
    rows = ((HomeAway.AWAY, treatment.away, 0), (HomeAway.HOME, treatment.home, _Q_ROW))
    for which, mark, top in rows:
        side = card.state.side(which)
        draw_treated_mark(canvas, context, card.team(which), mark)
        if card.shows_counts:
            counts_font, counts_row = _Q_COUNTS
            canvas.text(_Q_LANE_X, top + counts_row, _timeouts_text(side), _GOLD, font=counts_font)
            if card.state.phase is BasketballPhase.LIVE and side.in_bonus:
                bonus_font, bonus_row = _Q_BONUS
                _draw_inverted(canvas, _Q_LANE_X, top + bonus_row, "BONUS", bonus_font)
        else:
            _names(canvas, card, which, _Q_LANE_X, (top + code_row, top + record_row), (code_font, record_font))
        draw_right_aligned(canvas, _Q_SCORE_RIGHT, top + 1, str(side.score), card.text_color(which), "9x18B")
    _draw_meter(canvas, treatment, card)
    _quad_module(canvas, context, card)
    canvas.fill_rect(0, _Q_DIVIDER_Y, 128, 1, _DIVIDER)
    bottom = card.payload.bottom
    if isinstance(bottom, CaptionPanel):
        _quad_caption(canvas, context, card, bottom)
    elif isinstance(bottom, TeamBoxPanel):
        _quad_box(canvas, card, bottom)
    elif isinstance(bottom, PlayerLinesPanel):
        _quad_player_lines(canvas, context, card, bottom)
    else:
        _quad_line_score(canvas, card)


def _quad_module(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    """Right of the scores: the clock over the quarter pips (or the overtime), raised over a run when one is on;
    at a break, its words."""
    state = card.state
    left, right = _Q_MODULE
    if state.phase is BasketballPhase.LIVE:
        run = _run_lines(card)
        clock_y = _Q_CLOCK_Y[run is not None]
        clock = state.clock.text if state.clock is not None else "--:--"
        draw_centered(canvas, left, right, clock_y, clock, _GOLD, "8x13B")
        pip_y = clock_y + _Q_PIP_DROP
        if state.is_overtime:  # the label's ink rows match the pips'
            draw_centered(canvas, left, right, pip_y - 1, period_label(state.period), _GOLD, "6x10")
        else:
            width = _pips_width(_PIP_QUAD, _Q_PIP_GAP)
            _draw_pips(canvas, left + (right - left - width) // 2, pip_y, _PIP_QUAD, _Q_PIP_GAP, state.period)
        if run is not None:
            draw_centered(canvas, left, right, _Q_RUN_Y[0], run[0], _WHITE, "5x7")
            draw_centered(canvas, left, right, _Q_RUN_Y[1], run[1], _SECONDARY, "4x6")
        return
    words = _break_words(context, card)
    rows = [(words[0], sum(_Q_WORDS_Y) // 2)] if len(words) == 1 else list(zip(words, _Q_WORDS_Y))
    for i, (word, y) in enumerate(rows):
        bold = state.phase is BasketballPhase.FINAL and i == 0  # FINAL bold; its overtime in the regular face
        draw_centered(canvas, left, right, y, word, _GOLD, "8x13B" if bold else "8x13")


def _break_words(context: RenderContext, card: _Card) -> tuple[str, ...]:
    """A break as one or two words: ``END`` / ``Q1``, ``HALF`` / ``12:35`` (just ``HALF`` with no known end),
    ``FINAL`` / ``2OT`` (just ``FINAL`` in regulation)."""
    state = card.state
    if state.phase is BasketballPhase.END_OF_PERIOD:
        return "END", period_label(state.period)
    if state.phase is BasketballPhase.HALFTIME:
        ends = card.payload.break_ends_at
        return ("HALF", _countdown(ends - context.now)) if ends is not None else ("HALF",)
    return ("FINAL", period_label(state.period)) if state.is_overtime else ("FINAL",)


def _quad_line_score(canvas: Canvas, card: _Card) -> None:
    """Quarter by quarter under the divider: headers (the period under way, or just finished, gold), then the away
    and home rows; a period not yet scored reads ``-``. Past three overtimes the earliest periods give way."""
    state = card.state
    entered = max(len(state.away.periods), len(state.home.periods), state.period)
    headers = _period_headers(entered)
    first = max(0, len(headers) - len(_Q_LINE_CENTERS))
    columns = list(zip(range(first, len(headers)), _Q_LINE_CENTERS))
    for i, center in columns:
        color = _GOLD if i == state.period - 1 else _SECONDARY
        draw_centered(canvas, center - 7, center + 7, 42, headers[i], color, "4x6")
    for which, y in ((HomeAway.AWAY, 49), (HomeAway.HOME, 56)):
        side = state.side(which)
        color = card.text_color(which)
        canvas.text(4, y, card.team(which).abbreviation, color, font="4x6")
        for i, center in columns:
            known = i < len(side.periods)
            value, value_color = (str(side.periods[i]), color) if known else ("-", _SECONDARY)
            draw_centered(canvas, center - 7, center + 7, y, value, value_color, "4x6")


def _quad_caption(canvas: Canvas, context: RenderContext, card: _Card, panel: CaptionPanel) -> None:
    """The event as it happens, NFL's way: a headline in its tone, a detail line a size down in white. While more
    than one play waits, each shows 6 s, marked ``i/N`` at right; N grows as plays arrive."""
    words = _words(panel, card)
    marquee = context.marquee
    width = 128
    if panel.place is not None:
        draw_right_aligned(canvas, 127, 42, f"{panel.place.index}/{panel.place.count}", _SECONDARY, "4x6")
        width = 114
    headline = TextLane(x=0, y=42, width=width, font="6x10", style=marquee.style)
    draw_text_lane(canvas, headline, words.headline, _TONE_COLOR[words.tone], now=context.now, clock=marquee.clock)
    detail = TextLane(x=0, y=55, width=128, font="5x7", style=marquee.style)
    draw_text_lane(canvas, detail, words.detail, _WHITE, now=context.now, clock=marquee.clock)


def _quad_box(canvas: Canvas, card: _Card, panel: TeamBoxPanel) -> None:
    """The two teams' box-score lines as a table, laid out like the line score."""
    for label, right in _Q_BOX_COLUMNS:
        draw_right_aligned(canvas, right, 42, label, _SECONDARY, "4x6")
    for which, y in ((HomeAway.AWAY, 49), (HomeAway.HOME, 56)):
        box = panel.box[which]
        canvas.text(4, y, card.team(which).abbreviation, _WHITE, font="4x6")
        values = (
            str(box.fg_pct),
            f"{box.threes_made}-{box.threes_attempted}",
            str(box.rebounds),
            str(box.assists),
            str(box.turnovers),
        )
        for (_, right), value in zip(_Q_BOX_COLUMNS, values):
            draw_right_aligned(canvas, right, y, value, _WHITE, "4x6")


def _quad_player_lines(canvas: Canvas, context: RenderContext, card: _Card, panel: PlayerLinesPanel) -> None:
    """One player a team, away over home. Each row runs on its own: the code, the name, the plus-minus, then the
    stats, which scroll when they overflow while everything before them holds still."""
    marquee = context.marquee
    for which, y in ((HomeAway.AWAY, 44), (HomeAway.HOME, 55)):
        canvas.text(3, y + 1, card.team(which).abbreviation, _SECONDARY, font="4x6")
        line = panel.players[which]
        if line is None:
            continue
        canvas.text(_Q_NAME_X, y, line.name, _WHITE, font="5x7")
        plus_minus = _plus_minus_text(line)
        pm_x = _Q_NAME_X + text_width(f"{line.name} ", "5x7")
        canvas.text(pm_x, y, plus_minus, _plus_minus_color(line), font="5x7")
        stats_x = pm_x + text_width(f"{plus_minus} ", "5x7")
        lane = TextLane(x=stats_x, y=y, width=128 - stats_x, font="5x7", style=marquee.style)
        draw_text_lane(canvas, lane, _stats_text(line), _STATS_TEAL, now=context.now, clock=marquee.clock)


# --- the renderer -----------------------------------------------------------------------------------------

_DRAW: dict[PanelProfile, Callable[[Canvas, RenderContext, _Card], None]] = {
    PanelProfile.QUAD_128X64: _quad,
}


class LiveBasketballRenderer:
    """Draws an NBA game card, from tip-off through the final, onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(_DRAW)

    def render(self, card: ScoreboardCard[BasketballGameCardPayload], context: RenderContext, canvas: Canvas) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("an NBA game card requires a TeamGame contest")
        canvas.fill(_BLACK)
        _DRAW[context.profile](canvas, context, _Card(game, card.payload, turn_half(context)))
