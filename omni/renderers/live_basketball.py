"""Renderer for the NBA game card, live through final, across all three panel profiles.

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
  in the event's tone over a white detail line; the two teams' box-score lines; or one player a team, away over
  home, from each team's plus-minus order, the stats scrolling when they overflow.
- stack_64x64 : 20-row team rows; the clock pinned left on row 40 and the pips (or the overtime) pinned right; a
  divider at row 50 and two 4x6 lines under it. The code is 6x10 with two-digit scores and 5x7 from 100; the
  record is 4x6 at any score, under the code with two blank rows below the score's ink. Under the divider: the
  line score as two rows (``Q1`` over ``Q2``, the first half's total ``H1`` over ``Q3``, ``Q3`` over ``Q4``, the
  last two periods in overtime); a caption, its rulings split across the halves; a run; FG% and rebounds; one
  player a half, away first; and at a final, the winner's best player, then the loser's. COMPROMISE: with a 1xx
  score, the record passes under the score's first two columns; the box score keeps FG% and rebounds, and a
  player's line its points and field goals.
- single_64x32: 16-row team rows with color bars. The code is 6x10 with two-digit scores and 5x7 from 100, over
  a 5x7 record that may run beneath a three-digit score; the second half shows ``5TO`` over a boxed ``BON``. The
  6x10 scores end at x42; right of them, the clock (4x6) over the pips, ``TO`` over the team's code while a
  timeout lasts, a break's words, or a big ``F`` at a final (over ``2OT`` in overtime). A scoring play turns its
  team's score green. COMPROMISE: there is no bottom panel, so no line score, captions, rulings, stat panels or
  run.

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
from omni.events.basketball import CaptionWords, TechnicalFoul
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.font import rasterize
from omni.renderers.team_row import draw_treated_mark
from omni.renderers.text import draw_centered, draw_right_aligned, text_width
from omni.renderers.text_lane import LaneAnchor, TextLane, draw_text_lane
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
_Q_STATS_LEAST = 30  # the width a player row keeps for its stats, however long the name before them


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
    """The event as it happens, NFL's way: a headline in its tone, a detail line a size down in white."""
    words = _words(panel, card)
    marquee = context.marquee
    headline = TextLane(x=0, y=42, width=128, font="6x10", style=marquee.style)
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
    stats, which scroll when they overflow while everything before them holds still. A name so long it would leave
    the stats less than `_Q_STATS_LEAST` scrolls too, in the room left before the plus-minus."""
    marquee = context.marquee
    for which, y in ((HomeAway.AWAY, 44), (HomeAway.HOME, 55)):
        canvas.text(3, y + 1, card.team(which).abbreviation, _SECONDARY, font="4x6")
        line = panel.players[which]
        if line is None:
            continue
        plus_minus = _plus_minus_text(line)
        # The name holds still unless it would leave the stats less than their least: then it scrolls in what's left.
        room = 128 - _Q_NAME_X - text_width(f"{plus_minus} ", "5x7") - _Q_STATS_LEAST
        name_width = text_width(f"{line.name} ", "5x7")
        if name_width <= room:
            canvas.text(_Q_NAME_X, y, line.name, _WHITE, font="5x7")
        else:
            name = TextLane(x=_Q_NAME_X, y=y, width=room - text_width(" ", "5x7"), font="5x7", style=marquee.style)
            draw_text_lane(canvas, name, line.name, _WHITE, now=context.now, clock=marquee.clock)
            name_width = room
        pm_x = _Q_NAME_X + name_width
        canvas.text(pm_x, y, plus_minus, _plus_minus_color(line), font="5x7")
        stats_x = pm_x + text_width(f"{plus_minus} ", "5x7")
        lane = TextLane(x=stats_x, y=y, width=128 - stats_x, font="5x7", style=marquee.style)
        draw_text_lane(canvas, lane, _stats_text(line), _STATS_TEAL, now=context.now, clock=marquee.clock)


# --- stack 64x64 ------------------------------------------------------------------------------------------

_S_ROW = 20
_S_LANE_X = 24  # the code, record, timeouts and bonus box all start here
# The code is 6x10 with two-digit scores and 5x7 from 100, where a 6x10 code would touch the score's first column;
# its ink ends on the score's last row. The record is 4x6 at any score, with two blank rows under the score: it
# clears a two-digit score, and passes under a 1xx's first two columns, since no face fits a five-character record
# in the 16 columns left of one. Each is (face, cell row below the team row's top).
_S_CODE = {False: ("6x10", 4), True: ("5x7", 6)}
_S_RECORD = ("4x6", 14)
_S_COUNTS = {False: "5x7", True: "4x6"}
_S_COUNTS_ROW = 4
_S_BONUS = ("4x6", 13)
_S_CLOCK_Y = 40
_S_CLOCK_MARGIN = 2  # the clock this far from the left edge, the pips or overtime this far from the right
_S_PIP_GAP = 2
_S_DIVIDER_Y = 50
_S_LINES_Y = (52, 58)
_PIP_STACK = (".XXX.", "XXXXX", "XXXXX", "XXXXX", ".XXX.")


def _counting(line: PlayerLine) -> tuple[str, ...]:
    """The line's other numbers worth a mention, biggest first: ``11 AST``, ``8 REB``, ``3 BLK``.

    Rebounds and assists count from one; blocks and steals from two. A tie keeps that order.
    """
    found = [(line.rebounds, "REB", 1), (line.assists, "AST", 1), (line.blocks, "BLK", 2), (line.steals, "STL", 2)]
    kept = [(value, label) for value, label, least in found if value >= least]
    return tuple(f"{value} {label}" for value, label in sorted(kept, key=lambda item: -item[0]))


def _caption(card: _Card) -> CaptionPanel | None:
    bottom = card.payload.bottom
    return bottom if isinstance(bottom, CaptionPanel) else None


def _scorer(caption: CaptionPanel | None, words: CaptionWords | None) -> HomeAway | None:
    """The side whose score the single board greens: a scoring play's, or, for a technical whose free throw went in,
    the shooter's (the side the technical wasn't charged to)."""
    if caption is None or words is None:
        return None
    event = caption.caption
    if isinstance(event, TechnicalFoul):
        if not event.made:
            return None
        return HomeAway.HOME if event.side is HomeAway.AWAY else HomeAway.AWAY
    return event.side if words.tone is PlayTone.SCORE else None


def _stack(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    treatment = card.treatment(context, PanelProfile.STACK_64X64, _S_ROW)
    three = card.three_digits
    code_font, code_row = _S_CODE[three]
    record_font, record_row = _S_RECORD
    rows = ((HomeAway.AWAY, treatment.away, 0), (HomeAway.HOME, treatment.home, _S_ROW))
    for which, mark, top in rows:
        side = card.state.side(which)
        draw_treated_mark(canvas, context, card.team(which), mark)
        if card.shows_counts:
            canvas.text(_S_LANE_X, top + _S_COUNTS_ROW, _timeouts_text(side), _GOLD, font=_S_COUNTS[three])
            if card.state.phase is BasketballPhase.LIVE and side.in_bonus:
                bonus_font, bonus_row = _S_BONUS
                _draw_inverted(canvas, _S_LANE_X, top + bonus_row, "BONUS", bonus_font)
        else:
            _names(canvas, card, which, _S_LANE_X, (top + code_row, top + record_row), (code_font, record_font))
        draw_right_aligned(canvas, 64, top + 1, str(side.score), card.text_color(which), "8x13B")
    _draw_meter(canvas, treatment, card)
    _stack_clock_line(canvas, context, card)
    canvas.fill_rect(0, _S_DIVIDER_Y, 64, 1, _DIVIDER)
    _stack_bottom(canvas, context, card)


def _stack_clock_line(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    """The clock pinned left and the quarter pips (or the overtime) pinned right; at a break, the break's words."""
    state = card.state
    font, y = "6x10", _S_CLOCK_Y
    if state.phase is BasketballPhase.LIVE:
        clock = state.clock.text if state.clock is not None else "--:--"
        canvas.text(_S_CLOCK_MARGIN, y, clock, _GOLD, font=font)
        right = 64 - _S_CLOCK_MARGIN
        if state.is_overtime:  # the face's trailing blank column stands in for one pixel of the margin
            draw_right_aligned(canvas, right + 1, y, period_label(state.period), _GOLD, font)
        else:
            width = _pips_width(_PIP_STACK, _S_PIP_GAP)
            _draw_pips(canvas, right - width, y + 2, _PIP_STACK, _S_PIP_GAP, state.period)
        return
    if state.phase is BasketballPhase.END_OF_PERIOD:
        label = f"END {period_label(state.period)}"
    elif state.phase is BasketballPhase.HALFTIME:
        ends = card.payload.break_ends_at
        label = f"HALF {_countdown(ends - context.now)}" if ends is not None else "HALFTIME"
    else:
        label = f"FINAL/{period_label(state.period)}" if state.is_overtime else "FINAL"
    draw_centered(canvas, 0, 64, y, label, _GOLD, font)


def _stack_bottom(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    """Two small lines under the divider: the caption, a run, the rotation's panel, or a final's best players.

    A caption keeps the panel; otherwise a run on takes it from the stat panels and the line score.
    """
    first, second = _S_LINES_Y
    bottom = card.payload.bottom
    run = _run_lines(card)
    if card.state.phase is BasketballPhase.FINAL:
        _stack_best(canvas, card)
    elif isinstance(bottom, CaptionPanel):
        _stack_caption(canvas, context, card, bottom)
    elif run is not None:
        draw_centered(canvas, 0, 64, first, run[0], _WHITE, "4x6")
        draw_centered(canvas, 0, 64, second, run[1], _SECONDARY, "4x6")
    elif isinstance(bottom, TeamBoxPanel):
        away, home = bottom.box.away, bottom.box.home
        lines = (("FG%", f"{away.fg_pct}-{home.fg_pct}"), ("REB", f"{away.rebounds}-{home.rebounds}"))
        for (label, values), y in zip(lines, _S_LINES_Y):
            _stack_labeled(canvas, y, label, _SECONDARY, values)
    elif isinstance(bottom, PlayerLinesPanel):
        # One player a half, away first, as the tiles above sit: the name and plus-minus, then points and shooting.
        line = bottom.players[HomeAway.AWAY if card.half is TurnHalf.FIRST else HomeAway.HOME]
        if line is not None:
            _stack_name_line(canvas, first, line)
            stats = f"{line.points} PTS {line.fg_made}/{line.fg_attempted} FG"
            draw_centered(canvas, 0, 64, second, stats, _STATS_TEAL, "4x6")
    else:
        _stack_line_score(canvas, card)


def _stack_caption(canvas: Canvas, context: RenderContext, card: _Card, panel: CaptionPanel) -> None:
    """The caption's two lines, centered, scrolling when they overflow; a ruling in its narrower words, a pair of
    lines a half."""
    words = _words(panel, card)
    if words.narrow is None:
        headline, detail = words.headline, words.detail
    else:
        lines = words.narrow[0 if card.half is TurnHalf.FIRST else 1]
        headline, detail = lines.headline, lines.detail
    marquee = context.marquee
    for text, color, y in ((headline, _TONE_COLOR[words.tone], _S_LINES_Y[0]), (detail, _WHITE, _S_LINES_Y[1])):
        lane = TextLane(x=0, y=y, width=64, font="4x6", anchor=LaneAnchor.CENTER, style=marquee.style)
        draw_text_lane(canvas, lane, text, color, now=context.now, clock=marquee.clock)


def _stack_name_line(canvas: Canvas, y: int, line: PlayerLine) -> None:
    """A player's name (white) and plus-minus (in its sign's color), centered."""
    name, plus_minus = f"{line.name} ", _plus_minus_text(line)
    x = (64 - text_width(name + plus_minus, "4x6")) // 2
    canvas.text(x, y, name, _WHITE, font="4x6")
    canvas.text(x + text_width(name, "4x6"), y, plus_minus, _plus_minus_color(line), font="4x6")


def _stack_best(canvas: Canvas, card: _Card) -> None:
    """A final's bottom panel: the winner's best player in the first half, the loser's in the second.

    The name and plus-minus fill the first line; the points and then the line's biggest other numbers follow on
    the second, as many as fit.
    """
    best = card.payload.best
    winner = card.state.winner
    if best is None or winner is None:
        return
    loser = HomeAway.HOME if winner is HomeAway.AWAY else HomeAway.AWAY
    line = best.players[winner if card.half is TurnHalf.FIRST else loser]
    if line is None:
        return
    first, second = _S_LINES_Y
    _stack_name_line(canvas, first, line)
    text = f"{line.points} PTS"
    for item in _counting(line):
        trial = f"{text} {item}"
        if text_width(trial, "4x6") > 64:
            break
        text = trial
    draw_centered(canvas, 0, 64, second, text, _STATS_TEAL, "4x6")


def _stack_labeled(canvas: Canvas, y: int, label: str, label_color: RGBColor, values: str) -> None:
    """``label away-home``, centered."""
    x = (64 - text_width(f"{label} {values}", "4x6")) // 2
    canvas.text(x, y, label, label_color, font="4x6")
    canvas.text(x + text_width(f"{label} ", "4x6"), y, values, _WHITE, font="4x6")


def _stack_line_rows(card: _Card) -> list[tuple[str, int, int, bool]]:
    """The line score's two rows, (label, away, home, under way), latest last.

    Q1 alone after the first; Q1 over Q2 through the half; the first half's total (H1) over Q3; Q3 over Q4; in
    overtime the last two periods, ``Q4`` over ``OT`` or ``OT1`` over ``OT2``.
    """
    state = card.state
    away, home = state.away.periods, state.home.periods
    entered = min(len(away), len(home))
    headers = _period_headers(entered)

    def row(i: int) -> tuple[str, int, int, bool]:
        return headers[i], away[i], home[i], state.phase is BasketballPhase.LIVE and i == state.period - 1

    if entered == 3:
        return [("H1", away[0] + away[1], home[0] + home[1], False), row(2)]
    return [row(i) for i in range(max(0, entered - 2), entered)]


def _stack_line_score(canvas: Canvas, card: _Card) -> None:
    """The line score as two rows, ``Q2 25-33``: the period under way gold, a finished one gray."""
    for (label, away, home, live), y in zip(_stack_line_rows(card), _S_LINES_Y):
        _stack_labeled(canvas, y, label, _GOLD if live else _SECONDARY, f"{away}-{home}")


# --- single 64x32 -----------------------------------------------------------------------------------------

_I_ROW = 16
_I_CODE_X = 5
_I_SCORE_RIGHT = 42
_I_SCORE_ROW = 1  # the score's cell sits a row into its lane, so its ink is on rows 2-8
_I_MODULE = (44, 64)
_I_CLOCK_Y = 10  # the clock and pips together, centered in the module
_I_PIP_Y = 18
_I_PIP_GAP = 1
_I_WORDS_Y = (7, 16)
_I_CODE = {False: ("6x10", 1), True: ("5x7", 3)}  # its ink ends on the score's last row
_I_RECORD = ("5x7", 10)  # rows 10-15, under both, on every card
_I_COUNTS_FONT = "5x7"
_I_BONUS = "BON"
_I_STACK_GAP = 1  # rows between the timeouts and the bonus box under them
# Where each team's timeouts-over-bonus block starts. The block is 15 rows in 5x7 (6 of capitals, a blank row,
# an 8-row box), a row short of the 16-row lane, so the away block hugs the top of its lane and the home block
# the bottom of its own: the gap between the lanes is then wider than the gap inside a block.
_I_COUNT_TOPS = {HomeAway.AWAY: 0, HomeAway.HOME: _I_ROW + 1}
_PIP_SINGLE = (".XX.", "XXXX", "XXXX", ".XX.")


def _single(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    treatment = card.treatment(context, PanelProfile.SINGLE_64X32, _I_ROW)
    caption = _caption(card)
    words = _words(caption, card) if caption is not None else None
    scorer = _scorer(caption, words)
    code_font, code_row = _I_CODE[card.three_digits]
    record_font, record_row = _I_RECORD
    rows = ((HomeAway.AWAY, treatment.away, 0), (HomeAway.HOME, treatment.home, _I_ROW))
    for which, mark, top in rows:
        side = card.state.side(which)
        draw_treated_mark(canvas, context, card.team(which), mark)  # a color bar: a 20px tile can't fit a 16px row
        if card.shows_counts:
            count_top = _I_COUNT_TOPS[which]
            canvas.text(_I_CODE_X, count_top, _timeouts_text(side, ""), _GOLD, font=_I_COUNTS_FONT)
            if card.state.phase is BasketballPhase.LIVE and side.in_bonus:
                box_top = count_top + _ink_height(_I_COUNTS_FONT) + _I_STACK_GAP
                _draw_inverted(canvas, _I_CODE_X, box_top, _I_BONUS, _I_COUNTS_FONT)
        else:
            _names(canvas, card, which, _I_CODE_X, (top + code_row, top + record_row), (code_font, record_font))
        score_color = _GREEN if which is scorer else card.text_color(which)
        draw_right_aligned(canvas, _I_SCORE_RIGHT, top + _I_SCORE_ROW, str(side.score), score_color, "6x10")
    _draw_meter(canvas, treatment, card)
    _single_module(canvas, context, card)


def _single_module(canvas: Canvas, context: RenderContext, card: _Card) -> None:
    """Right of the scores: the clock and pips; ``TO`` over the code while a timeout lasts; a break's words."""
    state = card.state
    left, right = _I_MODULE
    if state.timeout is not None:
        draw_centered(canvas, left, right, _I_WORDS_Y[0], "TO", _GOLD, "5x7")
        draw_centered(canvas, left, right, _I_WORDS_Y[1], card.codes[state.timeout], _WHITE, "5x7")
        return
    if state.phase is BasketballPhase.LIVE:
        clock = state.clock.text if state.clock is not None else "--:--"
        draw_centered(canvas, left, right, _I_CLOCK_Y, clock, _GOLD, "4x6")
        if state.is_overtime:
            draw_centered(canvas, left, right, _I_PIP_Y - 1, period_label(state.period), _GOLD, "4x6")
        else:
            width = _pips_width(_PIP_SINGLE, _I_PIP_GAP)
            _draw_pips(canvas, left + (right - left - width) // 2, _I_PIP_Y, _PIP_SINGLE, _I_PIP_GAP, state.period)
        return
    if state.phase is BasketballPhase.FINAL:
        # A big F; in overtime, over the overtime in the largest face that fits the module, on the home score's
        # rows (the score's face and cell row) with two blank rows under the F.
        if state.is_overtime:
            ot_row = _I_ROW + _I_SCORE_ROW
            draw_centered(canvas, left, right, ot_row - 12, "F", _GOLD, "8x13B")
            draw_centered(canvas, left, right, ot_row, period_label(state.period), _GOLD, "6x10")
        else:
            draw_centered(canvas, left, right, 10, "F", _GOLD, "8x13B")
        return
    if state.phase is BasketballPhase.END_OF_PERIOD:
        words = ("END", period_label(state.period))
    else:
        ends = card.payload.break_ends_at
        words = ("HALF", _countdown(ends - context.now) if ends is not None else "TIME")
    for word, y in zip(words, _I_WORDS_Y):
        fits = text_width(word, "5x7") <= right - left  # 4x6 for a countdown too wide for 5x7, a row lower
        draw_centered(canvas, left, right, y if fits else y + 1, word, _GOLD, "5x7" if fits else "4x6")


# --- the renderer -----------------------------------------------------------------------------------------

_DRAW: dict[PanelProfile, Callable[[Canvas, RenderContext, _Card], None]] = {
    PanelProfile.QUAD_128X64: _quad,
    PanelProfile.STACK_64X64: _stack,
    PanelProfile.SINGLE_64X32: _single,
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
