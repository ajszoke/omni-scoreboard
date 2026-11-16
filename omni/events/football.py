"""Football plays, and the events a board shows for them.

A `FootballPlay` is one row of a game's play-by-play, typed. Its words are the league's own play text,
whose grammar is regular enough to read who did what ("J.Burrow pass short right to J.Chase to PIT 26 for
4 yards (J.Dean)"); `PlayText` reads it. A review rewrites a play's text in place, appending the ruling
and then the play as it now stands, so everything here reads the text after the last ruling.

A `FootballEvent` is what a board's play strip shows: a play, a timeout, a review's result, a conversion
or an injury, already put into a headline, a description, and the involved players' lines for the game.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from omni.core.enum import StrEnumMixin
from omni.domain.football import PlaySummary, PlayTone

__all__ = [
    "ConversionResult",
    "FieldGoalResult",
    "FootballEvent",
    "FootballEventKind",
    "FootballGameEventType",
    "FootballPlay",
    "FootballPlayType",
    "PlayText",
    "Review",
    "ReviewVerdict",
    "TimeoutCall",
    "summarize_play",
]


class FootballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    DRIVE_START = "drive_start"
    RED_ZONE = "red_zone"
    SCORE = "score"
    TOUCHDOWN = "touchdown"
    FIELD_GOAL = "field_goal"
    TURNOVER = "turnover"
    TWO_MINUTE_WARNING = "two_minute_warning"
    GAME_FINAL = "game_final"


class _Lenient(StrEnumMixin, str, Enum):
    """A feed vocabulary that can grow: a value not listed reads as ``OTHER``."""

    @classmethod
    def _missing_(cls, value: object) -> Any:
        return cls["OTHER"]


class FootballPlayType(_Lenient):
    """A play's type, as the live feed names it."""

    PASS_RECEPTION = "pass_reception"
    PASS_INCOMPLETION = "pass_incompletion"
    PASS_INTERCEPTION_RETURN = "pass_interception_return"
    PASSING_TOUCHDOWN = "passing_touchdown"
    INTERCEPTION_RETURN_TOUCHDOWN = "interception_return_touchdown"
    RUSH = "rush"
    RUSHING_TOUCHDOWN = "rushing_touchdown"
    SACK = "sack"
    FUMBLE_RECOVERY_OWN = "fumble_recovery_own"
    FUMBLE_RECOVERY_OPPONENT = "fumble_recovery_opponent"
    FUMBLE_RETURN_TOUCHDOWN = "fumble_return_touchdown"
    SAFETY = "safety"
    KICKOFF = "kickoff"
    KICKOFF_RETURN_TOUCHDOWN = "kickoff_return_touchdown"
    PUNT = "punt"
    PUNT_RETURN_TOUCHDOWN = "punt_return_touchdown"
    BLOCKED_PUNT = "blocked_punt"
    FIELD_GOAL_GOOD = "field_goal_good"
    FIELD_GOAL_MISSED = "field_goal_missed"
    BLOCKED_FIELD_GOAL = "blocked_field_goal"
    PENALTY = "penalty"
    TIMEOUT = "timeout"
    OFFICIAL_TIMEOUT = "official_timeout"
    TWO_MINUTE_WARNING = "two_minute_warning"
    END_PERIOD = "end_period"
    END_OF_HALF = "end_of_half"
    END_OF_GAME = "end_of_game"
    COIN_TOSS = "coin_toss"
    OTHER = "other"


class FieldGoalResult(_Lenient):
    MADE = "made"
    MISSED = "missed"
    BLOCKED = "blocked"
    OTHER = "other"


class ConversionResult(_Lenient):
    """An extra point's or a two-point try's result; the feed words the two differently."""

    GOOD = "good"
    SUCCESS = "success"
    FAILED = "failed"
    FAILURE = "failure"
    BLOCKED = "blocked"
    ABORTED = "aborted"
    OTHER = "other"

    @property
    def scored(self) -> bool:
        return self in (ConversionResult.GOOD, ConversionResult.SUCCESS)


class ReviewVerdict(StrEnumMixin, str, Enum):
    REVERSED = "reversed"
    STANDS = "stands"


class FootballEventKind(StrEnumMixin, str, Enum):
    """What a strip entry is about; only plays carry players' lines."""

    PLAY = "play"
    TIMEOUT = "timeout"  # a club's timeout
    TV_TIMEOUT = "tv_timeout"
    REVIEW = "review"  # a challenge's or the booth's ruling, arriving after the play
    CONVERSION = "conversion"  # the try after a touchdown, arriving after the touchdown
    INJURY = "injury"
    TWO_MINUTE_WARNING = "two_minute_warning"
    SCORE = "score"  # points the scoreboard shows that no play in the feed accounts for

    @property
    def spans(self) -> bool:
        """Whether this names a stoppage that lasts until play resumes, rather than a moment."""
        return self in _SPANNING


_SPANNING = frozenset(
    {
        FootballEventKind.TIMEOUT,
        FootballEventKind.TV_TIMEOUT,
        FootballEventKind.REVIEW,
        FootballEventKind.INJURY,
        FootballEventKind.TWO_MINUTE_WARNING,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FootballEvent:
    """One entry for the play strip: its headline and description, then the players' lines."""

    key: str  # stable across polls, so an entry is shown once
    kind: FootballEventKind
    summary: PlaySummary
    players_line: str | None = None  # the named players' game lines, shown after the description


@dataclass(frozen=True, slots=True)
class Review:
    """A ruling on a play: who asked (None for the booth), what was ruled on, and the verdict."""

    challenger: str | None
    ruling: str
    verdict: ReviewVerdict
    charged_timeout: int | None  # a lost challenge costs a timeout; this is its number in the half


@dataclass(frozen=True, slots=True)
class TimeoutCall:
    team: str  # the club's abbreviation
    number: int  # its count in the half: #2 means two used
    clock: str | None = None  # the game clock when it was called, "1:52"

    def describe(self) -> str:
        """The call in brief: "CIN #3 at 1:52"."""
        return f"{self.team} #{self.number}" + (f" at {self.clock}" if self.clock else "")


# The league's play text. A name is an initial or two, a dot, then the surname ("J.Burrow", "A.St. Brown").
_NAME = r"[A-Z][A-Za-z']{0,2}\.(?:St\. )?[A-Z][A-Za-z'\-]+"
_REVIEW = re.compile(
    r"(?:(?P<challenger>(?:[A-Z][A-Za-z]+ ){0,2}[A-Z][A-Za-z]+) challenged|The Replay Official reviewed)"
    r" the (?P<ruling>.+?) ruling, and the play was (?P<verdict>\w+)\."
    r"(?:[^(]*\(Timeout #(?P<charged>\d)\.\))?"
)
_TIMEOUT = re.compile(r"Timeout #(?P<number>\d) by (?P<team>[A-Z]{2,3})(?: at (?P<clock>\d{1,2}:\d{2}))?")
_PENALTY = re.compile(r"PENALTY on (?P<team>[A-Z]{2,3})-[^,]*, (?P<foul>[^,]+),")
# What leads a play's words without being the play: formation tags ("(Shotgun) ", "(No Huddle, Shotgun) ",
# "(14:21) ") and eligibility reports ("J.Bayer reported in as eligible. ").
_LEADING_TAGS = re.compile(rf"^(?:\s*\([^)]*\)\s*|\s*{_NAME} reported in as eligible\.\s*)+")
_CONVERSION = re.compile(r"TOUCHDOWN(?: \[[^\]]*\])?\.\s*(?P<try>.+)$")  # "[Y.Black]" credits a QB hit
_NO_PLAY = "No Play"


def _untagged(text: str) -> str:
    return _LEADING_TAGS.sub("", text).strip()


class PlayText:
    """A play's description: as published, and as the play stands after any ruling.

    A reversed play's text goes on to describe the play as it now stands; an upheld one stands as first
    described, before the ruling.
    """

    def __init__(self, raw: str) -> None:
        self.raw = raw.strip()
        self._ruling: re.Match[str] | None = None
        for self._ruling in _REVIEW.finditer(self.raw):
            pass
        standing = self.raw
        if self._ruling is not None:
            reversed_ = self._ruling["verdict"].upper() == "REVERSED"
            standing = self.raw[self._ruling.end("verdict") + 1 :] if reversed_ else self.raw[: self._ruling.start()]
        self.standing = _untagged(standing) or _untagged(self.raw)

    @property
    def nullified(self) -> bool:
        """Whether a penalty wiped the play out, so none of it counts."""
        return _NO_PLAY in self.standing

    @property
    def review(self) -> Review | None:
        found = self._ruling
        if found is None:
            return None
        verdict = ReviewVerdict.REVERSED if found["verdict"].upper() == "REVERSED" else ReviewVerdict.STANDS
        charged = int(found["charged"]) if found["charged"] else None
        return Review(found["challenger"], found["ruling"], verdict, charged)

    @property
    def timeout(self) -> TimeoutCall | None:
        found = _TIMEOUT.search(self.raw)
        if found is None:
            return None
        clock = found["clock"]
        if clock is not None:
            minutes, seconds = clock.split(":")
            clock = f"{int(minutes)}:{seconds}"
        return TimeoutCall(found["team"], int(found["number"]), clock)

    @property
    def penalty(self) -> tuple[str, str] | None:
        """The penalized club and the foul, if a flag was thrown."""
        found = _PENALTY.search(self.standing)
        return (found["team"], found["foul"]) if found else None

    @property
    def conversion(self) -> str | None:
        """The try after a touchdown, in words ("E.McPherson extra point is GOOD")."""
        found = _CONVERSION.search(self.standing)
        return found["try"].split(",")[0].rstrip(".") if found else None


@dataclass(frozen=True, slots=True, kw_only=True)
class FootballPlay:
    """One play-by-play row, typed; ``revision`` grows with each correction the feed publishes."""

    number: int
    revision: int
    play_type: FootballPlayType
    quarter: int
    description: str
    offense: str | None = None  # the club with the ball, by abbreviation
    yards_gained: int | None = None
    first_down: bool = False
    touchdown: bool = False
    interception: bool = False
    fumble_lost: bool = False
    penalty: bool = False
    field_goal: FieldGoalResult | None = None
    extra_point: ConversionResult | None = None
    two_point: ConversionResult | None = None

    @property
    def text(self) -> PlayText:
        return PlayText(self.description)

    @property
    def conversion(self) -> ConversionResult | None:
        return self.extra_point if self.extra_point is not None else self.two_point


class _Gain(Enum):
    """A play the strip names by its yardage: "4 YARD RUN", "RUN, NO GAIN", "7 YARD SACK"."""

    RUN = "RUN"
    PASS = "PASS"
    SACK = "SACK"

    def headline(self, gained: int | None, *, first_down: bool) -> str:
        if gained is None:
            return self.value
        if self is _Gain.SACK:
            return f"{abs(gained)} YARD SACK" if gained else self.value
        if gained > 0:
            said = f"{gained} YARD {self.value}"
        elif gained == 0:
            said = f"{self.value}, NO GAIN"
        else:
            said = f"{self.value}, {-gained} YARD LOSS"
        return said + (", 1ST DOWN" if first_down else "")


@dataclass(frozen=True, slots=True)
class _Reading:
    """How a play type reads on the strip: a fixed headline (``{team}`` fills in), or its yardage."""

    tone: PlayTone
    headline: str = ""
    kind: FootballEventKind = FootballEventKind.PLAY
    gain: _Gain | None = None


_STANDARD, _SCORE, _ALERT, _TURNOVER = PlayTone.STANDARD, PlayTone.SCORE, PlayTone.ALERT, PlayTone.TURNOVER
_READINGS: dict[FootballPlayType, _Reading] = {
    FootballPlayType.PASS_RECEPTION: _Reading(_STANDARD, gain=_Gain.PASS),
    FootballPlayType.PASS_INCOMPLETION: _Reading(_STANDARD, "INCOMPLETE"),
    FootballPlayType.PASS_INTERCEPTION_RETURN: _Reading(_TURNOVER, "INTERCEPTION"),
    FootballPlayType.PASSING_TOUCHDOWN: _Reading(_SCORE, "TOUCHDOWN {team}"),
    FootballPlayType.INTERCEPTION_RETURN_TOUCHDOWN: _Reading(_TURNOVER, "PICK SIX"),
    FootballPlayType.RUSH: _Reading(_STANDARD, gain=_Gain.RUN),
    FootballPlayType.RUSHING_TOUCHDOWN: _Reading(_SCORE, "TOUCHDOWN {team}"),
    FootballPlayType.SACK: _Reading(_STANDARD, gain=_Gain.SACK),
    FootballPlayType.FUMBLE_RECOVERY_OWN: _Reading(_STANDARD, "FUMBLE, RECOVERED"),
    FootballPlayType.FUMBLE_RECOVERY_OPPONENT: _Reading(_TURNOVER, "FUMBLE LOST"),
    FootballPlayType.FUMBLE_RETURN_TOUCHDOWN: _Reading(_TURNOVER, "FUMBLE RETURN TD"),
    FootballPlayType.SAFETY: _Reading(_SCORE, "SAFETY"),
    FootballPlayType.KICKOFF: _Reading(_STANDARD, "KICKOFF"),
    FootballPlayType.KICKOFF_RETURN_TOUCHDOWN: _Reading(_SCORE, "KICK RETURN TD"),
    FootballPlayType.PUNT: _Reading(_STANDARD, "PUNT"),
    FootballPlayType.PUNT_RETURN_TOUCHDOWN: _Reading(_SCORE, "PUNT RETURN TD"),
    FootballPlayType.BLOCKED_PUNT: _Reading(_TURNOVER, "PUNT BLOCKED"),
    FootballPlayType.FIELD_GOAL_GOOD: _Reading(_SCORE, "FIELD GOAL GOOD"),
    FootballPlayType.FIELD_GOAL_MISSED: _Reading(_TURNOVER, "FG NO GOOD"),
    FootballPlayType.BLOCKED_FIELD_GOAL: _Reading(_TURNOVER, "FG BLOCKED"),
    FootballPlayType.PENALTY: _Reading(_ALERT, "PENALTY"),
    FootballPlayType.TIMEOUT: _Reading(_ALERT, "TIMEOUT {team}", FootballEventKind.TIMEOUT),
    FootballPlayType.OFFICIAL_TIMEOUT: _Reading(_STANDARD, "TV TIMEOUT", FootballEventKind.TV_TIMEOUT),
    FootballPlayType.TWO_MINUTE_WARNING: _Reading(_ALERT, "TWO-MINUTE WARNING", FootballEventKind.TWO_MINUTE_WARNING),
    FootballPlayType.OTHER: _Reading(_STANDARD, "PLAY"),
}
# Plays the feed files under another type, known by their words: a kneel-down is a "rush".
_TEXT_READINGS: tuple[tuple[re.Pattern[str], _Reading], ...] = (
    (re.compile(r"\bkneels\b"), _Reading(_STANDARD, "KNEEL")),
    (re.compile(r"\bspiked\b"), _Reading(_STANDARD, "SPIKE")),
)
# What the flags say happened, when the row's type hasn't caught up; the first that applies wins.
_FLAG_READINGS: tuple[tuple[tuple[str, ...], _Reading], ...] = (
    (("touchdown", "interception"), _Reading(_TURNOVER, "PICK SIX")),
    (("touchdown", "fumble_lost"), _Reading(_TURNOVER, "FUMBLE RETURN TD")),
    (("touchdown",), _Reading(_SCORE, "TOUCHDOWN {team}")),
    (("interception",), _Reading(_TURNOVER, "INTERCEPTION")),
    (("fumble_lost",), _Reading(_TURNOVER, "FUMBLE LOST")),
)
_FIELD_GOAL_READINGS: dict[FieldGoalResult, _Reading] = {
    FieldGoalResult.MADE: _Reading(_SCORE, "FIELD GOAL GOOD"),
    FieldGoalResult.MISSED: _Reading(_TURNOVER, "FG NO GOOD"),  # the ball goes over
    FieldGoalResult.BLOCKED: _Reading(_TURNOVER, "FG BLOCKED"),
}
_VERDICT_WORDS = {ReviewVerdict.REVERSED: "REVERSED", ReviewVerdict.STANDS: "STANDS"}
# Feed entries that aren't for the strip: the last real play stays up through them.
_MARKERS = frozenset(
    {
        FootballPlayType.END_PERIOD,
        FootballPlayType.END_OF_HALF,
        FootballPlayType.END_OF_GAME,
        FootballPlayType.COIN_TOSS,
    }
)


def conversion_headline(play: FootballPlay) -> str:
    """What the try after a touchdown came to: "XP GOOD", "2-PT FAILS"."""
    result = play.conversion
    is_kick = play.extra_point is not None
    if result is not None and result.scored:
        return "XP GOOD" if is_kick else "2-PT GOOD"
    return "XP NO GOOD" if is_kick else "2-PT FAILS"


def review_headline(review: Review, teams: dict[str, str]) -> str:
    """A ruling's headline: "CIN CHALLENGE: STANDS", "REVIEW: REVERSED"."""
    who = "REVIEW" if review.challenger is None else f"{teams.get(review.challenger, review.challenger)} CHALLENGE"
    return f"{who.upper()}: {_VERDICT_WORDS[review.verdict]}"


def summarize_play(
    play: FootballPlay, *, teams: dict[str, str] | None = None
) -> tuple[FootballEventKind, PlaySummary] | None:
    """A play in a board's words, or None for a feed entry that isn't one (the end of a quarter).

    ``teams`` maps how play text names a club ("Cincinnati") to its abbreviation, for challenges.
    """
    if play.play_type in _MARKERS:
        return None
    text = play.text
    detail = text.standing or None
    reading = _READINGS[play.play_type]
    if (review := text.review) is not None:
        summary = PlaySummary(tone=_ALERT, headline=review_headline(review, teams or {}), detail=detail)
        return FootballEventKind.PLAY, summary
    if reading.kind is FootballEventKind.PLAY:
        reading = next((r for words, r in _TEXT_READINGS if words.search(text.standing)), reading)
        reading = next((r for flags, r in _FLAG_READINGS if all(getattr(play, f) for f in flags)), reading)
        if play.field_goal is not None:
            reading = _FIELD_GOAL_READINGS.get(play.field_goal, reading)
    if text.nullified and (penalty := text.penalty) is not None:
        reading = _Reading(_ALERT, f"FLAG {penalty[0]}: {penalty[1].upper()}")
    timeout = text.timeout
    team = timeout.team if timeout is not None else (play.offense or "")
    if reading.gain is not None:
        headline = reading.gain.headline(play.yards_gained, first_down=play.first_down)
    else:
        headline = reading.headline.format(team=team).strip()
    if play.penalty and not text.nullified and reading.tone is not _ALERT and text.penalty is not None:
        headline += " + FLAG"
    if reading.kind is not FootballEventKind.PLAY:
        # A club's timeout in brief ("CIN #3 at 1:52"); "Official Timeout at 08:26." says no more than its headline.
        detail = timeout.describe() if timeout is not None else None
    return reading.kind, PlaySummary(tone=reading.tone, headline=headline, detail=detail)
