"""The replay's policy lines and the scorecard's arithmetic, on decisions built by hand: every kind of line the policy
writes, and what the scorecard counts from a step's decisions, its card or frame, and its released view."""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import Any

import pytest

from omni.cards.base import CardId, ScoreboardCard
from omni.app.quota import BillingMonth, MonthUsage, ProviderAccount
from omni.core.clock import MonotonicTime
from omni.core.enum import League
from omni.domain.breaks import BreakId, BreakKind
from omni.policy.allocation import GameWeight, Targets, targets
from omni.policy.interrupts import BarContext, InterruptClass, InterruptValue, LeverageBasis
from omni.policy.leases import LeaseRefusal
from omni.policy.ledger import DebtClamp, Rescue
from omni.policy.leverage import Leverage, ModelStatus
from omni.policy.names import PolicyName
from omni.policy.pool import PoolEntry
from omni.policy.promotion import PromotionKey, PromotionKind, PromotionRefusal
from omni.policy.view import GameFacts, PolicyView
from omni.preferences.favorites import Favorites
from omni.replay.mixed import MixedRun
from omni.replay.policy_lines import policy_line
from omni.replay.scorecard import ScoreKeeper
from omni.policy.ladder import Rung
from omni.tower.decisions import (
    BaseReason,
    BaseTurnNote,
    CapNote,
    CardNote,
    CardRole,
    ClampNote,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    OfferSkip,
    PolicyDecision,
    UnspentNote,
    WaitNote,
    YieldNote,
)
from omni.tower.decisions import PromotionNote
from tests.board_policy import at, evidence, facts, games, live

V2 = PolicyName.BOARD_V2
GAMES = games(3)
HOST, GUEST, OTHER = GAMES
WEIGHT = GameWeight(g=0.5, status=ModelStatus.PROXY, f=4.0, q=0.5, t=1.0, b=7.0, favorite_rank=1, acting_favorite=True)
FAVORITES = Favorites({League.MLB: ["ATL"]})


def _targets(*playing: Any) -> Targets:
    leverage = Leverage(g=0.5, status=ModelStatus.PROXY)
    return targets([PoolEntry.of(game, leverage=leverage, favorites=FAVORITES) for game in playing])


def _base(
    seconds: float,
    contest: Any = GUEST,
    *,
    reason: BaseReason = BaseReason.WEIGHTED,
    hosts: tuple[Any, ...] = (),
    rescue: Rescue | None = None,
    actual: float | None = 0.25,
    continues: bool = False,
) -> BaseTurnNote:
    return BaseTurnNote(
        at=at(seconds),
        policy=V2,
        card=live(contest).id,
        contest=contest.id,
        reason=reason,
        continues=continues,
        weight=WEIGHT,
        target=0.5,
        league_target=1.0,
        within_league=0.5,
        actual=actual,
        debt=1.5,
        league_debt=-0.25,
        rescue=rescue,
        hosts=hosts,
        why="Braves: favorite",
        targets=_targets(HOST, GUEST),
    )


# --- the policy's lines -------------------------------------------------------------------------------------------

KEY = PromotionKey(GUEST.id, "ab:7")
BREAK = BreakId(HOST.id, BreakKind.MLB_INNING_BREAK, "top 5 middle")
WORTH = InterruptValue(event_class=InterruptClass.MAJOR, value=0.8, z_pre=0.5, basis=LeverageBasis.PROXY)
LINES: list[tuple[PolicyDecision, str]] = [
    (
        _base(
            12, rescue=Rescue(league=League.MLB, contest=GUEST.id, misses=6, threshold=6, wait=60.0), hosts=(HOST.id,)
        ),
        "base    mlb:mlb_statsapi:g01:live  (weighted) | G 0.500 proxy F 4.00 T 1.000 Q 0.500 | target 50.00% (league"
        " 100.00%, 50.00% within) | actual 25.00% | debt +1.50s (league -0.25s) | rescue 6/6 misses | guest of"
        " mlb:mlb_statsapi:g00 | why: Braves: favorite",
    ),
    (
        _base(24, actual=None, continues=True),
        "base    mlb:mlb_statsapi:g01:live  (weighted, again) | G 0.500 proxy F 4.00 T 1.000 Q 0.500 | target 50.00%"
        " (league 100.00%, 50.00% within) | actual - | debt +1.50s (league -0.25s) | why: Braves: favorite",
    ),
    (
        PromotionNote(
            at=at(30),
            policy=V2,
            card=CardId("mlb:mlb_statsapi:g01:bigplay:ab:7"),
            key=KEY,
            kind=PromotionKind.MAJOR,
            value=WORTH,
            bar=0.85,
            context=BarContext.MICRO_BREAK,
            hold=MonotonicTime(40.0),
            cooldown_until=MonotonicTime(90.0),
            hosts=(HOST.id,),
            why="Angels at D-backs: big moment; Braves in mound visit",
        ),
        "promote mlb:mlb_statsapi:g01:bigplay:ab:7  (major) | I 0.800 big moment on proxy | bar 0.85 micro_break"
        " | hold until 40.0 | cooldown until 90.0 | guest of mlb:mlb_statsapi:g00 | why: Angels at D-backs: big"
        " moment; Braves in mound visit",
    ),
    (
        PromotionNote(
            at=at(30),
            policy=V2,
            card=CardId("mlb:mlb_statsapi:g01:nohitter"),
            key=KEY,
            kind=PromotionKind.REMINDER,
            value=None,
            bar=0.35,
            context=BarContext.OPEN,
            hold=None,
            cooldown_until=MonotonicTime(90.0),
            hosts=(),
            why="Angels at D-backs: reminder; no favorite in action",
        ),
        "promote mlb:mlb_statsapi:g01:nohitter  (reminder) | I - | bar 0.35 open | cooldown until 90.0"
        " | why: Angels at D-backs: reminder; no favorite in action",
    ),
    (
        CardNote(
            at=at(5),
            policy=V2,
            card=CardId("c"),
            role=CardRole.RESULT,
            rung=Rung.INITIAL_RESULT,
            hosts=(HOST.id,),
            why="w",
        ),
        "card    c  (result) | guest of mlb:mlb_statsapi:g00 | why: w",
    ),
    (
        WaitNote(at=at(5), key=KEY, revision=1, reason=PromotionRefusal.BELOW_BAR, bar=1.1, context=BarContext.ACTION),
        "wait    mlb:mlb_statsapi:g01 ab:7 r1  (below_bar) | bar 1.10 action",
    ),
    (WaitNote(at=at(5), key=KEY, revision=0, reason=OfferSkip.MUTED), "wait    mlb:mlb_statsapi:g01 ab:7 r0  (muted)"),
    (
        UnspentNote(at=at(5), card=CardId("c"), key=KEY, reason=PromotionRefusal.BREAK_OVER),
        "unspent c  (break_over)",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.OPENED, remaining=timedelta(seconds=104.5), turns=None),
        "lease   opened mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle | 104.5s left | any guests that fit",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.OPENED, remaining=None, turns=2, provisional=True),
        "lease   opened mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle | open-ended | 2 guests | provisional",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.OPENED, remaining=timedelta(seconds=30), turns=1),
        "lease   opened mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle | 30.0s left | 1 guest",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.REFUSED, refusal=LeaseRefusal.BACKFILL),
        "lease   refused mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle  (backfill)",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.REFUSED),
        "lease   refused mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle",
    ),
    (
        LeaseNote(at=at(5), id=BREAK, event=LeaseEvent.ENDED),
        "lease   ended mlb:mlb_statsapi:g00 mlb inning break @ top 5 middle",
    ),
    (
        GuestNote(at=at(5), card=CardId("c"), hosts=(HOST.id,)),
        "guest   admitted c in mlb:mlb_statsapi:g00",
    ),
    (
        GuestNote(at=at(5), card=CardId("c"), hosts=(HOST.id,), refusal=LeaseRefusal.NO_ROOM),
        "guest   refused c in mlb:mlb_statsapi:g00  (no_room)",
    ),
    (
        YieldNote(at=at(9), card=CardId("c"), hosts=(HOST.id,), closed=MonotonicTime(4.0)),
        "yield   c  (its host's break closed 4.0)",
    ),
    (CapNote(at=at(5), refused=(Rung.PROMOTION, Rung.AUXILIARY)), "cap     refused promotion, auxiliary"),
    (
        ClampNote(
            at=at(5),
            clamps=(
                DebtClamp(League.MLB, GUEST.id, -26.5, -24.0),
                DebtClamp(League.NFL, None, 25.0, 24.0),
            ),
        ),
        "clamp   mlb:mlb_statsapi:g01 debt -26.50s held at -24.00s; nfl league debt +25.00s held at +24.00s",
    ),
]


@pytest.mark.parametrize(("note", "line"), LINES)
def test_each_decision_is_one_line_timed_on_the_replay_s_running_clock(note: PolicyDecision, line: str) -> None:
    assert policy_line(note) == line


# --- the scorecard ------------------------------------------------------------------------------------------------


def _view(*described: GameFacts) -> PolicyView:
    return PolicyView(games=MappingProxyType({each.game.id: each for each in described}))


def _card(contest: Any) -> ScoreboardCard[Any]:
    return live(contest)


def _run() -> MixedRun:
    """A run that spent nothing."""
    accounts = tuple(ProviderAccount)
    return MixedRun(
        decisions=(),
        fetched=(),
        early=(),
        usage=tuple(MonthUsage(BillingMonth(2026, 9)) for _ in accounts),
        accounts=accounts,
    )


def test_base_airtime_is_owed_by_the_turn_s_targets_and_counted_where_it_showed() -> None:
    keeper = ScoreKeeper()
    view = _view(facts(HOST, 0.5), facts(GUEST, 0.5))
    keeper.observe(0.0, decisions=(), drawn=None, frame=None, view=view)  # a paint that drew nothing
    keeper.observe(1.0, decisions=(), drawn=None, frame=None, view=view)
    keeper.observe(2.0, decisions=(_base(2.0),), drawn=_card(GUEST), frame=None, view=view)
    keeper.observe(14.0, decisions=(), drawn=None, frame="idle", view=view)
    keeper.observe(20.0, decisions=(), drawn=None, frame="idle", view=view)
    card = keeper.card(_run())
    assert card.base == 12.0 and dict(card.board) == {"live_game": 12.0, "frame idle": 6.0}
    guest, host = card.game(GUEST.id), card.game(HOST.id)
    assert (guest.actual, host.actual, guest.target + host.target) == (12.0, 0.0, pytest.approx(12.0))
    assert host.longest_wait == 12.0 and host.missed == 0
    assert card.league(League.MLB).actual == 12.0
    assert card.usage == {ProviderAccount.MLB_STATSAPI: (0, 0), ProviderAccount.NFLMETA: (0, 0)}


def test_a_guest_still_up_after_its_host_s_break_closed_and_past_its_minimum_is_an_overrun() -> None:
    keeper = ScoreKeeper()
    view = _view(facts(HOST, 0.5, breaks=(evidence(HOST, BreakKind.MLB_INNING_BREAK, received=0),)))
    guest_card = _card(OTHER)
    admitted = GuestNote(at=at(0), card=guest_card.id, hosts=(HOST.id,))
    note = CardNote(
        at=at(0), policy=V2, card=guest_card.id, role=CardRole.NEUTRAL, rung=Rung.AUXILIARY, hosts=(HOST.id,), why=""
    )
    opened = LeaseNote(at=at(0), id=BreakId(HOST.id, BreakKind.MLB_INNING_BREAK, "b1"), event=LeaseEvent.OPENED)
    keeper.observe(0.0, decisions=(opened, admitted, note), drawn=guest_card, frame=None, view=view)
    closed = LeaseNote(at=at(2), id=opened.id, event=LeaseEvent.REVOKED)
    keeper.observe(2.0, decisions=(closed,), drawn=guest_card, frame=None, view=view)
    keeper.observe(12.0, decisions=(), drawn=None, frame="idle", view=view)  # down 4 s past its 8 s minimum
    card = keeper.card(_run())
    assert card.overruns == ((guest_card.id.raw, 4.0),) and card.premature == ()


def test_a_guest_that_yields_on_time_or_a_reserved_recovery_that_finishes_its_turn_is_no_overrun() -> None:
    keeper = ScoreKeeper()
    opened = LeaseNote(at=at(0), id=BreakId(HOST.id, BreakKind.MLB_INNING_BREAK, "b1"), event=LeaseEvent.OPENED)
    view = _view(facts(HOST, 0.5, breaks=(evidence(HOST, BreakKind.MLB_INNING_BREAK, received=0),)))
    keeper.observe(0.0, decisions=(opened, _base(0.0, hosts=(HOST.id,))), drawn=_card(GUEST), frame=None, view=view)
    revoked = LeaseNote(at=at(1), id=opened.id, event=LeaseEvent.REVOKED)
    keeper.observe(1.0, decisions=(revoked,), drawn=_card(GUEST), frame=None, view=view)
    keeper.observe(8.0, decisions=(_base(8.0, OTHER),), drawn=_card(OTHER), frame=None, view=view)  # at its minimum
    recovery = _base(20.0, hosts=(HOST.id,), reason=BaseReason.RECOVERY)
    keeper.observe(20.0, decisions=(recovery,), drawn=_card(GUEST), frame=None, view=view)
    keeper.observe(
        21.0,
        decisions=(LeaseNote(at=at(21), id=opened.id, event=LeaseEvent.LAPSED),),
        drawn=_card(GUEST),
        frame=None,
        view=view,
    )
    keeper.observe(32.0, decisions=(), drawn=_card(OTHER), frame=None, view=view)
    keeper.observe(40.0, decisions=(_base(40.0, hosts=(HOST.id,)),), drawn=_card(GUEST), frame=None, view=view)
    keeper.observe(52.0, decisions=(_base(52.0, OTHER),), drawn=_card(OTHER), frame=None, view=view)  # no closure
    card = keeper.card(_run())
    assert card.overruns == ()
    assert dict(card.leases) == {"lapsed": 1, "opened": 1, "revoked": 1}


def test_a_guest_admitted_on_a_break_the_released_view_doesn_t_show_is_premature() -> None:
    keeper = ScoreKeeper()
    shown = _view(facts(HOST, 0.5, breaks=(evidence(HOST, BreakKind.MLB_INNING_BREAK, received=0, ended=True),)))
    admitted = GuestNote(at=at(0), card=CardId("c"), hosts=(HOST.id, OTHER.id))
    refused = GuestNote(at=at(0), card=CardId("d"), hosts=(HOST.id,), refusal=LeaseRefusal.NO_ROOM)
    keeper.observe(0.0, decisions=(admitted, refused), drawn=None, frame="idle", view=shown)
    card = keeper.card(_run())
    assert card.premature == ("c in mlb:mlb_statsapi:g00", "c in mlb:mlb_statsapi:g02")
    assert (card.guests, dict(card.guest_refusals)) == (1, {"no_room": 1})


def test_a_candidate_s_reason_is_counted_once_however_often_it_is_written() -> None:
    keeper = ScoreKeeper()
    waits = [
        WaitNote(at=at(0), key=KEY, revision=0, reason=PromotionRefusal.HOLDING),
        WaitNote(at=at(1), key=KEY, revision=1, reason=PromotionRefusal.HOLDING),
        WaitNote(at=at(2), key=KEY, revision=1, reason=PromotionRefusal.RETIRED),
    ]
    refused = LeaseNote(at=at(2), id=BREAK, event=LeaseEvent.REFUSED)
    keeper.observe(0.0, decisions=(*waits, refused), drawn=None, frame="idle", view=_view())
    card = keeper.card(_run())
    assert dict(card.waits) == {"holding": 1, "retired": 1} and (card.held, card.ran_out) == (1, 1)
    assert dict(card.lease_refusals) == {"-": 1}


def test_promotions_are_counted_by_kind_and_a_promotion_ends_the_base_turn_it_cut_into() -> None:
    keeper = ScoreKeeper()
    view = _view(facts(GUEST, 0.5))
    keeper.observe(0.0, decisions=(_base(0.0),), drawn=_card(GUEST), frame=None, view=view)
    promoted = next(note for note, _ in LINES if isinstance(note, PromotionNote) and note.value is not None)
    others = (
        CapNote(at=at(10), refused=(Rung.AUXILIARY,)),
        YieldNote(at=at(10), card=CardId("c"), hosts=(), closed=MonotonicTime(9.0)),
    )
    keeper.observe(10.0, decisions=(promoted, *others), drawn=_card(OTHER), frame=None, view=view)
    keeper.observe(22.0, decisions=(), drawn=None, frame="idle", view=view)
    card = keeper.card(_run())
    assert dict(card.promotions) == {"major": 1}
    assert card.base == 10.0 and card.board["live_game"] == 22.0


def test_the_scorecard_reads_as_markdown_with_none_for_what_didn_t_happen() -> None:
    keeper = ScoreKeeper()
    keeper.observe(0.0, decisions=(), drawn=None, frame="idle", view=_view())
    page = keeper.card(_run()).markdown(title="Nothing on")
    assert page.startswith("# Nothing on\n\nConfirmed base airtime: 0.0 s.")
    assert "- Promotions made: none." in page and "Spoilers: 0." in page
