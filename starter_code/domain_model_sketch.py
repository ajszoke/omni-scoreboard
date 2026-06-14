"""Typed domain model sketch for Omni Scoreboard.

This is intentionally a sketch, not a finished module. Use it to guide the
first implementation pass and split into proper packages as the repo evolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, Protocol, TypeVar

from starter_code.enum_core import (
    BaseballGameEventType,
    CardKind,
    DisplayPriority,
    GameStatus,
    League,
    PanelProfile,
    Sport,
    UpdateUrgency,
)


@dataclass(frozen=True, slots=True)
class SourceRef:
    name: str
    raw_url: str | None = None


@dataclass(frozen=True, slots=True)
class LeagueScopedId:
    league: League
    source: SourceRef
    raw: str

    def __str__(self) -> str:
        return f"{self.league}:{self.source.name}:{self.raw}"


@dataclass(frozen=True, slots=True)
class DurationSeconds:
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("duration cannot be negative")

    def as_timedelta(self) -> timedelta:
        return timedelta(seconds=self.value)


@dataclass(frozen=True, slots=True)
class RGBColor:
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for component in (self.r, self.g, self.b):
            if not 0 <= component <= 255:
                raise ValueError("RGB components must be 0..255")

    def relative_luminance(self) -> float:
        def convert(channel: int) -> float:
            c = channel / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * convert(self.r) + 0.7152 * convert(self.g) + 0.0722 * convert(self.b)

    def contrast_ratio(self, other: "RGBColor") -> float:
        lighter = max(self.relative_luminance(), other.relative_luminance())
        darker = min(self.relative_luminance(), other.relative_luminance())
        return (lighter + 0.05) / (darker + 0.05)


@dataclass(frozen=True, slots=True)
class LogoAsset:
    key: str
    path: str
    preferred_background: RGBColor | None = None


@dataclass(frozen=True, slots=True)
class PanelGeometry:
    profile: PanelProfile
    width: int
    height: int
    chain_length: int
    parallel: int


class Competitor(Protocol):
    id: LeagueScopedId
    display_name: str
    short_name: str


@dataclass(frozen=True, slots=True)
class Team:
    id: LeagueScopedId
    league: League
    display_name: str
    short_name: str
    abbreviation: str
    primary_color: RGBColor
    secondary_color: RGBColor
    logo: LogoAsset

    def best_text_color_on_primary(self) -> RGBColor:
        white = RGBColor(255, 255, 255)
        black = RGBColor(0, 0, 0)
        return white if white.contrast_ratio(self.primary_color) >= black.contrast_ratio(self.primary_color) else black


@dataclass(frozen=True, slots=True)
class BaseballTeam(Team):
    division: str | None = None
    league_side: str | None = None


@dataclass(frozen=True, slots=True)
class FootballTeam(Team):
    conference: str | None = None
    division: str | None = None


@dataclass(frozen=True, slots=True)
class BasketballTeam(Team):
    conference: str | None = None
    division: str | None = None


@dataclass(frozen=True, slots=True)
class HockeyTeam(Team):
    conference: str | None = None
    division: str | None = None


@dataclass(frozen=True, slots=True)
class Golfer:
    id: LeagueScopedId
    display_name: str
    short_name: str
    country: str | None = None


@dataclass(frozen=True, slots=True)
class Contest:
    id: LeagueScopedId
    league: League
    status: GameStatus
    scheduled_start: datetime
    competitors: tuple[Competitor, ...]
    venue_name: str | None = None

    @property
    def sport(self) -> Sport:
        return self.league.sport


@dataclass(frozen=True, slots=True)
class TeamGame(Contest):
    away: Team | None = None
    home: Team | None = None


@dataclass(frozen=True, slots=True)
class GolfTournament(Contest):
    tournament_name: str = ""
    round_number: int | None = None
    cut_line: int | None = None


@dataclass(frozen=True, slots=True)
class EventImportance:
    priority: DisplayPriority
    urgency: UpdateUrgency
    leverage: float
    rarity: float
    favorite_relevance: float
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("leverage", self.leverage),
            ("rarity", self.rarity),
            ("favorite_relevance", self.favorite_relevance),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be normalized 0..1")

    def combined_score(self) -> float:
        return (
            int(self.priority)
            + int(self.urgency) * 5
            + self.leverage * 20
            + self.rarity * 15
            + self.favorite_relevance * 20
        )


EventTypeT = TypeVar("EventTypeT", bound=Enum)
PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True)
class GameEvent(Generic[EventTypeT, PayloadT]):
    id: LeagueScopedId
    contest: Contest
    event_type: EventTypeT
    source: SourceRef
    source_time: datetime
    observed_at: datetime
    competitors: tuple[Competitor, ...]
    importance: EventImportance
    payload: PayloadT

    @property
    def league(self) -> League:
        return self.contest.league


@dataclass(frozen=True, slots=True)
class BaseballCount:
    balls: int
    strikes: int
    outs: int


@dataclass(frozen=True, slots=True)
class BaseballPlayPayload:
    inning: int
    half: str
    count: BaseballCount | None
    description: str
    rbi: int = 0
    pitch_type: str | None = None
    fielder_sequence: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class BaseballGameEvent(GameEvent[BaseballGameEventType, BaseballPlayPayload]):
    pass


@dataclass(frozen=True, slots=True)
class CardId:
    raw: str


@dataclass(frozen=True, slots=True)
class DedupeKey:
    raw: str


@dataclass(frozen=True, slots=True)
class DisplayTiming:
    available_at: datetime
    expires_at: datetime | None
    min_display: DurationSeconds
    max_display: DurationSeconds

    def is_available(self, now: datetime) -> bool:
        return now >= self.available_at and (self.expires_at is None or now < self.expires_at)


@dataclass(frozen=True, slots=True)
class LayoutSupport:
    profiles: frozenset[PanelProfile]
    fallback_card_kind: CardKind | None = None
    compromise_notes: tuple[str, ...] = ()

    def supports(self, profile: PanelProfile) -> bool:
        return profile in self.profiles


@dataclass(frozen=True, slots=True)
class CardPriority:
    band: DisplayPriority
    score: float
    reasons: tuple[str, ...]


CardPayloadT = TypeVar("CardPayloadT")


@dataclass(frozen=True, slots=True)
class ScoreboardCard(Generic[CardPayloadT]):
    id: CardId
    kind: CardKind
    contest: Contest
    source_event_ids: tuple[LeagueScopedId, ...]
    timing: DisplayTiming
    priority: CardPriority
    layout_support: LayoutSupport
    dedupe_key: DedupeKey
    payload: CardPayloadT

    @property
    def league(self) -> League:
        return self.contest.league
