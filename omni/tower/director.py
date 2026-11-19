"""PolicyDirector: how the shared board shares its airtime under `omni-board-policy/2` (`BOARD_POLICY.md` §3-§6).

The board (`omni.tower.board.BoardSession`) keeps the card contract: dwell from the display's commit, a card that
leaves or expires coming down, a card it can't draw set aside, the explicit frames. Under policy v2 it asks the
director, from the input last published (the cards, their source's health, and the `PolicyView` of released
facts), what should go up next and whether the card on air should give way. The director owns the policy's state
for one board: the airtime ledger, the promotion broker, the lease book and the run of non-base cards.

**The live pool.** A game is in it when the view's facts describe it (released and live), it isn't muted, and its
base card (its live card, or the final that replaced it) is in the cards and drawable on the panel. A delay or a
suspension is a status card, not a pool entry. A stale game is set aside from the weighted targets, and its ledger
account ends: its dim card airs only when nothing fresh can, as the last safe frame (`CardRole.FALLBACK`), with no
base turn, credit or wait.

**A legal decision** comes once the card on air has had its minimum. A base turn's protection runs from the first
frame of its game's card, so a game that wins again keeps its frame without a new minimum, and the base card that
follows an unpromoted big play within its turn inherits the turn's. At each:
- a guest of a favorite's break that has closed gives the board back (`yield_after_break`);
- an unpromoted big play, first in its game's turn, hands over to the game's base card at its own minimum;
- a promotion may cut into a base turn, unless it's the reserved recovery;
- once a turn has run its length, the ladder (`choose_rung`) picks what's next: an owed recovery, an overdue
  rescue, a promotion, an initial final in end order, a due pregame, status or repeat, then the weighted base.
  With nothing live to weigh, the cards on hand rotate at neutral weight, the longest unseen first.

**A final's turns** follow one path with or without a live game: its initial turn in end order, then at most two
repeats, at 5 and 15 minutes after its first physical start, each in a context that permits it and only if its whole
turn ends by the final's hard end, 30 minutes after confirmation. Between them it isn't due: the board shows what is,
or the clock.

**Base turns** go by debt (`targets`, `AirtimeLedger.choose`), league first, then game; the same game winning again
keeps its frame. When favorites are in breaks and none is in action, another game's card is their guest: it must
fit what's left of each lease with the return margin, or the board goes back to the favorite. A base turn is
credited with its confirmed time when it ends or is cut short; nothing accrues during other cards. When the games
eligible for it change mid-turn (a favorite's break begins or ends, a game leaves), the time so far is credited to
the eligible games as they were, and the rest of the turn to them as they are now: a favorite in its break accrues
nothing from the moment the board sees it, and nothing is credited back for a break once it ends.

**Promotions** reach the broker as the view offers them. The broker says when one may start (the bars, the 24 s
hold, lifetimes, the 60 s cooldown); the director adds what the broker leaves to the board: protected minimums,
drawability, the non-base cap. A start counts only at the card's first committed frame, so a cue that never lands
spends nothing. A non-base card waiting on its promotion airs by no other route meanwhile, but for a favorite's own big
play, which leads its game's next turn. A no-hit bid's reminders are the director's to pace (§6): when new, then once
60 open seconds have passed since the last one showed, three at most, each offered as a candidate of its own.

**A promotion airs its exact event.** Every scored offer names a card whose content is fixed for that event's
revision: the card as first offered is kept, and if the view's card under that key later shows something else, the
offer no longer describes it and is never promoted (a newer revision is taken afresh, keeping its eligibility). Each
published view is reconciled with the broker: a waiting candidate the view no longer offers is withdrawn, so a
correction that took it back can't air it, nor cite it in a "why" line. A moment whose story has aired, promoted, in
its own game's turn or as a result, is never promoted again while its game is on hand, whenever its offer arrives.

**The unseen major's result** (`omni.tower.summary`): a decisive event or a confirmed feat whose promotion ran out
unseen is kept as a `ResultSummary`, and its game's final, in its own place in end order, is first presented as that
summary: the event's still with the final's score. It spends no cooldown. Stale (five minutes past the final's
eligibility), already aired, or undrawable, it's dropped and the final airs plain, saying its deciding play is gone.

**Leases** open on the view's released break evidence and close when released action after the break began names
it (`ActionEvidence.closes`). Each is anchored at the release instant pinned for its evidence's answer, less the age
the break already had when that answer arrived, however late the decision that reads it; a new independent interval
supersedes a shorter one. A guest's permission names the exact leases it was granted in (`LeaseGrant`), stamped as
its card is handed to the board, and is checked again when its first frame is confirmed: a lease that changed or ran
short by then discards the guest unseen (`grant_refusal`, `started`). A guest whose break closes after a valid start
keeps its protected minimum, then gives the board back. On an outage the lease book and the ledger start over.

**NFL's caption in rotation:** within a base turn, a live football card shows a caption once, for 10 s, only if the
turn has 10 s of runway left for it; otherwise it's drawn as the field alone (its payload without ``last_play``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from omni.cards.base import CardKind, DedupeKey, ScoreboardCard
from omni.cards.baseball import StatusCardPayload
from omni.cards.football import LiveFootballCardPayload
from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League, PanelProfile
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakId
from omni.domain.contest import Contest, TeamGame
from omni.domain.football import PlaySummary
from omni.policy.allocation import AllocationPolicy, AllocationSettings, SetAsideReason, Targets, targets
from omni.policy.interrupts import BarContext
from omni.policy.ladder import (
    CardTimingPolicy,
    LadderPolicy,
    LadderRefusal,
    NonBaseRun,
    OpenTime,
    Rung,
    Tenure,
    choose_rung,
    favorite_status_due,
    final_repeat_due,
    initial_final_permitted,
    pregame_due,
    pregame_permitted,
    yield_after_break,
)
from omni.policy.leases import LeaseBook, LeaseGrant, LeasePolicy, LeaseRefusal, LeaseState, LeaseVerdict
from omni.policy.ledger import AirtimeLedger, ChoiceReason, Rescue
from omni.policy.pool import PoolEntry
from omni.policy.promotion import (
    OfferOutcome,
    PromotionBroker,
    PromotionCandidate,
    PromotionKey,
    PromotionKind,
    PromotionMoment,
    PromotionPolicy,
    PromotionRefusal,
)
from omni.policy.view import PolicyView
from omni.preferences.favorites import Favorites
from omni.queue.scheduler import CardView, SelectionReason
from omni.tower.decisions import (
    BREAK_WORDS,
    BaseReason,
    BaseTurnNote,
    CapNote,
    CardNote,
    CardRole,
    ClampNote,
    GrantBoundary,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    OfferSkip,
    PolicyDecision,
    PromotionNote,
    UnspentNote,
    WaitNote,
    YieldNote,
    event_words,
    rank_words,
    status_words,
)
from omni.tower.board import muted
from omni.tower.interruption import Interruption
from omni.tower.lane import EndReason, earliest
from omni.tower.summary import ResultSummary, SummaryPolicy, terminal

if TYPE_CHECKING:  # the settings' module imports the app's versions, whose package imports the board
    from omni.policy.settings import PolicySettings

__all__ = ["Air", "DirectorPolicy", "PolicyDirector", "Turn"]

_ZERO = timedelta(0)
_BASE_RUNGS = (Rung.BASE_RECOVERY, Rung.RESCUE, Rung.BASE)
_SHORT = frozenset({LeaseRefusal.NO_ROOM, LeaseRefusal.TURNS_SPENT})  # the break is on, but can't fit the card now


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectorPolicy:
    """The policy objects the director runs, and what the board adds to them: the NFL caption's run in rotation, a
    status card's turn, the poll interval that comes off a break whose onset is uncertain, and the leverage
    weights the "why" sentence calls a close late game, or a lopsided one."""

    allocation: AllocationPolicy = AllocationPolicy()
    promotion: PromotionPolicy = PromotionPolicy()
    leases: LeasePolicy = LeasePolicy()
    ladder: LadderPolicy = LadderPolicy()
    timing: CardTimingPolicy = CardTimingPolicy()
    summary: SummaryPolicy = SummaryPolicy()
    caption: timedelta = timedelta(seconds=10)
    status: timedelta = timedelta(seconds=8)
    # How often each league's live game feed is read: MLB's every 15 s, NFL's favorite every 10 s.
    poll_intervals: tuple[tuple[League, timedelta], ...] = (
        (League.MLB, timedelta(seconds=15)),
        (League.NFL, timedelta(seconds=10)),
        (League.NBA, timedelta(seconds=15)),
    )
    other_polls: timedelta = timedelta(seconds=15)  # a league not listed
    close_late: float = 0.6  # G at or above this reads as a close late game
    lopsided: float = 0.15  # G below this reads as a lopsided game

    def __post_init__(self) -> None:
        if self.caption <= _ZERO or self.status <= _ZERO:
            raise ValueError("a caption's and a status card's runs must be positive")

    def poll_interval(self, league: League) -> timedelta:
        """How long one read of a league's live game feed can lag."""
        return next((every for each, every in self.poll_intervals if each is league), self.other_polls)

    def turn(self, card: ScoreboardCard[Any], slot: timedelta) -> timedelta:
        """An ordinary card's nominal turn: 8 s for a final, a pregame or a status, otherwise the board's slot."""
        return {
            CardKind.FINAL: self.timing.final,
            CardKind.PREGAME: self.timing.pregame,
            CardKind.STATUS: self.status,
        }.get(card.kind, slot)


@dataclass(slots=True, kw_only=True)
class Turn:
    """One decision's hold on the board: a game's base turn (whichever of its cards is up), a promotion, or an
    ordinary card. Its length counts from its first confirmed frame."""

    role: CardRole
    rung: Rung
    length: timedelta
    minimum: timedelta
    tenure: Tenure
    contest: LeagueScopedId
    targets: Targets | None = None  # a base turn: the targets it was chosen by, which credit it
    reason: BaseReason | None = None  # a base turn: why its game won
    rescue: Rescue | None = None
    hosts: tuple[LeagueScopedId, ...] = ()  # the favorites whose break it's a guest of
    grants: tuple[LeaseGrant, ...] = ()  # a break's guest: its permission in each host's lease, once handed over
    candidate: PromotionCandidate[DedupeKey] | None = None
    why: str = ""
    started: MonotonicTime | None = None
    accounted: MonotonicTime | None = None  # a base turn: credited up to here
    live_base: bool = False  # a live base card was on hand when it was chosen: its airtime runs up the non-base cap
    ended: bool = False
    captioned: bool = False  # a caption has aired in it
    yield_at: MonotonicTime | None = None  # a guest whose host's break closed: when it gives the board back

    @property
    def base(self) -> bool:
        return self.rung in _BASE_RUNGS


@dataclass(slots=True, kw_only=True)
class Air:
    """A card the director put up: as the view holds it (what the queue books), as drawn, and the turn it's in."""

    card: ScoreboardCard[Any]
    shown: ScoreboardCard[Any]
    role: CardRole
    minimum: timedelta  # its own, before it may hand over within its turn
    turn: Turn
    since: MonotonicTime | None = None  # its first confirmed frame
    protected: MonotonicTime | None = None  # until when nothing ordinary may take its place
    caption_until: MonotonicTime | None = None
    summary: ResultSummary | None = None  # a final first presented as its unseen deciding play's story

    @property
    def reason(self) -> SelectionReason:
        """The queue's word for why it's up, for the cue and the trace's up line."""
        candidate = self.turn.candidate
        if candidate is None:
            return SelectionReason.ROTATION
        return SelectionReason.REMINDER if candidate.kind is PromotionKind.REMINDER else SelectionReason.TAKEOVER


@dataclass(slots=True)
class _Bid:
    """A no-hit bid's reminders: how many have shown, the open time since the last one did (None before the first),
    and which attempt the next is (one whose lifetime ran out unshown is offered again as the next)."""

    shown: int = 0
    since: OpenTime | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class _Final:
    """A final that has gone up: its first physical start, and when it was confirmed (its window opened)."""

    first: MonotonicTime
    confirmed: MonotonicTime


@dataclass(frozen=True, slots=True)
class _Offered:
    """A scored offer's card as it was first offered at its revision: the event the offer describes."""

    revision: int
    card: ScoreboardCard[Any]


@dataclass(frozen=True, slots=True)
class _Scene:
    """What one decision reads: the cards that may air now, the live pool, and the favorites' moment."""

    at: ClockReading
    view: CardView
    usable: Mapping[str, ScoreboardCard[Any]]  # in their window with time for their minimum, drawable, not muted
    base: Mapping[LeagueScopedId, ScoreboardCard[Any]]  # each pool game's base card
    entries: tuple[PoolEntry, ...]
    pool: Targets
    moment: PromotionMoment
    hosts: tuple[LeagueScopedId, ...]  # favorites in an open break while none is in action, best rank first
    reserved: frozenset[str]  # non-base cards waiting on their promotion

    @property
    def now(self) -> MonotonicTime:
        return self.at.monotonic

    def entry(self, contest: LeagueScopedId) -> PoolEntry:
        return next(entry for entry in self.entries if entry.contest == contest)


@dataclass(frozen=True, slots=True)
class _Option:
    """A card the ladder could put up, at its rung."""

    rung: Rung
    air: Air


class PolicyDirector:
    """The policy's state for one board, and its answers at each decision."""

    def __init__(
        self,
        profile: PanelProfile,
        settings: PolicySettings,
        *,
        favorites: Favorites,
        aired: Callable[[str], bool],
        policy: DirectorPolicy = DirectorPolicy(),
    ) -> None:
        self._profile = profile
        self._settings = settings
        self._favorites = favorites
        self._aired = aired  # whether the queue has booked a card
        self._policy = policy
        self._allocation = AllocationSettings(settings.league_weights, slot=settings.slot)
        self._seasons = frozenset(league for league, _ in policy.allocation.season_games)
        self._ledger = AirtimeLedger(policy=policy.allocation)
        self._broker = PromotionBroker[DedupeKey](policy.promotion)
        self._leases = LeaseBook(policy.leases)
        self._run = NonBaseRun()
        self._recovery_owed = False
        self._cards: CardView | None = None
        self._view = PolicyView()
        self._offline = False
        self._games: set[LeagueScopedId] = set()  # the games the facts last described
        self._lapsed: set[BreakId] = set()  # leases whose lapse is written
        self._frozen: set[LeagueScopedId] = set()
        self._accounts: set[LeagueScopedId] = set()  # games the ledger may hold an account for
        self._next: Air | None = None  # the card to put up next, decided as the one before came down
        self._current: Turn | None = None
        self._context = BarContext.OPEN  # the favorites' context at the last decision
        self._shown_at: dict[str, MonotonicTime] = {}  # when each card last went up
        self._repeats: dict[str, int] = {}  # each final's repeats shown
        self._finals: dict[str, _Final] = {}  # each final that has gone up, from its first start
        self._open: dict[str, OpenTime] = {}  # each status card's open time since it last showed
        self._base_turns: int | None = None  # base turns started since the last pregame; None until one shows
        self._captions: dict[LeagueScopedId, PlaySummary] = {}  # each game's caption that has aired
        self._bids: dict[LeagueScopedId, _Bid] = {}  # each standing no-hit bid's reminders
        self._waits: dict[PromotionKey, tuple[int, PromotionRefusal | OfferSkip]] = {}  # last reason written
        self._offered: dict[PromotionKey, _Offered] = {}  # each scored offer's event, as its revision first showed it
        self._consumed: set[PromotionKey] = set()  # moments whose story has aired: never promoted again
        self._drawn: dict[str, ScoreboardCard[Any]] = {}  # each card as it last went up
        self._stories: dict[PromotionKey, ResultSummary] = {}  # unseen decisive events, kept for their finals
        self._refused: dict[str, LeaseRefusal | None] = {}  # each card's last guest verdict written
        self._capped = False  # the cap's binding is written
        self._notes: list[PolicyDecision] = []

    @staticmethod
    def decides(settings: PolicySettings) -> bool:
        """Whether ``settings`` choose this policy: `omni-board-policy/2`."""
        from omni.app.versions import PolicyName  # deferred: the versions' package imports the board

        return settings.policy is PolicyName.BOARD_V2

    # --- the board's questions ---------------------------------------------------------------------------------

    def refresh(self, cards: CardView, view: PolicyView, *, offline: bool, at: ClockReading) -> None:
        """Take in a published input, once: its break evidence and action into the lease book, its offers into the
        broker. An outage starts the lease book and the ledger over."""
        if offline and not self._offline:
            self._restart(at)
        self._offline = offline
        if offline or (cards is self._cards and view is self._view):
            return
        self._cards, self._view = cards, view
        now = at.monotonic
        for contest in sorted(view.games, key=str):
            facts = view.games[contest]
            if facts.action is not None:
                revoked = self._leases.resumed(contest, facts.action.closes, now)
                if revoked is not None:
                    self._note(LeaseNote(at=at, id=revoked.id, event=LeaseEvent.REVOKED))
            for evidence in facts.breaks:
                verdict = self._leases.observe(
                    evidence,
                    at=now,
                    poll_interval=self._policy.poll_interval(contest.league),
                    national=facts.game.national_broadcast is True,
                    fresh=facts.fresh,
                )
                self._lease_noted(evidence.id, verdict, at)
        for gone in sorted(self._games - set(view.games), key=str):
            self._leases.forget(gone)
        self._games = set(view.games)
        if self._settings.promotions:
            for candidate in view.offers:
                if candidate.kind is PromotionKind.REMINDER:
                    self._remind(candidate, cards, at)
                else:
                    self._offer(candidate, cards, at)
            self._reconcile(view, at)
        self._prune(cards, view, now)

    def choose(self, view: CardView, at: ClockReading, *, skip: frozenset[str]) -> Air | None:
        """What to put up with nothing on air: the card decided in this same cue as the last one came down, or the
        ladder's choice now."""
        queued, self._next = self._next, None
        if queued is None:
            option = self._decide(self._scene(view, at, skip))
            queued = option.air if option is not None else None
        if queued is not None:
            self._grant(queued.turn)
        return queued

    def started(self, air: Air, at: ClockReading) -> bool:
        """The display committed ``air``'s first frame at ``at``. A promotion the broker rejects now (play resumed, a
        correction, its time ran out) spends nothing: False says to take it down unseen."""
        now = at.monotonic
        turn = air.turn
        refusal = self.grant_refusal(air, at)
        if refusal is not None:  # its break closed, changed or ran short before its frame: unseen, it spends nothing
            self.lapsed(air, refusal, at)
            return False
        if turn.candidate is not None and turn.started is None:
            assert self._cards is not None  # a card is up only from an input taken in
            scene = self._scene(self._cards, at, frozenset())
            hold = self._broker.hold(now)  # the episode a confirmed start ends
            verdict = self._broker.started(turn.candidate, scene.moment, now)
            if verdict.refusal is not None:
                self._note(UnspentNote(at=at, card=air.card.id, key=turn.candidate.key, reason=verdict.refusal))
                return False
            self._promotion_noted(air, scene, at, hold.deadline if hold is not None else None)
            self._consumed.add(turn.candidate.key)
            if turn.candidate.kind is PromotionKind.REMINDER:
                bid = self._bids.setdefault(turn.contest, _Bid())
                bid.shown, bid.attempt = bid.shown + 1, bid.attempt + 1
                bid.since = OpenTime.mark(open_now=scene.moment.context is not BarContext.ACTION, at=now)
        air.since = now
        self._drawn[air.card.dedupe_key.raw] = air.card
        if air.role is CardRole.PLAY_IN_TURN:  # shown in its game's turn: no promotion waits for it now
            self._consumed.update(settled.key for settled in self._broker.aired(air.card.dedupe_key))
        if air.summary is not None:  # its final's first presentation told the unseen event's story
            self._consumed.add(air.summary.key)
            self._stories.pop(air.summary.key, None)
        if turn.started is None:
            air.protected = now + air.minimum
            self._begin(turn, air, at, now)
        else:  # the game's base card, after its big play: it keeps the turn's protection
            air.protected = turn.started + turn.minimum
            self._card_noted(turn, air, at)
        self._showing(air, now)
        caption = _caption(air.shown)
        if caption is not None and turn.base:
            self._caption_from(air, caption, now)
        elif caption is not None:  # a promoted card shows its caption whole: it has aired
            self._captions[air.card.contest.id] = caption
        return True

    def review(self, air: Air, view: CardView, at: ClockReading, *, skip: frozenset[str]) -> EndReason | None:
        """Whether the card on air gives way at ``at``, and why; None keeps it, its drawing brought up to date in
        ``air.shown``."""
        now = at.monotonic
        turn = air.turn
        assert air.since is not None and air.protected is not None and turn.started is not None
        if air.caption_until is not None and now >= air.caption_until:
            air.caption_until = None
            air.shown = _without_caption(air.card)
        if turn.tenure is Tenure.BREAK_GUEST:
            closed = self._closed(turn)
            if closed is not None:
                minimum = max(air.protected.since(turn.started), _ZERO)
                turn.yield_at = yield_after_break(
                    tenure=turn.tenure, started=turn.started, minimum=minimum, turn=turn.length, closed=closed
                )
                if now >= turn.yield_at:
                    aired = now.since(turn.started)
                    self._note(YieldNote(at=at, card=air.card.id, hosts=turn.hosts, closed=closed, aired=aired))
                    return EndReason.NEXT_WAITING
        if air.role is CardRole.PLAY_IN_TURN and now >= air.since + air.minimum:
            scene = self._scene(view, at, skip)
            card = scene.base.get(turn.contest)
            if card is not None and card.dedupe_key.raw in scene.usable:
                self._next = self._air(card, CardRole.BASE, turn, now)
            return EndReason.RAN_ITS_TIME
        if now < air.protected:
            return None
        if turn.base and turn.reason is not BaseReason.RECOVERY and self._settings.promotions:
            promotion = self._promotion(self._scene(view, at, skip))
            if promotion is not None:
                self._next = promotion.air
                return EndReason.NEXT_WAITING
        if now < turn.started + turn.length:
            return None
        self._end(turn, at)  # it ran its turn: credit it, then decide again
        option = self._decide(self._scene(view, at, skip))
        if option is None:
            return EndReason.RAN_ITS_TIME
        if option.air.card.dedupe_key != air.card.dedupe_key:
            self._next = option.air
            return EndReason.RAN_ITS_TIME
        # The same card again (its game won again, or a lone card): its frame stays, and a new turn begins.
        renewed, role = option.air.turn, option.air.role
        again = role is air.role and role in _ROTATING  # the same card rotating on its own says nothing new
        air.turn, air.role, air.summary = renewed, role, option.air.summary
        start = turn.started + turn.length
        self._begin(renewed, air, at, start, again=again)
        self._showing(air, start)  # its new turn counts as a showing: a repeat, a status's mark, its last airing
        air.shown = self._present(air, air.card, now)
        return None

    def handoff(
        self, air: Air, successor: ScoreboardCard[Any], view: CardView, at: ClockReading, *, skip: frozenset[str]
    ) -> None:
        """The card on air left, replaced by ``successor`` (a live card by its final). When the successor is now its
        game's base card, the game's turn carries on with it; otherwise the ladder places it (an initial final)."""
        turn = air.turn
        scene = self._scene(view, at, skip)
        if turn.base and scene.base.get(turn.contest) is successor and successor.dedupe_key.raw in scene.usable:
            self._next = self._air(successor, CardRole.BASE, turn, at.monotonic)

    def revised(self, air: Air, card: ScoreboardCard[Any], at: ClockReading) -> None:
        """A newer version of the card on air arrived: it swaps in, drawn by the same rules."""
        air.card = card
        air.shown = self._present(air, card, at.monotonic)

    def down(self, air: Air, at: ClockReading) -> None:
        """``air`` came down at ``at``: its turn ends with it and is credited, unless the next card is the same
        turn's."""
        queued = self._next
        if queued is None or queued.turn is not air.turn:
            self._end(air.turn, at)

    def deadline(self, air: Air, at: ClockReading) -> MonotonicTime | None:
        """When the card on air next needs a decision by itself: its caption ending, its protection ending, its
        handover within its turn, its turn's end, or a guest's yield."""
        turn = air.turn
        assert air.since is not None and turn.started is not None  # asked only once its first frame committed
        handover = air.since + air.minimum if air.role is CardRole.PLAY_IN_TURN else None
        instants = (air.caption_until, air.protected, handover, turn.started + turn.length, turn.yield_at)
        return earliest(instant for instant in instants if instant is not None and instant > at.monotonic)

    def next_deadline(self, at: ClockReading) -> MonotonicTime | None:
        """When the policy's own state next changes by itself: the broker's hold, cooldown or a lifetime, a lease
        running out, or a bid's next reminder falling due."""
        now = at.monotonic
        dues = (self._reminder_due(bid, now) for bid in self._bids.values())
        reminders = [due for due in dues if due is not None and due > now]  # one due now is offered, or waits to be
        repeats = [due for due in map(self._repeat_due, self._finals) if due is not None and due > now]
        return earliest((self._broker.next_deadline(now), self._leases.next_deadline(now), *reminders, *repeats))

    def decisions(self) -> list[PolicyDecision]:
        """What the policy decided since the last ask, oldest first."""
        notes, self._notes = self._notes, []
        return notes

    # --- the scene -------------------------------------------------------------------------------------------

    def _muted(self, contest: Contest) -> bool:
        return muted(self._settings.mutes, contest)

    def _scene(self, view: CardView, at: ClockReading, skip: frozenset[str]) -> _Scene:
        now = at.monotonic
        usable: dict[str, ScoreboardCard[Any]] = {}
        live: dict[LeagueScopedId, ScoreboardCard[Any]] = {}
        replacing: dict[LeagueScopedId, ScoreboardCard[Any]] = {}
        for key in sorted(view.cards):
            card = view.cards[key]
            if card.kind is CardKind.LIVE_GAME:
                live.setdefault(card.contest.id, card)
            elif card.kind is CardKind.FINAL and card.replaces is not None:
                replacing.setdefault(card.contest.id, card)
            window = view.windows[key]
            drawable = card.layout_support.supports(self._profile) and key not in skip
            if drawable and window.is_open(now) and window.is_usable(now) and not self._muted(card.contest):
                usable[key] = card
        base: dict[LeagueScopedId, ScoreboardCard[Any]] = {}
        entries: list[PoolEntry] = []
        for contest in sorted(self._view.games, key=str):
            found = live.get(contest) or replacing.get(contest)
            if found is None or contest.league not in self._seasons:
                continue
            facts = self._view.games[contest]
            base[contest] = found
            entries.append(
                PoolEntry.of(
                    facts.game,
                    leverage=facts.leverage,
                    favorites=self._favorites,
                    in_break=self._leases.in_break(contest, now),
                    muted=self._settings.mutes.mutes(facts.game),
                    fresh=facts.fresh,
                    drawable=found.dedupe_key.raw in usable,
                    records=facts.records,
                    cup_knockout=facts.cup_knockout,
                )
            )
        pool = targets(entries, self._allocation, policy=self._policy.allocation)
        live_favorites = sorted(
            (e for e in entries if e.favorite_rank is not None and e.fresh and e.drawable and not e.muted),
            key=lambda e: (e.favorite_rank, str(e.contest)),
        )
        moment = PromotionMoment(tuple(self._leases.favorite(e.contest, now) for e in live_favorites))
        self._context = moment.context
        for bid in self._bids.values():
            if bid.since is not None:
                bid.since = bid.since.update(open_now=moment.context is not BarContext.ACTION, at=now)
        hosts: tuple[LeagueScopedId, ...] = ()
        if moment.context is not BarContext.ACTION:
            hosts = tuple(e.contest for e in live_favorites if self._leases.in_break(e.contest, now))
        base_keys = {card.dedupe_key.raw for card in base.values()}
        reserved = frozenset(candidate.card.raw for candidate in self._broker.pending()) - base_keys
        self._account(pool, entries, at)
        return _Scene(
            at=at,
            view=view,
            usable=MappingProxyType(usable),
            base=MappingProxyType(base),
            entries=tuple(entries),
            pool=pool,
            moment=moment,
            hosts=hosts,
            reserved=reserved,
        )

    def _account(self, pool: Targets, entries: Iterable[PoolEntry], at: ClockReading) -> None:
        """Keep the ledger in step with the pool: freeze a favorite resting in its break and thaw it after, retire one
        that left (once the turn on air, if it's that game's, is credited), and write a lease that ran out."""
        now = at.monotonic
        resting = {aside.contest for aside in pool.set_aside if aside.reason is SetAsideReason.FAVORITE_IN_BREAK}
        staying = {game.contest for game in pool.games} | resting
        if self._current is not None:
            staying.add(self._current.contest)
        leaving = sorted(self._accounts - staying, key=str)
        changing = [entry.contest for entry in entries if (entry.contest in resting) != (entry.contest in self._frozen)]
        if leaving or changing:
            self._split(pool, at)
        for contest in leaving:
            self._ledger.retire(contest)
            self._frozen.discard(contest)
        self._accounts &= staying
        for entry in entries:
            lease = self._leases.lease(entry.contest)
            if lease is not None and lease.state(now) is LeaseState.LAPSED and lease.id not in self._lapsed:
                self._lapsed.add(lease.id)
                self._note(LeaseNote(at=at, id=lease.id, event=LeaseEvent.LAPSED))
            # A favorite resting in its break is frozen; a game whose break leaves it in the pool still takes turns,
            # so it accrues as it pays, or games sharing a break would starve one another.
            if entry.contest in resting and entry.contest not in self._frozen:
                self._ledger.freeze(entry.contest)
                self._frozen.add(entry.contest)
            elif entry.contest not in resting and entry.contest in self._frozen:
                self._ledger.thaw(entry.contest)
                self._frozen.discard(entry.contest)

    # --- deciding --------------------------------------------------------------------------------------------

    def _decide(self, scene: _Scene) -> _Option | None:
        """The ladder's choice at a legal decision."""
        options: dict[Rung, _Option] = {}
        for option in (self._base(scene), self._promotion(scene), self._result(scene), self._auxiliary(scene)):
            if option is not None:
                options[option.rung] = option
        verdict = choose_rung(options, self._run, self._policy.ladder)
        if verdict.rung is None:
            return None
        capped = tuple(refusal.rung for refusal in verdict.refused if refusal.reason is LadderRefusal.NON_BASE_CAP)
        if capped and not self._capped:
            self._capped = True
            self._note(CapNote(at=scene.at, refused=capped))
        if not verdict.reserved:
            return options[verdict.rung]
        self._recovery_owed = True
        option = next(options[rung] for rung in _BASE_RUNGS if rung in options)
        turn = option.air.turn
        turn.rung, turn.reason, turn.tenure = Rung.BASE_RECOVERY, BaseReason.RECOVERY, Tenure.FULL_TURN
        turn.why = self._base_why(scene, turn)
        return _Option(Rung.BASE_RECOVERY, option.air)

    def _base(self, scene: _Scene) -> _Option | None:
        """The weighted base turn: the ledger's choice, or the favorite's own turn back when its break can't host
        the guest the ledger chose."""
        choice = self._ledger.choose(scene.pool, at=scene.now)
        if choice is None:
            return None
        contest, pool, reason = choice.contest, scene.pool, _BASE_REASONS[choice.reason]
        card = scene.base[contest]
        length = _clamped(self._settings.slot, card)
        hosts = scene.hosts if scene.entry(contest).favorite_rank is None else ()
        refusals = [(host, self._leases.guest_refusal(host, length, scene.now)) for host in hosts]
        refused = [(host, why) for host, why in refusals if why is not None]
        if refused:
            host, why = refused[0]
            self._guest_refused(card, hosts, why, scene.at)
            returning = [replace(e, in_break=False) if e.contest == host else e for e in scene.entries]
            pool = targets(returning, self._allocation, policy=self._policy.allocation)
            contest, reason, hosts = host, BaseReason.RETURN, ()
            card = scene.base[contest]
            length = _clamped(self._settings.slot, card)
        rung = Rung.RESCUE if reason is BaseReason.RESCUE else Rung.BASE
        if self._recovery_owed:
            rung, reason = Rung.BASE_RECOVERY, BaseReason.RECOVERY
        turn = Turn(
            role=CardRole.BASE,
            rung=rung,
            length=length,
            minimum=card.timing.min_display.as_timedelta(),
            tenure=Tenure.BREAK_GUEST if hosts and rung is not Rung.BASE_RECOVERY else Tenure.FULL_TURN,
            contest=contest,
            targets=pool,
            reason=reason,
            rescue=choice.rescue if reason is BaseReason.RESCUE else None,
            hosts=hosts,
        )
        turn.why = self._base_why(scene, turn)
        play = self._play_in_turn(scene, contest)
        first = (play, CardRole.PLAY_IN_TURN) if play is not None else (card, CardRole.BASE)
        return _Option(rung, self._air(first[0], first[1], turn, scene.now))

    def _play_in_turn(self, scene: _Scene, contest: LeagueScopedId) -> ScoreboardCard[Any] | None:
        """The game's oldest unaired big play still in its window, which no promotion is waiting for. A favorite's
        own play is the exception: its own turn shows it, promoted or not (`started` settles the promotion), since
        a play of a favorite in action waits on a bar it seldom clears, and its window would run out unseen."""
        plays = [
            card
            for key, card in scene.usable.items()
            if card.kind is CardKind.BIG_PLAY
            and card.contest.id == contest
            and (key not in scene.reserved or self._related(card.contest))
            and not self._aired(key)
        ]
        return min(
            plays, key=lambda c: (scene.view.windows[c.dedupe_key.raw].available, c.dedupe_key.raw), default=None
        )

    def _promotion(self, scene: _Scene) -> _Option | None:
        """The promoted card the broker would start now, if it may air."""
        if not self._settings.promotions:
            return None
        decision = self._broker.decide(scene.moment, scene.now)
        self._retire(decision.retired, scene.at)
        for refusal in decision.refusals:
            self._waited(refusal.key, refusal.revision, refusal.reason, scene.at, decision.bar, decision.context)
        start = decision.start
        if start is None:
            return None
        card = scene.usable.get(start.card.raw)
        if card is None:
            self._waited(start.key, start.revision, OfferSkip.SET_ASIDE, scene.at, decision.bar, decision.context)
            return None
        airtime = self._broker.policy.airtime(start.kind)
        # Started in a favorite's break, it's the break's guest; one that may cut into action needn't yield to it.
        qualifies = start.value is not None and self._broker.policy.bars.qualifies_action(start.value)
        turn = Turn(
            role=CardRole.PROMOTION,
            rung=Rung.PROMOTION,
            length=airtime.full,
            minimum=airtime.minimum,
            tenure=Tenure.BREAK_GUEST if scene.hosts and not qualifies else Tenure.FULL_TURN,
            contest=card.contest.id,
            hosts=scene.hosts,
            candidate=start,
            live_base=bool(scene.pool.games),
        )
        turn.why = self._promotion_why(scene, turn, card)
        return _Option(
            Rung.PROMOTION, Air(card=card, shown=card, role=CardRole.PROMOTION, minimum=airtime.minimum, turn=turn)
        )

    def _result(self, scene: _Scene) -> _Option | None:
        """The first initial final in end order of those that may play here (or the unseen play that decided it). An
        unrelated final waiting for a favorite's break doesn't hold back a favorite's own."""
        base_keys = {card.dedupe_key.raw for card in scene.base.values()}
        context = scene.moment.context
        finals = [
            card
            for key, card in scene.usable.items()
            if card.kind is CardKind.FINAL
            and key not in base_keys
            and key not in scene.reserved
            and not self._aired(key)
            and initial_final_permitted(context=context, related=self._related(card.contest))
        ]
        if not finals:
            return None
        final = min(finals, key=lambda card: (_ended(card, scene), card.dedupe_key.raw))
        before = [key.raw for key in final.after if key.raw in scene.usable and not self._aired(key.raw)]
        if any(key in scene.reserved for key in before):
            return None  # the play that decided it is waiting on its promotion
        card = scene.usable[before[0]] if before else final
        # A decisive play that can't air (its card expired or never staged) no longer holds its final back: its story
        # leads the final when it's kept and fresh, and otherwise the final says it's gone.
        gone = any(key.raw not in scene.usable and not self._aired(key.raw) for key in final.after)
        told = self._summary(scene, final) if gone and not before else None
        said = (
            "the play that decided it"
            if before
            else (
                "final, with the play that decided it"
                if told is not None
                else "final; the play that decided it is gone" if gone else "final"
            )
        )
        why = f"{self._subject(card.contest)}: {said}"
        option = self._ordinary(scene, card, CardRole.RESULT, Rung.INITIAL_RESULT, self._policy.timing.final, why)
        if option is not None and told is not None:
            option.air.summary, option.air.shown = told
        return option

    def _summary(self, scene: _Scene, final: ScoreboardCard[Any]) -> tuple[ResultSummary, ScoreboardCard[Any]] | None:
        """The kept story of the unseen play that decided ``final``, and the final drawn as it, if it may lead the
        final now: not aired by any route, fresh (before the earlier of the final's expiry and the policy's span after
        the final became eligible), from a current source, and drawable here."""
        window = scene.view.windows[final.dedupe_key.raw]
        fresh_until = earliest((window.available + self._policy.summary.fresh_for, window.expires))
        if fresh_until is None or scene.now >= fresh_until or final.stale_since is not None:
            return None
        leading = [
            story
            for _, story in sorted(self._stories.items(), key=lambda item: str(item[0]))
            if story.leads(final) and not self._aired(story.card.dedupe_key.raw)
        ]
        for story in leading:
            drawing = story.drawn(final)
            if drawing is not None and drawing.layout_support.supports(self._profile):
                return story, drawing
        return None

    def _auxiliary(self, scene: _Scene) -> _Option | None:
        """A due pregame, status or repeat while games are live; with none live, the cards on hand at neutral weight,
        the longest unseen first."""
        live = bool(scene.pool.games)
        base_keys = {card.dedupe_key.raw for card in scene.base.values()}
        options: list[tuple[CardRole, ScoreboardCard[Any]]] = []
        for key, card in scene.usable.items():
            if key in scene.reserved or key in base_keys:
                continue
            role = self._role(scene, card) if live or card.kind is CardKind.FINAL else CardRole.NEUTRAL
            if role is not None:
                options.append((role, card))
        if not options and not live:  # nothing fresh to show: a stale game's dim card is the last safe frame
            options = [(CardRole.FALLBACK, card) for card in scene.base.values() if card.dedupe_key.raw in scene.usable]
        if not options:
            return None
        role, card = min(options, key=lambda option: self._unseen(option[1]))
        nominal = self._policy.turn(card, self._settings.slot)
        return self._ordinary(scene, card, role, Rung.AUXILIARY, nominal, self._ordinary_why(role, card))

    def _role(self, scene: _Scene, card: ScoreboardCard[Any]) -> CardRole | None:
        """What a card is due as while games are live (a final, at any time), or None when it isn't due or may not
        play here."""
        key = card.dedupe_key.raw
        now, context = scene.now, scene.moment.context
        shown = self._shown_at.get(key)
        if card.kind is CardKind.PREGAME:
            due = pregame_due(
                until_start=card.contest.scheduled_start - scene.at.utc,
                since_last=now.since(shown) if shown is not None else None,
                policy=self._policy.timing,
            )
            permitted = pregame_permitted(
                live_games=True, base_turns_since=self._base_turns, context=context, policy=self._policy.timing
            )
            return CardRole.PREGAME if due and permitted else None
        if card.kind is CardKind.FINAL:
            final = self._finals.get(key)
            if final is None:
                return None  # an unaired final waits for its turn in end order
            due = final_repeat_due(
                since_first_start=now.since(final.first),
                since_confirmed=now.since(final.confirmed),
                repeats_shown=self._repeats.get(key, 0),
                policy=self._policy.timing,
            )
            permitted = initial_final_permitted(context=context, related=self._related(card.contest))
            return CardRole.REPEAT if due and permitted else None
        if card.kind is CardKind.STATUS:
            opened = self._open.get(key)
            if opened is None:  # a status not yet shown: once, off a favorite's action unless it's the favorite's
                return CardRole.STATUS if self._related(card.contest) or context is not BarContext.ACTION else None
            opened = self._open[key] = opened.update(open_now=context is not BarContext.ACTION, at=now)
            due = favorite_status_due(open_since_last=opened.total(now), policy=self._policy.timing)
            return CardRole.STATUS if due and self._related(card.contest) else None
        return None

    def _unseen(self, card: ScoreboardCard[Any]) -> tuple[float, str]:
        """Never shown first, then the longest since it showed, then by key."""
        shown = self._shown_at.get(card.dedupe_key.raw)
        return (float("-inf") if shown is None else shown.seconds, card.dedupe_key.raw)

    def _ordinary(
        self, scene: _Scene, card: ScoreboardCard[Any], role: CardRole, rung: Rung, nominal: timedelta, why: str
    ) -> _Option | None:
        """An ordinary card's turn, if the favorites' break (when it's one) can host it."""
        length = _clamped(nominal, card)
        hosts = () if self._related(card.contest) else scene.hosts
        for host in hosts:
            refusal = self._leases.guest_refusal(host, length, scene.now)
            if refusal is not None:
                self._guest_refused(card, hosts, refusal, scene.at)
                return None
        turn = Turn(
            role=role,
            rung=rung,
            length=length,
            minimum=min(card.timing.min_display.as_timedelta(), length),
            tenure=Tenure.BREAK_GUEST if hosts else Tenure.FULL_TURN,
            contest=card.contest.id,
            hosts=hosts,
            why=why + self._breaks(hosts),
            live_base=bool(scene.pool.games),
        )
        return _Option(rung, self._air(card, role, turn, scene.now))

    def _related(self, contest: Contest) -> bool:
        """Whether a card is a favorite's own."""
        return isinstance(contest, TeamGame) and self._favorites.of(contest) is not None

    def _air(self, card: ScoreboardCard[Any], role: CardRole, turn: Turn, now: MonotonicTime) -> Air:
        minimum = card.timing.min_display.as_timedelta() if role is CardRole.PLAY_IN_TURN else turn.minimum
        air = Air(card=card, shown=card, role=role, minimum=minimum, turn=turn)
        air.shown = self._present(air, card, now)
        return air

    def _present(self, air: Air, card: ScoreboardCard[Any], now: MonotonicTime) -> ScoreboardCard[Any]:
        """How a card is drawn in its turn: a live football card in a base turn shows a caption it hasn't shown, once
        and only if the turn has room for all of it; otherwise the field alone."""
        if air.summary is not None:  # a final led by its unseen deciding play: drawn as that story, its score current
            return air.summary.drawn(card) or card
        caption = _caption(card)
        turn = air.turn
        if caption is None or not turn.base or air.caption_until is not None:
            return card  # no caption, or it isn't the game's base turn, or its caption is airing
        started = turn.started if turn.started is not None else now
        runway = (started + turn.length).since(now)
        if turn.captioned or self._captions.get(card.contest.id) == caption or runway < self._policy.caption:
            return _without_caption(card)
        if air.since is not None:  # up already: the caption starts now
            self._caption_from(air, caption, now)
        return card

    def _caption_from(self, air: Air, caption: PlaySummary, now: MonotonicTime) -> None:
        air.caption_until = now + self._policy.caption
        air.turn.captioned = True
        self._captions[air.card.contest.id] = caption

    def _begin(self, turn: Turn, air: Air, at: ClockReading, start: MonotonicTime, *, again: bool = False) -> None:
        """A turn's first frame (or its card's frame staying up for it): the run, its guests, the trace. A card
        rotating on its own at neutral weight (``again``) isn't written again."""
        turn.started = turn.accounted = start
        self._current = turn
        self._run = self._run.after(turn.rung)
        if turn.rung.base:
            self._capped = False
        self._grant(turn)
        granted = {grant.contest: grant.id for grant in turn.grants}
        for host in turn.hosts:
            self._leases.hosted(host, start, granted.get(host))
        if turn.hosts:
            self._refused[air.card.dedupe_key.raw] = None
            self._note(GuestNote(at=at, card=air.card.id, hosts=turn.hosts))
        if turn.base:
            self._base_turns = None if self._base_turns is None else self._base_turns + 1
            continues = air.since is not None and air.since < start
            if not continues or turn.reason is not BaseReason.ONLY_CANDIDATE:
                self._turn_noted(turn, air, at, continues=continues)
        elif turn.candidate is None and not again:
            self._card_noted(turn, air, at)
        if air.role is CardRole.PREGAME:
            self._base_turns = 0

    def _showing(self, air: Air, at: MonotonicTime) -> None:
        """``air``'s card begins a turn on air at ``at``: when it last aired, a status's open time marked afresh, a
        repeat counted."""
        key = air.card.dedupe_key.raw
        self._shown_at[key] = at
        if air.card.kind is CardKind.FINAL and key not in self._finals:
            assert self._cards is not None  # a card is up only from an input taken in
            self._finals[key] = _Final(first=at, confirmed=self._cards.windows[key].available)
        if air.role is CardRole.STATUS:
            self._open[key] = OpenTime.mark(open_now=self._context is not BarContext.ACTION, at=at)
        if air.role is CardRole.REPEAT:
            self._repeats[key] = self._repeats.get(key, 0) + 1

    def _end(self, turn: Turn, at: ClockReading) -> None:
        """A turn is over: a base turn is credited with its confirmed time, and a full one clears an owed
        recovery."""
        if turn.ended:
            return
        turn.ended = True
        if self._current is turn:
            self._current = None
        if turn.started is None:
            return
        self._run = self._run.aired(turn.rung, at.monotonic.since(turn.started), live_base=turn.live_base)
        if turn.targets is None:
            return
        completed = at.monotonic.since(turn.started) >= turn.length
        self._credit(turn, at, completed=completed)
        if completed:
            self._recovery_owed = False

    def _split(self, pool: Targets, at: ClockReading) -> None:
        """The games eligible for the base turn on air are changing: credit its time so far to them as they were, and
        account the rest of it by ``pool``, when its own game is in it (else by the targets it was chosen by)."""
        turn = self._current
        if turn is None or turn.targets is None:
            return
        assert turn.started is not None  # a turn is current from its first frame
        if at.monotonic >= turn.started + turn.length:
            return  # it has run its length: all of it was the eligible games' as they were, credited as it ends
        self._credit(turn, at, completed=False)
        if pool.game(turn.contest) is not None:
            turn.targets = pool

    def _credit(self, turn: Turn, at: ClockReading, *, completed: bool) -> None:
        """Credit a base turn's confirmed time from where it was last accounted up to ``at`` (never past its length)."""
        assert turn.targets is not None and turn.started is not None and turn.accounted is not None
        end = min(at.monotonic, turn.started + turn.length)
        aired = max(end.since(turn.accounted), _ZERO)
        turn.accounted = max(end, turn.accounted)
        clamps = self._ledger.served(turn.targets, turn.contest, aired, completed=completed)
        self._accounts |= {game.contest for game in turn.targets.games}
        if clamps:
            self._note(ClampNote(at=at, clamps=clamps))

    def _restart(self, at: ClockReading) -> None:
        """An outage: old leases and every ledger account are void, and the turn on air isn't credited (a non-base
        card's airtime up to the outage still counts toward the cap). The non-base run and an owed recovery carry over:
        the board still owes that full base turn when it has games again."""
        current = self._current
        if current is not None:
            assert current.started is not None  # a turn is current from its first frame
            current.ended = True
            self._run = self._run.aired(current.rung, at.monotonic.since(current.started), live_base=current.live_base)
        self._leases = LeaseBook(self._policy.leases)
        for contest in sorted(self._accounts, key=str):
            self._ledger.retire(contest)
        self._accounts.clear()
        self._frozen.clear()
        self._games.clear()
        self._cards = None
        self._next, self._current = None, None

    def _prune(self, cards: CardView, view: PolicyView, now: MonotonicTime) -> None:
        """Forget what's remembered of cards, games and candidates no longer in the input, so nothing slowly leaks."""
        for memory in (self._shown_at, self._repeats, self._finals, self._open, self._refused, self._drawn):
            for key in [key for key in memory if key not in cards.cards]:
                del memory[key]
        standing = {offer.key for offer in view.offers}
        for promotion in [
            each for each in self._offered if each not in standing and self._broker.waiting(each) is None
        ]:
            del self._offered[promotion]
        present = {card.contest.id for card in cards.cards.values()} | set(view.games)
        self._consumed = {key for key in self._consumed if key.contest in present}
        self._stories = {
            key: story for key, story in self._stories.items() if key.contest in present and now < story.until
        }
        for contest in [contest for contest in self._captions if contest not in view.games]:
            del self._captions[contest]
        bidding = {offer.key.contest for offer in view.offers if offer.kind is PromotionKind.REMINDER}
        self._bids = {contest: bid for contest, bid in self._bids.items() if contest in bidding}
        self._lapsed = {id for id in self._lapsed if id.contest in view.games}
        waiting = {candidate.key for candidate in self._broker.pending()} | {offer.key for offer in view.offers}
        waiting |= {_reminder_key(contest, bid) for contest, bid in self._bids.items()}
        for stale in [promotion for promotion in self._waits if promotion not in waiting]:
            del self._waits[stale]

    def _closed(self, turn: Turn) -> MonotonicTime | None:
        """When the first of the leases a guest was granted in closed (play resumed, the feed marked its end, or a new
        interval superseded it), if one has. A guest fits its lease with the return margin from its first frame, so
        it's never up when its lease runs out; a host whose game is over has no lease left to close."""
        closures = [self._leases.closed(grant) for grant in turn.grants]
        return min((closure for closure in closures if closure is not None), default=None)

    def _grant(self, turn: Turn) -> None:
        """A break's guest handed to the board: its permission in each host's lease, as the decision made at that
        same instant found them, once."""
        if turn.tenure is Tenure.BREAK_GUEST and not turn.grants:
            turn.grants = tuple(self._leases.grant(host, turn.length) for host in turn.hosts)

    def lapsed(self, air: Air, refusal: LeaseRefusal, at: ClockReading) -> None:
        """A break's guest whose grant no longer held when its first frame landed: it spends nothing, and the
        decisions say why (a promotion's room or break gone; a guest's lease, at its first frame)."""
        turn = air.turn
        if turn.candidate is not None:
            reason = PromotionRefusal.NO_ROOM if refusal in _SHORT else PromotionRefusal.BREAK_OVER
            self._note(UnspentNote(at=at, card=air.card.id, key=turn.candidate.key, reason=reason))
        else:
            boundary = GrantBoundary.SWAP
            self._note(GuestNote(at=at, card=air.card.id, hosts=turn.hosts, refusal=refusal, boundary=boundary))

    def grant_refusal(self, air: Air, at: ClockReading) -> LeaseRefusal | None:
        """Whether a break's guest may still begin at ``at`` (None) or why not: each lease it was granted in must be
        the host's lease still, open, with a turn left and room for its whole turn and the return margin. Asked as its
        first frame is confirmed (`started`), and by a board about to send a frame not yet confirmed. A card that
        isn't a guest, or whose turn has begun, always may."""
        turn = air.turn
        if turn.started is not None:
            return None
        for grant in turn.grants:
            refusal = self._leases.holds(grant, turn.length, at.monotonic)
            if refusal is not None:
                return refusal
        return None

    def _remind(self, candidate: PromotionCandidate[DedupeKey], cards: CardView, at: ClockReading) -> None:
        """A no-hit bid's reminder (`BOARD_POLICY.md` §6): when new, then once 60 open seconds have passed since the
        last one showed, three at most, and never offered while a favorite acts. The pipeline offers the bid for as
        long as it stands; each reminder due goes to the broker as a candidate of its own, eligible from when the
        board offers it."""
        contest = candidate.key.contest
        bid = self._bids.setdefault(contest, _Bid())
        now = at.monotonic
        open_now = self._context is not BarContext.ACTION
        if bid.since is not None:
            bid.since = bid.since.update(open_now=open_now, at=now)
        if not open_now or self._reminder_due(bid, now) != now:
            return
        self._offer(replace(candidate, key=_reminder_key(contest, bid), eligible=now), cards, at)

    def _repeat_due(self, key: str) -> MonotonicTime | None:
        """When a final that has gone up is next due to repeat, if it has a repeat left whose whole turn would end by
        its hard end."""
        timing, final = self._policy.timing, self._finals[key]
        shown = self._repeats.get(key, 0)
        if shown >= len(timing.final_repeats):
            return None
        due = final.first + timing.final_repeats[shown]
        return due if due.since(final.confirmed) + timing.final <= timing.final_gone else None

    def _reminder_due(self, bid: _Bid, now: MonotonicTime) -> MonotonicTime | None:
        """When a bid's next reminder falls due: now when it's due already, the instant its open time since the last
        one reaches the interval while the board is open, or None (its reminders are spent, or no open time runs)."""
        timing = self._policy.timing
        if bid.shown >= timing.bid_reminders:
            return None
        if bid.since is None:
            return now
        left = timing.bid_every - bid.since.total(now)
        if left <= _ZERO:
            return now
        return now + left if bid.since.since is not None else None

    def _offer(self, candidate: PromotionCandidate[DedupeKey], cards: CardView, at: ClockReading) -> None:
        card = cards.card(candidate.card.raw)
        if card is None:
            self._waited(candidate.key, candidate.revision, OfferSkip.NOT_IN_VIEW, at)
            return
        skip: OfferSkip | None = None
        if not card.layout_support.supports(self._profile):
            skip = OfferSkip.NOT_DRAWABLE
        elif self._muted(card.contest):
            skip = OfferSkip.MUTED
        elif candidate.kind is not PromotionKind.REMINDER:  # a reminder's card is the standing bid's, as it stands
            skip = self._exact(candidate, card)
        if skip is not None:
            self._waited(candidate.key, candidate.revision, skip, at)
            return
        result = self._broker.offer(candidate, at.monotonic)
        if result.outcome is OfferOutcome.ACCEPTED and candidate.value is not None and self._told(candidate):
            self._broker.retract(candidate.key)  # its story aired before its offer arrived: never twice
            self._waited(candidate.key, candidate.revision, PromotionRefusal.ALREADY_SHOWN, at)
        elif result.outcome is OfferOutcome.REFUSED and result.refusal is not None:
            if result.refusal is PromotionRefusal.RETIRED:
                self._revise_story(candidate, card)
            self._waited(candidate.key, candidate.revision, result.refusal, at)

    def _exact(self, candidate: PromotionCandidate[DedupeKey], card: ScoreboardCard[Any]) -> OfferSkip | None:
        """Whether a scored offer still names its exact event. The card a revision is first offered with is the event
        it describes; once the card under its key shows something else, the offer is refused, and a candidate waiting
        on it withdrawn, until a newer revision describes the card anew."""
        key = candidate.key
        offered = self._offered.get(key)
        if offered is None or candidate.revision > offered.revision:
            self._offered[key] = _Offered(candidate.revision, card)
        elif candidate.revision == offered.revision and not _same_event(offered.card, card):
            self._broker.retract(key)
            return OfferSkip.CHANGED
        return None

    def _told(self, candidate: PromotionCandidate[DedupeKey]) -> bool:
        """Whether a scored moment's story has aired already: promoted, or its event's card committed as it was offered
        (in its own game's turn, or as a result), whenever its offer arrives."""
        drawn = self._drawn.get(candidate.card.raw)
        return candidate.key in self._consumed or (
            drawn is not None and _same_event(self._offered[candidate.key].card, drawn)
        )

    def _reconcile(self, view: PolicyView, at: ClockReading) -> None:
        """Withdraw each waiting candidate the published view no longer offers (a reminder: while its bid stands), so
        a correction that took an offer back keeps it off the board. One past its latest start had run out first:
        it's retired, as a decision would have."""
        offered = {offer.key for offer in view.offers if offer.kind is not PromotionKind.REMINDER}
        bidding = {offer.key.contest for offer in view.offers if offer.kind is PromotionKind.REMINDER}
        gone = [
            candidate
            for candidate in self._broker.pending()
            if not (
                candidate.key.contest in bidding
                if candidate.kind is PromotionKind.REMINDER
                else candidate.key in offered
            )
        ]
        now = at.monotonic
        if any(now > self._broker.policy.latest_start(candidate) for candidate in gone):
            self._retire(self._broker.sweep(now), at)
        for candidate in gone:
            if self._broker.retract(candidate.key) is not None:
                self._waited(candidate.key, candidate.revision, PromotionRefusal.WITHDRAWN, at)

    def _retire(self, retired: Iterable[PromotionCandidate[DedupeKey]], at: ClockReading) -> None:
        """Candidates whose lifetime ran out unseen: written so; a bid's reminder is offered again as its next attempt,
        and a decisive event's or a feat's story is kept for its game's final."""
        for candidate in retired:
            self._waited(candidate.key, candidate.revision, PromotionRefusal.RETIRED, at)
            bid = self._bids.get(candidate.key.contest) if candidate.kind is PromotionKind.REMINDER else None
            if bid is not None:
                bid.attempt += 1  # unshown: the reminder is still due, offered again as the next attempt
            value = candidate.value
            if value is None or not terminal(value.event_class):
                continue  # ordinary action: its state is obsolete once the game has moved on
            offered = self._offered[candidate.key]  # every scored candidate the broker holds was offered through here
            if offered.card.kind is not CardKind.FINAL:  # a feat named on its final airs as that final's own result
                self._stories[candidate.key] = ResultSummary(
                    key=candidate.key,
                    revision=candidate.revision,
                    event=value.event_class,
                    card=offered.card,
                    until=self._broker.policy.hard_end_of(candidate) + self._broker.policy.remember_for,
                )

    def _revise_story(self, candidate: PromotionCandidate[DedupeKey], card: ScoreboardCard[Any]) -> None:
        """A newer revision of an event whose promotion ran out replaces its kept story, on the same final's clock."""
        story = self._stories.get(candidate.key)
        if story is not None and candidate.revision > story.revision:
            self._stories[candidate.key] = replace(story, revision=candidate.revision, card=card)

    # --- notes -----------------------------------------------------------------------------------------------

    def _note(self, note: PolicyDecision) -> None:
        self._notes.append(note)

    def _waited(
        self,
        key: PromotionKey,
        revision: int,
        reason: PromotionRefusal | OfferSkip,
        at: ClockReading,
        bar: float | None = None,
        context: BarContext | None = None,
    ) -> None:
        if self._waits.get(key) == (revision, reason):
            return
        self._waits[key] = (revision, reason)
        self._note(WaitNote(at=at, key=key, revision=revision, reason=reason, bar=bar, context=context))

    def _lease_noted(self, id: BreakId, verdict: LeaseVerdict, at: ClockReading) -> None:
        lease, refusal = verdict.lease, verdict.refusal
        if refusal is LeaseRefusal.DUPLICATE:
            return
        if verdict.superseded is not None:
            self._note(LeaseNote(at=at, id=verdict.superseded.id, event=LeaseEvent.SUPERSEDED))
        if lease is None:
            self._note(
                LeaseNote(
                    at=at, id=id, event=LeaseEvent.REFUSED, refusal=refusal, age=verdict.age, elapsed=verdict.elapsed
                )
            )
        elif lease.closure is not None:
            self._note(LeaseNote(at=at, id=id, event=LeaseEvent.ENDED))
        else:
            remaining = lease.remaining(at.monotonic)
            self._note(
                LeaseNote(
                    at=at,
                    id=id,
                    event=LeaseEvent.OPENED,
                    remaining=remaining,
                    provisional=lease.provisional,
                    turns=lease.turns,
                    age=lease.age,
                    elapsed=verdict.elapsed,
                )
            )

    def _guest_refused(
        self, card: ScoreboardCard[Any], hosts: tuple[LeagueScopedId, ...], refusal: LeaseRefusal, at: ClockReading
    ) -> None:
        key = card.dedupe_key.raw
        if self._refused.get(key) is refusal:
            return
        self._refused[key] = refusal
        self._note(GuestNote(at=at, card=card.id, hosts=hosts, refusal=refusal))

    def _turn_noted(self, turn: Turn, air: Air, at: ClockReading, *, continues: bool) -> None:
        assert turn.targets is not None and turn.reason is not None
        target = turn.targets.game(turn.contest)
        league = turn.targets.league(turn.contest.league)
        assert target is not None and league is not None
        snapshot = self._ledger.snapshot()
        account, league_account = snapshot.game(turn.contest), snapshot.league(turn.contest.league)
        self._note(
            BaseTurnNote(
                at=at,
                policy=self._settings.policy,
                card=air.card.id,
                contest=turn.contest,
                reason=turn.reason,
                continues=continues,
                weight=target.weight,
                target=target.share,
                league_target=league.share,
                within_league=target.within_league,
                actual=account.actual if account is not None else None,
                debt=account.debt if account is not None else 0.0,
                league_debt=league_account.debt if league_account is not None else 0.0,
                rescue=turn.rescue,
                hosts=turn.hosts,
                why=turn.why,
                targets=turn.targets,
            )
        )

    def _card_noted(self, turn: Turn, air: Air, at: ClockReading) -> None:
        note = CardNote(
            at=at,
            policy=self._settings.policy,
            card=air.card.id,
            role=air.role,
            rung=turn.rung,
            hosts=turn.hosts,
            why=turn.why,
        )
        self._note(note)

    def _promotion_noted(self, air: Air, scene: _Scene, at: ClockReading, hold: MonotonicTime | None) -> None:
        turn = air.turn
        assert turn.candidate is not None
        context = scene.moment.context
        self._note(
            PromotionNote(
                at=at,
                policy=self._settings.policy,
                card=air.card.id,
                key=turn.candidate.key,
                kind=turn.candidate.kind,
                value=turn.candidate.value,
                bar=self._broker.policy.bars.for_context(context),
                context=context,
                hold=hold,
                cooldown_until=at.monotonic + self._broker.policy.cooldown,
                hosts=turn.hosts,
                why=turn.why,
            )
        )

    # --- the "why" sentence ----------------------------------------------------------------------------------

    def _subject(self, contest: Contest) -> str:
        """The card's matchup by name: its favorite's team, or "Away at Home"."""
        if not isinstance(contest, TeamGame):
            return f"{contest.league.value.upper()} game"
        ranked = sorted(
            (rank, team.short_name)
            for team in (contest.away, contest.home)
            if (rank := self._favorites.rank(team)) is not None
        )
        return ranked[0][1] if ranked else f"{contest.away.short_name} at {contest.home.short_name}"

    def _breaks(self, hosts: Iterable[LeagueScopedId]) -> str:
        """ "; Braves in inning break", for each favorite whose break the card is in."""
        parts: list[str] = []
        for host in hosts:  # a favorite in a confirmed break: described, with its lease open
            lease = self._leases.lease(host)
            assert lease is not None
            parts.append(f"; {self._subject(self._view.games[host].game)} in {BREAK_WORDS[lease.id.kind]}")
        return "".join(parts)

    def _base_why(self, scene: _Scene, turn: Turn) -> str:
        assert turn.targets is not None and turn.reason is not None
        entry = scene.entry(turn.contest)
        parts: list[str] = []
        if entry.favorite_rank is not None:
            parts.append(rank_words(entry.favorite_rank))
        if entry.leverage.g >= self._policy.close_late:
            parts.append("close late game")
        elif entry.leverage.g < self._policy.lopsided:
            parts.append("lopsided game")
        if entry.stakes.postseason:
            parts.append("postseason")
        parts.extend(_REASON_WORDS.get(turn.reason, ()))
        if self._play_in_turn(scene, turn.contest) is not None:
            parts.append("its big play first")
        resting = [
            aside.contest for aside in turn.targets.set_aside if aside.reason is SetAsideReason.FAVORITE_IN_BREAK
        ]
        said = ", ".join(parts) or "in rotation"
        return f"{self._subject(scene.base[turn.contest].contest)}: {said}" + self._breaks(turn.hosts or resting)

    def _promotion_why(self, scene: _Scene, turn: Turn, card: ScoreboardCard[Any]) -> str:
        assert turn.candidate is not None
        said = event_words(turn.candidate.kind, turn.candidate.value)
        context = scene.moment.context
        if turn.hosts:
            where = self._breaks(turn.hosts)
        elif context is BarContext.ACTION:
            where = "; worth cutting into a favorite's action"
        else:
            where = "; no favorite in action"
        return f"{self._subject(card.contest)}: {said}{where}"

    def _ordinary_why(self, role: CardRole, card: ScoreboardCard[Any]) -> str:
        subject = self._subject(card.contest)
        if isinstance(card.payload, StatusCardPayload):
            return f"{subject}: {status_words(card.payload.status)}"
        return f"{subject}: {_ROLE_WORDS[role]}"

    def interrupted(self, interruption: Interruption, at: ClockReading) -> None:
        """The board was interrupted (`BOARD_POLICY.md` §6, "Wake or output loss"). The turn on air ends at ``at``,
        the last moment its display is known to have shown it, credited to there; nothing queued carries over, nor
        any caption that aired. After a host's gap (a sleep, a stall) what the policy read before it is history: its
        leases close, its action markers go, and the promotions waiting are withdrawn. An output loss keeps them, and
        keeps what the policy has decided: cooldowns, debt and an owed recovery."""
        self._next = None
        current = self._current
        if current is not None:
            started = current.started
            assert started is not None  # a turn is current from its start
            self._end(current, at if at.monotonic >= started else ClockReading(at.project(started), started))
        self._captions.clear()
        if not interruption.host:
            return
        self._leases = LeaseBook(self._policy.leases)
        self._lapsed.clear()
        for contest in sorted({candidate.key.contest for candidate in self._broker.pending()}, key=str):
            self._broker.withdraw(contest)


_BASE_REASONS = MappingProxyType(
    {
        ChoiceReason.WEIGHTED: BaseReason.WEIGHTED,
        ChoiceReason.RESCUE: BaseReason.RESCUE,
        ChoiceReason.ONLY_CANDIDATE: BaseReason.ONLY_CANDIDATE,
    }
)

_REASON_WORDS: Mapping[BaseReason, tuple[str, ...]] = MappingProxyType(
    {
        BaseReason.RESCUE: ("overdue for a turn",),
        BaseReason.ONLY_CANDIDATE: ("the only game on",),
        BaseReason.RECOVERY: ("a full turn after the alerts",),
        BaseReason.RETURN: ("back before its break ends",),
    }
)

_ROLE_WORDS = MappingProxyType(
    {
        CardRole.PREGAME: "starting soon",
        CardRole.REPEAT: "final, again",
        CardRole.STATUS: "paused",
        CardRole.NEUTRAL: "no live game to weigh",
        CardRole.FALLBACK: "the last safe frame; nothing fresh to show",
    }
)

_ROTATING = frozenset({CardRole.NEUTRAL, CardRole.FALLBACK})  # roles a lone card renews quietly


def _reminder_key(contest: LeagueScopedId, bid: _Bid) -> PromotionKey:
    """The promotion a bid's next reminder is: one per attempt, so a shown or retired one is never offered again."""
    return PromotionKey(contest, f"bid reminder {bid.attempt}")


def _clamped(nominal: timedelta, card: ScoreboardCard[Any]) -> timedelta:
    """A turn of ``nominal``, held within the card's own minimum and maximum."""
    timing = card.timing
    return min(max(nominal, timing.min_display.as_timedelta()), timing.max_display.as_timedelta())


def _ended(card: ScoreboardCard[Any], scene: _Scene) -> float:
    """When a final's game ended, for the end order, as the queue orders finals."""
    instant = card.ended.at if card.ended is not None else card.timing.available_at
    return (instant - scene.at.utc).total_seconds()


def _same_event(offered: ScoreboardCard[Any], card: ScoreboardCard[Any]) -> bool:
    """Whether ``card`` still draws what ``offered`` drew: the same kind and payload (its timing or staleness may
    differ)."""
    return offered.kind is card.kind and offered.payload == card.payload


def _caption(card: ScoreboardCard[Any]) -> PlaySummary | None:
    payload = card.payload
    return payload.last_play if isinstance(payload, LiveFootballCardPayload) else None


def _without_caption(card: ScoreboardCard[Any]) -> ScoreboardCard[Any]:
    """A live football card drawn as the field alone."""
    payload = card.payload
    if not isinstance(payload, LiveFootballCardPayload) or payload.last_play is None:
        return card
    return replace(card, payload=replace(payload, last_play=None))
