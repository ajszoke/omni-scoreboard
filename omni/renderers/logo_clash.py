"""Resolve which logo variant each side of a matchup shows.

Two clubs whose tiles render on perceptually close backgrounds blur into one another
on a small LED panel — and MLB's palette is navy/black/red-heavy, so this is common
(BOS/DET/NYY share an identical cap navy; COL/CWS/MIA/PIT/SF an identical black). When
two bases clash, the away side renders its alt tile (a distinct second brand color)
instead; the home club always keeps its primary.

The decision reads `LogoAsset.preferred_background` — the color each committed tile
actually renders on — and compares backgrounds with CIE76 `delta_e`. The away side can
only switch if it carries an alt tile with a known background; without one (a test stub, a
not-yet-modelled league) both stay on their base, exactly as before logos clashed.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team

__all__ = ["CLASH_DELTA_E", "MatchupLogoVariants", "logo_background", "pick_distinct_variants", "resolve_logo_variants"]

# Below this CIE76 distance two backgrounds blur together on a small panel. It is the
# same floor `tests/test_mlb_palette.py` holds between every club's own base and alt —
# the perceptual question is identical: "do these two tiles read as one color?"
CLASH_DELTA_E = 25.0


@dataclass(frozen=True, slots=True)
class MatchupLogoVariants:
    """The logo variant each side of a matchup should render."""

    away: LogoVariant
    home: LogoVariant


def logo_background(team: Team, variant: LogoVariant) -> RGBColor | None:
    """The color `team`'s `variant` tile renders on, or `None` if it has no such tile."""
    asset = team.logo_alt if variant is LogoVariant.ALT else team.logo
    return asset.preferred_background if asset is not None else None


def resolve_logo_variants(away: Team, home: Team, *, threshold: float = CLASH_DELTA_E) -> MatchupLogoVariants:
    """Pick each side's logo variant so the two tiles stay perceptually distinct.

    The home club always keeps its primary. On a clash (`delta_e` below `threshold`) the away club
    is the one that switches to its alt — provided it has one and the switch actually separates the
    two marks; otherwise both stay on base.
    """
    return pick_distinct_variants(
        logo_background(away, LogoVariant.BASE),
        logo_background(away, LogoVariant.ALT),
        logo_background(home, LogoVariant.BASE),
        threshold=threshold,
    )


def pick_distinct_variants(
    away_base: RGBColor | None,
    away_alt: RGBColor | None,
    home_base: RGBColor | None,
    *,
    threshold: float,
) -> MatchupLogoVariants:
    """The switch decision on bare colors, so a mark drawn from other than the tiles can share it."""
    both_base = MatchupLogoVariants(LogoVariant.BASE, LogoVariant.BASE)
    if away_base is None or home_base is None or away_alt is None:
        return both_base  # nothing to compare, or nothing to switch to (e.g. a test stub) — leave as-is
    base_sep = away_base.delta_e(home_base)
    if base_sep >= threshold or away_alt.delta_e(home_base) <= base_sep:
        return both_base  # already distinct, or the alt would not help
    return MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)
