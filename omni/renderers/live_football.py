"""Renderer for the live-football card across all three panel profiles.

The card keeps the live-baseball card's frame, so both sports read the same way at a glance: team
rows down the left, each a logo tile (or color bar), the win meter hugging it, a lane of small
allowances, and the score. Football's allowances are timeouts (yellow squares) and coach's
challenges (red bulbs, round like baseball's ABS challenges), each stack burning down from the top so
what's left sits at the bottom. An orange football, the legacy board's own possession marker, sits
beside the score of the side with the ball.

- quad_128x64 : the 40px team block beside a state module (the quarter and clock, the down and
  distance, and the spot of the ball, red inside the 20), over a 24px strip with the last play: a
  headline colored by what kind of play it was, and a detail line. Either line marquees when too long.
- stack_64x64 : the two team rows over a status band: the quarter and clock, then the down and
  distance beside the spot. COMPROMISE: no last-play strip; big plays get their own card.
- single_64x32: an explicit COMPROMISE. Each 16px row carries the color bar, the meter, one lane of
  2px dots (timeouts, then challenges), a small football, the abbreviation and the score, with a
  column on the right stacking quarter, clock, down and distance, and the spot. No last-play strip.

At an interval (end of a quarter, halftime) there is no next snap, so the down, distance, spot and
clock give way to a label. Halftime keeps the challenges (they last the game) but not the timeouts,
which start over. A final settles the meter, full for the winner (even on a tie), and dims the loser's
abbreviation and score as the baseball final does; the marks stay in full color.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import math
from datetime import datetime, timedelta
from enum import Enum
from typing import assert_never

from omni.cards.base import ScoreboardCard
from omni.cards.football import LiveFootballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.baseball import WinProbability
from omni.domain.contest import TeamGame
from omni.domain.football import (
    CHALLENGES_PER_GAME,
    FootballGameState,
    FootballInterval,
    PlaySummary,
    PlayTone,
    SetPiece,
)
from omni.renderers.canvas import Canvas
from omni.renderers.font import rasterize
from omni.renderers.context import RenderContext
from omni.renderers.line_score import RHE_BIG_FONT
from omni.renderers.team_row import draw_treated_mark
from omni.renderers.text import draw_centered, draw_right_aligned, text_width
from omni.renderers.text_lane import Marquee, TextLane, draw_text_lane
from omni.renderers.visual_treatment import resolve_matchup_treatment
from omni.renderers.win_meter import draw_win_meter

__all__ = ["NO_STRIP_NOTE", "STRIP_PROFILES", "LiveFootballRenderer", "clock_text", "period_parts"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)  # the same state yellow the baseball card uses
_RED = RGBColor(255, 50, 50)  # the legacy board's turnover red
_LOSER = RGBColor(110, 110, 110)  # a final's loser, dimmed as on the baseball final but still legible
_GREEN = RGBColor(60, 220, 90)
_DIM = RGBColor(60, 60, 60)
_BALL = RGBColor(255, 120, 0)  # the legacy possession marker's orange

_TONE_COLOR = {
    PlayTone.STANDARD: _WHITE,
    PlayTone.SCORE: _GREEN,
    PlayTone.ALERT: _YELLOW,  # the color of a penalty flag
    PlayTone.TURNOVER: _RED,
}

_TIMEOUTS_PER_HALF = 3
_REGULATION_PERIODS = 4

# The legacy board's possession marker, 8x5, kept pixel for pixel.
_BALL_SPRITE = (
    "..XXXX..",
    ".XXXXXX.",
    "XXXXXXXX",
    ".XXXXXX.",
    "..XXXX..",
)
# The single board's smaller football, 5x3.
_BALL_SPRITE_SMALL = (
    ".XXX.",
    "XXXXX",
    ".XXX.",
)

# quad
_QUAD_ROW_HEIGHT = 20
_QUAD_TILE_LANE_X = 24  # past a flush 20px tile and its meter
_QUAD_BAR_LANE_X = 25  # past the fallback abbreviation
_BAR_ABBR_X = 6  # clear of the color bar and its meter
_QUAD_SCORE_RIGHT = 56  # the score's right edge; the football sits just past it
_QUAD_BALL_X = 58
_QUAD_MODULE_LEFT = 68
_QUAD_MODULE_RIGHT = 128
_QUAD_PERIOD_Y = 1
_QUAD_DOWN_Y = 15
_QUAD_SPOT_Y = 29
_QUAD_INTERVAL_Y = (5, 21)
_QUAD_HEADLINE_Y = 42
_QUAD_DETAIL_Y = 55
_PIP = 4  # quad and stack pip size
_PIP_GAP = 2
# stack
_STACK_ROW_TOPS = (0, 22)
_STACK_SCORE_LEFT = 37
_STACK_SCORE_RIGHT = 56
_STACK_BALL_X = 56
_STACK_ABBR_X = _BAR_ABBR_X
_STACK_PERIOD_Y = 45
_STACK_DOWN_Y = 56
_STACK_INTERVAL_Y = 50
# single
_SINGLE_ROW_HEIGHT = 16
_SINGLE_DOT = 2
_SINGLE_DOT_PITCH = 3
_SINGLE_DOT_X = 5
_SINGLE_TIMEOUT_TOP = 1
_SINGLE_CHALLENGE_TOP = 11
_SINGLE_BALL_X = 8
_SINGLE_ABBR_X = 14
_SINGLE_SCORE_RIGHT = 39
_SINGLE_COLUMN_LEFT = 41
_SINGLE_COLUMN_RIGHT = 64
_SINGLE_LINES_Y = (1, 9, 17, 25)
_SINGLE_INTERVAL_Y = (7, 16)

_AMP_GAP = 1  # air either side of the down's ampersand
_STACK_RUN_GAP = 4  # between the stack's down and its spot
_STACK_SPOT_GAP = 2  # between the stack's team and yard line: a full space would crowd out the down
_SINGLE_SPOT_GAP = 1  # between the single board's team and yard line

_SCORE_FONT = "6x10"
_STATE_FONT = "8x13"
_SMALL_FONT = "5x7"
_TINY_FONT = "4x6"
_SUFFIX_DROP = {_STATE_FONT: 1, _SCORE_FONT: 0, _SMALL_FONT: 0}  # where a raised suffix sits on each face
_MICRO_FONT = "tom-thumb"
# The next face down, for a label set a size smaller than the line it stands in for.
_SIZE_DOWN = {_STATE_FONT: _SCORE_FONT, _SCORE_FONT: _SMALL_FONT, _SMALL_FONT: _TINY_FONT, _TINY_FONT: _MICRO_FONT}


def period_parts(period: int) -> tuple[str, str]:
    """A period as its number and raised suffix (``3`` + ``rd``), or an overtime label with no suffix."""
    if period > _REGULATION_PERIODS:
        overtime = period - _REGULATION_PERIODS
        return ("OT" if overtime == 1 else f"{overtime}OT"), ""
    return str(period), {1: "st", 2: "nd", 3: "rd"}.get(period, "th")


def clock_text(seconds: int) -> str:
    """The game clock as broadcasts show it: ``12:34``, ``4:05``, ``0:07``."""
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True, slots=True)
class _Piece:
    """One piece of a line of state text: its text and face, how far it drops, and the gap before it."""

    text: str
    font: str
    drop: int = 0
    gap: int = 0


def _period_pieces(period: int, font: str) -> list[_Piece]:
    """A period's number with its suffix raised beside it in the tiny face."""
    number, suffix = period_parts(period)
    pieces = [_Piece(number, font)]
    if suffix:
        pieces.append(_Piece(suffix, _TINY_FONT, drop=_SUFFIX_DROP[font]))
    return pieces


class _Suffix(Enum):
    """How a down's ordinal suffix is written."""

    NONE = "none"  # 3&7
    RAISED = "raised"  # 3rd&7, the suffix small and raised, the way a broadcast bug writes it
    INLINE = "inline"  # 3rd&7, the suffix in the number's own face


_STACK_SUFFIX = _Suffix.INLINE  # at the stack's size a raised suffix is barely smaller than the number


def _down_pieces(
    state: FootballGameState, font: str, *, suffix: _Suffix, goal: str = "Goal", kickoff: str = "KICKOFF"
) -> list[_Piece] | None:
    """The down and distance, ``3rd & 7``, with ``goal`` standing in for the distance at goal to go.

    A kickoff names itself (``kickoff``) in the down's place. None with no live down. The ampersand
    keeps a pixel of air either side, so ``2&3`` can't run together into ``283``.
    """
    if state.set_piece is SetPiece.KICKOFF:
        smaller = _SIZE_DOWN[font]  # a size down, on the down line's bottom row
        return [_Piece(kickoff, smaller, drop=_bottom_row(font) - _bottom_row(smaller))]
    if state.down is None or state.distance is None:
        return None
    number, ordinal = period_parts(state.down)
    pieces = [_Piece(number, font)]
    if suffix is _Suffix.RAISED:
        pieces.append(_Piece(ordinal, _TINY_FONT, drop=_SUFFIX_DROP[font]))
    elif suffix is _Suffix.INLINE:
        pieces.append(_Piece(ordinal, font))
    rest = goal if state.goal_to_go else str(state.distance)
    return [*pieces, _Piece("&", font, gap=_AMP_GAP), _Piece(rest, font, gap=_AMP_GAP)]


@functools.cache
def _bottom_row(font: str) -> int:
    """The lowest lit row of a capital in ``font``, counted from its cell's top."""
    rows = rasterize(font, "E")
    return max(i for i, row in enumerate(rows) if any(row))


def _spot_pieces(game: TeamGame, state: FootballGameState, font: str, *, gap: int) -> list[_Piece] | None:
    """Where the ball sits, named by the side of the field (``CIN 38``), or ``50`` at midfield."""
    spot = state.ball_on
    if spot is None:
        return None
    if spot.side is None:
        return [_Piece(str(spot.yard_line), font)]
    team = game.away if spot.side is HomeAway.AWAY else game.home
    return [_Piece(team.abbreviation, font), _Piece(str(spot.yard_line), font, gap=gap)]


def _pieces_width(pieces: list[_Piece]) -> int:
    return sum(piece.gap + text_width(piece.text, piece.font) for piece in pieces)


def _runs_width(runs: list[tuple[list[_Piece], RGBColor]]) -> int:
    """Runs drawn side by side on the stack's down line, with the gap between them."""
    return sum(_pieces_width(pieces) for pieces, _ in runs) + _STACK_RUN_GAP * (len(runs) - 1)


def _draw_pieces(canvas: Canvas, x: int, y: int, pieces: list[_Piece], color: RGBColor) -> int:
    """Draw ``pieces`` left to right from ``x``; returns the right edge."""
    for piece in pieces:
        x += piece.gap
        canvas.text(x, y + piece.drop, piece.text, color, font=piece.font)
        x += text_width(piece.text, piece.font)
    return x


def _draw_pieces_centered(canvas: Canvas, left: int, right: int, y: int, pieces: list[_Piece], color: RGBColor) -> None:
    _draw_pieces(canvas, left + (right - left - _pieces_width(pieces)) // 2, y, pieces, color)


def _winner(state: FootballGameState) -> HomeAway | None:
    """The side a final went to; None while the game goes on, or for a tie."""
    if state.interval is not FootballInterval.FINAL or state.away_score == state.home_score:
        return None
    return HomeAway.AWAY if state.away_score > state.home_score else HomeAway.HOME


def _meter(state: FootballGameState, payload: LiveFootballCardPayload) -> WinProbability | None:
    """The win meter: the market's reading while the game goes on, then the result, full for the winner."""
    if state.interval is not FootballInterval.FINAL:
        return payload.win_probability
    winner = _winner(state)
    if winner is None:
        return WinProbability(home=50.0, away=50.0)  # a tie splits it
    won = 100.0 if winner is HomeAway.HOME else 0.0
    return WinProbability(home=won, away=100.0 - won)


def _side_color(state: FootballGameState, side: HomeAway) -> RGBColor:
    """White for a side's abbreviation and score; dimmed for a final's loser."""
    winner = _winner(state)
    return _LOSER if winner is not None and side is not winner else _WHITE


def _interval_words(state: FootballGameState, payload: LiveFootballCardPayload, now: datetime) -> tuple[str, ...]:
    """A break as words to stack or set side by side: ``HALF`` / ``TIME``, ``END`` / ``1st``, ``FINAL`` / ``OT``.

    A halftime whose end the payload knows counts down to it instead, ``HALF`` / ``12:34``.
    """
    if state.interval is FootballInterval.HALFTIME:
        ends = payload.break_ends_at
        return ("HALF", _countdown(ends - now)) if ends is not None and now < ends else ("HALF", "TIME")
    if state.interval is FootballInterval.FINAL:
        return ("FINAL", "OT") if state.is_overtime else ("FINAL",)
    number, suffix = period_parts(state.period)
    return "END", number + suffix


def _countdown(left: timedelta) -> str:
    """Time left on a running clock, ``12:34``, its colon blinking once a second to show it's running."""
    seconds = left.total_seconds()
    shown = clock_text(math.ceil(seconds))
    return shown if seconds % 1.0 >= 0.5 else shown.replace(":", " ")  # the same width: the digits hold still


def _word_rows(words: tuple[str, ...], rows: tuple[int, int]) -> list[tuple[str, int]]:
    """Words against two row positions; a lone word sits midway between them."""
    if len(words) == 1:
        return [(words[0], sum(rows) // 2)]
    return list(zip(words, rows))


# The profiles whose card draws the last-play strip; the others make the documented compromise without one.
STRIP_PROFILES = frozenset({PanelProfile.QUAD_128X64})
NO_STRIP_NOTE = "stack and single: no last-play strip (plays are logged, not shown)"


class LiveFootballRenderer:
    """Draws a live NFL game card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset({PanelProfile.SINGLE_64X32, PanelProfile.STACK_64X64, PanelProfile.QUAD_128X64})

    def render(self, card: ScoreboardCard[LiveFootballCardPayload], context: RenderContext, canvas: Canvas) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("live-football card requires a TeamGame contest")
        payload = card.payload
        profile = context.profile
        canvas.fill(_BLACK)
        if profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, context, game, payload)
        elif profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, context, game, payload)
        elif profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, context, game, payload)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    # --- quad ---------------------------------------------------------------------------------------

    def _render_quad(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveFootballCardPayload
    ) -> None:
        state = payload.state
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.QUAD_128X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=_QUAD_ROW_HEIGHT,
        )
        rows = (
            (HomeAway.AWAY, game.away, treatment.away, 0, state.away_score),
            (HomeAway.HOME, game.home, treatment.home, _QUAD_ROW_HEIGHT, state.home_score),
        )
        for side, team, mark, top, score in rows:
            draw_treated_mark(canvas, context, team, mark)
            lane_x = _QUAD_TILE_LANE_X
            color = _side_color(state, side)
            if not mark.is_tile:  # a bare color bar can't name the club, so the abbreviation stays
                canvas.text(_BAR_ABBR_X, top + 5, team.abbreviation, color, font=_SCORE_FONT)
                lane_x = _QUAD_BAR_LANE_X
            self._allowances(canvas, state, side, x=lane_x, row_top=top, row_height=_QUAD_ROW_HEIGHT)
            draw_right_aligned(canvas, _QUAD_SCORE_RIGHT, top + 1, str(score), color, RHE_BIG_FONT)
            if state.possession is side:
                _sprite(canvas, _QUAD_BALL_X, top + 8, _BALL_SPRITE, _BALL)
        if (meter := _meter(state, payload)) is not None:
            draw_win_meter(canvas, treatment, meter)
        self._quad_state_module(canvas, game, state, _interval_words(state, payload, context.now))
        if payload.last_play is not None:
            self._play_strip(canvas, payload.last_play, now=context.now, marquee=context.marquee)

    def _quad_state_module(
        self, canvas: Canvas, game: TeamGame, state: FootballGameState, words: tuple[str, ...]
    ) -> None:
        """Quarter and clock over the down and distance over the spot, right of the score block.

        The quarter's suffix is raised beside its number, the way a broadcast bug writes it, and the
        down's the same way. The spot turns red inside the 20.
        """
        left, right = _QUAD_MODULE_LEFT, _QUAD_MODULE_RIGHT
        if state.interval is not None:
            for word, y in _word_rows(words, _QUAD_INTERVAL_Y):
                draw_centered(canvas, left, right, y, word, _YELLOW, _STATE_FONT)
            return
        clock = clock_text(state.clock_seconds)
        period_right = _draw_pieces(canvas, left, _QUAD_PERIOD_Y, _period_pieces(state.period, _STATE_FONT), _YELLOW)
        clock_font = _STATE_FONT if period_right + 3 + text_width(clock, _STATE_FONT) <= right else _SCORE_FONT
        draw_right_aligned(canvas, right, _QUAD_PERIOD_Y, clock, _YELLOW, clock_font)
        down = _down_pieces(state, _STATE_FONT, suffix=_Suffix.RAISED)
        if down is not None:
            _draw_pieces_centered(canvas, left, right, _QUAD_DOWN_Y, down, _YELLOW)
        spot = _spot_pieces(game, state, _SCORE_FONT, gap=text_width(" ", _SCORE_FONT))
        if spot is not None:
            _draw_pieces_centered(canvas, left, right, _QUAD_SPOT_Y, spot, _RED if state.in_red_zone else _YELLOW)

    def _play_strip(self, canvas: Canvas, play: PlaySummary, *, now: datetime, marquee: Marquee) -> None:
        """The last play across the bottom: its headline in the play's color, the detail a size down in white.

        A headline one letter too long for the strip ("24 YARD PASS, 1ST DOWN") would scroll, so one that overflows
        says its yardage short ("24 YD PASS, 1ST DOWN") and scrolls only if it still doesn't fit."""
        headline = TextLane(x=0, y=_QUAD_HEADLINE_Y, width=128, font=_SCORE_FONT, style=marquee.style)
        words = play.headline
        if text_width(words, _SCORE_FONT) > headline.width:
            words = play.short_headline
        draw_text_lane(canvas, headline, words, _TONE_COLOR[play.tone], now=now, clock=marquee.clock)
        if play.detail:
            detail = TextLane(x=0, y=_QUAD_DETAIL_Y, width=128, font=_SMALL_FONT, style=marquee.style)
            draw_text_lane(canvas, detail, play.detail, _WHITE, now=now, clock=marquee.clock)

    # --- stack --------------------------------------------------------------------------------------

    def _render_stack(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveFootballCardPayload
    ) -> None:
        state = payload.state
        away_top, home_top = _STACK_ROW_TOPS
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.STACK_64X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=away_top,
            home_top=home_top,
        )
        rows = (
            (HomeAway.AWAY, game.away, treatment.away, away_top, state.away_score),
            (HomeAway.HOME, game.home, treatment.home, home_top, state.home_score),
        )
        for side, team, mark, top, score in rows:
            draw_treated_mark(canvas, context, team, mark)
            color = _side_color(state, side)
            if not mark.is_tile:
                canvas.text(_STACK_ABBR_X, top + 5, team.abbreviation, color, font=_SCORE_FONT)
            self._allowances(canvas, state, side, x=25, row_top=top, row_height=_QUAD_ROW_HEIGHT)
            draw_centered(canvas, _STACK_SCORE_LEFT, _STACK_SCORE_RIGHT, top + 1, str(score), color, RHE_BIG_FONT)
            if state.possession is side:
                _sprite(canvas, _STACK_BALL_X, top + 8, _BALL_SPRITE, _BALL)
        if (meter := _meter(state, payload)) is not None:
            draw_win_meter(canvas, treatment, meter)
        self._stack_band(canvas, game, state, _interval_words(state, payload, context.now))

    def _stack_band(self, canvas: Canvas, game: TeamGame, state: FootballGameState, words: tuple[str, ...]) -> None:
        """The quarter and clock, then the down and distance beside the spot, each line centered."""
        if state.interval is not None:
            label = "".join(words) if words == ("HALF", "TIME") else " ".join(words)
            draw_centered(canvas, 0, 64, _STACK_INTERVAL_Y, label, _YELLOW, _SCORE_FONT)
            return
        clock = _Piece(clock_text(state.clock_seconds), _SCORE_FONT, gap=text_width(" ", _SCORE_FONT))
        _draw_pieces_centered(
            canvas, 0, 64, _STACK_PERIOD_Y, [*_period_pieces(state.period, _SCORE_FONT), clock], _YELLOW
        )
        spot = _spot_pieces(game, state, _SMALL_FONT, gap=_STACK_SPOT_GAP)
        spot_color = _RED if state.in_red_zone else _YELLOW
        # The full down, "3rd & 7"; at goal to go "& Goal" can crowd the spot off the board, so it shortens to
        # "& G", as "KICKOFF" does to "KICK".
        runs = self._stack_runs(state, spot, spot_color, goal="Goal", kickoff="KICKOFF")
        if _runs_width(runs) > 64:
            runs = self._stack_runs(state, spot, spot_color, goal="G", kickoff="KICK")
        x = (64 - _runs_width(runs)) // 2
        for pieces, color in runs:
            x = _draw_pieces(canvas, x, _STACK_DOWN_Y, pieces, color) + _STACK_RUN_GAP

    @staticmethod
    def _stack_runs(
        state: FootballGameState, spot: list[_Piece] | None, spot_color: RGBColor, *, goal: str, kickoff: str
    ) -> list[tuple[list[_Piece], RGBColor]]:
        down = _down_pieces(state, _SMALL_FONT, suffix=_STACK_SUFFIX, goal=goal, kickoff=kickoff)
        return [(pieces, color) for pieces, color in ((down, _YELLOW), (spot, spot_color)) if pieces]

    # --- single -------------------------------------------------------------------------------------

    def _render_single(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveFootballCardPayload
    ) -> None:
        state = payload.state
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.SINGLE_64X32,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=_SINGLE_ROW_HEIGHT,
        )
        two_digits = max(state.away_score, state.home_score) >= 10
        abbr_font = _TINY_FONT if two_digits else _SMALL_FONT
        rows = (
            (HomeAway.AWAY, game.away, treatment.away, 0, state.away_score),
            (HomeAway.HOME, game.home, treatment.home, _SINGLE_ROW_HEIGHT, state.home_score),
        )
        for side, team, mark, top, score in rows:
            draw_treated_mark(canvas, context, team, mark)  # never a tile here: a 20px tile can't fit a 16px row
            self._single_dots(canvas, state, side, row_top=top)
            if state.possession is side:
                _sprite(canvas, _SINGLE_BALL_X, top + 6, _BALL_SPRITE_SMALL, _BALL)
            color = _side_color(state, side)
            canvas.text(_SINGLE_ABBR_X, top + 5, team.abbreviation, color, font=abbr_font)
            draw_right_aligned(canvas, _SINGLE_SCORE_RIGHT, top + 3, str(score), color, _SCORE_FONT)
        if (meter := _meter(state, payload)) is not None:
            draw_win_meter(canvas, treatment, meter)
        self._single_column(canvas, game, state, _interval_words(state, payload, context.now))

    def _single_column(self, canvas: Canvas, game: TeamGame, state: FootballGameState, words: tuple[str, ...]) -> None:
        """Quarter, clock, down and distance, and spot, stacked and centered in the right-hand column."""
        left, right = _SINGLE_COLUMN_LEFT, _SINGLE_COLUMN_RIGHT
        if state.interval is not None:
            for word, y in _word_rows(words, _SINGLE_INTERVAL_Y):
                font = _SMALL_FONT if text_width(word, _SMALL_FONT) <= right - left else _TINY_FONT
                draw_centered(canvas, left, right, y, word, _YELLOW, font)
            return
        period_y, clock_y, down_y, spot_y = _SINGLE_LINES_Y
        _draw_pieces_centered(canvas, left, right, period_y, _period_pieces(state.period, _SMALL_FONT), _YELLOW)
        draw_centered(canvas, left, right, clock_y, clock_text(state.clock_seconds), _YELLOW, _TINY_FONT)
        down = _down_pieces(state, _TINY_FONT, suffix=_Suffix.NONE, goal="G", kickoff="KICK")
        if down is not None:
            _draw_pieces_centered(canvas, left, right, down_y, down, _YELLOW)
        spot = _spot_pieces(game, state, _TINY_FONT, gap=_SINGLE_SPOT_GAP)  # a pixel apart: PIT 8, not PIT8
        if spot is not None:
            _draw_pieces_centered(canvas, left, right, spot_y, spot, _RED if state.in_red_zone else _YELLOW)

    # --- allowances ---------------------------------------------------------------------------------

    def _allowances(
        self, canvas: Canvas, state: FootballGameState, side: HomeAway, *, x: int, row_top: int, row_height: int
    ) -> None:
        """A side's timeouts (yellow squares), and beside them its challenges (red bulbs), each centered in the row."""
        if state.timeouts is not None:
            left = state.timeouts.away if side is HomeAway.AWAY else state.timeouts.home
            _pips(canvas, x, row_top, row_height, left, total=_TIMEOUTS_PER_HALF, color=_YELLOW, round_=False)
        if state.challenges is not None:
            left = state.challenges.away if side is HomeAway.AWAY else state.challenges.home
            _pips(
                canvas,
                x + _PIP + _PIP_GAP,
                row_top,
                row_height,
                left,
                total=CHALLENGES_PER_GAME,
                color=_RED,
                round_=True,
            )

    def _single_dots(self, canvas: Canvas, state: FootballGameState, side: HomeAway, *, row_top: int) -> None:
        """Timeouts then challenges as 2px dots in one lane; dim once spent, spent ones on top."""
        groups = (
            (state.timeouts, _TIMEOUTS_PER_HALF, _YELLOW, _SINGLE_TIMEOUT_TOP),
            (state.challenges, CHALLENGES_PER_GAME, _RED, _SINGLE_CHALLENGE_TOP),
        )
        for counts, total, color, offset in groups:
            if counts is None:
                continue
            left = counts.away if side is HomeAway.AWAY else counts.home
            for i in range(total):
                fill = _DIM if i < total - left else color
                canvas.fill_rect(
                    _SINGLE_DOT_X, row_top + offset + i * _SINGLE_DOT_PITCH, _SINGLE_DOT, _SINGLE_DOT, fill
                )


def _pips(
    canvas: Canvas, x: int, row_top: int, row_height: int, left: int, *, total: int, color: RGBColor, round_: bool
) -> None:
    """``total`` pips stacked and centered in the row: the bottom ``left`` lit, the spent ones above them dim."""
    span = total * _PIP + (total - 1) * _PIP_GAP
    top = row_top + (row_height - span) // 2
    for i in range(total):
        fill = _DIM if i < total - left else color
        y = top + i * (_PIP + _PIP_GAP)
        if round_:  # a bulb: the first and last rows inset a pixel, which is all it takes to read round
            for row in range(_PIP):
                inset = 1 if row in (0, _PIP - 1) else 0
                canvas.fill_rect(x + inset, y + row, _PIP - 2 * inset, 1, fill)
        else:
            canvas.fill_rect(x, y, _PIP, _PIP, fill)


def _sprite(canvas: Canvas, x: int, y: int, rows: tuple[str, ...], color: RGBColor) -> None:
    for dy, row in enumerate(rows):
        for dx, cell in enumerate(row):
            if cell == "X":
                canvas.set_pixel(x + dx, y + dy, color)
