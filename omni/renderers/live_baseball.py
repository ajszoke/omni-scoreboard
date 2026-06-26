"""Renderer for the live-baseball card across all three panel profiles.

Each profile gets its OWN layout, never a crop of another (AGENTS.md forbids
"cropping 128x64 cards down to 64x32"):

- quad_128x64 : a 40px top block — two team rows (logo tile + an inline ``R H E`` line score;
  the abbreviation is dropped when a tile resolves, since the logo names the team) beside a
  compact 3-row state module (inning + 2nd base / 1st + 3rd base / count + out-dots) — over a
  24px full-width pitcher/batter strip.
- stack_64x64 : a compressed layout — two team rows (logo or bar, run score + a dim ``H E``
  line) up top, status + bases below.
- single_64x32: an explicit COMPROMISE — team abbreviations, scores, and the
  inning-phase label only. Count, outs, the bases, and the H/E detail are omitted
  (not legible at 64x32). The compromise is asserted by tests so it cannot silently crop.

During a between-halves break (`InningPhase.MIDDLE`/`END`) there is no active
at-bat, so the larger profiles show the phase label alone and suppress the count,
outs, and bases — a stale "1-2, 2 OUT" between innings would be misinformation.
"""

from __future__ import annotations

from datetime import datetime
from typing import assert_never

from omni.cards.base import ScoreboardCard
from omni.cards.baseball import LiveBaseballCardPayload
from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.baseball import BatterGameLine, InningPhase, TeamLinescore
from omni.domain.contest import TeamGame
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.team_row import draw_matchup_marks
from omni.renderers.visual_treatment import MarkTreatment, resolve_matchup_treatment
from omni.renderers.win_meter import draw_win_meter
from omni.renderers.text import draw_centered, draw_right_aligned, text_width
from omni.renderers.text_lane import LaneAnchor, TextLane, draw_text_lane

__all__ = ["LiveBaseballRenderer"]

_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)
_YELLOW = RGBColor(255, 215, 0)
_DIM = RGBColor(60, 60, 60)
_HE = RGBColor(160, 160, 160)  # hits/errors line — legible but secondary to the bright run score

_LABEL_FONT = "4x6"
_SCORE_FONT = "6x10"
_THIN = "\N{THIN SPACE}"  # U+2009, a 2px space in 4x6 — packs dense statlines tighter than a full cell

# Active halves point in the batting team's direction (broadcast convention): up = top of the
# inning (visitor batting), down = bottom (home batting); a break keeps a short word. The quad's
# 6x10 font carries the board's filled triangles, so it uses those; the 64x32/64x64 status sits
# in 4x6, which has no triangle glyph, so those profiles keep the arrows — a per-profile compromise.
_ARROW_UP, _ARROW_DOWN = "↑", "↓"  # stack/single (4x6)
_TRIANGLE_UP, _TRIANGLE_DOWN = "▲", "▼"  # quad (6x10)

# Quad strip geometry: roster text runs from x=2 to _STRIP_RIGHT. The live pitch is the
# pitcher's own action, so it gets a reserved lane on the right of the pitcher row; the pitcher's
# stat line stops short of it, marqueeing within what's left rather than colliding with the token.
_STRIP_RIGHT = 126  # right inset for the strip's text lanes (a 2px margin off the 128 edge)
_PITCH_LANE_X = 93  # the pitch's reserved lane spans [_PITCH_LANE_X, _STRIP_RIGHT) on the pitcher row
_STATS_GAP = 3  # the gap between a roster name and the stat line that trails it


def _phase_label(phase: InningPhase, inning: int, *, up: str, down: str) -> str:
    """Compact inning-phase label: the ``up``/``down`` head when active, ``MID``/``END`` on a break."""
    heads = {InningPhase.TOP: up, InningPhase.MIDDLE: "MID", InningPhase.BOTTOM: down, InningPhase.END: "END"}
    return f"{heads[phase]}{inning}"


class LiveBaseballRenderer:
    """Draws a live MLB game card onto a `Canvas` for any supported profile."""

    supported_profiles = frozenset(
        {
            PanelProfile.SINGLE_64X32,
            PanelProfile.STACK_64X64,
            PanelProfile.QUAD_128X64,
        }
    )

    def render(
        self,
        card: ScoreboardCard[LiveBaseballCardPayload],
        context: RenderContext,
        canvas: Canvas,
    ) -> None:
        game = card.contest
        if not isinstance(game, TeamGame):
            raise TypeError("live-baseball card requires a TeamGame contest")
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

    def _render_quad(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # Top 40px: two team rows (logo + inline R/H/E) on the left, the game-state module on the
        # right. Bottom 24px: the pitcher/batter strip. The logo identifies the team, so the
        # abbreviation is dropped whenever a tile resolves (kept only as the color-bar fallback).
        treatment = resolve_matchup_treatment(
            game.away,
            game.home,
            profile=PanelProfile.QUAD_128X64,
            logos=context.logos,
            policy=context.contrast,
            away_top=0,
            home_top=20,
        )
        self._draw_mark(canvas, context, game.away, treatment.away)
        self._draw_mark(canvas, context, game.home, treatment.home)
        if payload.win_probability is not None:
            draw_win_meter(canvas, treatment, payload.win_probability)
        self._line_score(canvas, game.away, payload.away_line, has_logo=treatment.away.is_tile, y=5)
        self._line_score(canvas, game.home, payload.home_line, has_logo=treatment.home.is_tile, y=25)
        if payload.phase.is_break:  # between halves: no at-bat, no bases — just the inning
            label = _phase_label(payload.phase, payload.inning, up=_TRIANGLE_UP, down=_TRIANGLE_DOWN)
            draw_centered(canvas, 60, 128, 14, label, _YELLOW, _SCORE_FONT)
            return
        self._state_module(canvas, payload)
        self._batter_pitcher_strip(canvas, payload, now=context.now)

    def _draw_mark(self, canvas: Canvas, context: RenderContext, team: Team, side: MarkTreatment) -> None:
        """Draw the team's mark from its resolved treatment: the logo tile, or the color-bar fallback.

        The bounds come from the treatment — the same source the win meter derives from — so the tile
        and its gauge are guaranteed to agree, never the 6px drift a separate meter geometry once had.
        """
        logo = None
        if side.is_tile and context.logos is not None:
            asset = team.logo_alt if side.variant is LogoVariant.ALT and team.logo_alt is not None else team.logo
            logo = context.logos.resolve(asset)
        if logo is not None:
            canvas.draw_image(side.mark.x, side.mark.y, logo)
        else:
            canvas.fill_rect(side.mark.x, side.mark.y, side.mark.width, side.mark.height, team.primary_color)

    def _line_score(self, canvas: Canvas, team: Team, line: TeamLinescore, *, has_logo: bool, y: int) -> None:
        """Draw a team's ``R H E`` as three equal numbers; prepend the abbr only without a logo."""
        rhe = f"{line.runs} {line.hits} {line.errors}"
        if has_logo:
            canvas.text(26, y, rhe, _WHITE, font=_SCORE_FONT)
        else:
            canvas.text(8, y, team.abbreviation, _WHITE, font=_SCORE_FONT)
            canvas.text(8 + len(team.abbreviation) * 6 + 4, y, rhe, _WHITE, font=_SCORE_FONT)

    def _state_module(self, canvas: Canvas, payload: LiveBaseballCardPayload) -> None:
        """The compact 3-row game-state cluster on the right: inning+2B / 1B+3B / count+outs.

        Inning and count are the big (score) font; there is room, and they read at a glance.
        """
        inning = _phase_label(payload.phase, payload.inning, up=_TRIANGLE_UP, down=_TRIANGLE_DOWN)
        canvas.text(64, 2, inning, _YELLOW, font=_SCORE_FONT)  # row 1: inning (the triangle points to the batting half)
        self._diamond(canvas, 102, 8, 3, payload.bases.second)  # 2nd base — top, row 1
        self._diamond(canvas, 96, 20, 3, payload.bases.third)  # 3rd base — left, row 2
        self._diamond(canvas, 108, 20, 3, payload.bases.first)  # 1st base — right, row 2
        canvas.text(64, 28, f"{payload.count.balls}-{payload.count.strikes}", _WHITE, font=_SCORE_FONT)  # row 3: count
        self._out_dots(canvas, 98, 31, payload.count.outs)  # row 3: outs, below the diamond (where home would be)

    def _batter_pitcher_strip(self, canvas: Canvas, payload: LiveBaseballCardPayload, *, now: datetime) -> None:
        """The bottom strip: the pitcher, the batter, and the at-bat's live pitch.

        ``P:`` leads the pitcher and the lineup spot (``4.``) the batter; the leader + name is the
        big (score) font, drawn still, the stat line a size smaller and dimmer beside it. The live
        pitch token (velocity + 4-char type, e.g. ``99 SWPR``) takes a reserved lane on the right of
        the *pitcher* row — it is the pitcher's own throw — so the pitcher's stats stop short of it
        and the batter row stays clear of it entirely. Drawn only on a live at-bat (the caller skips
        the whole strip on a break).
        """
        pitcher_right = _STRIP_RIGHT
        pitch = payload.last_pitch
        if pitch is not None:
            # The live pitch shows bright (not the dim stat gray) and the 4-char type names it; its
            # own lane right-anchors the token and keeps the pitcher's stat line from running under it.
            lane = TextLane(
                x=_PITCH_LANE_X, y=44, width=_STRIP_RIGHT - _PITCH_LANE_X, font=_LABEL_FONT, anchor=LaneAnchor.RIGHT
            )
            draw_text_lane(canvas, lane, pitch.token, _WHITE, now=now)
            pitcher_right = _PITCH_LANE_X - 2  # the pitcher's stats stop a hair before the pitch lane
        pitcher = payload.pitcher
        if pitcher is not None:
            stats = f"{pitcher.innings_pitched}IP{_THIN}{pitcher.strikeouts}K{_THIN}{pitcher.pitches}P"
            self._roster_line(canvas, f"P: {pitcher.name}", stats, y=41, right=pitcher_right, now=now)
        batter = payload.batter
        if batter is not None:
            leader = f"{batter.order}." if batter.order is not None else "#."
            self._roster_line(
                canvas, f"{leader} {batter.name}", self._batter_line(batter), y=52, right=_STRIP_RIGHT, now=now
            )

    @staticmethod
    def _batter_line(batter: BatterGameLine) -> str:
        """The batter's day in the legacy stat order — hits-for-AB, then HR, then RBI.

        Cribbed from the legacy board: a lone HR/RBI shows just the label (a flag that it
        happened), two or more carry the count. Tokens are thin-spaced to stay compact.
        (The legacy order slots 3B/2B between HR and RBI — restored here once modelled.)
        """
        parts = [f"{batter.hits}-{batter.at_bats}"]
        if batter.home_runs > 0:
            parts.append(f"{batter.home_runs}HR" if batter.home_runs > 1 else "HR")
        if batter.rbi > 0:
            parts.append(f"{batter.rbi}RBI" if batter.rbi > 1 else "RBI")
        return _THIN.join(parts)

    def _roster_line(self, canvas: Canvas, name: str, stats: str, *, y: int, right: int, now: datetime) -> None:
        """A strip line: ``name`` in the big font, then ``stats`` a size smaller beside it.

        When the name leaves room, it is drawn where it sits — always readable — and the stats take
        a lane from just past it to ``right``, marqueeing there when the day's line outgrows the gap
        (a long pitcher line, extra-innings totals). A name long enough to crowd out its own stats
        instead claims the whole line as its lane and marquees, so it can never overrun ``right``
        (and the pitch lane past it). Either way nothing draws beyond the line's bounds.
        """
        stats_x = 2 + text_width(name, _SCORE_FONT) + _STATS_GAP
        if stats_x < right:  # the name leaves room for a stat line beside it
            canvas.text(2, y, name, _WHITE, font=_SCORE_FONT)
            draw_text_lane(
                canvas, TextLane(x=stats_x, y=y + 3, width=right - stats_x, font=_LABEL_FONT), stats, _HE, now=now
            )
        else:  # an overflowing name takes the whole line and marquees; no room left for stats
            draw_text_lane(canvas, TextLane(x=2, y=y, width=right - 2, font=_SCORE_FONT), name, _WHITE, now=now)

    def _render_stack(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # 64x64: the full layout compressed — two team rows up top (logo or bar), status + bases below.
        away_x, home_x = draw_matchup_marks(canvas, context, game.away, game.home, away_top=0, home_top=22)
        if payload.win_probability is not None:
            treatment = resolve_matchup_treatment(
                game.away,
                game.home,
                profile=PanelProfile.STACK_64X64,
                logos=context.logos,
                policy=context.contrast,
                away_top=0,
                home_top=22,
            )
            draw_win_meter(canvas, treatment, payload.win_probability)
        canvas.text(away_x, 6, game.away.abbreviation, _WHITE, font=_SCORE_FONT)
        canvas.text(home_x, 28, game.home.abbreviation, _WHITE, font=_SCORE_FONT)
        draw_right_aligned(canvas, 62, 6, str(payload.away_line.runs), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, 62, 28, str(payload.home_line.runs), _WHITE, _SCORE_FONT)
        label = _phase_label(payload.phase, payload.inning, up=_ARROW_UP, down=_ARROW_DOWN)
        if payload.phase.is_break:
            draw_centered(canvas, 0, 64, 50, label, _YELLOW, _LABEL_FONT)  # break: no live at-bat
            return
        canvas.text(3, 46, label, _YELLOW, font=_LABEL_FONT)
        canvas.text(20, 46, f"{payload.count.balls}-{payload.count.strikes}", _WHITE, font=_LABEL_FONT)
        canvas.text(3, 55, f"{payload.count.outs}{_THIN}OUT", _WHITE, font=_LABEL_FONT)
        self._base(canvas, 49, 45, 5, payload.bases.second)
        self._base(canvas, 43, 52, 5, payload.bases.third)
        self._base(canvas, 55, 52, 5, payload.bases.first)

    def _render_single(
        self, canvas: Canvas, context: RenderContext, game: TeamGame, payload: LiveBaseballCardPayload
    ) -> None:
        # 64x32 compromise: team identity + scores + the inning-phase label. The win meter is
        # omitted here — a probability bar crowds the team stripe at 64px wide and reads poorly;
        # it waits for a dedicated low-res treatment (a small lean glyph) on a later pass.
        canvas.fill_rect(0, 0, 2, 16, game.away.primary_color)
        canvas.fill_rect(0, 16, 2, 16, game.home.primary_color)
        canvas.text(4, 5, game.away.abbreviation, _WHITE, font=_LABEL_FONT)
        canvas.text(4, 21, game.home.abbreviation, _WHITE, font=_LABEL_FONT)
        draw_right_aligned(canvas, 42, 3, str(payload.away_line.runs), _WHITE, _SCORE_FONT)
        draw_right_aligned(canvas, 42, 19, str(payload.home_line.runs), _WHITE, _SCORE_FONT)
        # The phase label (↑7/MID7/↓7/END7) already conveys the break; no count/bases here anyway.
        label = _phase_label(payload.phase, payload.inning, up=_ARROW_UP, down=_ARROW_DOWN)
        canvas.text(46, 13, label, _YELLOW, font=_LABEL_FONT)

    @staticmethod
    def _diamond(canvas: Canvas, cx: int, cy: int, r: int, occupied: bool) -> None:
        """A base marker rotated 45° (a diamond): filled when occupied, dim outline when empty."""
        for dy in range(-r, r + 1):
            half = r - abs(dy)
            if occupied:
                canvas.fill_rect(cx - half, cy + dy, 2 * half + 1, 1, _WHITE)
            else:
                canvas.set_pixel(cx - half, cy + dy, _DIM)
                if half > 0:
                    canvas.set_pixel(cx + half, cy + dy, _DIM)

    @staticmethod
    def _out_dots(canvas: Canvas, x: int, y: int, outs: int) -> None:
        """Three out indicators: a 4px dot each, filled once recorded — the empty ones show outs to go."""
        for i in range(3):
            left = x + i * 6
            if i < outs:
                canvas.fill_rect(left, y, 4, 4, _WHITE)
            else:
                canvas.fill_rect(left, y, 4, 1, _DIM)  # top
                canvas.fill_rect(left, y + 3, 4, 1, _DIM)  # bottom
                canvas.fill_rect(left, y, 1, 4, _DIM)  # left
                canvas.fill_rect(left + 3, y, 1, 4, _DIM)  # right

    @staticmethod
    def _base(canvas: Canvas, x: int, y: int, size: int, occupied: bool) -> None:
        if occupied:
            canvas.fill_rect(x, y, size, size, _WHITE)
            return
        canvas.fill_rect(x, y, size, 1, _DIM)  # top edge
        canvas.fill_rect(x, y + size - 1, size, 1, _DIM)  # bottom edge
        canvas.fill_rect(x, y, 1, size, _DIM)  # left edge
        canvas.fill_rect(x + size - 1, y, 1, size, _DIM)  # right edge
