"""The matchup logo resolver: keep both bases unless they clash, then flip one alt."""

from __future__ import annotations

from itertools import permutations

import pytest

from omni.core.colors import RGBColor
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.base import LogoAsset
from omni.domain.logos import LogoVariant
from omni.domain.teams import Team
from omni.providers.mlb_teams import _TEAM_ID_INFO, MlbTeamRegistry
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry
from omni.providers.nfl_teams import NFL_TEAMS, NflTeamRegistry
from omni.renderers.logo_clash import (
    CLASH_DE2000,
    CLASH_DELTA_E,
    MatchupLogoVariants,
    clash_metric,
    logo_background,
    resolve_logo_variants,
)
from omni.renderers.visual_treatment import (
    DEFAULT_CONTRAST_POLICY,
    VisualContrastPolicy,
    _bar_palette,
    resolve_bar_colors,
)

REG = MlbTeamRegistry.from_color_file()
NFL = NflTeamRegistry.default()
# CIEDE2000 never falls below 0, so this policy is CIE76 alone: the rule before CIEDE2000 joined it.
CIE76_ONLY = VisualContrastPolicy(clash_de2000=0.0)
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


def test_both_thresholds_are_honoured() -> None:
    # Bases 29.7 apart by CIE76 and 19.5 by CIEDE2000: a clash at the default CIEDE2000 cutoff (20)...
    a = _stub("aaa", RGBColor(0, 0, 0), RGBColor(255, 255, 255))
    b = _stub("bbb", RGBColor(70, 70, 70), RGBColor(255, 0, 0))
    assert resolve_logo_variants(a, b) != BOTH_BASE
    # ...distinct once either cutoff is lowered under it and the other doesn't catch them...
    assert resolve_logo_variants(a, b, de2000=19.0) == BOTH_BASE
    # ...and a clash again under a stricter CIE76 floor alone.
    assert resolve_logo_variants(a, b, delta_e=30.0, de2000=19.0) != BOTH_BASE


def test_clash_floors_match_the_palette_distinctness_floors() -> None:
    # The clash thresholds are the same perceptual floors the palette holds between a club's own
    # base and alt (tests/test_mlb_palette.py::_FLOOR and _FLOOR_DE2000), kept in sync on purpose.
    assert (CLASH_DELTA_E, CLASH_DE2000) == (25.0, 20.0)


def test_two_reds_cie76_calls_distinct_switch_the_visitor_to_its_black_alt() -> None:
    # The Cardinals' and the Chiefs' reds are 37.5 apart by CIE76 but 17.0 by CIEDE2000: one red on
    # a lit panel. Arizona, visiting, shows its black alt; Kansas City keeps its red.
    ari, kc = NFL.resolve("ARI"), NFL.resolve("KC")
    assert logo_background(ari, LogoVariant.ALT) == RGBColor(0, 0, 0)
    assert resolve_logo_variants(ari, kc) == MatchupLogoVariants(LogoVariant.ALT, LogoVariant.BASE)


def test_two_blues_cie76_calls_distinct_switch_the_visitor() -> None:
    # The Bills' royal blue and the Colts' navy are 25.4 apart by CIE76 and 7.1 by CIEDE2000.
    assert resolve_logo_variants(NFL.resolve("BUF"), NFL.resolve("IND")) == MatchupLogoVariants(
        LogoVariant.ALT, LogoVariant.BASE
    )


def test_a_clash_only_ciede2000_sees_keeps_both_bases_when_the_alt_reads_no_farther() -> None:
    # The Rockies' black and Boston's navy are 25.2 apart by CIE76 but 16.7 by CIEDE2000, a clash. The
    # Rockies' purple alt sits 16.2 from the navy by CIEDE2000, no farther, so neither club switches.
    col, bos = REG.resolve(115), REG.resolve(111)
    away_base, home_base = logo_background(col, LogoVariant.BASE), logo_background(bos, LogoVariant.BASE)
    assert away_base is not None and home_base is not None
    assert clash_metric(away_base, home_base) == RGBColor.delta_e_2000
    assert resolve_logo_variants(col, bos) == BOTH_BASE


_LEAGUES: dict[str, list[Team]] = {
    "mlb": [REG.resolve(team_id) for team_id in _TEAM_ID_INFO],
    "nfl": [NFL.resolve(abbreviation) for abbreviation in NFL_TEAMS],
    "nba": [NbaTeamRegistry.default().resolve(abbreviation) for abbreviation in NBA_TEAMS],
}


def _ciede2000_only_clash(a: RGBColor, b: RGBColor) -> bool:
    return a.delta_e(b) >= CLASH_DELTA_E and a.delta_e_2000(b) < CLASH_DE2000


@pytest.mark.parametrize("league", sorted(_LEAGUES))
def test_every_decision_is_cie76s_but_for_the_clashes_only_ciede2000_sees(league: str) -> None:
    # Against CIE76 alone, a matchup decides differently only where the two clubs' tiles (or, for bars,
    # their first colors) clash under CIEDE2000 alone: every clash CIE76 saw still decides as it did.
    gained = 0
    for away, home in permutations(_LEAGUES[league], 2):
        away_base, home_base = logo_background(away, LogoVariant.BASE), logo_background(home, LogoVariant.BASE)
        assert away_base is not None and home_base is not None
        tiles = resolve_logo_variants(away, home)
        if tiles != resolve_logo_variants(away, home, de2000=CIE76_ONLY.clash_de2000):
            assert _ciede2000_only_clash(away_base, home_base), (away.abbreviation, home.abbreviation)
            gained += 1
        bars = resolve_bar_colors(away, home, policy=DEFAULT_CONTRAST_POLICY)
        if bars != resolve_bar_colors(away, home, policy=CIE76_ONLY):
            away_first = _bar_palette(away, DEFAULT_CONTRAST_POLICY)[0]
            home_first = _bar_palette(home, DEFAULT_CONTRAST_POLICY)[0]
            assert _ciede2000_only_clash(away_first, home_first), (away.abbreviation, home.abbreviation)
    assert gained > 0
