"""A game's plays so far, and what a board reads off them: the strip's events, players' lines, timeouts.

The feed publishes a play, then corrects it, sometimes minutes later and sometimes more than once. The
ledger keeps each play's newest revision and recomputes everything from those, so a correction flows
into every total. A correction that changes what happened (a review's ruling, the try after a touchdown)
is announced as an event of its own; the rest (a spot, a drive's result) are absorbed quietly. A play
first seen after a later one has already been announced is late: the field has moved past it, so it is
absorbed for the totals but held back from the strip (`held_back`).

Players' lines come from the play text. Timeouts come from the text too, as the live feed carries no
counts: a club's timeout reads "Timeout #2 by PIT", and a lost challenge "(Timeout #1.)".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from omni.core.enum import HomeAway
from omni.domain.football import (
    CHALLENGES_PER_GAME,
    PlaySummary,
    PlayTone,
    TeamChallenges,
    TeamTimeouts,
    other_side,
)
from omni.domain.teams import Team
from omni.events.football import (
    _NAME,
    FieldGoalResult,
    FootballEvent,
    FootballEventKind,
    FootballPlay,
    FootballPlayType,
    ReviewVerdict,
    conversion_headline,
    review_headline,
    summarize_play,
)
from omni.events.football_clubs import club_code

__all__ = ["PlayLedger", "PlayerLine"]

_TIMEOUTS_PER_HALF = 3
_TIMEOUTS_IN_OVERTIME = 2
_REGULATION_PERIODS = 4

_PASS = re.compile(rf"^(?P<passer>{_NAME}) pass (?!incomplete)(?:[a-z ]+ )?to (?P<catcher>{_NAME})")
_INCOMPLETE = re.compile(rf"^(?P<passer>{_NAME}) pass incomplete")
_INTERCEPTED = re.compile(rf"^(?P<passer>{_NAME}) pass .*?INTERCEPTED by (?P<defender>{_NAME})")
_SACK = re.compile(rf"^(?P<passer>{_NAME}) sacked .*?\((?P<by>[^)]*)\)")
_RUN = re.compile(rf"^(?P<runner>{_NAME}) (?!pass|sacked|kicks|punts|extra point|\d+ yard field goal)")
_RETURN = re.compile(rf"(?:kicks|punts) .*?\. (?P<returner>{_NAME}) to [A-Z]{{2,3}} -?\d+ for (?P<yards>-?\d+|no gain)")
_FUMBLE = re.compile(rf"(?P<player>{_NAME}) FUMBLES")
_TACKLERS = re.compile(r"\((?P<names>[^()]*)\)\.?\s*$")
_NAMES = re.compile(_NAME)
_SNAPS = frozenset(
    {
        FootballPlayType.PASS_RECEPTION,
        FootballPlayType.PASS_INCOMPLETION,
        FootballPlayType.PASS_INTERCEPTION_RETURN,
        FootballPlayType.PASSING_TOUCHDOWN,
        FootballPlayType.INTERCEPTION_RETURN_TOUCHDOWN,
        FootballPlayType.RUSH,
        FootballPlayType.RUSHING_TOUCHDOWN,
        FootballPlayType.SACK,
        FootballPlayType.FUMBLE_RECOVERY_OWN,
        FootballPlayType.FUMBLE_RECOVERY_OPPONENT,
        FootballPlayType.FUMBLE_RETURN_TOUCHDOWN,
        FootballPlayType.SAFETY,
        FootballPlayType.PENALTY,
        FootballPlayType.OTHER,
    }
)
_KICKOFFS = frozenset({FootballPlayType.KICKOFF, FootballPlayType.KICKOFF_RETURN_TOUCHDOWN})
KICKOFF_YARDS = 35  # a kickoff goes from the kicking team's 35
SAFETY_KICK_YARDS = 20  # the free kick after a safety goes from the kicking team's 20
_FIELD = 100
# A penalty's enforcement: who was flagged, how far, and where ("at CIN 35", "on the kickoff").
_ENFORCED = re.compile(
    r"PENALTY on (?P<team>[A-Z]{2,3})-[^,]*, [^,]+, (?P<yards>\d+) yards?, enforced (?P<where>.*?)(?: - No Play|\.(?:\s|$)|$)"
)
_AT_SPOT = re.compile(r"\bat (?P<side>[A-Z]{2,3}) (?P<line>\d{1,2})\b")
_STOPPAGES = frozenset(
    {
        FootballPlayType.TIMEOUT,
        FootballPlayType.OFFICIAL_TIMEOUT,
        FootballPlayType.TWO_MINUTE_WARNING,
        FootballPlayType.END_PERIOD,
        FootballPlayType.END_OF_HALF,
        FootballPlayType.END_OF_GAME,
        FootballPlayType.COIN_TOSS,
    }
)
# Scores by the team without the ball, whose rows still name the other team as having it.
_DEFENSIVE_SCORES = frozenset(
    {
        FootballPlayType.INTERCEPTION_RETURN_TOUCHDOWN,
        FootballPlayType.FUMBLE_RETURN_TOUCHDOWN,
        FootballPlayType.PUNT_RETURN_TOUCHDOWN,
        FootballPlayType.BLOCKED_PUNT,
        FootballPlayType.BLOCKED_FIELD_GOAL,
    }
)
_KICKS = frozenset(
    {
        FootballPlayType.KICKOFF,
        FootballPlayType.KICKOFF_RETURN_TOUCHDOWN,
        FootballPlayType.PUNT,
        FootballPlayType.PUNT_RETURN_TOUCHDOWN,
    }
)


@dataclass(slots=True)
class PlayerLine:
    """One player's game so far."""

    name: str
    completions: int = 0
    attempts: int = 0
    passing_yards: int = 0
    passing_touchdowns: int = 0
    interceptions_thrown: int = 0
    carries: int = 0
    rushing_yards: int = 0
    receptions: int = 0
    receiving_yards: int = 0
    returns: int = 0
    return_yards: int = 0
    touchdowns: int = 0  # scored with the ball: rushing, receiving, returns
    fumbles: int = 0
    tackles: int = 0
    sacks: float = 0.0
    interceptions: int = 0

    @property
    def all_purpose_yards(self) -> int:
        return self.rushing_yards + self.receiving_yards + self.return_yards

    def describe(self) -> str:
        """The line a board shows, leading with what the player mostly did."""
        parts: list[str] = []
        if self.attempts:
            parts.append(f"{self.completions}/{self.attempts} {self.passing_yards} YDS")
            parts += _counts((self.passing_touchdowns, "TD"), (self.interceptions_thrown, "INT"))
            if self.carries:
                parts.append(f"{self.rushing_yards} RUSH")
        elif self.carries or self.receptions or self.returns:
            parts.append(f"{self.all_purpose_yards} YDS")
            parts += _counts((self.touchdowns, "TD"), (self.receptions, "REC"), (self.carries, "CAR"))
        if self.tackles or self.sacks or self.interceptions:
            sacks = f"{self.sacks:g} SCK" if self.sacks else ""
            parts += _counts((self.tackles, "TKL")) + ([sacks] if sacks else []) + _counts((self.interceptions, "INT"))
        parts += _counts((self.fumbles, "FUM"))
        return " ".join([self.name, *parts])


def _counts(*pairs: tuple[int, str]) -> list[str]:
    return [f"{count} {label}" for count, label in pairs if count]


@dataclass(slots=True)
class _Totals:
    lines: dict[str, PlayerLine] = field(default_factory=dict)

    def __getitem__(self, name: str) -> PlayerLine:
        return self.lines.setdefault(name, PlayerLine(name))


class PlayLedger:
    """One game's plays, newest revision each, and what follows from them."""

    def __init__(self, away: Team, home: Team) -> None:
        self._sides = {away.abbreviation: HomeAway.AWAY, home.abbreviation: HomeAway.HOME}
        # How play text names each club in a challenge: its city, its nickname, or both.
        self._names: dict[str, str] = {}
        for team in (away, home):
            city = team.display_name.removesuffix(team.short_name).strip()
            for name in (city, team.short_name, team.display_name, team.abbreviation):
                self._names[name] = team.abbreviation
        self._plays: dict[int, FootballPlay] = {}
        self._totals = _Totals()
        self.held_back: tuple[FootballEvent, ...] = ()  # the last batch's late plays, absorbed but not announced
        self._late: set[int] = set()  # every play held back as late, and so never shown

    def absorb(self, plays: Iterable[FootballPlay], *, announce: bool = True) -> list[FootballEvent]:
        """Take in a batch of new and corrected plays; the events it brings, in the order they happened.

        With ``announce`` off (a board catching up on a game already under way), only the latest is
        returned, so the strip starts on the last play rather than replaying the game.
        """
        newest = self.latest  # a new play older than this is late
        arrivals: dict[int, FootballPlay] = {}  # new plays, at their latest revision in the batch
        corrected: dict[int, tuple[FootballPlay, FootballPlay]] = {}  # each play's revision before and after
        for play in plays:
            before = self._plays.get(play.number)
            if before is not None and play.revision <= before.revision:
                continue
            self._plays[play.number] = play
            if before is None or play.number in arrivals:
                arrivals[play.number] = play
            else:
                corrected[play.number] = (corrected.get(play.number, (before, play))[0], play)
        self._recount()  # before any event reads a player's line
        late = [number for number in sorted(arrivals) if number < newest]
        self._late.update(late)
        events: list[FootballEvent] = []
        held: list[FootballEvent] = []
        for number, (before, after) in corrected.items():
            # A late play's corrections stay as unseen as the play.
            (held if number in self._late else events).extend(self._correction_events(before, after))
        held += [event for number in late if (event := self._event(arrivals[number]))]
        self.held_back = tuple(held)
        events += [event for number in sorted(arrivals) if number > newest and (event := self._event(arrivals[number]))]
        return events if announce else events[-1:]

    def timeouts(self, period: int) -> TeamTimeouts:
        """Timeouts each club has left in the half (or overtime) that ``period`` is in."""
        half = _half(period)
        allowed = _TIMEOUTS_IN_OVERTIME if half > 2 else _TIMEOUTS_PER_HALF
        used = {HomeAway.AWAY: 0, HomeAway.HOME: 0}
        for play in self._plays.values():
            if _half(play.quarter) != half:
                continue
            text = play.text
            calls: list[tuple[HomeAway | None, int]] = []
            if (call := text.timeout) is not None:
                calls.append((self._sides.get(call.team), call.number))
            review = text.review
            if review is not None and review.charged_timeout is not None and review.challenger is not None:
                calls.append((self._side_named(review.challenger), review.charged_timeout))
            for side, number in calls:
                if side is not None:
                    used[side] = max(used[side], number)
        return TeamTimeouts(away=max(allowed - used[HomeAway.AWAY], 0), home=max(allowed - used[HomeAway.HOME], 0))

    def challenges(self) -> TeamChallenges:
        """Challenges each club has left; winning either of the first two earns a third."""
        made = {HomeAway.AWAY: 0, HomeAway.HOME: 0}
        won = {HomeAway.AWAY: 0, HomeAway.HOME: 0}
        for play in self._plays.values():
            review = play.text.review
            if review is None or review.challenger is None:
                continue
            side = self._side_named(review.challenger)
            if side is not None:
                made[side] += 1
                won[side] += review.verdict is ReviewVerdict.REVERSED

        def left(side: HomeAway) -> int:
            earned = CHALLENGES_PER_GAME + (won[side] > 0)
            return min(max(earned - made[side], 0), CHALLENGES_PER_GAME)

        return TeamChallenges(away=left(HomeAway.AWAY), home=left(HomeAway.HOME))

    def second_half_receiver(self) -> HomeAway | None:
        """Who gets the ball to start the second half: whoever kicked off the first.

        The toss loser chooses for the second half and takes the ball, which is the team that kicked off
        the first unless the toss winner chose to kick, a rarity this doesn't cover. A kickoff's row
        names the receiving team as the one with the ball.
        """
        kickoffs = [play for play in self._plays.values() if play.play_type in _KICKOFFS and play.quarter == 1]
        opening = min(kickoffs, key=lambda play: play.number, default=None)
        if opening is None or opening.offense not in self._sides:
            return None
        return other_side(self._sides[opening.offense])

    def next_kickoff(self, period: int) -> tuple[HomeAway | None, int | None] | None:
        """Whether a kickoff is next in ``period``: the kicking side and its own yard line, or None.

        Either can be unknown (None) when the coin toss decides: before the opening kickoff, and in
        overtime. A kickoff follows a score once its try is done, and opens the second half. A later
        overtime period carries on from the one before, so it doesn't open with one.
        """
        snaps = sorted(
            (play for play in self._plays.values() if play.quarter == period and play.play_type not in _STOPPAGES),
            key=lambda play: play.number,
        )
        if not snaps:
            if period == 1 or period == _REGULATION_PERIODS + 1:
                return None, None
            if period == 3:
                receiver = self.second_half_receiver()
                return (other_side(receiver), KICKOFF_YARDS) if receiver is not None else (None, None)
            return None
        last = snaps[-1]
        if last.play_type in _KICKOFFS and last.text.nullified:
            return self._rekick(last)
        if last.play_type is FootballPlayType.SAFETY:
            kicker = self._sides.get(last.offense or "")  # the team caught in its own end zone kicks
            if kicker is None:
                return None, None
            return kicker, self._moved_by_penalties(kicker, SAFETY_KICK_YARDS, last)
        if last.touchdown and last.conversion is None:
            return None  # the try comes first
        if not (last.touchdown or last.field_goal is FieldGoalResult.MADE):
            if last.play_type is not FootballPlayType.FIELD_GOAL_GOOD:
                return None
        scorer = self.scorer(last)
        if scorer is None:
            return None, None
        return scorer, self._moved_by_penalties(scorer, KICKOFF_YARDS, last)

    def _rekick(self, kickoff: FootballPlay) -> tuple[HomeAway | None, int | None]:
        """A kickoff a penalty wiped out is kicked again, from where the penalty moves it."""
        receiver = self._sides.get(kickoff.offense or "")  # a kickoff's row names the receiving team
        if receiver is None:
            return None, None
        kicker = other_side(receiver)
        penalty = _ENFORCED.search(kickoff.description)
        spot = _AT_SPOT.search(penalty["where"]) if penalty is not None else None
        if penalty is None or spot is None:
            return kicker, None  # moved by a penalty this can't place: the spot is unknown, not the 35
        line = int(spot["line"])
        own = line if self._side_coded(spot["side"]) is kicker else _FIELD - line
        return kicker, _moved(own, int(penalty["yards"]), against_kicker=self._side_coded(penalty["team"]) is kicker)

    def _moved_by_penalties(self, kicker: HomeAway, own: int, play: FootballPlay) -> int:
        """``own`` yard line, moved by any penalty on ``play`` that's enforced on the kickoff."""
        for found in _ENFORCED.finditer(play.description):
            if "kickoff" in found["where"].lower():
                flagged = self._side_coded(found["team"]) is kicker
                own = _moved(own, int(found["yards"]), against_kicker=flagged)
        return own

    @property
    def ended(self) -> bool:
        """Whether the feed has written the game's end."""
        return any(play.play_type is FootballPlayType.END_OF_GAME for play in self._plays.values())

    @property
    def latest(self) -> int:
        """The newest play's number, or -1 before any; a newer play means the feed has moved on."""
        return max(self._plays, default=-1)

    def try_pending(self, side: HomeAway) -> bool:
        """Whether ``side``'s latest shown touchdown is still waiting on its try."""
        touchdowns = [
            play
            for play in self._plays.values()
            if play.touchdown and play.number not in self._late and self.scorer(play) is side
        ]
        last = max(touchdowns, key=lambda play: play.number, default=None)
        return last is not None and last.conversion is None

    def points(self, side: HomeAway, *, shown: bool = False) -> int:
        """The points ``side``'s plays account for: touchdowns, tries, field goals, safeties.

        With ``shown``, only plays the strip could show count, not ones held back as late.
        """
        total = 0
        for play in self._plays.values():
            if play.text.nullified or self.scorer(play) is not side or (shown and play.number in self._late):
                continue
            if play.touchdown:
                total += 6
            elif play.play_type is FootballPlayType.SAFETY:
                total += 2
            elif play.field_goal is FieldGoalResult.MADE or play.play_type is FootballPlayType.FIELD_GOAL_GOOD:
                total += 3
            conversion = play.conversion
            if conversion is not None and conversion.scored:
                total += 1 if play.extra_point is not None else 2
        return total

    def scorer(self, play: FootballPlay) -> HomeAway | None:
        """The side a play put points on the board for, or None if it scored none (or can't be told).

        A row names the team with the ball, so a score by the other team (a pick six, a punt return, a
        safety) belongs to the side it doesn't name.
        """
        offense = self._sides.get(play.offense or "")
        if offense is None:
            return None
        defensive = play.play_type in _DEFENSIVE_SCORES or play.interception or play.fumble_lost
        if play.play_type is FootballPlayType.SAFETY:
            return other_side(offense)
        if play.touchdown:
            return other_side(offense) if defensive else offense
        made = play.field_goal is FieldGoalResult.MADE or play.play_type is FootballPlayType.FIELD_GOAL_GOOD
        converted = play.conversion is not None and play.conversion.scored
        return offense if made or converted else None

    def line(self, name: str) -> PlayerLine | None:
        return self._totals.lines.get(name)

    def _event(self, play: FootballPlay) -> FootballEvent | None:
        summarized = summarize_play(play, teams=self._names)
        if summarized is None:
            return None
        kind, summary = summarized
        players = self._players_line(play) if kind is FootballEventKind.PLAY else None
        return FootballEvent(
            key=f"{play.number}:{kind.value}",
            kind=kind,
            summary=summary,
            players_line=players,
            play=play.number,
            scored_or_turned_over=play.scored_or_turned_over,
        )

    def _correction_events(self, before: FootballPlay, after: FootballPlay) -> list[FootballEvent]:
        events: list[FootballEvent] = []
        ruled, text = before.text.review, after.text
        if ruled is None and (review := text.review) is not None:
            summary = PlaySummary(
                tone=PlayTone.ALERT, headline=review_headline(review, self._names), detail=text.standing
            )
            events.append(
                FootballEvent(
                    key=f"{after.number}:review",
                    kind=FootballEventKind.REVIEW,
                    summary=summary,
                    players_line=self._players_line(after),
                    play=after.number,
                )
            )
        if before.conversion is None and after.conversion is not None:
            result = after.conversion
            tone = PlayTone.SCORE if result.scored else PlayTone.STANDARD
            summary = PlaySummary(tone=tone, headline=conversion_headline(after), detail=text.conversion)
            events.append(
                FootballEvent(
                    key=f"{after.number}:conversion",
                    kind=FootballEventKind.CONVERSION,
                    summary=summary,
                    play=after.number,
                )
            )
        return events

    def _players_line(self, play: FootballPlay) -> str | None:
        lines = [line.describe() for name in _named(play) if (line := self._totals.lines.get(name)) is not None]
        return " | ".join(lines) or None

    def _side_coded(self, code: str) -> HomeAway | None:
        """The side a club code in play text names ("BLT" names Baltimore's), if either."""
        return self._sides.get(club_code(code))

    def _side_named(self, name: str) -> HomeAway | None:
        abbreviation = self._names.get(name) or next((a for n, a in self._names.items() if n and n in name), None)
        return self._sides.get(abbreviation) if abbreviation is not None else None

    def _recount(self) -> None:
        totals = _Totals()
        for play in self._plays.values():
            _credit(play, totals)
        self._totals = totals


def _moved(own: int, yards: int, *, against_kicker: bool) -> int:
    """The kicking team's own yard line after a penalty: back for its own foul, up for the other side's."""
    moved = own - yards if against_kicker else own + yards
    return min(max(moved, 1), _FIELD - 1)


def _half(period: int) -> int:
    return 1 if period <= 2 else 2 if period <= _REGULATION_PERIODS else 3


def _named(play: FootballPlay) -> list[str]:
    """The players a play's standing text names, in order, once each."""
    return list(dict.fromkeys(_NAMES.findall(play.text.standing)))


def _credit(play: FootballPlay, totals: _Totals) -> None:
    """Add one play to the players' totals; a play a penalty wiped out adds nothing."""
    text = play.text
    if text.nullified or play.play_type not in _SNAPS | _KICKS:
        return
    words = text.standing.split("PENALTY")[0]
    gained = play.yards_gained or 0
    scored = play.touchdown and not (play.interception or play.fumble_lost)
    tackled = False  # whether the play ended with a ball carrier brought down, whose tacklers count
    if (intercepted := _INTERCEPTED.search(words)) is not None:
        totals[intercepted["passer"]].attempts += 1
        totals[intercepted["passer"]].interceptions_thrown += 1
        totals[intercepted["defender"]].interceptions += 1
    elif (caught := _PASS.search(words)) is not None:
        passer, catcher = totals[caught["passer"]], totals[caught["catcher"]]
        passer.attempts += 1
        passer.completions += 1
        passer.passing_yards += gained
        catcher.receptions += 1
        catcher.receiving_yards += gained
        if scored:
            passer.passing_touchdowns += 1
            catcher.touchdowns += 1
        tackled = True
    elif (missed := _INCOMPLETE.search(words)) is not None:
        totals[missed["passer"]].attempts += 1
    elif (sacked := _SACK.search(words)) is not None:
        by = _NAMES.findall(sacked["by"])
        for name in by:
            totals[name].sacks += 1 / len(by)
    elif play.play_type in _KICKS:
        if (returned := _RETURN.search(words)) is not None:
            totals[returned["returner"]].returns += 1
            totals[returned["returner"]].return_yards += 0 if returned["yards"] == "no gain" else int(returned["yards"])
            totals[returned["returner"]].touchdowns += scored
            tackled = True
    elif (ran := _RUN.search(words)) is not None:
        runner = totals[ran["runner"]]
        runner.carries += 1
        runner.rushing_yards += gained
        runner.touchdowns += scored
        tackled = True
    for fumbled in _FUMBLE.finditer(words):
        totals[fumbled["player"]].fumbles += 1
    if tackled and (tacklers := _TACKLERS.search(words.strip())) is not None:
        for name in _NAMES.findall(tacklers["names"]):
            totals[name].tackles += 1
