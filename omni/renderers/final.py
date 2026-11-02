"""Renderer for the final (postgame) baseball card across all three panel profiles.

The completed game with a **winner-derived treatment**: the winning side is drawn
bright and the loser dimmed (a tie leaves both bright), with a FINAL marker. Which
side won is the payload's derivation (`FinalCardPayload.winner`) — never a
position-based fade of "the home row". When the payload carries pitching decisions,
a W/L/S line (last names) accompanies the marker — degrading by profile:

- quad_128x64 : the live card's tiles and full R/H/E on the left, "FINAL" under them, and the
  W/L/S (winner, loser, and save when there is one, each over their season total) down the right.
- stack_64x64 : team rows + scores up top; "FINAL" then W/L below — the save line
  is dropped (no vertical room), an explicit, asserted COMPROMISE.
- single_64x32: the status reads "FIN" ("FINAL" does not fit at 64px wide) and the
  pitching line is dropped entirely — an explicit, asserted COMPROMISE.
"""

from __future__ import annotations

from typing import assert_never

from omni.cards.baseball import FinalCardPayload
from omni.cards.base import ScoreboardCard
from omni.core.colors import RGBColor
from omni.core.enum import HomeAway, PanelProfile
from omni.domain.baseball import PitchingDecisions, TeamLinescore
from omni.domain.contest import TeamGame
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.line_score import LINE_SCORE_RIGHT, draw_line_score_row, line_score_face
from omni.renderers.player_lines import PanelEntry, draw_panel_entry
from omni.renderers.team_row import draw_matchup_marks, draw_single_bars, draw_treated_mark
from omni.renderers.text import draw_centered, draw_right_aligned
from omni.renderers.visual_treatment import resolve_matchup_treatment

__all__ = ["FinalRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)  # the winner (or both sides on a tie)
_LOSER = RGBColor(110, 110, 110)  # the loser, dimmed but still legible
_YELLOW = RGBColor(255, 215, 0)
_HE_DIM = RGBColor(204, 204, 204)  # the winner's hits + errors, a step under its runs

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"
_MARKER_FONT = "8x13"

_REGULATION_INNINGS = 9
_ROW_HEIGHT = 20
_MARKER_Y = 46  # the same spot the live card's break marker takes
_ENTRY_HEIGHT = 12  # a decision's name + stat line, from the first lit row to the last
_ENTRY_GAP = 8
_BOARD_HEIGHT = 64


def _side_color(side: HomeAway, winner: HomeAway | None) -> RGBColor:
    """Bright for the winner (or both, on a tie); dimmed for the loser."""
    return _WHITE if winner is None or side is winner else _LOSER


def _last_name(full_name: str) -> str:
    """The last whitespace-delimited token of a pitcher's name — what fits on a panel."""
    parts = full_name.split()
    return parts[-1] if parts else full_name


def _final_marker(inning: int | None) -> str:
    """``FINAL``, with the inning count when the game ran other than nine, like ``FINAL/10``."""
    return "FINAL" if inning is None or inning == _REGULATION_INNINGS else f"FINAL/{inning}"


def _line_values(line: TeamLinescore | None, score: int) -> tuple[int, ...]:
    """A row's columns: the full R/H/E, or the runs alone when that's all the card knows."""
    return (line.runs, line.hits, line.errors) if line is not None else (score,)


def _decision_entries(decisions: PitchingDecisions) -> list[PanelEntry]:
    """The W/L/S panel: names in yellow over season totals in white, the loser faded to match."""
    entries = [
        PanelEntry(
            leader="W",
            name=_last_name(decisions.winner),
            name_color=_YELLOW,
            stats=str(decisions.winner_record) if decisions.winner_record is not None else None,
            stats_color=_WHITE,
        ),
        PanelEntry(
            leader="L",
            name=_last_name(decisions.loser),
            name_color=_LOSER,
            stats=str(decisions.loser_record) if decisions.loser_record is not None else None,
            stats_color=_LOSER,
        ),
    ]
    if decisions.save is not None:
        entries.append(
            PanelEntry(
                leader="S",
                name=_last_name(decisions.save),
                name_color=_YELLOW,
                stats=f"{decisions.saves} SV" if decisions.saves is not None else None,
                stats_color=_WHITE,
            )
        )
    return entries


class FinalRenderer:
    """Draws a final MLB game card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[FinalCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("final card requires a TeamGame contest")
        payload = card.payload
        away = _side_color(HomeAway.AWAY, payload.winner)
        home = _side_color(HomeAway.HOME, payload.winner)

        canvas.fill(_BLACK)
        if context.profile is PanelProfile.QUAD_128X64:
            self._render_quad(canvas, context, game, payload, away, home)
        elif context.profile is PanelProfile.STACK_64X64:
            self._render_stack(canvas, context, game, payload, away, home)
        elif context.profile is PanelProfile.SINGLE_64X32:
            self._render_single(canvas, context, game, payload, away, home)
        else:  # pragma: no cover - exhaustiveness guard; mypy errors if a profile is unhandled
            assert_never(context.profile)

    def _render_quad(
        self,
        canvas: Canvas,
        context: RenderContext,
        game: TeamGame,
        payload: FinalCardPayload,
        away: RGBColor,
        home: RGBColor,
    ) -> None:
        # The team mark (logo or bar) is full-color; only the label/score carry the winner/loser dim.
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.QUAD_128X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=_ROW_HEIGHT,
        )
        away_values = _line_values(payload.away_line, payload.away_score)
        home_values = _line_values(payload.home_line, payload.home_score)
        face = line_score_face(away_values + home_values)
        rows = (
            (game.away, treatment.away, away_values, away, 0),
            (game.home, treatment.home, home_values, home, _ROW_HEIGHT),
        )
        for team, side, values, color, row_top in rows:
            draw_treated_mark(canvas, context, team, side)
            if not side.is_tile:  # only the color bar drew, so the abbreviation has to name the team
                canvas.text(3, row_top + 5, team.abbreviation, color, font=_SCORE_FONT)
            secondary = _HE_DIM if color == _WHITE else color
            draw_line_score_row(canvas, values, face, row_top=row_top, colors=(color, secondary, secondary))
        draw_centered(canvas, 0, LINE_SCORE_RIGHT, _MARKER_Y, _final_marker(payload.inning), _YELLOW, _MARKER_FONT)
        if payload.decisions is not None:
            entries = _decision_entries(payload.decisions)
            span = len(entries) * _ENTRY_HEIGHT + (len(entries) - 1) * _ENTRY_GAP
            top = (_BOARD_HEIGHT - span) // 2 - 1  # the name font lights from its cell's second row
            for i, entry in enumerate(entries):
                draw_panel_entry(canvas, entry, y=top + i * (_ENTRY_HEIGHT + _ENTRY_GAP), now=context.now)

    def _render_stack(
        self,
        canvas: Canvas,
        context: RenderContext,
        game: TeamGame,
        payload: FinalCardPayload,
        away: RGBColor,
        home: RGBColor,
    ) -> None:
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=22)
        canvas.text(away_x, 6, game.away.abbreviation, away, font=_SCORE_FONT)
        canvas.text(home_x, 28, game.home.abbreviation, home, font=_SCORE_FONT)
        draw_right_aligned(canvas, 62, 6, str(payload.away_score), away, _SCORE_FONT)
        draw_right_aligned(canvas, 62, 28, str(payload.home_score), home, _SCORE_FONT)
        draw_centered(canvas, 0, 64, 43, "FINAL", _YELLOW, _LABEL_FONT)
        decisions = payload.decisions
        if decisions is not None:
            # 64x64 compromise: W and L only — there is no vertical room for the save line.
            canvas.text(4, 51, f"W {_last_name(decisions.winner)}", _WHITE, font=_LABEL_FONT)
            canvas.text(4, 58, f"L {_last_name(decisions.loser)}", _LOSER, font=_LABEL_FONT)

    def _render_single(
        self,
        canvas: Canvas,
        context: RenderContext,
        game: TeamGame,
        payload: FinalCardPayload,
        away: RGBColor,
        home: RGBColor,
    ) -> None:
        # 64x32 compromise: abbreviations + scores + a shortened "FIN" status.
        draw_single_bars(canvas, context, game.away, game.home)
        canvas.text(4, 5, game.away.abbreviation, away, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, home, font=_LABEL_FONT)
        draw_right_aligned(canvas, 42, 3, str(payload.away_score), away, _SCORE_FONT)
        draw_right_aligned(canvas, 42, 19, str(payload.home_score), home, _SCORE_FONT)
        canvas.text(46, 13, "FIN", _YELLOW, font=_LABEL_FONT)
