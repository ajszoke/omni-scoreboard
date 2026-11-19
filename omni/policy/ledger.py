"""Base turns by debt: who is owed airtime, who goes next, and who is overdue (`BOARD_POLICY.md` §3.5).

The ledger keeps an account per league and per game. It reads only **confirmed** base airtime: what the
display committed, as the board reports it, never a cue. Over each confirmed turn of ``dt`` seconds:
- every eligible league is owed its share ``Q_l * dt``, and every eligible game in the league shown is owed
  its share of that league, ``p_g * dt``;
- the league and the game shown pay ``dt``.

So a debt is airtime owed: positive behind its target, negative ahead. Each is held within +-24 s, and every
clamp is reported for the trace. The caller credits only time that counts: nothing during alerts, results,
auxiliary cards, an unavailable display or a source outage.

**Choosing a turn:** the league with the largest ``debt + slot * Q_l``, then the game within it with the
largest ``debt + slot * p_g``. Scores are compared to the microsecond. Ties go to the longest eligible wait
since service, then to the league's declared order or the game's id.

**Rescue:** before weighing, an item that has missed too many full turns goes first, the longest-waiting
first. A league is overdue at ``max(6, L, ceil(2/Q_l))`` misses and a game at ``max(n_l, ceil(2/p_g))``
misses, a game's misses counting its own league's turns. Only completed full turns while eligible count; a
confirmed turn of any length resets the shown items.

**Breaks and endings:** a frozen game (a confirmed break) neither accrues, ages nor misses, and freezing
clears its streak. A final, a mute, an expiry or a lost service retires a game; after an outage it starts
again at zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from omni.core.clock import MonotonicTime
from omni.core.enum import League, StrEnumMixin
from omni.core.ids import LeagueScopedId
from omni.policy.allocation import AllocationPolicy, GameTarget, LeagueTarget, Targets

__all__ = ["Account", "AirtimeLedger", "BaseChoice", "ChoiceReason", "DebtClamp", "LedgerSnapshot", "Rescue"]

_LEAGUE_ORDER = {league: index for index, league in enumerate(League)}
_DEFAULT = AllocationPolicy()


class ChoiceReason(StrEnumMixin, str, Enum):
    """Why the ledger chose a base turn's game."""

    WEIGHTED = "weighted"  # the largest debt plus a slot of its target
    RESCUE = "rescue"  # an overdue league or game
    ONLY_CANDIDATE = "only candidate"


@dataclass(frozen=True, slots=True, kw_only=True)
class Account:
    """One league's or one game's standing, in seconds: its debt, its misses in a row, its eligible wait since
    service, and the airtime it got of the base airtime while it was eligible."""

    league: League
    contest: LeagueScopedId | None  # None for the league's own account
    debt: float
    misses: int
    wait: float
    airtime: float
    eligible: float
    frozen: bool

    @property
    def actual(self) -> float | None:
        """Its actual share of the base airtime while it was eligible; None before any."""
        return self.airtime / self.eligible if self.eligible > 0 else None


@dataclass(frozen=True, slots=True)
class DebtClamp:
    """A debt held at the limit: whose (``contest`` None for a league's), what it came to, and the limit."""

    league: League
    contest: LeagueScopedId | None
    debt: float
    limit: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Rescue:
    """An overdue league (``contest`` None) or game, its misses and the threshold they reached."""

    league: League
    contest: LeagueScopedId | None
    misses: int
    threshold: int
    wait: float


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseChoice:
    """The game for the next base turn, why, and everything behind it, for the trace and the "why" line.

    ``continues`` says the game shown last won again, so the board keeps its frame rather than redraw it.
    ``clamps`` are the debts held at the limit since the previous choice.
    """

    contest: LeagueScopedId
    reason: ChoiceReason
    at: MonotonicTime
    continues: bool
    league_target: LeagueTarget
    game_target: GameTarget
    league_account: Account
    game_account: Account
    rescue: Rescue | None
    clamps: tuple[DebtClamp, ...]

    @property
    def league(self) -> League:
        return self.contest.league


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerSnapshot:
    """Every account, leagues in declared order and games by league and id, and the game shown last."""

    leagues: tuple[Account, ...]
    games: tuple[Account, ...]
    last_shown: LeagueScopedId | None

    def league(self, league: League) -> Account | None:
        return next((each for each in self.leagues if each.league is league), None)

    def game(self, contest: LeagueScopedId) -> Account | None:
        return next((each for each in self.games if each.contest == contest), None)


@dataclass(slots=True)
class _Entry:
    debt: float = 0.0
    misses: int = 0
    wait: float = 0.0
    airtime: float = 0.0
    eligible: float = 0.0
    frozen: bool = False

    def account(self, league: League, contest: LeagueScopedId | None) -> Account:
        return Account(
            league=league,
            contest=contest,
            debt=self.debt,
            misses=self.misses,
            wait=self.wait,
            airtime=self.airtime,
            eligible=self.eligible,
            frozen=self.frozen,
        )


def _ceil(value: float) -> int:
    """``ceil``, without a float's last-digit noise pushing a whole number up one."""
    return math.ceil(round(value, 9))


class AirtimeLedger:
    """The base turns' debts and rescue, across turns. Survives a settings reload; `retire` ends a game's."""

    def __init__(self, *, policy: AllocationPolicy = _DEFAULT) -> None:
        self._policy = policy
        self._leagues: dict[League, _Entry] = {}
        self._games: dict[LeagueScopedId, _Entry] = {}
        self._last: LeagueScopedId | None = None
        self._clamps: list[DebtClamp] = []  # since the last choice

    # --- choosing -------------------------------------------------------------------------------------------

    def choose(self, targets: Targets, *, at: MonotonicTime) -> BaseChoice | None:
        """The game for the next base turn of ``targets.settings.slot``; None when the pool is empty."""
        if not targets.games:
            return None
        self._open(targets)
        clamps, self._clamps = tuple(self._clamps), []
        slot = targets.settings.slot.total_seconds()
        rescue: Rescue | None = None
        if len(targets.games) == 1:
            game, reason = targets.games[0], ChoiceReason.ONLY_CANDIDATE
        else:
            overdue = self._overdue(targets)
            if overdue:
                # An overdue league never holds an overdue game that waited less: the league turns a game
                # missed reset its league's wait. So a rescued league's game is weighed as usual.
                rescue, reason = overdue[0], ChoiceReason.RESCUE
                if rescue.contest is None:
                    game = self._pick_game(targets, rescue.league, slot)
                else:
                    game = self._target(targets, rescue.contest)
            else:
                reason = ChoiceReason.WEIGHTED
                game = self._pick_game(targets, self._pick_league(targets, slot), slot)
        league_target = self._league_target(targets, game.league)
        return BaseChoice(
            contest=game.contest,
            reason=reason,
            at=at,
            continues=game.contest == self._last,
            league_target=league_target,
            game_target=game,
            league_account=self._leagues[game.league].account(game.league, None),
            game_account=self._games[game.contest].account(game.league, game.contest),
            rescue=rescue,
            clamps=clamps,
        )

    def _score(self, entry: _Entry, share: float, slot: float) -> float:
        return round(entry.debt + slot * share, self._policy.debt_digits)

    def _waited(self, entry: _Entry) -> float:
        return round(entry.wait, self._policy.debt_digits)

    def _pick_league(self, targets: Targets, slot: float) -> League:
        best = min(
            targets.leagues,
            key=lambda each: (
                -self._score(self._leagues[each.league], each.share, slot),
                -self._waited(self._leagues[each.league]),
                _LEAGUE_ORDER[each.league],
            ),
        )
        return best.league

    def _pick_game(self, targets: Targets, league: League, slot: float) -> GameTarget:
        return min(
            targets.in_league(league),
            key=lambda each: (
                -self._score(self._games[each.contest], each.within_league, slot),
                -self._waited(self._games[each.contest]),
                str(each.contest),
            ),
        )

    def _overdue(self, targets: Targets) -> list[Rescue]:
        """Every overdue league and game, the longest-waiting first (then leagues before games, then by id)."""
        policy = self._policy
        found: list[Rescue] = []
        for league in targets.leagues:
            entry = self._leagues[league.league]
            threshold = max(policy.rescue_misses, len(targets.leagues), _ceil(policy.rescue_turns / league.share))
            if entry.misses >= threshold:
                found.append(
                    Rescue(
                        league=league.league, contest=None, misses=entry.misses, threshold=threshold, wait=entry.wait
                    )
                )
        for game in targets.games:
            entry = self._games[game.contest]
            size = self._league_target(targets, game.league).games
            threshold = max(size, _ceil(policy.rescue_turns / game.within_league))
            if entry.misses >= threshold:
                found.append(
                    Rescue(
                        league=game.league,
                        contest=game.contest,
                        misses=entry.misses,
                        threshold=threshold,
                        wait=entry.wait,
                    )
                )
        return sorted(
            found,
            key=lambda each: (
                -round(each.wait, policy.debt_digits),
                each.contest is not None,
                _LEAGUE_ORDER[each.league],
                str(each.contest),
            ),
        )

    # --- crediting ------------------------------------------------------------------------------------------

    def served(
        self, targets: Targets, contest: LeagueScopedId, aired: timedelta, *, completed: bool
    ) -> tuple[DebtClamp, ...]:
        """Credit ``aired`` of confirmed base airtime showing ``contest``, under the ``targets`` its turn was
        chosen by. ``completed`` says it ran its full turn, so every other eligible item missed one; a
        shortened turn credits only its own time. Returns the debts it clamped.

        A turn the display never confirmed (no airtime) changes nothing.
        """
        seconds = aired.total_seconds()
        if seconds < 0:
            raise ValueError("airtime can't be negative")
        self._target(targets, contest)
        if seconds == 0:
            return ()
        self._open(targets)
        shown = contest.league
        for league in targets.leagues:
            entry = self._leagues[league.league]
            entry.debt += league.share * seconds
            entry.eligible += seconds
            entry.wait += seconds
            if completed and league.league is not shown:
                entry.misses += 1
        for game in targets.games:
            entry = self._games[game.contest]
            entry.eligible += seconds
            if entry.frozen:
                continue
            entry.wait += seconds
            if game.league is shown:
                entry.debt += game.within_league * seconds
                if completed and game.contest != contest:
                    entry.misses += 1
        for entry in (self._leagues[shown], self._games[contest]):
            entry.debt -= seconds
            entry.airtime += seconds
            entry.wait = 0.0
            entry.misses = 0
        self._last = contest
        clamps = self._clamp()
        self._clamps.extend(clamps)
        return clamps

    def _clamp(self) -> tuple[DebtClamp, ...]:
        limit = self._policy.debt_limit.total_seconds()
        accounts: list[tuple[League, LeagueScopedId | None, _Entry]] = [
            (league, None, entry) for league, entry in self._leagues.items()
        ]
        accounts.extend((contest.league, contest, entry) for contest, entry in self._games.items())
        clamps: list[DebtClamp] = []
        for league, contest, entry in accounts:
            if abs(entry.debt) > limit:
                clamps.append(DebtClamp(league, contest, entry.debt, math.copysign(limit, entry.debt)))
                entry.debt = math.copysign(limit, entry.debt)
        return tuple(clamps)

    # --- breaks and endings ---------------------------------------------------------------------------------

    def freeze(self, contest: LeagueScopedId) -> None:
        """A confirmed break: hold the game's debt and wait, and clear its misses, until `thaw`."""
        entry = self._games.setdefault(contest, _Entry())
        entry.frozen, entry.misses = True, 0

    def thaw(self, contest: LeagueScopedId) -> None:
        """The break is over: the game accrues, ages and misses again."""
        entry = self._games.get(contest)
        if entry is not None:
            entry.frozen = False

    def retire(self, contest: LeagueScopedId) -> None:
        """A final, a mute, an expiry or a lost service: the game's account ends, and its league's with its
        last game. If it comes back (after an outage, say) it starts at zero."""
        self._games.pop(contest, None)
        if not any(each.league is contest.league for each in self._games):
            self._leagues.pop(contest.league, None)
        if self._last == contest:
            self._last = None

    # --- reading --------------------------------------------------------------------------------------------

    def snapshot(self) -> LedgerSnapshot:
        """Every account as it stands, for the trace."""
        return LedgerSnapshot(
            leagues=tuple(
                self._leagues[league].account(league, None)
                for league in sorted(self._leagues, key=_LEAGUE_ORDER.__getitem__)
            ),
            games=tuple(
                self._games[contest].account(contest.league, contest)
                for contest in sorted(self._games, key=lambda each: (_LEAGUE_ORDER[each.league], str(each)))
            ),
            last_shown=self._last,
        )

    # --- internals ------------------------------------------------------------------------------------------

    def _open(self, targets: Targets) -> None:
        """Open an account at zero for each league and game the targets name that has none."""
        for league in targets.leagues:
            self._leagues.setdefault(league.league, _Entry())
        for game in targets.games:
            self._games.setdefault(game.contest, _Entry())

    @staticmethod
    def _target(targets: Targets, contest: LeagueScopedId) -> GameTarget:
        game = targets.game(contest)
        if game is None:
            raise ValueError(f"{contest} isn't in the targets; credit a turn under the targets it was chosen by")
        return game

    @staticmethod
    def _league_target(targets: Targets, league: League) -> LeagueTarget:
        return next(each for each in targets.leagues if each.league is league)  # every game's league has one
