"""The matchup logo resolver: keep both bases unless they clash, then flip one alt."""

from __future__ import annotations

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.renderers.logo_clash import CLASH_DELTA_E, MatchupLogoVariants, resolve_logo_variants

REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("test")
BOTH_BASE = MatchupLogoVariants(LogoVariant.BASE, LogoVariant.BASE)


def _stub(abbr: str, base_bg: RGBColor | None, alt_bg: RGBColor | None, *, has_alt: bool = True) -> Team:
    """A team carrying only the backgrounds the resolver reads (everything else is filler)."""
    alt = LogoAsset(key=f"{abbr}-alt", path=f"x/{abbr}-alt.png", preferred_background=alt_bg) if has_alt else None
    return Team(
        id=LeagueScopedId(League.MLB, SOURCE, abbr),
        league=League.MLB,
        display_name=abbr,
        short_name=abbr,
        abbreviation=abbr,
        primary_color=base_bg or RGBColor(0, 0, 0),
        secondary_color=RGBColor(0, 0, 0),
        logo=LogoAsset(key=abbr, path=f"x/{abbr}.png", preferred_background=base_bg),
        logo_alt=alt,
    )


def test_distinct_bases_both_stay_on_base() -> None:
    # Rockies black vs Dodgers blue read as different colours — nobody flips.
    assert resolve_logo_variants(REG.resolve(115), REG.resolve(119)) == BOTH_BASE


def test_clashing_bases_flip_exactly_the_home_side() -> None:
    # NYY and DET share an identical cap navy; the home side flips to its alt (orange),
    # deterministically (it separates the two marks more than the away alt would).
    assert resolve_logo_variants(REG.resolve(147), REG.resolve(116)) == MatchupLogoVariants(
        LogoVariant.BASE, LogoVariant.ALT
    )


def test_identical_black_backgrounds_flip_one_side() -> None:
    # COL and PIT both render on pure black — indistinguishable until one shows its alt.
    variants = resolve_logo_variants(REG.resolve(115), REG.resolve(134))
    assert (variants.away is LogoVariant.ALT) ^ (variants.home is LogoVariant.ALT)  # exactly one flipped


def test_missing_backgrounds_leave_both_on_base() -> None:
    # A team with no committed background (a stub, a not-yet-modelled league) can't be
    # compared, so the resolver makes no change rather than guessing.
    a = _stub("aaa", None, RGBColor(255, 255, 255))
    b = _stub("bbb", RGBColor(0, 0, 0), RGBColor(255, 255, 255))
    assert resolve_logo_variants(a, b) == BOTH_BASE


def test_a_tie_breaks_to_the_home_side() -> None:
    # Identical bases, identical alts: both flips separate the marks equally, so the
    # tie-break (favour the home side) decides — and it must be deterministic.
    black, white = RGBColor(0, 0, 0), RGBColor(255, 255, 255)
    a = _stub("aaa", black, white)
    b = _stub("bbb", black, white)
    assert resolve_logo_variants(a, b) == MatchupLogoVariants(LogoVariant.BASE, LogoVariant.ALT)


def test_no_flip_when_neither_alt_improves_separation() -> None:
    # Bases clash (delta_e ~2.4) but each alt sits on its own base colour, so flipping
    # would not separate the marks — the resolver leaves both on base rather than swap.
    a = _stub("aaa", RGBColor(20, 20, 20), RGBColor(20, 20, 20))
    b = _stub("bbb", RGBColor(25, 25, 25), RGBColor(25, 25, 25))
    assert resolve_logo_variants(a, b) == BOTH_BASE


def test_flips_the_side_whose_alt_separates_more() -> None:
    # Bases clash; only the away alt is far from the opponent base, so the away side flips.
    a = _stub("aaa", RGBColor(0, 0, 0), RGBColor(255, 0, 0))  # alt red: far from home base black
    b = _stub("bbb", RGBColor(0, 0, 0), RGBColor(8, 8, 8))  # alt near-black: no help if home flips
    assert resolve_logo_variants(a, b) == MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)


def test_threshold_is_honoured() -> None:
    # Bases 25.3 apart: distinct at the default floor (25), a clash at a stricter 30.
    a = _stub("aaa", RGBColor(0, 0, 0), RGBColor(255, 255, 255))
    b = _stub("bbb", RGBColor(60, 60, 60), RGBColor(255, 0, 0))
    assert resolve_logo_variants(a, b) == BOTH_BASE  # default CLASH_DELTA_E = 25
    assert resolve_logo_variants(a, b, threshold=30.0) != BOTH_BASE  # stricter floor -> a flip


def test_clash_floor_matches_the_palette_distinctness_floor() -> None:
    # The clash threshold is the same perceptual floor the palette holds between a
    # club's own base and alt (tests/test_mlb_palette.py::_FLOOR) — kept in sync on purpose.
    assert CLASH_DELTA_E == 25.0
