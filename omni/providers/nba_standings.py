"""NBA records and standing ranks, counted from the season's finals.

The board's tier has no standings, so they're counted from results: each club's wins and losses from its regular-season
finals, then its rank in its conference and in its division.

**Ranks** order the clubs by win percentage, then by more wins, then by fewer losses, then by abbreviation
(alphabetically), so every club has a rank of its own. A club with no games yet has no win percentage and sorts as
.000: below every club that has won a game, above any with the same percentage and more losses. The NBA's own
tiebreakers (head to head, division leader, conference and division records, point differential) need more than each
club's record and are out of scope: ties fall to the order above instead.

**What counts:** regular-season finals only. The provider leaves out the postseason and the NBA Cup's championship
game, which the NBA doesn't count toward the records (the Cup's other games count as ordinary regular-season games).

**Refreshing** is cheap: `replace` takes one full read of the season's finals (once a day), and `fold` adds each
final as it happens, without another read. A game is counted once however often it's folded. A full read replaces
everything it has, and a final folded since that the read doesn't have yet (it went final while the read was being
made) stays counted. So at a final, the clubs' records already include it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from omni.core.enum import HomeAway
from omni.core.ids import LeagueScopedId
from omni.domain.baseball import WinLossRecord
from omni.domain.basketball import StandingRanks
from omni.domain.teams import BasketballTeam, Team
from omni.providers.nba_teams import NBA_TEAMS, NbaTeamRegistry

__all__ = ["GameResult", "NbaStandings"]


@dataclass(frozen=True, slots=True, kw_only=True)
class GameResult:
    """A final that counts toward the clubs' records: its game, its clubs, and the side that won."""

    game: LeagueScopedId
    away: Team
    home: Team
    winner: HomeAway

    def __post_init__(self) -> None:
        if self.away.abbreviation == self.home.abbreviation:
            raise ValueError("a game is between two clubs")


class NbaStandings:
    """Each club's record and standing ranks, from the finals it's been given."""

    def __init__(self, registry: NbaTeamRegistry) -> None:
        self._clubs: dict[str, BasketballTeam] = {}
        for abbreviation in NBA_TEAMS:
            club = registry.resolve(abbreviation)
            if club.conference is None or club.division is None:
                raise ValueError(f"{abbreviation} needs its conference and division to be ranked")
            self._clubs[abbreviation] = club
        self._read: dict[LeagueScopedId, GameResult] = {}  # the last full read's finals
        self._folded: dict[LeagueScopedId, GameResult] = {}  # finals folded since that the read didn't have
        self._tally = {abbreviation: [0, 0] for abbreviation in self._clubs}  # wins and losses

    def replace(self, results: Iterable[GameResult]) -> None:
        """Count the season from a full read of its finals; a final folded since that the read doesn't have stays."""
        read = {result.game: self._checked(result) for result in results}
        self._folded = {game: result for game, result in self._folded.items() if game not in read}
        self._read = read
        self._tally = {abbreviation: [0, 0] for abbreviation in self._clubs}
        for result in (*read.values(), *self._folded.values()):
            self._count(result)

    def fold(self, result: GameResult) -> bool:
        """Count one final as it happens; False when it's already counted."""
        if result.game in self._read or result.game in self._folded:
            return False
        self._folded[result.game] = self._checked(result)
        self._count(result)
        return True

    def record(self, team: Team) -> WinLossRecord:
        """``team``'s wins and losses in the finals counted; 0-0 before its first."""
        wins, losses = self._tally[self._club(team).abbreviation]
        return WinLossRecord(wins=wins, losses=losses)

    def ranks(self, team: Team) -> StandingRanks:
        """Where ``team`` ranks in its conference and its division, by the order this module describes."""
        club = self._club(team)
        order = sorted(self._clubs, key=lambda abbreviation: _standing(abbreviation, *self._tally[abbreviation]))
        conference = [each for each in order if self._clubs[each].conference == club.conference]
        division = [each for each in order if self._clubs[each].division == club.division]
        return StandingRanks(
            conference=conference.index(club.abbreviation) + 1, division=division.index(club.abbreviation) + 1
        )

    def _club(self, team: Team) -> BasketballTeam:
        club = self._clubs.get(team.abbreviation)
        if club is None:
            raise ValueError(f"{team.abbreviation} isn't an NBA club")
        return club

    def _checked(self, result: GameResult) -> GameResult:
        self._club(result.away)
        self._club(result.home)
        return result

    def _count(self, result: GameResult) -> None:
        won, lost = (result.away, result.home) if result.winner is HomeAway.AWAY else (result.home, result.away)
        self._tally[won.abbreviation][0] += 1
        self._tally[lost.abbreviation][1] += 1


def _standing(abbreviation: str, wins: int, losses: int) -> tuple[Fraction, int, int, str]:
    """A club's place in the order: win percentage, then more wins, then fewer losses, then its abbreviation."""
    percentage = Fraction(wins, wins + losses) if wins + losses else Fraction(0)
    return -percentage, -wins, losses, abbreviation
