"""The standing offers a pipeline keeps: one per moment, its newest revision, while its card is staged."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from omni.app.policy_facts import Offer, StandingOffers, interrupt_stakes, major_offer
from omni.cards.base import DedupeKey
from omni.core.clock import MonotonicTime
from omni.core.enum import SeasonStage
from omni.policy.interrupts import InterruptClass, Stakes
from omni.policy.pool import MatchupRecords, TeamRecord
from omni.policy.promotion import PromotionKey
from tests.test_app_nfl_pipeline import nfl_game

GAME = nfl_game()
CARD = DedupeKey("nfl:nflmeta:401:live")


def _offer(play: str, revision: int) -> Offer:
    offer = major_offer(
        InterruptClass.MAJOR,
        key=PromotionKey(GAME.id, play),
        card=CARD,
        eligible=MonotonicTime(0.0),
        predecessor=None,
        stakes=Stakes(),
        revision=revision,
    )
    assert offer is not None
    return offer


def test_each_moment_stands_once_at_its_newest_revision_whatever_order_its_revisions_arrive_in() -> None:
    offers = StandingOffers(lifetime=timedelta(seconds=120))
    newer, older, other = _offer("57", 1), _offer("57", 0), _offer("58", 0)
    for offer in (newer, older, other, newer):
        offers.add(offer)
    assert offers.current(MonotonicTime(0.0), lambda card: True) == (newer, other)
    assert offers.current(MonotonicTime(0.0), lambda card: False) == ()  # nothing whose card isn't staged
    offers.withdraw(PromotionKey(GAME.id, "58"))
    assert offers.current(MonotonicTime(119.9), lambda card: True) == (newer,)
    assert offers.current(MonotonicTime(120.0), lambda card: True) == ()


def test_routine_scores_nothing_and_offers_nothing() -> None:
    routine = major_offer(
        InterruptClass.ROUTINE,
        key=PromotionKey(GAME.id, "58"),
        card=CARD,
        eligible=MonotonicTime(0.0),
        predecessor=None,
        stakes=Stakes(),
    )
    assert routine is None


def test_an_interrupt_s_stakes_read_the_records_and_a_confirmed_postseason_game() -> None:
    assert interrupt_stakes(GAME, None) == Stakes()  # unknown records: no quality, which counts as nothing
    even = MatchupRecords(away=TeamRecord(wins=8, losses=8), home=TeamRecord(wins=8, losses=8))
    playoff = dataclasses.replace(GAME, stage=SeasonStage.POSTSEASON)
    assert interrupt_stakes(playoff, even) == Stakes(quality=0.5, postseason=1.0)
