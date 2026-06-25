"""Tests for baseball domain value objects and the live game-state snapshot."""

from __future__ import annotations

import pytest

from omni.core.enum import HomeAway, try_coerce_enum
from omni.domain.baseball import (
    BaseballBaseState,
    BaseballCount,
    BaseballGameState,
    BaseballScoringImpact,
    InningPhase,
    PitchingDecisions,
    PitchingFeatKind,
    BatterGameLine,
    PitcherGameLine,
    PitchingFeatProgress,
    PitchType,
    TeamLinescore,
    WinProbability,
    pitching_feat_progress,
    scoring_impact,
)


def _state(**overrides: object) -> BaseballGameState:
    base: dict[str, object] = dict(
        away_score=3,
        home_score=5,
        inning=7,
        phase=InningPhase.TOP,
        count=BaseballCount(balls=2, strikes=1, outs=2),
        bases=BaseballBaseState(first=True, third=True),
    )
    base.update(overrides)
    return BaseballGameState(**base)  # type: ignore[arg-type]


def test_game_state_holds_the_snapshot() -> None:
    state = _state()
    assert state.away_score == 3 and state.home_score == 5
    assert state.inning == 7 and state.phase is InningPhase.TOP
    assert state.count.outs == 2
    assert state.bases.first and state.bases.third and not state.bases.second


def test_game_state_rejects_negative_scores() -> None:
    with pytest.raises(ValueError):
        _state(away_score=-1)
    with pytest.raises(ValueError):
        _state(home_score=-2)


def test_game_state_requires_inning_at_least_one() -> None:
    with pytest.raises(ValueError):
        _state(inning=0)


def test_game_state_is_frozen() -> None:
    state = _state()
    with pytest.raises(AttributeError):
        state.inning = 8  # type: ignore[misc]


def test_inning_phase_breaks() -> None:
    assert not InningPhase.TOP.is_break and not InningPhase.BOTTOM.is_break  # active halves
    assert InningPhase.MIDDLE.is_break and InningPhase.END.is_break  # between-halves breaks


def test_game_state_holds_hits_and_rejects_negative() -> None:
    assert _state(away_hits=4, home_hits=0).away_hits == 4
    with pytest.raises(ValueError):
        _state(away_hits=-1)
    with pytest.raises(ValueError):
        _state(home_hits=-1)


def test_game_state_holds_errors_and_rejects_negative() -> None:
    assert _state(away_errors=2, home_errors=0).away_errors == 2
    with pytest.raises(ValueError):
        _state(away_errors=-1)
    with pytest.raises(ValueError):
        _state(home_errors=-1)


def test_team_linescore_holds_the_rhe_triple() -> None:
    line = TeamLinescore(runs=4, hits=9, errors=1)
    assert (line.runs, line.hits, line.errors) == (4, 9, 1)


@pytest.mark.parametrize("field", ["runs", "hits", "errors"])
def test_team_linescore_rejects_negative_values(field: str) -> None:
    values = {"runs": 0, "hits": 0, "errors": 0, field: -1}
    with pytest.raises(ValueError, match=f"{field} cannot be negative"):
        TeamLinescore(**values)


def test_batter_game_line_holds_the_at_bat() -> None:
    line = BatterGameLine(name="Betts", at_bats=4, hits=2, rbi=1, home_runs=1)
    assert (line.name, line.hits, line.at_bats, line.rbi, line.home_runs) == ("Betts", 2, 4, 1, 1)


def test_batter_game_line_validates() -> None:
    with pytest.raises(ValueError, match="needs a name"):
        BatterGameLine(name="", at_bats=0, hits=0)
    with pytest.raises(ValueError, match="hits cannot be negative"):
        BatterGameLine(name="x", at_bats=0, hits=-1)


def test_pitcher_game_line_holds_the_outing() -> None:
    line = PitcherGameLine(name="Kershaw", innings_pitched="6.1", pitches=95, strikeouts=7)
    assert (line.name, line.innings_pitched, line.pitches, line.strikeouts) == ("Kershaw", "6.1", 95, 7)


def test_pitcher_game_line_validates() -> None:
    with pytest.raises(ValueError, match="needs a name"):
        PitcherGameLine(name="", innings_pitched="0.0", pitches=0, strikeouts=0)
    with pytest.raises(ValueError, match="innings-pitched"):
        PitcherGameLine(name="x", innings_pitched="", pitches=0, strikeouts=0)
    with pytest.raises(ValueError, match="pitches cannot be negative"):
        PitcherGameLine(name="x", innings_pitched="0.0", pitches=-1, strikeouts=0)


def test_pitching_feat_names_the_pitching_side() -> None:
    # The hitless batting side names who is throwing it: away hitless -> home, and vice versa.
    home = pitching_feat_progress(
        _state(inning=7, phase=InningPhase.TOP, away_hits=0, home_hits=3), min_completed_innings=6
    )
    assert home is not None and home.side is HomeAway.HOME
    # The away side pitches the bottom, so it finishes its 6th only at the End break.
    away = pitching_feat_progress(
        _state(inning=7, phase=InningPhase.END, away_hits=3, home_hits=0), min_completed_innings=6
    )
    assert away is not None and away.side is HomeAway.AWAY


@pytest.mark.parametrize(
    "phase, expected",
    [
        (InningPhase.TOP, 6),  # top of the 7th in progress -> only six finished (the off-by-one)
        (InningPhase.MIDDLE, 7),  # the top is done
        (InningPhase.BOTTOM, 7),
        (InningPhase.END, 7),
    ],
)
def test_home_feat_counts_finished_tops(phase: InningPhase, expected: int) -> None:
    feat = pitching_feat_progress(_state(inning=7, phase=phase, away_hits=0, home_hits=3), min_completed_innings=1)
    assert feat is not None and feat.side is HomeAway.HOME and feat.completed_innings == expected


@pytest.mark.parametrize(
    "phase, expected",
    [
        (InningPhase.TOP, 6),
        (InningPhase.MIDDLE, 6),
        (InningPhase.BOTTOM, 6),  # bottom of the 7th in progress -> still six finished
        (InningPhase.END, 7),  # the bottom is done
    ],
)
def test_away_feat_counts_finished_bottoms(phase: InningPhase, expected: int) -> None:
    feat = pitching_feat_progress(_state(inning=7, phase=phase, away_hits=3, home_hits=0), min_completed_innings=1)
    assert feat is not None and feat.side is HomeAway.AWAY and feat.completed_innings == expected


def test_pitching_feat_not_news_until_min_completed_innings() -> None:
    # The top of the 6th is only five finished innings — the bug that surfaced it a half-inning early.
    assert pitching_feat_progress(_state(inning=6, phase=InningPhase.TOP, away_hits=0), min_completed_innings=6) is None
    # The same top *done* (Middle) is the sixth finished inning — now it is news.
    feat = pitching_feat_progress(_state(inning=6, phase=InningPhase.MIDDLE, away_hits=0), min_completed_innings=6)
    assert feat is not None and feat.completed_innings == 6


def test_pitching_feat_none_when_both_sides_have_hits() -> None:
    assert pitching_feat_progress(_state(inning=8, away_hits=2, home_hits=1), min_completed_innings=6) is None


def test_double_no_hitter_reports_the_home_side_as_the_deeper_bid() -> None:
    feat = pitching_feat_progress(
        _state(inning=7, phase=InningPhase.MIDDLE, away_hits=0, home_hits=0), min_completed_innings=6
    )
    assert feat is not None and feat.side is HomeAway.HOME


def test_perfect_game_needs_a_confirmed_clean_sheet() -> None:
    common: dict[str, object] = dict(inning=7, phase=InningPhase.TOP, away_hits=0, home_hits=3)
    # The batting side is confirmed not to have reached -> perfect game.
    perfect = pitching_feat_progress(_state(**common, away_reached_base=False), min_completed_innings=6)
    assert perfect is not None and perfect.kind is PitchingFeatKind.PERFECT_GAME and perfect.perfect is True
    # A confirmed baserunner -> plain no-hitter.
    blemished = pitching_feat_progress(_state(**common, away_reached_base=True), min_completed_innings=6)
    assert blemished is not None and blemished.kind is PitchingFeatKind.NO_HITTER and blemished.perfect is False
    # Unknown (None) -> stay a plain no-hitter; never claim perfection without evidence.
    unknown = pitching_feat_progress(_state(**common, away_reached_base=None), min_completed_innings=6)
    assert unknown is not None and unknown.kind is PitchingFeatKind.NO_HITTER


def test_perfect_game_reads_the_batting_side_for_an_away_pitcher() -> None:
    # Home is hitless (away pitching); the relevant clean sheet is the *home* batting side's.
    feat = pitching_feat_progress(
        _state(inning=7, phase=InningPhase.END, away_hits=3, home_hits=0, home_reached_base=False),
        min_completed_innings=6,
    )
    assert feat is not None and feat.side is HomeAway.AWAY and feat.perfect is True


def test_pitching_feat_progress_rejects_zero_completed_innings() -> None:
    with pytest.raises(ValueError):
        PitchingFeatProgress(side=HomeAway.HOME, kind=PitchingFeatKind.NO_HITTER, completed_innings=0)


def test_pitching_feat_kind_value_is_the_token() -> None:
    assert PitchingFeatKind.PERFECT_GAME.value == "perfect_game"
    assert PitchingFeatKind.NO_HITTER.value == "no_hitter"


def test_pitch_type_value_is_the_statsapi_code() -> None:
    # The enum value is the StatsAPI code, so it doubles as the short display token.
    assert PitchType.SWEEPER.value == "ST"
    assert PitchType.FOUR_SEAM_FASTBALL.value == "FF"
    assert str(PitchType.SLIDER) == "SL"  # StrEnumMixin renders as the code


def test_pitch_type_labels_are_complete_and_human() -> None:
    # Every member has a long label (no member falls through the mapping).
    assert {p.label for p in PitchType} and all(p.label for p in PitchType)
    assert PitchType.SWEEPER.label == "Sweeper"
    assert PitchType.FOUR_SEAM_FASTBALL.label == "Four-Seam Fastball"


def test_pitch_type_coerces_from_a_raw_code() -> None:
    assert try_coerce_enum(PitchType, "ST") is PitchType.SWEEPER  # the sweeper is mapped
    assert try_coerce_enum(PitchType, "CH") is PitchType.CHANGEUP
    assert try_coerce_enum(PitchType, "ZZ") is None  # an unrecognized code
    assert try_coerce_enum(PitchType, None) is None  # absent


def test_pitching_decisions_carry_the_winner_loser_and_optional_save() -> None:
    saved = PitchingDecisions(winner="Tarik Skubal", loser="Kevin Gausman", save="Will Vest")
    assert (saved.winner, saved.loser, saved.save) == ("Tarik Skubal", "Kevin Gausman", "Will Vest")
    unsaved = PitchingDecisions(winner="Tarik Skubal", loser="Kevin Gausman")
    assert unsaved.save is None  # most games have no save


def test_pitching_decisions_require_a_winner_and_a_loser() -> None:
    with pytest.raises(ValueError):
        PitchingDecisions(winner="", loser="Kevin Gausman")
    with pytest.raises(ValueError):
        PitchingDecisions(winner="Tarik Skubal", loser="")


def test_win_probability_reports_the_favoured_side_and_each_percentage() -> None:
    wp = WinProbability(home=26.0, away=74.0)
    assert wp.favored is HomeAway.AWAY
    assert wp.percent_for(HomeAway.HOME) == 26.0
    assert wp.percent_for(HomeAway.AWAY) == 74.0
    assert WinProbability(home=63.0, away=37.0).favored is HomeAway.HOME  # the other way too


def test_win_probability_is_none_favoured_at_an_exact_tie() -> None:
    assert WinProbability(home=50.0, away=50.0).favored is None


def test_win_probability_rejects_out_of_range_percentages() -> None:
    with pytest.raises(ValueError):
        WinProbability(home=120.0, away=0.0)
    with pytest.raises(ValueError):
        WinProbability(home=-1.0, away=101.0)


# --- scoring impact -------------------------------------------------------------


def test_scoring_impact_empty_without_a_run() -> None:
    impact = scoring_impact(phase=InningPhase.TOP, inning=3, rbi=0, away_score=2, home_score=2)
    assert not impact.scored and impact.rbi == 0
    assert not (impact.tying or impact.go_ahead or impact.walk_off)


def test_scoring_impact_records_rbi_even_without_a_known_score() -> None:
    # A run scored (rbi>0) but the resulting score is absent: count the run, classify nothing.
    impact = scoring_impact(phase=InningPhase.TOP, inning=3, rbi=2, away_score=None, home_score=None)
    assert impact.scored and impact.rbi == 2
    assert not (impact.tying or impact.go_ahead or impact.walk_off)


def test_scoring_impact_detects_a_tying_run() -> None:
    impact = scoring_impact(phase=InningPhase.TOP, inning=8, rbi=1, away_score=3, home_score=3)
    assert impact.tying and not impact.go_ahead and not impact.walk_off


def test_scoring_impact_detects_a_go_ahead_run_for_either_side() -> None:
    away = scoring_impact(phase=InningPhase.TOP, inning=7, rbi=2, away_score=5, home_score=4)
    assert away.go_ahead and not away.walk_off  # away leads by 1 (<= 2 rbi) -> go-ahead
    home = scoring_impact(phase=InningPhase.BOTTOM, inning=7, rbi=1, away_score=4, home_score=5)
    assert home.go_ahead and not home.walk_off  # not the 9th yet -> not a walk-off


def test_scoring_impact_insurance_run_is_not_go_ahead() -> None:
    # Home already led 5-1; a 1-RBI single makes it 6-1 — scored, but not tying/go-ahead.
    impact = scoring_impact(phase=InningPhase.BOTTOM, inning=6, rbi=1, away_score=1, home_score=6)
    assert impact.scored and not impact.go_ahead and not impact.tying


def test_scoring_impact_detects_a_walk_off() -> None:
    ninth = scoring_impact(phase=InningPhase.BOTTOM, inning=9, rbi=1, away_score=3, home_score=4)
    assert ninth.walk_off and ninth.go_ahead
    extras = scoring_impact(phase=InningPhase.BOTTOM, inning=11, rbi=1, away_score=2, home_score=3)
    assert extras.walk_off


def test_scoring_impact_rejects_contradictions() -> None:
    assert BaseballScoringImpact(rbi=1, tying=True).scored  # a valid tying run
    with pytest.raises(ValueError):
        BaseballScoringImpact(rbi=-1)
    with pytest.raises(ValueError):
        BaseballScoringImpact(rbi=1, walk_off=True)  # a walk-off must be a go-ahead
    with pytest.raises(ValueError):
        BaseballScoringImpact(rbi=0, go_ahead=True)  # go-ahead needs a run
    with pytest.raises(ValueError):
        BaseballScoringImpact(rbi=0, tying=True)  # tying needs a run
    with pytest.raises(ValueError):
        BaseballScoringImpact(rbi=1, tying=True, go_ahead=True)  # cannot both tie and lead
