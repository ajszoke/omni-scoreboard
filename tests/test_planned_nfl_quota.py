"""NFL on the shared board near the end of NFLMeta's month: a cold start that still lists its games, the switch from
Free to Builder, and the provider's own count of what's left."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.app.quota import BillingMonth, MonthUsage, NflMetaPlan, QuotaLedger, account_limits
from omni.app.quota_store import USAGE_FILE, UsageFile, open_ledger
from omni.preferences.favorites import Favorites
from tests.test_planned_nfl import CAR_ATL, EARLY, NFL, PIT_CIN, S, SCORES, SUNDAY, _board, _row, _start

NOVEMBER = BillingMonth(2026, 11)
LIVE: dict[str, Any] = {"phase": "in"}


def _spent(requests: int) -> QuotaLedger:
    return QuotaLedger(account_limits(NflMetaPlan.FREE), usage={NFL: MonthUsage(NOVEMBER, requests=requests)})


def test_a_cold_start_at_4800_of_free_s_5000_still_lists_the_day_and_follows_the_favorite() -> None:
    board = _board(EARLY, ledger=_spent(4_800))
    board.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    board.nflmeta.rows[102] = _row(*PIT_CIN, **LIVE)
    board.run(EARLY + 30 * S)
    assert board.ids(SUNDAY) == ["101", "102"]  # the day's list went out, as the favorite's reads need it
    assert board.nflmeta.asked(game_id=101) == [EARLY + 5 * S + n * 10 * S for n in range(3)]  # focus, the favorite
    assert board.nflmeta.asked(phase="in", game_ids="102") == []  # the others are normal: held back below 5%
    assert (
        board.planned.held_back(board.reports[-1].span.current) == "quota: NFLMeta requests 3% left, normal held back"
    )


def test_a_cold_start_near_the_month_s_end_with_no_nfl_favorite_says_the_list_is_held_back() -> None:
    board = _board(EARLY, ledger=_spent(4_800), favorites=Favorites())
    board.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    board.run(EARLY + 30 * S)
    assert board.nflmeta.requests == []  # nothing it could read once listed fits: the list isn't spent on
    assert (
        board.planned.held_back(board.reports[-1].span.current) == "quota: NFLMeta requests 4% left, normal held back"
    )


def test_free_s_spent_month_switched_to_builder_reads_again_counting_on_from_the_month_s_requests(
    tmp_path: Path,
) -> None:
    UsageFile(tmp_path / USAGE_FILE).save({NFL: MonthUsage(NOVEMBER, requests=5_000, rows=18_000, exhausted=True)})
    spent = _board(EARLY, ledger=open_ledger(tmp_path, NflMetaPlan.FREE, log=lambda line: None))
    spent.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    spent.run(EARLY + 30 * S)
    assert spent.nflmeta.requests == []
    reason = "quota: NFLMeta Free spent for 2026-11: switch to Builder"
    assert spent.planned.held_back(spent.reports[-1].span.current) == reason
    spent.ledger.save(spent.clock.read(), force=True)
    spent.ledger.close()  # the run stops; the user switches the account and restarts on Builder

    builder = _board(EARLY + 60 * S, ledger=open_ledger(tmp_path, NflMetaPlan.BUILDER, log=lambda line: None))
    builder.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    builder.nflmeta.requests_left = 75_000 - 5_000  # Builder's own count of the month: what Free spent counts on it
    builder.run(EARLY + 90 * S)
    assert builder.nflmeta.requests[0][1:] == (SCORES, {})
    assert builder.ids(SUNDAY) == ["101"] and len(builder.nflmeta.asked(game_id=101)) == 3
    assert builder.ledger.usage(NFL, builder.clock.read()).requests == 5_000 + len(builder.nflmeta.requests)


def test_the_provider_s_count_of_what_s_left_raises_the_month_s_count_and_never_lowers_it() -> None:
    board = _board(EARLY, ledger=_spent(1_000))
    board.nflmeta.rows[101] = _row(*CAR_ATL)
    board.nflmeta.requests_left = 3_001  # NFLMeta has counted 1,999 before this answer: another board, a lost save
    board.step()
    assert board.ledger.usage(NFL, board.clock.read()).requests == 2_000
    board.nflmeta.requests_left = 4_500  # an answer that says less is spent never lowers the count
    _start(board, 101)
    board.clock.advance(10 * S)
    board.step()  # the favorite's first look: its scores and its plays
    assert len(board.nflmeta.requests) == 3
    assert board.ledger.usage(NFL, board.clock.read()).requests == 2_002
