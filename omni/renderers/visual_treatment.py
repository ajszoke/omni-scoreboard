"""The unified visual treatment for a matchup: one source of truth for marks + meter.

A live card draws two team rows, each with a left **mark** (a logo tile or a color-bar
fallback) and a thin win-probability **meter** hugging that mark. Historically those two
decisions lived in three places — the clash resolver, the mark renderer, and the meter
renderer — each re-deriving geometry. They drifted: the meter assumed a tile inset the
mark renderer didn't use, so a full home gauge could spill into the strip below.

`MatchupVisualTreatment` resolves the pair **once** per render: each side's logo variant
(clash-resolved), whether a tile or a bar actually draws, the mark's bounds, and the
meter's bounds *derived from the mark* (same vertical span, hugging its right edge — so a
full gauge can never exceed the mark it belongs to). The meter color is value-lifted
through a `VisualContrastPolicy`, the one hardware-tunable knob for panel legibility, so a
brighter diffuser or a dimmer build can raise the floor without touching renderer code.

A color-bar mark paints from the same two-color palette the tiles do, so a club reads alike on
every board. It carries no logo, though, so black — an unlit LED — can't name the club, and two
clubs that are distinct as tiles can still merge as bars once both are lifted; the bars therefore
resolve their own clash, on the colors they actually paint — home keeping its first color, away
switching, same as the tiles. A club left with nothing else to spend on its meter, or whose spare
color would read as the mark it sits against, meters in white, and white is what an away bar with no
second color switches to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omni.core.colors import RGBColor
from omni.core.enum import PanelProfile
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.renderers.logo_clash import logo_background, pick_distinct_variants, resolve_logo_variants

if TYPE_CHECKING:
    from omni.renderers.image import LogoStore

__all__ = [
    "VisualContrastPolicy",
    "DEFAULT_CONTRAST_POLICY",
    "Rect",
    "MarkTreatment",
    "MatchupVisualTreatment",
    "resolve_matchup_treatment",
    "resolve_bar_colors",
]

_LOGO_SIZE = 20  # the committed cap-insignia tiles are 20x20
_IDEAL_METER_WIDTH = 2  # the target gauge width; it narrows only when a layout is tight
_MIN_METER_WIDTH = 1
_BLACK = RGBColor(0, 0, 0)
_WHITE = RGBColor(255, 255, 255)


@dataclass(frozen=True, slots=True)
class VisualContrastPolicy:
    """Hardware-tunable legibility thresholds for a matchup's colors on the black panel.

    One policy per `DisplaySink`/device (with optional per-profile overrides): a brighter
    diffuser, a dimmer brightness, or a particular matrix batch path can want a higher floor.
    `min_contrast` is the WCAG ratio a dim brand color is value-lifted to before it paints a
    meter; `clash_delta_e` is the CIE76 distance below which two team tiles read as one color
    and one side flips to its alt. Defaults reproduce the pre-policy hardcoded behavior.
    """

    min_contrast: float = 3.0
    clash_delta_e: float = 25.0

    def lift(self, color: RGBColor) -> RGBColor:
        """Value-lift `color` to this policy's contrast floor against the black panel."""
        return color.value_lifted(min_contrast=self.min_contrast)


DEFAULT_CONTRAST_POLICY = VisualContrastPolicy()


@dataclass(frozen=True, slots=True)
class Rect:
    """An integer pixel rectangle (top-left origin)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        """The x just past the rectangle's right edge."""
        return self.x + self.width


@dataclass(frozen=True, slots=True)
class MarkTreatment:
    """One team's resolved treatment: variant, whether a tile draws, and the mark + meter bounds.

    `mark` and `meter` come from one derivation — the meter hugs the mark's right edge and shares
    its vertical span — so a full gauge can never drift past the mark it belongs to. `meter_color`
    is the team's *freed* color (the palette color its mark is NOT showing), already value-lifted
    through the contrast policy, or white when it has none that reads apart from that mark.
    `bar_color` is what the mark paints when `is_tile` is False.
    """

    variant: LogoVariant
    is_tile: bool
    mark: Rect
    meter: Rect
    meter_color: RGBColor
    bar_color: RGBColor


@dataclass(frozen=True, slots=True)
class MatchupVisualTreatment:
    """Both sides' resolved visual treatment for one card render."""

    away: MarkTreatment
    home: MarkTreatment


@dataclass(frozen=True, slots=True)
class _ProfileGeom:
    """Where a live-card team mark sits for one profile — the single geometry the meter derives from.

    `tiles_fit` is False for the single profile, whose 16px rows never fit a 20px tile (it always
    draws the color bar). `label_x` is where the score/abbreviation begins; the meter fills the gap
    between the mark's right edge and it.
    """

    tiles_fit: bool
    tile_x: int
    bar_x: int
    bar_width: int
    label_x: int
    height: int


# The live card's own row geometry (20px rows on quad/stack with flush tiles; 16px bars on single).
# Distinct from `team_row._GEOM` (the 32px pregame/final rows) — keeping the meter tied to *these*
# bounds is exactly what fixes the quad gauge that used to sit 6px too low and clip the strip.
_LIVE_GEOM: dict[PanelProfile, _ProfileGeom] = {
    PanelProfile.QUAD_128X64: _ProfileGeom(tiles_fit=True, tile_x=0, bar_x=0, bar_width=4, label_x=22, height=20),
    PanelProfile.STACK_64X64: _ProfileGeom(tiles_fit=True, tile_x=1, bar_x=0, bar_width=3, label_x=23, height=20),
    PanelProfile.SINGLE_64X32: _ProfileGeom(tiles_fit=False, tile_x=0, bar_x=0, bar_width=2, label_x=4, height=16),
}


@dataclass(frozen=True, slots=True)
class _BarColors:
    """What one side's color bar paints, and the palette color that leaves spare for its meter."""

    bar: RGBColor
    spare: RGBColor


def _meter_width(gap: int) -> int:
    """The gauge width fitting a `gap`-pixel space before the label: the ideal 2px, but as little
    as 1px when the gap is tight — never wider, which would crowd the score."""
    return max(_MIN_METER_WIDTH, min(_IDEAL_METER_WIDTH, gap))


def _meter_color(spare: RGBColor | None, mark: RGBColor | None, policy: VisualContrastPolicy) -> RGBColor:
    """Lift `spare` for the meter beside `mark`; white when there is no spare to spend (black lights
    nothing, and lifting it only yields a grey no club wears) or it would read as the mark itself."""
    if spare is None or spare == _BLACK:
        return _WHITE
    lifted = policy.lift(spare)
    return _WHITE if mark is not None and lifted.delta_e(mark) < policy.clash_delta_e else lifted


def _freed_base_meter(team: Team) -> RGBColor | None:
    """The meter color a club names for when its base is freed, if it names one."""
    return team.logo.meter_color if team.logo is not None else None


def _bar_palette(team: Team, policy: VisualContrastPolicy) -> tuple[RGBColor, RGBColor]:
    """The two colors a bar can paint for `team`, best first: its palette lifted with black dropped,
    and white filling in for whatever that leaves it short of."""
    backgrounds = (logo_background(team, LogoVariant.BASE), logo_background(team, LogoVariant.ALT))
    lit = [policy.lift(c) for c in backgrounds if c is not None and c != _BLACK] or [policy.lift(team.primary_color)]
    return lit[0], lit[1] if len(lit) > 1 else _WHITE


def _resolve_bars(away: Team, home: Team, policy: VisualContrastPolicy) -> tuple[_BarColors, _BarColors]:
    """Pick both sides' bar colors: home paints its first, away switches when the two would merge."""
    away_first, away_second = _bar_palette(away, policy)
    home_first, home_second = _bar_palette(home, policy)
    picks = pick_distinct_variants(away_first, away_second, home_first, threshold=policy.clash_delta_e)
    away_bars = (
        _BarColors(bar=away_second, spare=_freed_base_meter(away) or away_first)
        if picks.away is LogoVariant.ALT
        else _BarColors(bar=away_first, spare=away_second)
    )
    return away_bars, _BarColors(bar=home_first, spare=home_second)


def _mark_treatment(
    team: Team,
    variant: LogoVariant,
    bars: _BarColors,
    *,
    profile: PanelProfile,
    logos: LogoStore | None,
    row_top: int,
    policy: VisualContrastPolicy,
) -> MarkTreatment:
    geom = _LIVE_GEOM[profile]
    asset = team.logo_alt if variant is LogoVariant.ALT and team.logo_alt is not None else team.logo
    is_tile = geom.tiles_fit and logos is not None and logos.resolve(asset) is not None
    if is_tile:
        mark = Rect(geom.tile_x, row_top, _LOGO_SIZE, geom.height)
        if variant is LogoVariant.ALT:
            spare = _freed_base_meter(team) or logo_background(team, LogoVariant.BASE)
        else:
            spare = logo_background(team, LogoVariant.ALT)
        meter_color = _meter_color(spare, logo_background(team, variant), policy)
    else:
        mark = Rect(geom.bar_x, row_top, geom.bar_width, geom.height)
        meter_color = _meter_color(bars.spare, bars.bar, policy)
    meter = Rect(mark.right, row_top, _meter_width(geom.label_x - mark.right), geom.height)
    return MarkTreatment(
        variant=variant, is_tile=is_tile, mark=mark, meter=meter, meter_color=meter_color, bar_color=bars.bar
    )


def resolve_bar_colors(away: Team, home: Team, *, policy: VisualContrastPolicy) -> tuple[RGBColor, RGBColor]:
    """The colors both sides' bars paint, for a card that draws bars without a full treatment."""
    away_bars, home_bars = _resolve_bars(away, home, policy)
    return away_bars.bar, home_bars.bar


def resolve_matchup_treatment(
    away: Team,
    home: Team,
    *,
    profile: PanelProfile,
    logos: LogoStore | None,
    policy: VisualContrastPolicy,
    away_top: int,
    home_top: int,
) -> MatchupVisualTreatment:
    """Resolve both sides' visual treatment for one live-card render.

    The variant pair is clash-resolved once (so the two sides never disagree on who flipped), each
    side's tile-vs-bar is read from the store, and the meter geometry is derived from the actual
    mark — so mark and gauge are guaranteed to agree.
    """
    variants = resolve_logo_variants(away, home, threshold=policy.clash_delta_e)
    away_bars, home_bars = _resolve_bars(away, home, policy)
    return MatchupVisualTreatment(
        away=_mark_treatment(
            away, variants.away, away_bars, profile=profile, logos=logos, row_top=away_top, policy=policy
        ),
        home=_mark_treatment(
            home, variants.home, home_bars, profile=profile, logos=logos, row_top=home_top, policy=policy
        ),
    )
