"""Resolve which logo variant each side of a matchup shows.

Two clubs whose tiles render on perceptually close backgrounds blur into one another
on a small LED panel — and MLB's palette is navy/black/red-heavy, so this is common
(BOS/DET/NYY share an identical cap navy; COL/CWS/MIA/PIT/SF an identical black). When
two bases clash, one side renders its alt tile (a distinct second brand colour)
instead, chosen so the two marks separate as much as possible.

The decision reads `LogoAsset.preferred_background` — the colour each committed tile
actually renders on — and compares backgrounds with CIE76 `delta_e`. A side can only
flip if it carries an alt tile with a known background; without one (a test stub, a
not-yet-modelled league) both stay on their base, exactly as before logos clashed.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni.core.colors import RGBColor
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team

__all__ = ["CLASH_DELTA_E", "MatchupLogoVariants", "resolve_logo_variants"]

# Below this CIE76 distance two backgrounds blur together on a small panel. It is the
# same floor `tests/test_mlb_palette.py` holds between every club's own base and alt —
# the perceptual question is identical: "do these two tiles read as one colour?"
CLASH_DELTA_E = 25.0


@dataclass(frozen=True, slots=True)
class MatchupLogoVariants:
    """The logo variant each side of a matchup should render."""

    away: LogoVariant
    home: LogoVariant


def _background(team: Team, variant: LogoVariant) -> RGBColor | None:
    """The colour `team`'s `variant` tile renders on, or `None` if it has no such tile."""
    asset = team.logo_alt if variant is LogoVariant.ALT else team.logo
    return asset.preferred_background if asset is not None else None


def resolve_logo_variants(away: Team, home: Team, *, threshold: float = CLASH_DELTA_E) -> MatchupLogoVariants:
    """Pick each side's logo variant so the two tiles stay perceptually distinct.

    Both sides keep their base tile unless the two base backgrounds clash (`delta_e`
    below `threshold`). On a clash exactly one side flips to its alt — the flip that
    maximises the separation between the two displayed tiles — provided that side has an
    alt background to flip to and the flip actually beats leaving both on base. Ties (and
    the case where neither alt helps) resolve deterministically, favouring the home side.
    """
    both_base = MatchupLogoVariants(LogoVariant.BASE, LogoVariant.BASE)
    away_base = _background(away, LogoVariant.BASE)
    home_base = _background(home, LogoVariant.BASE)
    if away_base is None or home_base is None:
        return both_base  # no committed backgrounds to compare (e.g. a test stub) — leave as-is
    if away_base.delta_e(home_base) >= threshold:
        return both_base  # the two clubs already read as distinct

    base_sep = away_base.delta_e(home_base)
    away_alt = _background(away, LogoVariant.ALT)
    home_alt = _background(home, LogoVariant.ALT)
    # Separation each single flip would yield against the opponent's (unchanged) base.
    flip_home_sep = away_base.delta_e(home_alt) if home_alt is not None else -1.0
    flip_away_sep = away_alt.delta_e(home_base) if away_alt is not None else -1.0

    if max(flip_home_sep, flip_away_sep) <= base_sep:
        return both_base  # neither alt is an improvement — a swap would not help, so don't
    if flip_home_sep >= flip_away_sep:
        return MatchupLogoVariants(LogoVariant.BASE, LogoVariant.ALT)
    return MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)
