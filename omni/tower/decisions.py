"""What the board decided under `omni-board-policy/2`, as typed records for the trace (`BOARD_POLICY.md` §8).

The policy director (`omni.tower.director`) writes one record per decision that changes something:
- a base turn (`BaseTurnNote`): its game's ``G``, ``F``, ``T`` and ``Q``, its target and actual shares, the debts,
  and any rescue, as they stood when the ledger chose it; a debt clamped when a turn was credited (`ClampNote`);
- a promoted card's confirmed start (`PromotionNote`): its ``I``, the bar and its context, the hold and the cooldown;
  why a waiting candidate didn't start, each time that reason changes (`WaitNote`), and a cued promotion whose
  start was rejected at its first frame, so it spent nothing (`UnspentNote`);
- each lease opened, revoked, ended, lapsed or superseded, or evidence that opened none and why, with the break's
  age and how much of its ceiling had gone (`LeaseNote`); each guest admitted into a favorite's break or refused,
  and where its permission was checked (`GuestNote`), and a guest that gave the board back when the break closed,
  with how long it had aired (`YieldNote`);
- an ordinary card's turn (`CardNote`), and the non-base cap binding (`CapNote`).

Every turn's record carries the "why this card" sentence: one plain line from released facts only, such as
"Rockies: second favorite, close late game; Braves in inning break". It names the card's own matchup and the
favorites whose breaks it fills; never a number, a waiting candidate, or anything still inside the delay.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Union

from omni.cards.base import CardId
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import GameStatus, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakAge, BreakId, BreakKind
from omni.policy.allocation import GameWeight, Targets
from omni.policy.interrupts import BarContext, InterruptClass, InterruptValue
from omni.policy.ladder import Rung
from omni.policy.leases import LeaseRefusal
from omni.policy.ledger import DebtClamp, Rescue
from omni.policy.promotion import PromotionKey, PromotionKind, PromotionRefusal

if TYPE_CHECKING:  # the versions module imports the app, whose trace imports these notes
    from omni.app.versions import PolicyName

__all__ = [
    "BREAK_WORDS",
    "BaseReason",
    "BaseTurnNote",
    "CapNote",
    "CardNote",
    "CardRole",
    "ClampNote",
    "GrantBoundary",
    "GuestNote",
    "LeaseEvent",
    "LeaseNote",
    "OfferSkip",
    "PolicyDecision",
    "PromotionNote",
    "UnspentNote",
    "WaitNote",
    "YieldNote",
    "aired_words",
    "event_words",
    "judged_words",
    "rank_words",
    "refused_guest",
    "status_words",
]


class CardRole(StrEnumMixin, str, Enum):
    """What a card the policy put up is doing on the board."""

    BASE = "base"  # a live game's base turn
    PLAY_IN_TURN = "play in turn"  # a big play that wasn't promoted, first in its own game's base turn
    PROMOTION = "promotion"  # a promoted event, snapshot or reminder
    RESULT = "result"  # an initial final in end order, or the unseen play that decided it
    REPEAT = "repeat"  # a final shown again
    PREGAME = "pregame"
    STATUS = "status"  # a delay or a suspension
    NEUTRAL = "neutral"  # nothing live to weigh: the cards on hand at neutral weight
    FALLBACK = "fallback"  # nothing fresh to show: a stale game's dim card, the last safe frame, with no base turn


class BaseReason(StrEnumMixin, str, Enum):
    """Why a base turn went to its game."""

    WEIGHTED = "weighted"  # the largest debt plus a slot of its target
    RESCUE = "rescue"  # an overdue league or game
    ONLY_CANDIDATE = "only candidate"
    RECOVERY = "recovery"  # the full base turn owed after the non-base cap
    RETURN = "return"  # the favorite's break can't host another guest, so the board goes back to it


class OfferSkip(StrEnumMixin, str, Enum):
    """Why the board kept a promotion candidate from the broker, or couldn't start the one it chose."""

    NOT_IN_VIEW = "not_in_view"  # the card it names isn't in the same refresh's cards
    NOT_DRAWABLE = "not_drawable"  # the card can't be drawn on this panel
    MUTED = "muted"  # its game is muted
    SET_ASIDE = "set_aside"  # chosen, but its card is set aside or out of its window now: it waits
    CHANGED = "changed"  # the card it names no longer shows the event it was offered for: never promoted


class LeaseEvent(StrEnumMixin, str, Enum):
    OPENED = "opened"
    REVOKED = "revoked"  # released action after the break began ended it
    ENDED = "ended"  # the feed marked its end
    LAPSED = "lapsed"  # its ceiling ran out with no sign of play
    SUPERSEDED = "superseded"  # a new independent interval replaced it
    REFUSED = "refused"  # the evidence opened no lease


class GrantBoundary(StrEnumMixin, str, Enum):
    """Where a guest's permission in a favorite's break was checked."""

    DECISION = "decision"  # when the board chose what goes up next
    SWAP = "swap"  # when the display confirmed its first frame


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseTurnNote:
    """A base turn, from its first confirmed frame (or the boundary where its game won again)."""

    at: ClockReading
    policy: PolicyName
    card: CardId
    contest: LeagueScopedId
    reason: BaseReason
    continues: bool  # its game won again: the frame stayed up
    weight: GameWeight  # G, F, T, Q and the weight they make
    target: float  # q'_g: its share of all base airtime
    league_target: float  # Q_l
    within_league: float  # p_g
    actual: float | None  # its share of the base airtime while it was eligible, before this turn
    debt: float  # its debt in seconds, before this turn
    league_debt: float
    rescue: Rescue | None
    hosts: tuple[LeagueScopedId, ...]  # the favorites whose break it's a guest of
    why: str
    targets: Targets | None = None  # every pool game's and league's targets the turn was chosen by


@dataclass(frozen=True, slots=True, kw_only=True)
class ClampNote:
    """Debts held at the limit when a turn was credited."""

    at: ClockReading
    clamps: tuple[DebtClamp, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionNote:
    """A promoted card's confirmed start: what it's worth, the bar it cleared, the hold and the cooldown it starts."""

    at: ClockReading
    policy: PolicyName
    card: CardId
    key: PromotionKey
    kind: PromotionKind
    value: InterruptValue | None  # None for a snapshot or reminder
    bar: float
    context: BarContext
    hold: MonotonicTime | None  # the hold episode's deadline, if one was open
    cooldown_until: MonotonicTime
    hosts: tuple[LeagueScopedId, ...]
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WaitNote:
    """Why a promotion candidate didn't start (or was kept from the broker), written when that reason changes."""

    at: ClockReading
    key: PromotionKey
    revision: int
    reason: PromotionRefusal | OfferSkip
    bar: float | None = None
    context: BarContext | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UnspentNote:
    """A cued promotion the broker rejected at its first frame: it came down, and spent no cooldown or turn."""

    at: ClockReading
    card: CardId
    key: PromotionKey
    reason: PromotionRefusal


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseNote:
    """A favorite's break lease changing, or evidence that opened none and why."""

    at: ClockReading
    id: BreakId
    event: LeaseEvent
    refusal: LeaseRefusal | None = None
    remaining: timedelta | None = None  # an opened lease: what's left of its ceiling (None: open-ended)
    provisional: bool = False  # the onset was uncertain, so one poll interval came off
    turns: int | None = None  # the guests it may host (None: as many as fit)
    age: BreakAge | None = None  # evidence judged for a lease: its age at detection, and how that's known
    elapsed: timedelta | None = None  # evidence judged for a lease: how much of its ceiling had gone at the decision


@dataclass(frozen=True, slots=True, kw_only=True)
class GuestNote:
    """A card admitted into the favorites' break (``refusal`` None), or refused and why, and where."""

    at: ClockReading
    card: CardId
    hosts: tuple[LeagueScopedId, ...]
    refusal: LeaseRefusal | None = None
    boundary: GrantBoundary = GrantBoundary.DECISION


@dataclass(frozen=True, slots=True, kw_only=True)
class YieldNote:
    """A break's guest giving the board back: its host's break closed at ``closed``, and it had aired ``aired``."""

    at: ClockReading
    card: CardId
    hosts: tuple[LeagueScopedId, ...]
    closed: MonotonicTime
    aired: timedelta | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CardNote:
    """An ordinary card's turn: a result, a repeat, a pregame, a status, a big play in its game's turn, or neutral
    rotation."""

    at: ClockReading
    policy: PolicyName
    card: CardId
    role: CardRole
    rung: Rung
    hosts: tuple[LeagueScopedId, ...]
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CapNote:
    """The non-base cap bound: these rungs were refused, and a full base turn is reserved."""

    at: ClockReading
    refused: tuple[Rung, ...]


PolicyDecision = Union[
    BaseTurnNote,
    CapNote,
    CardNote,
    ClampNote,
    GuestNote,
    LeaseNote,
    PromotionNote,
    UnspentNote,
    WaitNote,
    YieldNote,
]


# --- The "why" sentence's words ----------------------------------------------------------------------------------

BREAK_WORDS: Mapping[BreakKind, str] = MappingProxyType(
    {
        BreakKind.MLB_INNING_BREAK: "inning break",
        BreakKind.MLB_MOUND_VISIT: "mound visit",
        BreakKind.MLB_PITCHING_CHANGE: "pitching change",
        BreakKind.MLB_REVIEW: "replay review",
        BreakKind.MLB_STOPPAGE: "stoppage",
        BreakKind.MLB_WEATHER: "weather delay",
        BreakKind.NFL_TV_TIMEOUT: "TV timeout",
        BreakKind.NFL_OFFICIAL_TIMEOUT: "official timeout",
        BreakKind.NFL_TEAM_TIMEOUT: "timeout",
        BreakKind.NFL_TWO_MINUTE_WARNING: "two-minute warning",
        BreakKind.NFL_QUARTER_BREAK: "quarter break",
        BreakKind.NFL_HALFTIME: "halftime",
        BreakKind.NFL_INJURY_TIMEOUT: "injury timeout",
        BreakKind.NFL_REVIEW: "review",
        BreakKind.NBA_TEAM_TIMEOUT: "timeout",
        BreakKind.NBA_TV_TIMEOUT: "TV timeout",
        BreakKind.NBA_QUARTER_BREAK: "quarter break",
        BreakKind.NBA_HALFTIME: "halftime",
        BreakKind.NBA_BEFORE_OVERTIME: "break before overtime",
    }
)

_RANKS = ("favorite", "second favorite", "third favorite")

_EVENTS = MappingProxyType(
    {
        InterruptClass.PERFECT_GAME: "perfect game",
        InterruptClass.NO_HITTER: "no-hitter",
        InterruptClass.DECISIVE_LATE_WINNER: "late winner",
        InterruptClass.WIN_SWING: "win swing",
        InterruptClass.MAJOR: "big moment",
        InterruptClass.ROUTINE: "play",
    }
)

_UNSCORED = MappingProxyType({PromotionKind.SNAPSHOT: "close-game snapshot", PromotionKind.REMINDER: "reminder"})

_STATUSES = MappingProxyType({GameStatus.DELAYED: "delayed", GameStatus.SUSPENDED: "suspended"})


def rank_words(rank: int) -> str:
    """A favorite's rank in words: "favorite", "second favorite", ...; a rank past the third is a favorite."""
    return _RANKS[rank - 1] if rank <= len(_RANKS) else _RANKS[0]


def event_words(kind: PromotionKind, value: InterruptValue | None) -> str:
    """What a promoted card is about, in words: its event's class, or what an unscored card is."""
    return _EVENTS[value.event_class] if value is not None else _UNSCORED[kind]


def status_words(status: GameStatus) -> str:
    """A paused game's status in words."""
    return _STATUSES.get(status, "paused")


def judged_words(note: LeaseNote) -> str:
    """What a lease refusal weighed, for its line: the break's age at detection, how that's known and how much of it
    is an allowance, and how much of its ceiling had gone at the decision; empty for one refused before it was
    dated."""
    age, elapsed = note.age, note.elapsed
    if age is None or elapsed is None:
        return ""
    said = f" | age {age.age.total_seconds():.1f}s {age.basis.value}"
    if age.provisional:
        said += f", {age.uncertainty.total_seconds():.1f}s of it an allowance"
    return said + f" | {elapsed.total_seconds():.1f}s of its ceiling gone"


def refused_guest(note: GuestNote) -> str:
    """Why a guest was refused, and where, when it wasn't at the decision."""
    assert note.refusal is not None
    said = f"{note.refusal.value}"
    return said if note.boundary is GrantBoundary.DECISION else f"{said} at its first frame"


def aired_words(note: YieldNote) -> str:
    """How long a guest that gave the board back had aired, when that's known."""
    return f"; aired {note.aired.total_seconds():.1f}s" if note.aired is not None else ""
