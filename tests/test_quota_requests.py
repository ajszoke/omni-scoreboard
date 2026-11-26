"""Every NFLMeta request admitted against its account: a followed poll's pages and a failed attempt's retry each
take their own place in the minute and the month, the ask's own place is its first request's, both NFL entry
points share the one admission, and one process at a time polls the account."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.app.football_feed import GameFeed
from omni.app.poll_plan import Demand, PollPriority
from omni.app.quota import (
    UNLIMITED,
    AccountLimits,
    MeteredHttp,
    Meter,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    account_limits,
    nflmeta_billed,
)
from omni.app.quota_store import open_ledger
from omni.core.clock import FakeClock
from omni.providers.nfl_teams import NflTeamRegistry
from omni.providers.nflmeta import NFLMETA_SOURCE, NflMetaLiveProvider
from tests.test_planned_nfl import CAR_ATL, EARLY, NFL, S, SCORES, _board, _change, _row

PAGES_150 = [_change(f"p{n}", n) for n in range(1, 151)]  # a first look reads them in two pages
LIVE: dict[str, Any] = {"phase": "in", "play_sync": {"status": "matched", "last_play_id": "p150"}}


def _hold(ledger: QuotaLedger, clock: FakeClock, places: int) -> None:
    """Other asks handed out on the account, each holding its place in the minute until its request goes."""
    for _ in range(places):
        assert ledger.admit(NFL, Demand(PollPriority.FOCUS), clock.read()).admitted


def test_eighteen_places_held_in_free_s_minute_leave_a_followed_poll_two_requests_and_no_more() -> None:
    board = _board(EARLY - 60 * S, ledger=QuotaLedger(account_limits(NflMetaPlan.FREE)))
    board.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    board.nflmeta.plays[101] = list(PAGES_150)
    board.step()  # the day list, a minute before kickoff
    board.clock.advance(60 * S)
    _hold(board.ledger, board.clock, 18)
    board.step()  # the favorite's first look: its scores, then its plays in two pages
    made = [at for at, _, _ in board.nflmeta.requests if at >= EARLY]
    assert len(made) == 2  # the scores and the plays' first page: twenty in the minute
    assert board.nflmeta.play_reads(101) == [EARLY]
    held = board.ledger.admit(NFL, Demand(PollPriority.FOCUS), board.clock.read())
    assert not held.admitted and (held.used, held.per_minute) == (20, 20)


def test_a_followed_poll_s_pages_are_each_charged_once_and_its_ask_s_place_is_its_first_request_s() -> None:
    ledger = QuotaLedger({NFL: AccountLimits(per_minute=5), ProviderAccount.POLYMARKET_US: UNLIMITED})  # the meter's
    board = _board(EARLY - 60 * S, ledger=ledger)
    board.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    board.nflmeta.plays[101] = list(PAGES_150)
    board.step()
    board.clock.advance(60 * S)
    board.step()  # the scores and two pages: three requests, three places
    assert len(board.nflmeta.requests) == 4 and board.ledger.usage(NFL, board.clock.read()).requests == 4
    assert board.ledger.admit(NFL, Demand(PollPriority.FOCUS), board.clock.read()).admitted  # the fourth place
    assert board.ledger.admit(NFL, Demand(PollPriority.FOCUS), board.clock.read()).admitted  # the fifth
    assert not board.ledger.admit(NFL, Demand(PollPriority.FOCUS), board.clock.read()).admitted


def test_a_failed_attempt_with_no_response_is_charged_as_a_request_and_so_is_its_retry() -> None:
    board = _board(EARLY, ledger=QuotaLedger(account_limits(NflMetaPlan.BUILDER)))
    board.nflmeta.rows[101] = _row(*CAR_ATL)
    board.nflmeta.down = True
    board.step()  # the day list fails: no response, and it may still have reached NFLMeta
    assert board.ledger.usage(NFL, board.clock.read()).requests == 1
    board.nflmeta.down = False
    board.clock.advance(2 * S)
    board.step()  # its retry
    assert [path for _, path, _ in board.nflmeta.requests] == [SCORES, SCORES]
    assert board.ledger.usage(NFL, board.clock.read()).requests == 2


def test_the_solo_runner_s_feed_takes_a_place_for_each_page_on_the_account_it_shares() -> None:
    board = _board(EARLY, ledger=QuotaLedger(account_limits(NflMetaPlan.FREE)))
    board.nflmeta.rows[101] = _row(*CAR_ATL, **LIVE)
    board.nflmeta.plays[101] = list(PAGES_150)
    meter = Meter(board.ledger, NFL, board.clock, billed=nflmeta_billed)
    provider = NflMetaLiveProvider(
        NflTeamRegistry.default(source=NFLMETA_SOURCE), fetch=MeteredHttp(board.nflmeta, meter)
    )
    feed = GameFeed(provider, 101, ledger=board.ledger)
    _hold(board.ledger, board.clock, 18)
    answer = feed.ask(board.clock.read())
    assert answer.snapshot is not None  # the scores came back; the plays' second page didn't fit the minute
    assert len(board.nflmeta.requests) == 2 and board.nflmeta.play_reads(101) == [EARLY]
    assert not board.ledger.admit(NFL, Demand(PollPriority.FOCUS), board.clock.read()).admitted


def test_a_second_process_on_the_same_state_directory_never_polls_nflmeta_and_says_why(tmp_path: Path) -> None:
    first = _board(EARLY, ledger=open_ledger(tmp_path, NflMetaPlan.FREE, log=lambda line: None, who="omni.app"))
    second = _board(EARLY, ledger=open_ledger(tmp_path, NflMetaPlan.FREE, log=lambda line: None))
    for board in (first, second):
        board.nflmeta.rows[101] = _row(*CAR_ATL)
        board.step()
    assert len(first.nflmeta.requests) == 1 and second.nflmeta.requests == []
    reason = second.planned.held_back(second.reports[-1].span.current)
    assert reason is not None and reason.startswith("quota: NFLMeta is polled by another process (pid ")
    assert reason.endswith(", omni.app); not asking here")
    first.ledger.close()
