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


def test_clashing_bases_switch_the_away_side_and_leave_home_on_its_primary() -> None:
    # NYY and DET share an identical cap navy: the visiting Yankees switch, the Tigers stay in navy.
    assert resolve_logo_variants(REG.resolve(147), REG.resolve(116)) == MatchupLogoVariants(
        LogoVariant.ALT, LogoVariant.BASE
    )
    # Reversed, it is the visiting Tigers who switch — the home side never does.
    assert resolve_logo_variants(REG.resolve(116), REG.resolve(147)) == MatchupLogoVariants(
        LogoVariant.ALT, LogoVariant.BASE
    )


def test_identical_black_backgrounds_switch_the_away_side() -> None:
    # COL and PIT both render on pure black — indistinguishable until the visitor shows its alt.
    assert resolve_logo_variants(REG.resolve(115), REG.resolve(134)) == MatchupLogoVariants(
        LogoVariant.ALT, LogoVariant.BASE
    )


def test_missing_backgrounds_leave_both_on_base() -> None:
    # A team with no committed background (a stub, a not-yet-modelled league) can't be
    # compared, so the resolver makes no change rather than guessing.
    a = _stub("aaa", None, RGBColor(255, 255, 255))
    b = _stub("bbb", RGBColor(0, 0, 0), RGBColor(255, 255, 255))
    assert resolve_logo_variants(a, b) == BOTH_BASE


def test_an_away_side_with_no_alt_leaves_both_on_base_rather_than_switch_home() -> None:
    a = _stub("aaa", RGBColor(0, 0, 0), None, has_alt=False)
    b = _stub("bbb", RGBColor(0, 0, 0), RGBColor(255, 255, 255))
    assert resolve_logo_variants(a, b) == BOTH_BASE


def test_no_switch_when_the_away_alt_would_not_separate_the_marks() -> None:
    # Bases clash (delta_e ~2.4) but the away alt sits on its own base color, so switching would not
    # help; the home alt is far away, but home keeps its primary regardless.
    a = _stub("aaa", RGBColor(20, 20, 20), RGBColor(20, 20, 20))
    b = _stub("bbb", RGBColor(25, 25, 25), RGBColor(255, 0, 0))
    assert resolve_logo_variants(a, b) == BOTH_BASE


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
