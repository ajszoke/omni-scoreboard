"""Renderer for the pregame baseball card across all three panel profiles.

A pregame card shows the matchup and a live **first-pitch countdown**. The countdown
is derived from the render clock (`RenderContext.now`) against the card's snapshotted
`scheduled_start`, so one card's display advances every tick — "2h05m" -> "45m" ->
"SOON" — without the card being rebuilt.

Each profile gets its OWN layout, never a crop of another:

- quad_128x64 : with the clubs' records, starters and odds in hand, each tile carries a line of
  record + odds over a line of starter + season stats, above the start time and the countdown.
  Without them, team rows on the left (color bar + abbreviation, no scores yet), a "FIRST PITCH"
  label and the countdown on the right.
- stack_64x64 : the two team rows up top, label + countdown below.
- single_64x32: an explicit COMPROMISE — matchup abbreviations + countdown only.
  The "FIRST PITCH" label is dropped (no room). Asserted by tests so it cannot
  silently regress into a crop.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import assert_never

from omni.cards.baseball import PregameCardPayload
from omni.domain.baseball import BettingLine, PregameSide, StartingPitcher
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.player_lines import draw_name_with_stats
from omni.renderers.team_row import draw_matchup_marks, draw_single_bars, draw_treated_mark
from omni.renderers.text import draw_centered, draw_right_aligned
from omni.renderers.visual_treatment import resolve_matchup_treatment

__all__ = ["PregameRenderer", "first_pitch_label"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)
_ODDS_GREEN = RGBColor(50, 180, 50)
_STATS_TEAL = RGBColor(50, 180, 180)

_LABEL_FONT = "4x6"
_VALUE_FONT = "6x10"
_TIME_FONT = "8x13"
_FIRST_PITCH = "FIRST PITCH"

# The quad's detailed layout: flush tiles, two text lines beside each, the start time below.
_DETAIL_X = 22  # just past the tile
_DETAIL_RIGHT = 128
_DETAIL_ROW_HEIGHT = 20
_DETAIL_LINE_PITCH = 10
_START_TIME_Y = 41
_COUNTDOWN_Y = 55
_TBD = "TBD"


def first_pitch_label(now: datetime, scheduled_start: datetime) -> str:
    """A compact "time until first pitch" string for a small panel.

    ``2h05m`` an hour or more out, ``45m`` within the hour, ``SOON`` under a minute
    out or once first pitch has passed (warmups / a delayed start that is still
    pregame). Both datetimes are timezone-aware (validated upstream).
    """
    seconds = (scheduled_start - now).total_seconds()
    if seconds < 60:
        return "SOON"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def start_time_label(scheduled_start: datetime, zone: tzinfo | None) -> str:
    """The first pitch as a board clock reads it, like ``7:10 PM``, in ``zone``."""
    local = scheduled_start.astimezone(zone)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _line_label(line: BettingLine) -> str:
    """One side's odds as the board prints them: moneyline, then run line and its price if posted."""
    if line.runline is None or line.runline_price is None:
        return f"{line.moneyline:+d}"
    return f"{line.moneyline:+d} ({line.runline:+g} {line.runline_price:+d})"


def _starter_label(starter: StartingPitcher) -> str:
    """A starter's season, as much of it as is known: ``5-9 (4.68 ERA)``."""
    parts = []
    if starter.record is not None:
        parts.append(str(starter.record))
    if starter.era is not None:
        parts.append(f"({starter.era} ERA)")
    return " ".join(parts)


class PregameRenderer:
    """Draws a pregame MLB card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[PregameCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("pregame card requires a TeamGame contest")
        countdown = first_pitch_label(context.now, card.payload.scheduled_start)
        profile = context.profile

        canvas.fill(_BLACK)
        if profile is PanelProfile.QUAD_128X64:
            if card.payload.away is not None or card.payload.home is not None:
                self._render_quad_detailed(canvas, context, game, card.payload, countdown)
            else:
                self._render_quad(canvas, context, game, countdown)
        elif profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, context, game, countdown)
        elif profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, context, game, countdown)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(profile)

    def _render_quad(self, canvas: Canvas, context: RenderContext, game: TeamGame, countdown: str) -> None:
        # Team rows on the left (logo or colour bar, no scores pregame); label + countdown on the right.
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=32)
        canvas.text(away_x, 11, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(home_x, 43, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(66, 16, _FIRST_PITCH, _YELLOW, font=_LABEL_FONT)
        draw_centered(canvas, 64, 128, 34, countdown, _WHITE, _VALUE_FONT)

    def _render_quad_detailed(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: PregameCardPayload, countdown: str
    ) -> None:
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.QUAD_128X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=_DETAIL_ROW_HEIGHT,
        )
        rows = (
            (game.away, treatment.away, payload.away or PregameSide(), 0),
            (game.home, treatment.home, payload.home or PregameSide(), _DETAIL_ROW_HEIGHT),
        )
        for team, side, details, row_top in rows:
            draw_treated_mark(canvas, context, team, side)
            if not side.is_tile:  # only the color bar drew, so the abbreviation has to name the team
                canvas.text(3, row_top + 5, team.abbreviation, _WHITE, font=_VALUE_FONT)
            self._detail_lines(canvas, details, row_top=row_top, now=context.now)
        start_time = start_time_label(payload.scheduled_start, context.timezone or context.now.tzinfo)
        draw_centered(canvas, 0, 128, _START_TIME_Y, start_time, _WHITE, _TIME_FONT)
        draw_centered(canvas, 0, 128, _COUNTDOWN_Y, f"{_FIRST_PITCH} {countdown}", _YELLOW, _LABEL_FONT)

    @staticmethod
    def _detail_lines(canvas: Canvas, details: PregameSide, *, row_top: int, now: datetime) -> None:
        """A club's two lines beside its tile: record + odds, then starter + season stats."""
        record = str(details.record) if details.record is not None else ""
        odds = _line_label(details.line) if details.line is not None else ""
        draw_name_with_stats(
            canvas,
            record,
            odds,
            x=_DETAIL_X,
            y=row_top,
            right=_DETAIL_RIGHT,
            name_color=_WHITE,
            stats_color=_ODDS_GREEN,
            now=now,
        )
        starter = details.starter
        draw_name_with_stats(
            canvas,
            starter.name if starter is not None else _TBD,
            _starter_label(starter) if starter is not None else "",
            x=_DETAIL_X,
            y=row_top + _DETAIL_LINE_PITCH,
            right=_DETAIL_RIGHT,
            name_color=_YELLOW,
            stats_color=_STATS_TEAL,
            now=now,
        )

    def _render_stack(self, canvas: Canvas, context: RenderContext, game: TeamGame, countdown: str) -> None:
        # 64x64: two team rows up top (logo or bar), label + countdown below.
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=22)
        canvas.text(away_x, 6, game.away.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(home_x, 28, game.home.abbreviation, _WHITE, font=_VALUE_FONT)
        canvas.text(3, 46, _FIRST_PITCH, _YELLOW, font=_LABEL_FONT)
        draw_centered(canvas, 0, 64, 54, countdown, _WHITE, _VALUE_FONT)

    def _render_single(self, canvas: Canvas, context: RenderContext, game: TeamGame, countdown: str) -> None:
        # 64x32 compromise: abbreviations + countdown only, no "first pitch" label.
        draw_single_bars(canvas, context, game.away, game.home)
        canvas.text(4, 5, game.away.abbreviation, _WHITE, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, _WHITE, font=_LABEL_FONT)
        draw_right_aligned(canvas, 62, 11, countdown, _WHITE, _VALUE_FONT)
