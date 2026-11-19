"""Basketball's events: the taxonomy, and the plays and rulings a board captions.

A caption says only what the data can establish: a made basket (with its assister when one is known), free
throws, a personal foul and the player's count, a timeout and what's left, a won challenge, and a call taken back
with no challenge signal. Each is a typed event, and its words on the board (the headline, the detail and the tone
they're drawn in) come from the event, never from free text.

A ruling's full lines don't fit a 16-character lane, so a ruling also words itself for one: a pair of lines for
each 6 s half of its turn (`CaptionWords.narrow`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omni.core.enum import HomeAway, StrEnumMixin
from omni.domain.basketball import FOUL_LIMIT, TIMEOUTS_PER_GAME, BySide, CallChange, PlayerCallChange
from omni.domain.football import PlayTone

__all__ = [
    "BasketballCaption",
    "BasketballGameEventType",
    "CallReversed",
    "CaptionLines",
    "CaptionWords",
    "ChallengeWon",
    "FieldGoalMade",
    "FreeThrows",
    "PersonalFoul",
    "ShotValue",
    "TimeoutTaken",
]


class BasketballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    SCORE_CHANGE = "score_change"
    LEAD_CHANGE = "lead_change"
    CLUTCH_TIME = "clutch_time"
    TIMEOUT = "timeout"
    GAME_FINAL = "game_final"


@dataclass(frozen=True, slots=True)
class CaptionLines:
    """A caption's two lines: the headline over the detail."""

    headline: str
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptionWords:
    """An event in a board's words: a headline drawn in the event's tone, over a detail line.

    ``narrow`` is the wording for a 16-character lane when the lines above don't fit one: the first 6 s half's
    lines, then the second's. None when the full lines serve both halves.
    """

    tone: PlayTone
    headline: str
    detail: str
    narrow: tuple[CaptionLines, CaptionLines] | None = None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _named(player: str) -> None:
    if not player:
        raise ValueError("a caption names its player")


class ShotValue(StrEnumMixin, str, Enum):
    """What a made field goal counted for."""

    TWO = "two"
    THREE = "three"

    @property
    def points(self) -> int:
        return 3 if self is ShotValue.THREE else 2


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldGoalMade:
    """A made field goal, and the teammate credited with the assist when one is: ``3-POINT FG GOOD`` over
    ``J.Grant (J.Holiday asst.)``."""

    side: HomeAway
    shooter: str
    value: ShotValue
    assister: str | None = None

    def __post_init__(self) -> None:
        _named(self.shooter)
        if self.assister is not None:
            _named(self.assister)

    def words(self, codes: BySide[str]) -> CaptionWords:
        detail = self.shooter if self.assister is None else f"{self.shooter} ({self.assister} asst.)"
        return CaptionWords(tone=PlayTone.SCORE, headline=f"{self.value.points}-POINT FG GOOD", detail=detail)


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeThrows:
    """A trip to the line: ``made`` of ``attempted`` on this trip, and the shooter's free throws for the game.

    ``FREE THROWS GOOD`` over ``A.Edwards (6/6 FT)`` when every one went in; ``1 OF 2 FREE THROWS`` when some
    did; ``FREE THROWS MISSED`` when none did. A lone free throw reads in the singular.
    """

    side: HomeAway
    shooter: str
    made: int
    attempted: int
    game_made: int
    game_attempted: int

    def __post_init__(self) -> None:
        _named(self.shooter)
        if not 0 <= self.made <= self.attempted or self.attempted < 1:
            raise ValueError("a trip to the line has an attempt, and no more makes than attempts")
        if not self.made <= self.game_made <= self.game_attempted:
            raise ValueError("the game's free throws include the trip's, with no more makes than attempts")
        if self.game_attempted - self.game_made < self.attempted - self.made:
            raise ValueError("the game's misses include the trip's")

    def words(self, codes: BySide[str]) -> CaptionWords:
        noun = "FREE THROWS" if self.attempted > 1 else "FREE THROW"
        if self.made == self.attempted:
            headline = f"{noun} GOOD"
        elif self.made == 0:
            headline = f"{noun} MISSED"
        else:
            headline = f"{self.made} OF {self.attempted} {noun}"
        tone = PlayTone.SCORE if self.made else PlayTone.STANDARD
        detail = f"{self.shooter} ({self.game_made}/{self.game_attempted} FT)"
        return CaptionWords(tone=tone, headline=headline, detail=detail)


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonalFoul:
    """A personal foul, with the player's count for the game: ``PERSONAL FOUL`` over ``D.Clingan (4th foul)``."""

    side: HomeAway
    player: str
    count: int

    def __post_init__(self) -> None:
        _named(self.player)
        if not 1 <= self.count <= FOUL_LIMIT:
            raise ValueError(f"a player's foul count is 1..{FOUL_LIMIT}")

    def words(self, codes: BySide[str]) -> CaptionWords:
        return CaptionWords(
            tone=PlayTone.ALERT, headline="PERSONAL FOUL", detail=f"{self.player} ({_ordinal(self.count)} foul)"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeoutTaken:
    """A side's timeouts dropped by one: ``TIMEOUT POR`` over ``4 LEFT``, whatever its length class."""

    side: HomeAway
    left: int

    def __post_init__(self) -> None:
        if not 0 <= self.left < TIMEOUTS_PER_GAME:
            raise ValueError(f"a side has 0..{TIMEOUTS_PER_GAME - 1} timeouts left after taking one")

    def words(self, codes: BySide[str]) -> CaptionWords:
        return CaptionWords(tone=PlayTone.ALERT, headline=f"TIMEOUT {codes[self.side]}", detail=f"{self.left} LEFT")


# How each change reads on a full lane, and in the few words a 16-character lane puts over the player's name.
_CHANGE_DETAIL = {
    CallChange.FOUL_OFF: "FOUL OFF {player}",
    CallChange.FOUL_TO_BLOCK: "BLOCK, NOT FOUL: {player}",
    CallChange.OFFENSIVE_FOUL_OFF: "OFFENSIVE FOUL OFF: {player}",
}
_CHANGE_WORDS = {
    CallChange.FOUL_OFF: "FOUL OFF",
    CallChange.FOUL_TO_BLOCK: "BLOCK, NOT FOUL",
    CallChange.OFFENSIVE_FOUL_OFF: "OFFENSIVE FOUL OFF",
}
_REVERSED = "CALL REVERSED"


def _change_detail(change: PlayerCallChange) -> str:
    return _CHANGE_DETAIL[change.change].format(player=change.player)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChallengeWon:
    """A side's timeouts came back with the clock still stopped: its challenge won, and the call reversed.

    ``POR CHALLENGE WON`` over ``CALL REVERSED``, or over the change when a player's line shows one
    (``BLOCK, NOT FOUL: T.Camara``). A narrow lane gives the first half to ``POR CHALLENGE`` over
    ``CALL REVERSED``, and the second to the change over the player, when there is one.
    """

    side: HomeAway
    change: PlayerCallChange | None = None

    def words(self, codes: BySide[str]) -> CaptionWords:
        code = codes[self.side]
        first = CaptionLines(f"{code} CHALLENGE", _REVERSED)
        if self.change is None:
            return CaptionWords(
                tone=PlayTone.ALERT, headline=f"{code} CHALLENGE WON", detail=_REVERSED, narrow=(first, first)
            )
        second = CaptionLines(_CHANGE_WORDS[self.change.change], self.change.player)
        return CaptionWords(
            tone=PlayTone.ALERT,
            headline=f"{code} CHALLENGE WON",
            detail=_change_detail(self.change),
            narrow=(first, second),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CallReversed:
    """A player's line took a call back with no challenge signal: ``CALL REVERSED`` over ``FOUL OFF J.Randle``.

    Nobody is named as having asked, and no side is credited: the data doesn't say who did.
    """

    change: PlayerCallChange

    @property
    def side(self) -> None:
        return None

    def words(self, codes: BySide[str]) -> CaptionWords:
        narrow = (
            CaptionLines(_REVERSED, _CHANGE_WORDS[self.change.change]),
            CaptionLines(_REVERSED, self.change.player),
        )
        return CaptionWords(tone=PlayTone.ALERT, headline=_REVERSED, detail=_change_detail(self.change), narrow=narrow)


BasketballCaption = FieldGoalMade | FreeThrows | PersonalFoul | TimeoutTaken | ChallengeWon | CallReversed
"""Anything a basketball card captions: a play, a timeout, or a ruling."""
