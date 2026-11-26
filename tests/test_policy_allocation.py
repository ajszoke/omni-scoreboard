"""The base allocation: game weights, league and game shares, and the favorite floor (`BOARD_POLICY.md`
§3.2-§3.4), checked against §9's allocation table."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omni.core.enum import GameStatus, League, SeasonStage
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import WinLossRecord
from omni.domain.contest import TeamGame
from omni.domain.teams import Team
from omni.policy.allocation import (
    AllocationPolicy,
    AllocationSettings,
    SetAside,
    SetAsideReason,
    Targets,
    interrupt_quality,
    rotation_quality,
    stakes_factor,
    targets,
    weigh,
)
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.pool import MatchupRecords, PoolEntry, Stakes, StartingRecords, TeamRecord
from omni.preferences.favorites import Favorites
from omni.providers.mlb_teams import MlbTeamRegistry

_SOURCE = SourceRef("test")
_T = datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc)
_MLB = MlbTeamRegistry.from_color_file()
ATL, COL, NYM = (_MLB.resolve(team_id) for team_id in (144, 115, 121))
USER = Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",), League.NBA: ("CHA",)})


def _id(league: League, raw: str) -> LeagueScopedId:
    return LeagueScopedId(league, _SOURCE, raw)


def _entry(
    league: League,
    raw: str,
    g: float,
    *,
    rank: int | None = None,
    in_break: bool = False,
    fresh: bool = True,
    muted: bool = False,
    drawable: bool = True,
    postseason: bool = False,
) -> PoolEntry:
    return PoolEntry(
        contest=_id(league, raw),
        leverage=Leverage(g=g, status=ModelStatus.PROXY),
        favorite_rank=rank,
        in_break=in_break,
        fresh=fresh,
        muted=muted,
        drawable=drawable,
        stakes=Stakes(postseason=postseason),
    )


def _others(league: League, count: int, g: float) -> list[PoolEntry]:
    return [_entry(league, f"g{index:02}", g) for index in range(1, count + 1)]


def _share(result: Targets, league: League, raw: str) -> str:
    game = result.game(_id(league, raw))
    assert game is not None
    return f"{game.share:.2%}"


def _league(result: Targets, league: League) -> str:
    found = result.league(league)
    assert found is not None
    return f"{found.share:.2%}"


def _game(home: Team, away: Team) -> TeamGame:
    return TeamGame(
        id=_id(League.MLB, f"{away.abbreviation}@{home.abbreviation}"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=_T,
        away=away,
        home=home,
    )


# --- BOARD_POLICY.md §9, the allocation table -------------------------------------------------------------------


def test_two_same_league_non_favorites_at_g_1_and_0_split_two_and_a_half_to_one() -> None:
    result = targets([_entry(League.MLB, "high", 1.0), _entry(League.MLB, "low", 0.0)])
    assert (_share(result, League.MLB, "high"), _share(result, League.MLB, "low")) == ("71.43%", "28.57%")
    high, low = result.games
    assert high.share / low.share == pytest.approx(2.5)  # no within-league blend
    assert result.floor is None


def test_atl_and_col_both_acting_at_equal_leverage_split_by_rank_with_no_floor_to_project() -> None:
    result = targets([_entry(League.MLB, "atl", 0.4, rank=1), _entry(League.MLB, "col", 0.4, rank=2)])
    assert (_share(result, League.MLB, "atl"), _share(result, League.MLB, "col")) == ("61.54%", "38.46%")
    assert result.floor is not None and not result.floor.projected


def test_col_close_and_late_beats_an_atl_blowout_leverage_over_rank() -> None:
    result = targets([_entry(League.MLB, "col", 1.0, rank=2), _entry(League.MLB, "atl", 0.0, rank=1)])
    assert (_share(result, League.MLB, "col"), _share(result, League.MLB, "atl")) == ("60.98%", "39.02%")


def test_car_and_atl_alone_and_equal_tilt_to_nfl_for_its_short_season() -> None:
    result = targets([_entry(League.NFL, "car", 0.4, rank=1), _entry(League.MLB, "atl", 0.4, rank=1)])
    assert (_share(result, League.NFL, "car"), _share(result, League.MLB, "atl")) == ("60.98%", "39.02%")


def test_monday_night_against_eight_nba_games_gets_nfl_its_share() -> None:
    result = targets([_entry(League.NFL, "mnf", 0.4), *_others(League.NBA, 8, 0.4)])
    assert _league(result, League.NFL) == "22.50%"


def test_one_nfl_game_against_fifteen_mlb_games_gets_nfl_its_share() -> None:
    result = targets([_entry(League.NFL, "g01", 0.4), *_others(League.MLB, 15, 0.4)])
    assert _league(result, League.NFL) == "18.39%"


def test_a_postseason_game_among_four_high_leverage_nba_games_gets_its_stakes() -> None:
    pool = [_entry(League.NBA, "g01", 1.0, postseason=True), *_others(League.NBA, 4, 1.0)[1:]]
    result = targets(pool)
    assert [f"{game.share:.2%}" for game in result.games] == ["27.71%", "24.10%", "24.10%", "24.10%"]


def test_cha_at_high_leverage_with_two_others_is_lifted_to_the_ninety_percent_floor() -> None:
    result = targets([_entry(League.NBA, "cha", 1.0, rank=1), *_others(League.NBA, 2, 1.0)])
    assert [_share(result, League.NBA, raw) for raw in ("cha", "g01", "g02")] == ["90.00%", "5.00%", "5.00%"]
    assert result.floor is not None and result.floor.projected and result.floor.level == pytest.approx(0.90)


def test_a_cha_blowout_with_two_high_leverage_others_is_lifted_to_its_sixty_percent_floor() -> None:
    result = targets([_entry(League.NBA, "cha", 0.0, rank=1), *_others(League.NBA, 2, 1.0)])
    assert [_share(result, League.NBA, raw) for raw in ("cha", "g01", "g02")] == ["60.00%", "20.00%", "20.00%"]
    assert result.floor is not None and result.floor.projected and result.floor.level == pytest.approx(0.60)
    assert f"{result.floor.before:.2%}" == "44.44%"  # before the floor: a weight of 4 against 2.5 and 2.5


def test_a_cha_blowout_with_one_high_leverage_other_stays_above_its_sixty_percent_floor() -> None:
    result = targets([_entry(League.NBA, "cha", 0.0, rank=1), *_others(League.NBA, 1, 1.0)])
    assert [_share(result, League.NBA, raw) for raw in ("cha", "g01")] == ["61.54%", "38.46%"]
    assert result.floor is not None and not result.floor.projected and result.floor.level == pytest.approx(0.60)


@pytest.mark.parametrize("g, share", [(0.0, "60.00%"), (1.0, "90.00%")])
def test_atl_against_fourteen_high_leverage_mlb_games_gets_the_floor_its_leverage_sets(g: float, share: str) -> None:
    result = targets([_entry(League.MLB, "atl", g, rank=1), *_others(League.MLB, 14, 1.0)])
    assert _share(result, League.MLB, "atl") == share
    others = {f"{game.share:.2%}" for game in result.games if game.contest.raw != "atl"}
    assert others == {"2.86%" if g == 0.0 else "0.71%"}


def _september_sunday(*, car_acting: bool = True, atl_acting: bool = True) -> Targets:
    return targets(
        [
            _entry(League.NFL, "car", 0.4, rank=1, in_break=not car_acting),
            *_others(League.NFL, 8, 0.4),
            _entry(League.MLB, "atl", 0.4, rank=1, in_break=not atl_acting),
            *_others(League.MLB, 9, 0.4),
        ]
    )


def test_a_september_sunday_with_both_favorites_acting_shares_the_seventy_two_percent_floor() -> None:
    result = _september_sunday()
    assert (_share(result, League.NFL, "car"), _share(result, League.MLB, "atl")) == ("44.21%", "27.79%")
    assert _league(result, League.NFL) == "60.61%"
    assert result.floor is not None and f"{result.floor.level:.2%}" == "72.00%"


def test_a_september_sunday_with_one_favorite_acting_gives_it_the_whole_floor() -> None:
    assert _share(_september_sunday(atl_acting=False), League.NFL, "car") == "72.00%"
    assert _share(_september_sunday(car_acting=False), League.MLB, "atl") == "72.00%"


# --- weights: rank, matchup quality and stakes -------------------------------------------------------------------


def test_a_favorite_in_action_is_weighed_by_its_rank_and_two_favorites_meeting_count_the_better() -> None:
    entry = PoolEntry.of(_game(COL, ATL), leverage=Leverage(g=0.0, status=ModelStatus.PROXY), favorites=USER)
    assert entry.favorite_rank == 1 and weigh(entry).f == 4.0
    second = PoolEntry.of(_game(COL, NYM), leverage=Leverage(g=0.0, status=ModelStatus.PROXY), favorites=USER)
    assert second.favorite_rank == 2 and weigh(second).f == 2.5
    neither = PoolEntry.of(
        _game(NYM, _MLB.resolve(119)), leverage=Leverage(g=1.0, status=ModelStatus.PROXY), favorites=USER
    )
    weight = weigh(neither)
    assert (weight.f, weight.q, weight.t, weight.b, weight.acting_favorite) == (1.0, 0.5, 1.0, 2.5, False)


def test_a_stale_game_gets_no_share_and_a_stale_favorite_no_boost_or_floor() -> None:
    result = targets([_entry(League.MLB, "atl", 0.0, rank=1, fresh=False), *_others(League.MLB, 14, 1.0)])
    assert result.game(_id(League.MLB, "atl")) is None and len(result.games) == 14
    assert result.set_aside == (SetAside(_id(League.MLB, "atl"), SetAsideReason.STALE),)
    assert result.floor is None and {f"{game.share:.2%}" for game in result.games} == {"7.14%"}  # 1 of 14 each


@pytest.mark.parametrize(
    "away, home, q",
    [
        (TeamRecord(wins=10, losses=0), TeamRecord(wins=0, losses=10), 0.5),  # shrunk 14/18 and 4/18
        (TeamRecord(wins=99, losses=63), TeamRecord(wins=95, losses=67), 0.688235),
        (TeamRecord(wins=10, losses=6, ties=1), TeamRecord(wins=10, losses=6, ties=1), 0.66),  # a tie is half
        (TeamRecord(wins=162, losses=0), TeamRecord(wins=162, losses=0), 1.0),  # clamped
        (TeamRecord(wins=0, losses=162), TeamRecord(wins=0, losses=162), 0.0),
    ],
)
def test_matchup_quality_reads_both_records_shrunk_toward_500(away: TeamRecord, home: TeamRecord, q: float) -> None:
    records = MatchupRecords(away=away, home=home)
    assert rotation_quality(records) == pytest.approx(q, abs=5e-7) == interrupt_quality(records)


def test_unknown_records_are_average_for_rotation_but_nothing_for_an_interrupt() -> None:
    assert (rotation_quality(None), interrupt_quality(None)) == (0.5, 0.0)


@pytest.mark.parametrize(
    "stakes, quality, t",
    [
        (Stakes(), 0.5, 1.0),
        (Stakes(postseason=True), 0.5, 1.15),
        (Stakes(cup_knockout=True), 0.5, 1.075),
        (Stakes(postseason=True, cup_knockout=True), 0.5, 1.15),  # a postseason game isn't a Cup game too
        (Stakes(season_decider=True), 0.5, 1.15),
        (Stakes(national_tv=True), 0.5, 1.03),
        (Stakes(postseason=True, season_decider=True, national_tv=True), 1.0, 1.43),
        (Stakes(), 0.0, 0.90),
    ],
)
def test_stakes_run_from_ninety_to_a_hundred_forty_three_percent(stakes: Stakes, quality: float, t: float) -> None:
    assert stakes_factor(stakes, quality) == pytest.approx(t)


def test_a_contest_s_stakes_come_only_from_what_its_schedule_says() -> None:
    game = _game(ATL, NYM)
    assert Stakes.of(game) == Stakes()
    postseason = TeamGame(
        id=game.id,
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=_T,
        away=NYM,
        home=ATL,
        stage=SeasonStage.POSTSEASON,
        national_broadcast=True,
    )
    assert Stakes.of(postseason, cup_knockout=False) == Stakes(postseason=True, national_tv=True)
    unknown_tv = TeamGame(id=game.id, league=League.MLB, status=GameStatus.LIVE, scheduled_start=_T, away=NYM, home=ATL)
    assert not Stakes.of(unknown_tv).national_tv  # not known isn't national


def test_records_freeze_at_the_game_s_start() -> None:
    kept = StartingRecords()
    contest = _id(League.MLB, "atl")
    before = MatchupRecords(away=TeamRecord(wins=80, losses=70), home=TeamRecord(wins=75, losses=75))
    later = MatchupRecords(away=TeamRecord(wins=81, losses=70), home=TeamRecord(wins=75, losses=76))
    assert kept.read(contest, None, started=True) is None  # nothing to keep yet
    assert kept.read(contest, before, started=False) is before  # before the start, the latest stands
    assert kept.read(contest, before, started=True) is before
    assert kept.read(contest, later, started=True) is before  # a result during the game never moves them
    kept.forget(contest)
    assert kept.read(contest, later, started=False) is later


def test_a_team_record_reads_a_win_loss_record_and_refuses_a_negative_one() -> None:
    assert TeamRecord.of(WinLossRecord(wins=90, losses=72)) == TeamRecord(wins=90, losses=72, ties=0)
    with pytest.raises(ValueError, match="negative"):
        TeamRecord(wins=1, losses=0, ties=-1)


# --- the pool: who gets a share -----------------------------------------------------------------------------------


def test_a_mute_beats_a_favorite_in_the_same_matchup() -> None:
    muted = PoolEntry.of(
        _game(NYM, ATL), leverage=Leverage(g=1.0, status=ModelStatus.PROXY), favorites=USER, muted=True
    )
    assert muted.favorite_rank == 1
    result = targets([muted, _entry(League.MLB, "other", 0.0)])
    assert [game.contest.raw for game in result.games] == ["other"]
    assert result.set_aside == (SetAside(muted.contest, SetAsideReason.MUTED),) and result.floor is None


def test_a_game_without_a_drawable_card_gets_no_share() -> None:
    result = targets([_entry(League.MLB, "blank", 1.0, drawable=False), _entry(League.MLB, "other", 0.0)])
    assert [game.contest.raw for game in result.games] == ["other"]
    assert result.set_aside == (SetAside(_id(League.MLB, "blank"), SetAsideReason.NOT_DRAWABLE),)


def test_a_favorite_s_break_takes_it_out_of_the_pool_and_the_floor_while_others_can_be_shown() -> None:
    result = targets(
        [_entry(League.MLB, "atl", 1.0, rank=1, in_break=True), _entry(League.MLB, "col", 0.0, rank=2)]
        + _others(League.MLB, 14, 1.0)
    )
    assert result.game(_id(League.MLB, "atl")) is None
    assert SetAside(_id(League.MLB, "atl"), SetAsideReason.FAVORITE_IN_BREAK) in result.set_aside
    assert result.floor is not None and result.floor.level == pytest.approx(0.60)  # from COL's G alone
    assert _share(result, League.MLB, "col") == "60.00%"


def test_a_favorite_in_a_break_stays_when_nothing_else_can_be_shown_but_without_its_boost() -> None:
    result = targets([_entry(League.MLB, "atl", 0.4, rank=1, in_break=True)])
    (atl,) = result.games
    assert atl.share == pytest.approx(1.0) and atl.weight.f == 1.0 and result.floor is None
    assert result.set_aside == ()


def test_a_non_favorite_in_a_break_keeps_its_share() -> None:
    result = targets([_entry(League.MLB, "a", 1.0, in_break=True), _entry(League.MLB, "b", 0.0)])
    assert [f"{game.share:.2%}" for game in result.games] == ["71.43%", "28.57%"]


def test_the_pool_holds_each_contest_once() -> None:
    with pytest.raises(ValueError, match="each contest once"):
        targets([_entry(League.MLB, "a", 1.0), _entry(League.MLB, "a", 0.0)])


def test_an_empty_pool_has_no_targets() -> None:
    result = targets([])
    assert (result.leagues, result.games, result.floor, result.set_aside) == ((), (), None, ())
    assert result.league(League.MLB) is None and result.game(_id(League.MLB, "a")) is None


def test_targets_list_leagues_in_declared_order_and_games_by_league_then_id() -> None:
    result = targets([_entry(League.NBA, "b", 0.4), _entry(League.MLB, "z", 0.4), _entry(League.NBA, "a", 0.4)])
    assert [league.league for league in result.leagues] == [League.MLB, League.NBA]
    assert [str(game.contest) for game in result.games] == ["mlb:test:z", "nba:test:a", "nba:test:b"]
    assert [game.contest.raw for game in result.in_league(League.NBA)] == ["a", "b"]
    nba = result.league(League.NBA)
    assert nba is not None and nba.games == 2 and nba.user_weight == 1.0
    assert nba.season_boost == pytest.approx((162 / 82) ** 0.25)


# --- settings and policy --------------------------------------------------------------------------------------


def test_a_user_league_weight_scales_that_league_s_mass() -> None:
    pool = [_entry(League.NFL, "mnf", 0.4), *_others(League.NBA, 8, 0.4)]
    doubled = targets(pool, AllocationSettings({League.NFL: 2.0}))
    assert _league(doubled, League.NFL) == "31.63%"  # 0.8 * 5.6223 / 20.7976 + 0.1
    nfl = doubled.league(League.NFL)
    assert nfl is not None and nfl.user_weight == 2.0 and nfl.blended == pytest.approx(nfl.share)


@pytest.mark.parametrize("weight", [0.49, 2.01])
def test_a_league_weight_outside_half_to_double_is_refused(weight: float) -> None:
    with pytest.raises(ValueError, match="league weight is 0.5..2.0"):
        targets([_entry(League.MLB, "a", 0.4)], AllocationSettings({League.MLB: weight}))


def test_allocation_settings_default_to_even_weights_and_a_12_second_turn() -> None:
    settings = AllocationSettings({League.NBA: 1.5, League.MLB: 0.5})
    assert settings.league_weights == ((League.MLB, 0.5), (League.NBA, 1.5))
    assert (settings.weight(League.NFL), settings.slot) == (1.0, timedelta(seconds=12))
    with pytest.raises(ValueError, match="positive"):
        AllocationSettings(slot=timedelta(0))


def test_a_league_without_a_season_length_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="no season length for NHL"):
        targets([_entry(League.NHL, "a", 0.4)])


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"blend": 1.2}, "within 0..1"),
        ({"floor_slope": 0.8}, "within 0..1"),
        ({"league_weight_low": 1.5}, "default weight"),
        ({"league_weight_high": 0.9}, "default weight"),
        ({"season_games": ((League.MLB, 0),)}, "at least one game"),
        ({"reference_season": 0}, "at least one game"),
        ({"debt_limit": timedelta(0)}, "positive"),
        ({"rescue_turns": 0.0}, "positive"),
    ],
)
def test_an_allocation_policy_refuses_constants_that_cant_work(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AllocationPolicy(**changes)  # type: ignore[arg-type]


def test_a_pool_entry_refuses_a_rank_below_one() -> None:
    with pytest.raises(ValueError, match="counts from 1"):
        _entry(League.MLB, "a", 0.4, rank=0)
