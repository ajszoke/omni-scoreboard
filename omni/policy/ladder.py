"""The card ladder: what kind of card goes next at a legal decision, the non-base cap, and the ordinary cards'
timing rules (`BOARD_POLICY.md` §6, with §3.5's rules for how a card leaves when a break closes).

At each legal decision the first rung with a card due wins:
1. device controls, corrections and hard expiry;
2. an owed full base recovery;
3. an overdue base rescue;
4. an eligible promoted alert;
5. an initial ordered result, when the context permits;
6. a due highlight or auxiliary card;
7. the ordinary weighted base.

**The non-base cap:** after 30 s of consecutive non-base cards, admit no more; the one admitted finishes, then a full
12 s base turn is reserved. It's independent of the promotion cooldown. Minimums are always protected: the board
asks only at legal decisions. The run counts confirmed airtime only, and only while a live base card was on hand to
give the board to: neutral circulation with no live game runs up nothing, and a run already at the cap stays there
through a spell with no base card, so the turn it owes is still owed when one is back.

The per-card rules are pure answers to "is this card due, and may it play here": a pregame's cadence by how near
its start is, a final's turn and repeats, a no-hit bid's reminders, a favorite's status, a basketball highlight.
Spans that count "open" time count only time when no favorite is in action (`OpenTime`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, IntEnum

from omni.core.clock import MonotonicTime
from omni.core.enum import IntEnumMixin, StrEnumMixin
from omni.domain.baseball import PitchingFeatProgress
from omni.policy.interrupts import BarContext

__all__ = [
    "CardTimingPolicy",
    "LadderPolicy",
    "LadderRefusal",
    "NonBaseRun",
    "OpenTime",
    "PregameRamp",
    "Rung",
    "RungChoice",
    "RungRefusal",
    "Tenure",
    "bid_reminder_due",
    "choose_rung",
    "favorite_status_due",
    "final_gone",
    "final_repeat_due",
    "highlight_due",
    "initial_final_permitted",
    "pregame_due",
    "pregame_every",
    "pregame_permitted",
    "red_zone_reminder_due",
    "yield_after_break",
]

_ZERO = timedelta(0)


class Rung(IntEnumMixin, IntEnum):
    """A kind of card at a legal decision, in priority order: the lowest goes first."""

    CONTROL = 1  # device controls, corrections and hard expiry
    BASE_RECOVERY = 2  # an owed full base recovery
    RESCUE = 3  # an overdue base rescue
    PROMOTION = 4  # an eligible promoted alert
    INITIAL_RESULT = 5  # an initial ordered result, when the context permits
    AUXILIARY = 6  # a due highlight or auxiliary card
    BASE = 7  # the ordinary weighted base

    @property
    def base(self) -> bool:
        """A base card: a live game's turn, whether owed, rescued or weighted."""
        return self in (Rung.BASE_RECOVERY, Rung.RESCUE, Rung.BASE)

    @property
    def non_base(self) -> bool:
        """A card the non-base cap counts: a promotion, a result, a highlight or an auxiliary card."""
        return self in (Rung.PROMOTION, Rung.INITIAL_RESULT, Rung.AUXILIARY)


@dataclass(frozen=True, slots=True, kw_only=True)
class LadderPolicy:
    """The non-base cap and the base turn it reserves."""

    non_base_cap: timedelta = timedelta(seconds=30)
    base_turn: timedelta = timedelta(seconds=12)

    def __post_init__(self) -> None:
        if self.non_base_cap <= _ZERO or self.base_turn <= _ZERO:
            raise ValueError("the non-base cap and the base turn must be positive")


@dataclass(frozen=True, slots=True)
class NonBaseRun:
    """The current run of consecutive non-base cards, as the confirmed airtime they had while a live base card was
    on hand: zero while a base card holds the board. A value the board carries from decision to decision."""

    accrued: timedelta = _ZERO

    def capped(self, policy: LadderPolicy = LadderPolicy()) -> bool:
        """Whether the run has reached the cap: no more non-base cards until a full base turn."""
        return self.accrued >= policy.non_base_cap

    def aired(self, rung: Rung, airtime: timedelta, *, live_base: bool) -> NonBaseRun:
        """The run once a card of ``rung`` had ``airtime`` of confirmed frames: a non-base card shown while a live base
        card was on hand extends it; anything else (neutral circulation with no live game, a control) leaves it."""
        if rung.non_base and live_base:
            return NonBaseRun(self.accrued + airtime)
        return self

    def after(self, rung: Rung) -> NonBaseRun:
        """The run once a card of ``rung`` started: a base card ends it; any other leaves it as it was."""
        return NonBaseRun() if rung.base else self


class LadderRefusal(StrEnumMixin, str, Enum):
    NON_BASE_CAP = "non_base_cap"  # the run of non-base cards reached the cap: a full base turn is owed first
    OUTRANKED = "outranked"  # a higher rung goes first


@dataclass(frozen=True, slots=True)
class RungRefusal:
    rung: Rung
    reason: LadderRefusal


@dataclass(frozen=True, slots=True, kw_only=True)
class RungChoice:
    """The rung that goes now (None with nothing due), whether it's the cap's reserved base turn, and why each other
    due rung waits."""

    rung: Rung | None
    reserved: bool = False
    refused: tuple[RungRefusal, ...] = ()


def choose_rung(due: Iterable[Rung], run: NonBaseRun, policy: LadderPolicy = LadderPolicy()) -> RungChoice:
    """The rung that goes at this legal decision, from the rungs with a card due.

    Once the run reaches the cap with a base card waiting, no non-base rung is admitted, and the base card that goes
    is the reserved full turn: a base recovery, however it was due. With no base card waiting, the cap has nothing
    to reserve, so it doesn't bind.
    """
    ordered = sorted(set(due))
    owed = run.capped(policy) and any(rung.base for rung in ordered)
    chosen: Rung | None = None
    refused: list[RungRefusal] = []
    for rung in ordered:
        if owed and rung.non_base:
            refused.append(RungRefusal(rung, LadderRefusal.NON_BASE_CAP))
        elif chosen is None:
            chosen = rung
        else:
            refused.append(RungRefusal(rung, LadderRefusal.OUTRANKED))
    if owed and chosen is not None and chosen.base:
        return RungChoice(rung=Rung.BASE_RECOVERY, reserved=True, refused=tuple(refused))
    return RungChoice(rung=chosen, refused=tuple(refused))


class Tenure(StrEnumMixin, str, Enum):
    """Why the card on air holds the board, which sets how it leaves when the favorite's break closes."""

    BREAK_GUEST = "break_guest"  # admitted only because of the break: it yields once its minimum is done
    FULL_TURN = "full_turn"  # a reserved base recovery, or a still-valid background live turn: it finishes its turn


def yield_after_break(
    *, tenure: Tenure, started: MonotonicTime, minimum: timedelta, turn: timedelta, closed: MonotonicTime
) -> MonotonicTime:
    """When the card on air gives the board back after the break closed at ``closed`` (`BOARD_POLICY.md` §3.5): a
    break guest at its minimum's end or the close, whichever is later, but never past its own run; a full turn at its
    turn's end. A correction, a hard expiry, a mute or a blank clears at once, and isn't asked here."""
    end = started + turn
    if tenure is Tenure.BREAK_GUEST:
        return min(end, max(closed, started + minimum))
    return end


# --- The ordinary cards' timing --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PregameRamp:
    """While more than ``above`` is left before the start (and the ramp before hasn't taken over), a pregame card is
    due every ``every``."""

    above: timedelta
    every: timedelta


_PREGAME_RAMPS = (
    PregameRamp(timedelta(minutes=30), timedelta(seconds=300)),
    PregameRamp(timedelta(minutes=10), timedelta(seconds=120)),
    PregameRamp(timedelta(minutes=2), timedelta(seconds=60)),
    PregameRamp(_ZERO, timedelta(seconds=30)),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CardTimingPolicy:
    """The ordinary cards' timing, as `BOARD_POLICY.md` §6's table sets it."""

    pregame_lead: timedelta = timedelta(minutes=90)  # pregame cards begin this long before the start
    pregame_ramps: tuple[PregameRamp, ...] = _PREGAME_RAMPS  # nearest the start last
    pregame: timedelta = timedelta(seconds=8)
    pregame_base_turns: int = 4  # with live games, at most one pregame per this many base turns
    final: timedelta = timedelta(seconds=8)  # an initial final's one turn, and each repeat
    final_repeats: tuple[timedelta, ...] = (timedelta(minutes=5), timedelta(minutes=15))  # since its first start
    final_gone: timedelta = timedelta(minutes=30)  # since confirmation: every turn of it ends by then
    bid_innings: int = 6  # a no-hit or perfect bid's reminders start after this many completed innings
    bid_reminders: int = 3
    bid_every: timedelta = timedelta(seconds=60)  # open time between a bid's reminders
    red_zone_reminders: int = 1  # an NFL late one-score red-zone drive
    favorite_status_every: timedelta = timedelta(seconds=120)  # open time between a favorite's delay statuses
    highlight_every: timedelta = timedelta(seconds=90)
    highlights_per_game: int = 2

    def __post_init__(self) -> None:
        aboves = [ramp.above for ramp in self.pregame_ramps]
        if not aboves or aboves != sorted(aboves, reverse=True) or aboves[-1] != _ZERO:
            raise ValueError("the pregame ramps run toward the start and end at it")
        if list(self.final_repeats) != sorted(self.final_repeats) or any(
            repeat >= self.final_gone for repeat in self.final_repeats
        ):
            raise ValueError("a final's repeats come in order, before it's gone")


def pregame_every(until_start: timedelta, policy: CardTimingPolicy = CardTimingPolicy()) -> timedelta | None:
    """How often a pregame card is due this long before its start; None outside the pregame window."""
    if until_start > policy.pregame_lead or until_start <= _ZERO:
        return None
    return next(ramp.every for ramp in policy.pregame_ramps if until_start > ramp.above)


def pregame_due(
    *, until_start: timedelta, since_last: timedelta | None, policy: CardTimingPolicy = CardTimingPolicy()
) -> bool:
    """Whether a game's pregame card is due: inside the window, and a ramp's interval since it last showed."""
    every = pregame_every(until_start, policy)
    return every is not None and (since_last is None or since_last >= every)


def pregame_permitted(
    *,
    live_games: bool,
    base_turns_since: int | None,
    context: BarContext,
    policy: CardTimingPolicy = CardTimingPolicy(),
) -> bool:
    """Whether a due pregame may play here. With no live games, always; with live games, at most one per four base
    turns (``base_turns_since`` since the last pregame, None if none has shown), and only in a break or open
    rotation, never into a favorite's action."""
    if not live_games:
        return True
    rested = base_turns_since is None or base_turns_since >= policy.pregame_base_turns
    return rested and context is not BarContext.ACTION


def initial_final_permitted(*, context: BarContext, related: bool) -> bool:
    """Whether a final's initial turn may play here: while a favorite acts, a final unrelated to it waits for a
    break (unless it's promoted, which the promotion broker decides)."""
    return related or context is not BarContext.ACTION


def final_repeat_due(
    *,
    since_first_start: timedelta,
    since_confirmed: timedelta,
    repeats_shown: int,
    policy: CardTimingPolicy = CardTimingPolicy(),
) -> bool:
    """Whether a final's next repeat is due now: at most two, the first once 5 minutes have passed since the final
    first went up, the second once 15 have, each only if its whole turn ends by the final's hard end, 30 minutes after
    it was confirmed. So a result first shown late may have no repeats left."""
    if repeats_shown >= len(policy.final_repeats) or since_confirmed + policy.final > policy.final_gone:
        return False
    return since_first_start >= policy.final_repeats[repeats_shown]


def final_gone(since_confirmed: timedelta, policy: CardTimingPolicy = CardTimingPolicy()) -> bool:
    """Whether a final has left the board: 30 minutes after it was confirmed."""
    return since_confirmed >= policy.final_gone


def bid_reminder_due(
    progress: PitchingFeatProgress,
    *,
    shown: int,
    open_since_last: timedelta,
    policy: CardTimingPolicy = CardTimingPolicy(),
) -> bool:
    """Whether a no-hit or perfect bid's reminder is due: after six completed innings, when new, then every 60 open
    seconds, three at most. It never plays into a favorite's action (a reminder is break-only in the broker)."""
    if progress.completed_innings < policy.bid_innings or shown >= policy.bid_reminders:
        return False
    return shown == 0 or open_since_last >= policy.bid_every


def red_zone_reminder_due(*, shown: int, policy: CardTimingPolicy = CardTimingPolicy()) -> bool:
    """Whether an NFL late one-score red-zone drive's reminder is due: once."""
    return shown < policy.red_zone_reminders


def favorite_status_due(*, open_since_last: timedelta | None, policy: CardTimingPolicy = CardTimingPolicy()) -> bool:
    """Whether a favorite's delay or suspension status may show again: at most once per 120 open seconds (None: it
    hasn't shown)."""
    return open_since_last is None or open_since_last >= policy.favorite_status_every


def highlight_due(
    *, shown_this_game: int, since_last: timedelta | None, policy: CardTimingPolicy = CardTimingPolicy()
) -> bool:
    """Whether a basketball highlight (a close-game snapshot) may be offered: at most one per 90 s and two a game.
    It plays only in breaks and open rotation, inside the promotion cooldown (a snapshot in the broker)."""
    if shown_this_game >= policy.highlights_per_game:
        return False
    return since_last is None or since_last >= policy.highlight_every


@dataclass(frozen=True, slots=True)
class OpenTime:
    """Open time, when no favorite is in action, accrued since a mark: what a reminder's or a status's cadence
    counts. A value; the board updates it as the context changes, and marks a fresh one each time the card shows."""

    accrued: timedelta = _ZERO
    since: MonotonicTime | None = None  # open since this instant, while open

    @staticmethod
    def mark(*, open_now: bool, at: MonotonicTime) -> OpenTime:
        """A fresh count from ``at``."""
        return OpenTime(_ZERO, at if open_now else None)

    def update(self, *, open_now: bool, at: MonotonicTime) -> OpenTime:
        """The count once the board is (or isn't) open from ``at``."""
        if open_now and self.since is None:
            return OpenTime(self.accrued, at)
        if not open_now and self.since is not None:
            return OpenTime(self.accrued + at.since(self.since), None)
        return self

    def total(self, at: MonotonicTime) -> timedelta:
        return self.accrued + (at.since(self.since) if self.since is not None else _ZERO)
