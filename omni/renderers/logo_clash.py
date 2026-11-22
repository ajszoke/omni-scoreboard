"""Resolve which logo variant each side of a matchup shows.

Two clubs whose tiles render on perceptually close backgrounds blur into one another
on a small LED panel — and MLB's palette is navy/black/red-heavy, so this is common
(BOS/DET/NYY share an identical cap navy; COL/CWS/MIA/PIT/SF an identical black). When
two bases clash, the away side renders its alt tile (a distinct second brand color)
instead; the home club always keeps its primary.

The decision reads `LogoAsset.preferred_background` — the color each committed tile
actually renders on — and asks `clash_metric` whether two backgrounds read as one. Two
metrics answer, and either one can call a clash: CIE76 (`delta_e`), and CIEDE2000
(`delta_e_2000`), which weights hue the way the eye does. CIE76 counts a lightness and chroma
gap at full weight, so it calls two saturated colors of nearly the same hue distinct (the
Cardinals' deep red and the Chiefs' bright red) that read as one on a lit panel; CIEDE2000
catches those. The away side can only switch if it carries an alt tile with a known
background; without one (a test stub, a not-yet-modelled league) both stay on their base,
exactly as before logos clashed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team

__all__ = [
    "CLASH_DE2000",
    "CLASH_DELTA_E",
    "ColorMetric",
    "MatchupLogoVariants",
    "clash_metric",
    "logo_background",
    "pick_distinct_variants",
    "resolve_logo_variants",
]

# Below this CIE76 distance two backgrounds blur together on a small panel. It is the
# same floor `tests/test_mlb_palette.py` holds between every club's own base and alt —
# the perceptual question is identical: "do these two tiles read as one color?"
CLASH_DELTA_E = 25.0
# Below this CIEDE2000 distance two backgrounds blur together too, however far apart CIE76
# puts them: the Cardinals' and the Chiefs' reds sit at 17.0, the Bills' and the Colts' blues
# at 7.1. The palette tests hold each club's own base and alt this far apart as well.
CLASH_DE2000 = 20.0

ColorMetric = Callable[[RGBColor, RGBColor], float]
"""A distance between two colors: `RGBColor.delta_e` or `RGBColor.delta_e_2000`."""


def clash_metric(
    a: RGBColor, b: RGBColor, *, delta_e: float = CLASH_DELTA_E, de2000: float = CLASH_DE2000
) -> ColorMetric | None:
    """The metric under which `a` and `b` read as one color, or `None` when they read apart.

    CIE76 is asked first, so a pair it calls a clash is judged by it alone; CIEDE2000 answers
    only for a pair CIE76 calls distinct. Whoever saw the clash also judges whether a switch
    would separate the two, which keeps every CIE76 clash deciding exactly as CIE76 alone would.
    """
    if a.delta_e(b) < delta_e:
        return RGBColor.delta_e
    if a.delta_e_2000(b) < de2000:
        return RGBColor.delta_e_2000
    return None


@dataclass(frozen=True, slots=True)
class MatchupLogoVariants:
    """The logo variant each side of a matchup should render."""

    away: LogoVariant
    home: LogoVariant


def logo_background(team: Team, variant: LogoVariant) -> RGBColor | None:
    """The color `team`'s `variant` tile renders on, or `None` if it has no such tile."""
    asset = team.logo_alt if variant is LogoVariant.ALT else team.logo
    return asset.preferred_background if asset is not None else None


def resolve_logo_variants(
    away: Team, home: Team, *, delta_e: float = CLASH_DELTA_E, de2000: float = CLASH_DE2000
) -> MatchupLogoVariants:
    """Pick each side's logo variant so the two tiles stay perceptually distinct.

    The home club always keeps its primary. On a clash (see `clash_metric`) the away club is the one
    that switches to its alt — provided it has one and the switch actually separates the two marks;
    otherwise both stay on base.
    """
    return pick_distinct_variants(
        logo_background(away, LogoVariant.BASE),
        logo_background(away, LogoVariant.ALT),
        logo_background(home, LogoVariant.BASE),
        delta_e=delta_e,
        de2000=de2000,
    )


def pick_distinct_variants(
    away_base: RGBColor | None,
    away_alt: RGBColor | None,
    home_base: RGBColor | None,
    *,
    delta_e: float,
    de2000: float,
) -> MatchupLogoVariants:
    """The switch decision on bare colors, so a mark drawn from other than the tiles can share it."""
    both_base = MatchupLogoVariants(LogoVariant.BASE, LogoVariant.BASE)
    if away_base is None or home_base is None or away_alt is None:
        return both_base  # nothing to compare, or nothing to switch to (e.g. a test stub) — leave as-is
    metric = clash_metric(away_base, home_base, delta_e=delta_e, de2000=de2000)
    if metric is None or metric(away_alt, home_base) <= metric(away_base, home_base):
        return both_base  # already distinct, or the alt would not help
    return MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)
