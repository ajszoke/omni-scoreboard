"""The board policy's scorecard: what a mixed replay under `omni-board-policy/2` gave each league and game, and how.

A `ScoreKeeper` watches every step of `omni.replay.mixed.run_mixed` (pass it as ``watch``) and `ScoreKeeper.card` adds
what the run measured. The replay steps at every instant anything can change, so the board holds what a step painted
until the next step: each figure that counts time integrates that, on the replay's running clock.

**Base airtime** is confirmed base airtime: the time a base turn's card was committed to the display, from the turn's
first frame (its ``base`` decision) until a card of another kind or a frame replaced it. A big play shown first in its
own game's turn is part of the turn.
- A game's **target** is the airtime its share (``q'_g``, after the favorite floor) owed it: each second of base
  airtime times its share under the targets that second's turn was chosen by. Its **actual** is the base airtime that
  showed it. A league's are the same with ``Q_l``.
- A game's **longest wait** is the most eligible base airtime it went without a turn: the base airtime other games
  showed while it was in the pool, since its last turn.
- Its **missed** turns are the turns it was owed and didn't get: the whole slots by which its target exceeds its
  actual airtime.

**Whole-board seconds** count everything on the board by what it was: a card by its kind, a frame by its own.

**The policy's decisions**, from the replay's decision records (`omni.tower.decisions`):
- base turns chosen while the favorite floor bound, debts clamped, rescues;
- promotions made; candidates that waited, by why, each counted once per reason it gave; those that held for a better
  moment; those whose time ran out (expired or retired);
- leases opened and how each ended; guests admitted and refused, by why;
- an **overrun**: a guest (not a reserved recovery) still on air after its host's break closed and past its minimum;
- a **premature break**: a guest admitted in a break the released state the board was given didn't show.

**Requests and rows** are each account's spend, as the quota ledger counted it. **Leverage** is what each base turn's
``G`` rested on; **unknowns** are base turns on unknown leverage and games whose records were never known.
**Spoilers** are cards drawn with a score or a final before their release: zero, always.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from omni.app.quota import ProviderAccount
from omni.cards.base import ScoreboardCard
from omni.core.enum import League
from omni.core.ids import LeagueScopedId
from omni.domain.breaks import BreakId
from omni.policy.allocation import Targets
from omni.policy.leverage import ModelStatus
from omni.policy.promotion import PromotionKey, PromotionRefusal
from omni.policy.view import PolicyView
from omni.replay.mixed import MixedRun, MixedStep
from omni.tower.decisions import (
    BaseReason,
    BaseTurnNote,
    CardNote,
    CardRole,
    ClampNote,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    PolicyDecision,
    PromotionNote,
    WaitNote,
)

__all__ = ["GameScore", "LeagueScore", "ScoreKeeper", "Scorecard"]

_SLOT = 12.0  # seconds: a base turn, for the turns a game was owed
_IN_TURN = frozenset({CardRole.BASE, CardRole.PLAY_IN_TURN})  # a card that carries on its game's base turn
_RAN_OUT = frozenset({PromotionRefusal.EXPIRED, PromotionRefusal.RETIRED})


@dataclass(frozen=True, slots=True, kw_only=True)
class LeagueScore:
    """One league's base airtime: owed by its targets, and shown."""

    league: League
    target: float  # seconds
    actual: float  # seconds


@dataclass(frozen=True, slots=True, kw_only=True)
class GameScore:
    """One game's base airtime, turns and waits."""

    contest: LeagueScopedId
    favorite: int | None  # its favorite rank, if a favorite plays in it
    target: float  # seconds
    actual: float  # seconds
    longest_wait: float  # seconds of eligible base airtime
    missed: int  # whole slots of its target it didn't get


@dataclass(frozen=True, slots=True, kw_only=True)
class Scorecard:
    """What a run under the board policy gave each league and game, and how its decisions went."""

    base: float  # seconds of confirmed base airtime
    board: Mapping[str, float]  # whole-board seconds by what was up: a card's kind, or a frame's
    leagues: tuple[LeagueScore, ...]
    games: tuple[GameScore, ...]
    floor_turns: int
    clamps: int
    rescues: int
    promotions: Mapping[str, int]  # by kind
    waits: Mapping[str, int]  # candidates' reasons, each counted once per candidate
    held: int  # candidates that held for a better moment
    ran_out: int  # candidates whose time ran out: expired or retired
    leases: Mapping[str, int]  # opened, and how each ended, by event
    lease_kinds: Mapping[str, int]  # leases opened, by kind of break
    lease_refusals: Mapping[str, int]
    guests: int
    guest_refusals: Mapping[str, int]
    overruns: tuple[tuple[str, float], ...]  # (card, seconds past its yield)
    premature: tuple[str, ...]  # guests admitted on a break the released state didn't show
    usage: Mapping[ProviderAccount, tuple[int, int]]  # requests and rows
    leverage: Mapping[ModelStatus, int]  # base turns by what G rested on
    unknown_records: tuple[LeagueScopedId, ...]
    spoilers: int

    def league(self, league: League) -> LeagueScore:
        return next(each for each in self.leagues if each.league is league)

    def game(self, contest: LeagueScopedId) -> GameScore:
        return next(each for each in self.games if each.contest == contest)

    def markdown(self, *, title: str) -> str:
        """The scorecard as a markdown page, deterministically."""
        return _markdown(self, title)


@dataclass(slots=True)
class _Turn:
    """The base turn on the board: its game, and the targets it was chosen by."""

    contest: LeagueScopedId
    targets: Targets


@dataclass(slots=True)
class _Guest:
    """A guest on air: its card, when it went up, its minimum, its hosts, and whether it's a reserved recovery."""

    card: str
    started: float
    hosts: tuple[LeagueScopedId, ...]
    recovery: bool
    minimum: float | None = None  # its card's, once it's up


@dataclass(slots=True)
class ScoreKeeper:
    """Watches a mixed run's steps for its scorecard; pass it as ``run_mixed``'s ``watch``."""

    last: float | None = None
    shown: ScoreboardCard[Any] | None = None
    frame: str | None = None
    turn: _Turn | None = None
    guest: _Guest | None = None
    base: float = 0.0
    board: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    target: defaultdict[LeagueScopedId, float] = field(default_factory=lambda: defaultdict(float))
    actual: defaultdict[LeagueScopedId, float] = field(default_factory=lambda: defaultdict(float))
    league_target: defaultdict[League, float] = field(default_factory=lambda: defaultdict(float))
    league_actual: defaultdict[League, float] = field(default_factory=lambda: defaultdict(float))
    wait: defaultdict[LeagueScopedId, float] = field(default_factory=lambda: defaultdict(float))
    longest: defaultdict[LeagueScopedId, float] = field(default_factory=lambda: defaultdict(float))
    favorites: dict[LeagueScopedId, int | None] = field(default_factory=dict)
    floor_turns: int = 0
    clamps: int = 0
    rescues: int = 0
    promotions: Counter[str] = field(default_factory=Counter)
    waits: Counter[str] = field(default_factory=Counter)
    reasons: dict[PromotionKey, set[PromotionRefusal | str]] = field(default_factory=dict)
    leases: Counter[str] = field(default_factory=Counter)
    lease_kinds: Counter[str] = field(default_factory=Counter)
    lease_refusals: Counter[str] = field(default_factory=Counter)
    closed: dict[LeagueScopedId, float] = field(default_factory=dict)  # each host's newest lease closure
    open_breaks: dict[LeagueScopedId, BreakId] = field(default_factory=dict)
    guests: int = 0
    guest_refusals: Counter[str] = field(default_factory=Counter)
    overruns: list[tuple[str, float]] = field(default_factory=list)
    premature: list[str] = field(default_factory=list)
    leverage: Counter[ModelStatus] = field(default_factory=Counter)
    records: dict[LeagueScopedId, bool] = field(default_factory=dict)  # whether each game's records were ever known

    def __call__(self, step: MixedStep) -> None:
        painted = step.painted
        self.observe(
            step.elapsed.total_seconds(),
            decisions=painted.decisions,
            drawn=step.drawn if painted.shown is not None else None,
            frame=painted.frame.value if painted.frame is not None else None,
            view=step.refresh.policy,
        )

    def observe(
        self,
        now: float,
        *,
        decisions: Iterable[PolicyDecision],
        drawn: ScoreboardCard[Any] | None,
        frame: str | None,
        view: PolicyView,
    ) -> None:
        """One step at ``now`` (seconds on the running clock): what the policy decided, the card it drew or the frame
        it put up, and the released view the board was given."""
        if self.last is not None:
            self._held(now - self.last)
        self.last = now
        showing = drawn.id.raw if drawn is not None else None
        if self.guest is not None and self.guest.minimum is not None and showing != self.guest.card:
            self._yielded(self.guest, now)
        for contest, facts in view.games.items():
            self.records[contest] = self.records.get(contest, False) or facts.records is not None
        for decision in decisions:
            seconds = decision.at.monotonic.seconds
            if isinstance(decision, BaseTurnNote):
                self._base(decision)
            elif isinstance(decision, CardNote):
                if decision.role not in _IN_TURN:
                    self.turn = None
                self._hosted(decision.card.raw, seconds, decision.hosts, recovery=False)
            elif isinstance(decision, PromotionNote):
                self.turn = None
                self.promotions[decision.kind.value] += 1
                self._hosted(decision.card.raw, seconds, decision.hosts, recovery=False)
            elif isinstance(decision, WaitNote):
                seen = self.reasons.setdefault(decision.key, set())
                if decision.reason not in seen:
                    seen.add(decision.reason)
                    self.waits[decision.reason.value] += 1
            elif isinstance(decision, LeaseNote):
                self._lease(decision, seconds)
            elif isinstance(decision, GuestNote):
                self._guested(decision, view)
            elif isinstance(decision, ClampNote):
                self.clamps += len(decision.clamps)
        if frame is not None:
            self.shown, self.frame, self.turn = None, frame, None
        elif drawn is not None:
            self.shown, self.frame = drawn, None
        guest = self.guest
        if guest is not None and guest.minimum is None and self.shown is not None and self.shown.id.raw == guest.card:
            guest.minimum = self.shown.timing.min_display.value

    def card(self, run: MixedRun) -> Scorecard:
        """The scorecard, once the run is done."""
        holding = sum(1 for reasons in self.reasons.values() if PromotionRefusal.HOLDING in reasons)
        ran_out = sum(1 for reasons in self.reasons.values() if reasons & _RAN_OUT)
        contests = sorted(set(self.target) | set(self.actual), key=lambda each: (_order(each.league), str(each)))
        games = tuple(
            GameScore(
                contest=contest,
                favorite=self.favorites.get(contest),
                target=self.target[contest],
                actual=self.actual[contest],
                longest_wait=self.longest[contest],
                missed=max(int((self.target[contest] - self.actual[contest]) // _SLOT), 0),
            )
            for contest in contests
        )
        leagues = tuple(
            LeagueScore(league=league, target=self.league_target[league], actual=self.league_actual[league])
            for league in sorted(set(self.league_target) | set(self.league_actual), key=_order)
        )
        return Scorecard(
            base=self.base,
            board=_frozen(self.board),
            leagues=leagues,
            games=games,
            floor_turns=self.floor_turns,
            clamps=self.clamps,
            rescues=self.rescues,
            promotions=_frozen(self.promotions),
            waits=_frozen(self.waits),
            held=holding,
            ran_out=ran_out,
            leases=_frozen(self.leases),
            lease_kinds=_frozen(self.lease_kinds),
            lease_refusals=_frozen(self.lease_refusals),
            guests=self.guests,
            guest_refusals=_frozen(self.guest_refusals),
            overruns=tuple(self.overruns),
            premature=tuple(self.premature),
            usage=MappingProxyType(
                {account: (run.spent(account).requests, run.spent(account).rows) for account in _ACCOUNTS}
            ),
            leverage=MappingProxyType(dict(sorted(self.leverage.items()))),
            unknown_records=tuple(sorted((game for game, known in self.records.items() if not known), key=str)),
            spoilers=len(run.early),
        )

    # --- the steps ---------------------------------------------------------------------------------------------

    def _held(self, seconds: float) -> None:
        """What the board held since the last step, for ``seconds``."""
        if self.frame is not None:
            self.board[f"frame {self.frame}"] += seconds
            return
        if self.shown is None:
            return
        self.board[self.shown.kind.value] += seconds
        turn = self.turn
        if turn is None:
            return
        self.base += seconds
        shown = turn.contest
        self.actual[shown] += seconds
        self.league_actual[shown.league] += seconds
        for league in turn.targets.leagues:
            self.league_target[league.league] += seconds * league.share
        for game in turn.targets.games:
            self.target[game.contest] += seconds * game.share
            if game.contest != shown:
                self.wait[game.contest] += seconds
                self.longest[game.contest] = max(self.longest[game.contest], self.wait[game.contest])
        self.wait[shown] = 0.0

    def _base(self, note: BaseTurnNote) -> None:
        assert note.targets is not None  # the director names every turn's targets
        self.turn = _Turn(note.contest, note.targets)
        self.favorites[note.contest] = note.weight.favorite_rank
        for game in note.targets.games:
            self.favorites.setdefault(game.contest, game.weight.favorite_rank)
        self.leverage[note.weight.status] += 1
        floor = note.targets.floor
        if floor is not None and floor.projected:
            self.floor_turns += 1
        if note.reason is BaseReason.RESCUE:
            self.rescues += 1
        recovery = note.reason is BaseReason.RECOVERY
        self._hosted(note.card.raw, note.at.monotonic.seconds, note.hosts, recovery=recovery)

    def _hosted(self, card: str, at: float, hosts: tuple[LeagueScopedId, ...], *, recovery: bool) -> None:
        """A turn began: a guest of ``hosts``' breaks, or not one."""
        self.guest = _Guest(card, at, hosts, recovery) if hosts else None

    def _lease(self, note: LeaseNote, at: float) -> None:
        self.leases[note.event.value] += 1
        contest = note.id.contest
        if note.event is LeaseEvent.OPENED:
            self.lease_kinds[note.id.kind.value] += 1
            self.open_breaks[contest] = note.id
            self.closed.pop(contest, None)
        elif note.event is LeaseEvent.REFUSED:
            self.lease_refusals[note.refusal.value if note.refusal is not None else "-"] += 1
        elif self.open_breaks.get(contest) == note.id:
            del self.open_breaks[contest]
            self.closed[contest] = at

    def _guested(self, note: GuestNote, view: PolicyView) -> None:
        if note.refusal is not None:
            self.guest_refusals[note.refusal.value] += 1
            return
        self.guests += 1
        for host in note.hosts:
            facts = view.facts(host)
            shown = facts is not None and any(not evidence.ended for evidence in facts.breaks)
            if not shown:
                self.premature.append(f"{note.card.raw} in {host}")

    def _yielded(self, guest: _Guest, now: float) -> None:
        """A guest came down at ``now``: an overrun if that was past its yield, when its host's break closed (or its
        minimum's end, if that's later). A reserved recovery finishes its turn, so it never overruns."""
        self.guest = None
        assert guest.minimum is not None
        closures = [self.closed[host] for host in guest.hosts if self.closed.get(host, -1.0) >= guest.started]
        if guest.recovery or not closures:
            return
        due = max(min(closures), guest.started + guest.minimum)
        if now > due + _TOLERANCE:
            self.overruns.append((guest.card, round(now - due, 3)))


_TOLERANCE = 1 / 32  # seconds: two of the replay's steps, past which a guest still up is late
_ACCOUNTS = (ProviderAccount.MLB_STATSAPI, ProviderAccount.NFLMETA)
_LEAGUES = tuple(League)


def _order(league: League) -> int:
    return _LEAGUES.index(league)


def _frozen(counts: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return MappingProxyType(dict(sorted(counts.items())))


def _markdown(card: Scorecard, title: str) -> str:
    base = card.base or 1.0
    lines = [f"# {title}", "", f"Confirmed base airtime: {card.base:.1f} s.", ""]
    lines += ["## Leagues", "", "| League | Target | Actual |", "|---|---:|---:|"]
    lines += [
        f"| {each.league.value} | {each.target:.1f} s ({each.target / base:.2%}) | {each.actual:.1f} s"
        f" ({each.actual / base:.2%}) |"
        for each in card.leagues
    ]
    lines += ["", "## Games", ""]
    lines += ["| Game | Favorite | Target | Actual | Longest wait | Missed turns |", "|---|---|---:|---:|---:|---:|"]
    lines += [
        f"| {each.contest} | {each.favorite if each.favorite is not None else '-'} | {each.target:.1f} s"
        f" ({each.target / base:.2%}) | {each.actual:.1f} s ({each.actual / base:.2%})"
        f" | {each.longest_wait:.1f} s | {each.missed} |"
        for each in card.games
    ]
    lines += ["", "## Whole board", "", "| Up | Seconds |", "|---|---:|"]
    lines += [f"| {what} | {seconds:.1f} |" for what, seconds in card.board.items()]
    lines += ["", "## Decisions", ""]
    lines += [
        f"- Base turns while the favorite floor bound: {card.floor_turns}; debts clamped: {card.clamps};"
        f" rescues: {card.rescues}.",
        f"- Promotions made: {_counts(card.promotions)}. Held for a better moment: {card.held}."
        f" Ran out of time: {card.ran_out}.",
        f"- Candidates' reasons: {_counts(card.waits)}.",
        f"- Leases: {_counts(card.leases)}; opened by kind: {_counts(card.lease_kinds)};"
        f" evidence refused: {_counts(card.lease_refusals)}.",
        f"- Guests admitted: {card.guests}; refused: {_counts(card.guest_refusals)}.",
        f"- Overruns: {len(card.overruns)}"
        + "".join(f"; {name} {seconds:.3f} s" for name, seconds in card.overruns)
        + f". Premature breaks: {len(card.premature)}.",
    ]
    lines += ["", "## Sources", "", "| Account | Requests | Rows |", "|---|---:|---:|"]
    lines += [f"| {account.label} | {requests} | {rows} |" for account, (requests, rows) in card.usage.items()]
    lines += [
        "",
        f"Leverage behind each base turn: {_counts({status.value: n for status, n in card.leverage.items()})}."
        f" Games whose records were never known: {len(card.unknown_records)}.",
        "",
        f"Spoilers: {card.spoilers}.",
        "",
    ]
    return "\n".join(lines)


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "none"
