"""Base turns by debt: selection, clamps, rescue, breaks and endings (`BOARD_POLICY.md` §3.5), and the
reference simulations of the policy's arithmetic run through the airtime ledger."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

import pytest

from omni.core.clock import MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.policy.allocation import AllocationSettings, Targets, targets
from omni.policy.ledger import AirtimeLedger, BaseChoice, ChoiceReason, DebtClamp
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.pool import PoolEntry, Stakes

_SOURCE = SourceRef("test")
_TURN = timedelta(seconds=12)


def _id(league: League, raw: str) -> LeagueScopedId:
    return LeagueScopedId(league, _SOURCE, raw)


def _entry(league: League, raw: str, g: float, *, rank: int | None = None, postseason: bool = False) -> PoolEntry:
    return PoolEntry(
        contest=_id(league, raw),
        leverage=Leverage(g=g, status=ModelStatus.PROXY),
        favorite_rank=rank,
        stakes=Stakes(postseason=postseason),
    )


def _numbered(league: League, first: int, count: int, g: float) -> list[PoolEntry]:
    return [_entry(league, f"g{index:02}", g) for index in range(first, first + count)]


def _choose(ledger: AirtimeLedger, result: Targets, at: float = 0.0) -> BaseChoice:
    choice = ledger.choose(result, at=MonotonicTime(at))
    assert choice is not None
    return choice


def _serve(ledger: AirtimeLedger, result: Targets, contest: LeagueScopedId, turns: int = 1) -> list[DebtClamp]:
    clamps: list[DebtClamp] = []
    for _ in range(turns):
        clamps.extend(ledger.served(result, contest, _TURN, completed=True))
    return clamps


# --- the reference simulations ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    targets: Targets
    grants: Counter[str]
    gaps: dict[str, float]  # the longest time between two of a game's turn starts
    reasons: frozenset[ChoiceReason]
    clamps: tuple[DebtClamp, ...]

    def share(self, raw: str) -> str:
        return next(f"{game.share:.4%}" for game in self.targets.games if game.contest.raw == raw)

    def league(self, league: League) -> str:
        return next(f"{each.share:.4%}" for each in self.targets.leagues if each.league is league)

    def league_grants(self, league: League) -> int:
        return sum(count for raw, count in self.grants.items() if raw.startswith(league.value))


def _simulate(pool: list[PoolEntry], grants: int) -> _Run:
    """Grants of 12 confirmed seconds from zero debt, each to the ledger's choice."""
    result = targets(pool)
    ledger = AirtimeLedger()
    starts: defaultdict[str, list[float]] = defaultdict(list)
    reasons: set[ChoiceReason] = set()
    clamps: list[DebtClamp] = []
    for turn in range(grants):
        choice = _choose(ledger, result, at=turn * 12.0)
        reasons.add(choice.reason)
        key = f"{choice.league.value}:{choice.contest.raw}"
        starts[key].append(turn * 12.0)
        clamps.extend(_serve(ledger, result, choice.contest))
    gaps = {key: max((b - a for a, b in zip(times, times[1:])), default=0.0) for key, times in starts.items()}
    return _Run(
        targets=result,
        grants=Counter({key: len(times) for key, times in starts.items()}),
        gaps=gaps,
        reasons=frozenset(reasons),
        clamps=tuple(clamps),
    )


def _healthy(run: _Run) -> None:
    """The reference runs had no clamp and no rescue."""
    assert run.clamps == () and run.reasons == {ChoiceReason.WEIGHTED}


def test_two_same_league_non_favorites_at_g_1_and_0_take_250_and_100_of_350_turns() -> None:
    run = _simulate([_entry(League.MLB, "g1", 1.0), _entry(League.MLB, "g2", 0.0)], 350)
    assert (run.share("g1"), run.share("g2")) == ("71.4286%", "28.5714%")
    assert (run.grants["mlb:g1"], run.grants["mlb:g2"]) == (250, 100)
    assert (run.gaps["mlb:g1"], run.gaps["mlb:g2"]) == (24.0, 48.0)
    _healthy(run)


def test_atl_and_col_acting_at_equal_leverage_take_80_and_50_of_130_turns() -> None:
    run = _simulate([_entry(League.MLB, "g1", 0.4, rank=1), _entry(League.MLB, "g2", 0.4, rank=2)], 130)
    assert (run.share("g1"), run.share("g2")) == ("61.5385%", "38.4615%")
    assert (run.grants["mlb:g1"], run.grants["mlb:g2"]) == (80, 50)
    assert (run.gaps["mlb:g1"], run.gaps["mlb:g2"]) == (24.0, 36.0)
    _healthy(run)


def test_col_close_late_against_an_atl_blowout_takes_50_of_82_turns() -> None:
    run = _simulate([_entry(League.MLB, "g1", 1.0, rank=2), _entry(League.MLB, "g2", 0.0, rank=1)], 82)
    assert (run.share("g1"), run.share("g2")) == ("60.9756%", "39.0244%")
    assert (run.grants["mlb:g1"], run.grants["mlb:g2"]) == (50, 32)
    _healthy(run)


def test_car_and_atl_alone_and_equal_take_61_and_39_of_100_turns() -> None:
    run = _simulate([_entry(League.NFL, "g1", 0.4, rank=1), _entry(League.MLB, "g2", 0.4, rank=1)], 100)
    assert (run.share("g1"), run.share("g2")) == ("60.9827%", "39.0173%")
    assert (run.grants["nfl:g1"], run.grants["mlb:g2"]) == (61, 39)
    _healthy(run)


def test_monday_night_against_eight_nba_games_takes_23_of_100_turns() -> None:
    run = _simulate([_entry(League.NFL, "g01", 0.4), *_numbered(League.NBA, 2, 8, 0.4)], 100)
    assert run.league(League.NFL) == "22.5035%"
    assert (run.league_grants(League.NFL), run.league_grants(League.NBA)) == (23, 77)
    assert run.gaps["nfl:g01"] == 60.0
    _healthy(run)


def test_one_nfl_game_against_fifteen_mlb_games_takes_18_of_100_turns() -> None:
    run = _simulate([_entry(League.NFL, "g01", 0.4), *_numbered(League.MLB, 2, 15, 0.4)], 100)
    assert run.league(League.NFL) == "18.3880%"
    assert (run.league_grants(League.NFL), run.league_grants(League.MLB)) == (18, 82)
    assert run.gaps["nfl:g01"] == 72.0
    _healthy(run)


def test_four_high_leverage_nba_games_one_postseason_take_22_20_19_19_of_80_turns() -> None:
    run = _simulate([_entry(League.NBA, "g1", 1.0, postseason=True), *_numbered(League.NBA, 2, 3, 1.0)], 80)
    assert [run.share(raw) for raw in ("g1", "g02", "g03", "g04")] == ["27.7108%"] + ["24.0964%"] * 3
    assert [run.grants[f"nba:{raw}"] for raw in ("g1", "g02", "g03", "g04")] == [22, 20, 19, 19]
    _healthy(run)


def test_an_atl_blowout_against_fourteen_high_leverage_games_takes_its_floor_of_100_turns() -> None:
    run = _simulate([_entry(League.MLB, "g01", 0.0, rank=1), *_numbered(League.MLB, 2, 14, 1.0)], 100)
    assert run.share("g01") == "30.0000%" and {run.share(f"g{n:02}") for n in range(2, 16)} == {"5.0000%"}
    assert run.grants["mlb:g01"] == 30 and {run.grants[f"mlb:g{n:02}"] for n in range(2, 16)} == {5}
    _healthy(run)


# --- choosing -------------------------------------------------------------------------------------------------


def test_an_empty_pool_has_no_base_turn() -> None:
    assert AirtimeLedger().choose(targets([]), at=MonotonicTime(0.0)) is None


def test_a_lone_game_is_the_only_candidate_and_winning_again_keeps_its_frame() -> None:
    result = targets([_entry(League.MLB, "a", 0.4)])
    ledger = AirtimeLedger()
    first = _choose(ledger, result)
    assert (first.reason, first.continues) == (ChoiceReason.ONLY_CANDIDATE, False)
    _serve(ledger, result, first.contest)
    again = _choose(ledger, result, at=12.0)
    assert (again.contest, again.continues, again.at) == (first.contest, True, MonotonicTime(12.0))


def test_a_choice_carries_its_factors_targets_and_debts_for_the_trace() -> None:
    result = targets([_entry(League.MLB, "atl", 0.4, rank=1), _entry(League.MLB, "col", 0.4, rank=2)])
    ledger = AirtimeLedger()
    before = _choose(ledger, result)
    assert before.game_account.actual is None and before.league_account.actual is None
    _serve(ledger, result, _id(League.MLB, "atl"))
    choice = _choose(ledger, result, at=12.0)
    assert choice.contest.raw == "col" and choice.reason is ChoiceReason.WEIGHTED and not choice.continues
    weight = choice.game_target.weight
    assert (weight.g, weight.f, weight.t, weight.q, weight.favorite_rank) == (0.4, 2.5, 1.0, 0.5, 2)
    assert f"{choice.game_target.share:.2%}" == "38.46%" and choice.league_target.share == pytest.approx(1.0)
    assert choice.game_account.debt == pytest.approx(12 * 4.0 / 10.4)  # owed its share of the one turn
    assert choice.game_account.actual == 0.0 and choice.league_account.actual == 1.0
    assert choice.rescue is None and choice.clamps == ()


def test_scores_compare_to_the_microsecond_and_a_tie_goes_to_the_league_declared_first() -> None:
    # Cancel NFL's short-season boost, and then some: NFL's score is ahead by a few billionths of a second.
    nudged = AllocationSettings({League.NFL: (17 / 162) ** 0.25 * (1 + 1e-9)})
    result = targets([_entry(League.NFL, "car", 0.4), _entry(League.MLB, "atl", 0.4)], nudged)
    nfl, mlb = (result.league(league) for league in (League.NFL, League.MLB))
    assert nfl is not None and mlb is not None and 0 < 12 * (nfl.share - mlb.share) < 1e-7
    ledger = AirtimeLedger()
    assert _choose(ledger, result).league is League.MLB


def test_a_shortened_turn_credits_only_its_own_time_and_costs_no_one_a_miss() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    ledger.served(result, _id(League.MLB, "a"), timedelta(seconds=5), completed=False)
    a, b = (ledger.snapshot().game(_id(League.MLB, raw)) for raw in ("a", "b"))
    assert a is not None and b is not None
    assert (a.debt, a.airtime, a.misses) == (-2.5, 5.0, 0)
    assert (b.debt, b.wait, b.misses) == (2.5, 5.0, 0)


def test_a_turn_the_display_never_confirmed_changes_nothing() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    _serve(ledger, result, _id(League.MLB, "a"), turns=3)
    before = ledger.snapshot()
    assert ledger.served(result, _id(League.MLB, "b"), timedelta(0), completed=True) == ()
    assert ledger.snapshot() == before  # an unacknowledged cue resets no streak


def test_crediting_a_game_outside_its_targets_or_negative_airtime_is_refused() -> None:
    result = targets([_entry(League.MLB, "a", 0.4)])
    ledger = AirtimeLedger()
    with pytest.raises(ValueError, match="isn't in the targets"):
        ledger.served(result, _id(League.MLB, "elsewhere"), _TURN, completed=True)
    with pytest.raises(ValueError, match="negative"):
        ledger.served(result, _id(League.MLB, "a"), timedelta(seconds=-1), completed=True)


# --- clamps and rescue -----------------------------------------------------------------------------------------


def test_a_debt_past_24_seconds_is_held_there_and_the_clamp_is_reported() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    a, b = _id(League.MLB, "a"), _id(League.MLB, "b")
    assert _serve(ledger, result, a, turns=4) == []  # at exactly 24 s nothing is clamped
    clamps = _serve(ledger, result, a)
    assert clamps == [DebtClamp(League.MLB, a, -30.0, -24.0), DebtClamp(League.MLB, b, 30.0, 24.0)]
    choice = _choose(ledger, result)
    assert choice.clamps == tuple(clamps) and choice.game_account.debt == 24.0
    assert _choose(ledger, result).clamps == ()  # each clamp is reported once


def test_a_game_is_rescued_at_its_miss_threshold_and_a_service_resets_it() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    a, b = _id(League.MLB, "a"), _id(League.MLB, "b")
    _serve(ledger, result, a, turns=3)
    assert _choose(ledger, result).reason is ChoiceReason.WEIGHTED  # 3 misses: under max(2, ceil(2/0.5))
    _serve(ledger, result, a)
    rescued = _choose(ledger, result)
    assert (rescued.contest, rescued.reason) == (b, ChoiceReason.RESCUE)
    assert rescued.rescue is not None
    assert (rescued.rescue.contest, rescued.rescue.misses, rescued.rescue.threshold) == (b, 4, 4)
    _serve(ledger, result, b)
    after = _choose(ledger, result)
    assert after.reason is ChoiceReason.WEIGHTED and after.rescue is None


def test_the_longest_waiting_overdue_game_is_rescued_first_whatever_its_debt() -> None:
    a, x, y = _id(League.MLB, "a"), _id(League.MLB, "x"), _id(League.MLB, "y")
    ledger = AirtimeLedger()
    _serve(ledger, targets([_entry(League.MLB, "a", 1.0), _entry(League.MLB, "x", 0.0)]), a)
    result = targets(
        [_entry(League.MLB, "a", 1.0), _entry(League.MLB, "x", 0.0), _entry(League.MLB, "y", 0.0, postseason=True)]
    )
    _serve(ledger, result, a, turns=9)  # x has missed 10 turns (threshold 10), y 9 (threshold 9)
    choice = _choose(ledger, result)
    assert choice.reason is ChoiceReason.RESCUE and choice.contest == x  # y's score is higher: 26.97 to 26.58
    assert choice.rescue is not None and (choice.rescue.wait, choice.rescue.threshold) == (120.0, 10)
    _serve(ledger, result, x)
    assert _choose(ledger, result).contest == y  # still overdue


def test_a_league_is_rescued_after_missing_its_threshold_of_full_turns() -> None:
    result = targets([_entry(League.NFL, "car", 0.4, rank=1), _entry(League.MLB, "atl", 0.4, rank=1)])
    ledger = AirtimeLedger()
    _serve(ledger, result, _id(League.NFL, "car"), turns=6)  # max(6, 2, ceil(2/0.39))
    choice = _choose(ledger, result)
    assert (choice.contest.raw, choice.reason) == ("atl", ChoiceReason.RESCUE)
    assert choice.rescue is not None and (choice.rescue.contest, choice.rescue.misses) == (None, 6)


# --- breaks and endings ----------------------------------------------------------------------------------------


def test_a_confirmed_break_freezes_debt_and_wait_and_clears_the_miss_streak() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    a, b = _id(League.MLB, "a"), _id(League.MLB, "b")
    _serve(ledger, result, a, turns=3)
    ledger.freeze(b)
    _serve(ledger, result, a, turns=2)
    frozen = ledger.snapshot().game(b)
    assert frozen is not None and (frozen.debt, frozen.wait, frozen.misses, frozen.frozen) == (18.0, 36.0, 0, True)
    ledger.thaw(b)
    _serve(ledger, result, a)
    thawed = ledger.snapshot().game(b)
    assert thawed is not None and (thawed.debt, thawed.wait, thawed.misses, thawed.frozen) == (24.0, 48.0, 1, False)


def test_a_game_frozen_before_it_is_seen_starts_frozen_and_thawing_an_unknown_game_is_harmless() -> None:
    ledger = AirtimeLedger()
    later = _id(League.MLB, "later")
    ledger.freeze(later)
    ledger.thaw(_id(League.MLB, "never"))
    snapshot = ledger.snapshot()
    account = snapshot.game(later)
    assert account is not None and account.frozen and snapshot.game(_id(League.MLB, "never")) is None
    assert snapshot.leagues == ()


def test_a_frozen_game_shown_still_pays_for_its_turn() -> None:
    result = targets([_entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    b = _id(League.MLB, "b")
    ledger.freeze(b)
    _serve(ledger, result, b)
    account = ledger.snapshot().game(b)
    assert account is not None and (account.debt, account.airtime) == (-12.0, 12.0)


def test_a_retired_game_returning_after_an_outage_starts_at_zero() -> None:
    result = targets([_entry(League.NFL, "car", 0.4), _entry(League.MLB, "a", 0.4), _entry(League.MLB, "b", 0.4)])
    ledger = AirtimeLedger()
    a, b = _id(League.MLB, "a"), _id(League.MLB, "b")
    _serve(ledger, result, a, turns=3)
    ledger.retire(b)
    snapshot = ledger.snapshot()
    assert snapshot.game(b) is None and snapshot.league(League.MLB) is not None  # a is still on
    ledger.retire(a)
    gone = ledger.snapshot()
    assert gone.league(League.MLB) is None and gone.last_shown is None  # the league's last game took it along
    back = _choose(ledger, result)
    assert not back.continues
    returned = ledger.snapshot().game(b)
    assert returned is not None and (returned.debt, returned.misses, returned.wait, returned.airtime) == (0, 0, 0, 0)


def test_a_snapshot_lists_leagues_in_declared_order_and_games_by_league_then_id() -> None:
    result = targets([_entry(League.NBA, "z", 0.4), _entry(League.MLB, "b", 0.4), _entry(League.MLB, "a", 0.4)])
    ledger = AirtimeLedger()
    _serve(ledger, result, _id(League.NBA, "z"))
    snapshot = ledger.snapshot()
    assert [account.league for account in snapshot.leagues] == [League.MLB, League.NBA]
    assert [str(account.contest) for account in snapshot.games] == ["mlb:test:a", "mlb:test:b", "nba:test:z"]
    assert snapshot.last_shown == _id(League.NBA, "z")
