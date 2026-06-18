# Omni Scoreboard Typed Domain Architecture

## Guiding principle

Raw primitives belong at the boundaries only: config JSON, provider JSON, CLI args, and file paths. Inside the application, domain concepts should be typed objects that own their own concerns.

Examples:

- Do not pass `"mlb"`; pass `League.MLB`.
- Do not pass `"COL"`; pass a `BaseballTeam` or `TeamId` resolved by the registry.
- Do not pass `"home_run"`; pass `BaseballGameEventType.HOME_RUN`.
- Do not pass `(64, 32)`; pass `DisplayProfile.SINGLE_64X32` or a `PanelGeometry` object.
- Do not pass loose priority floats everywhere; pass `DisplayPriority`/`PriorityScore` with reason codes.

## Enum policy

Crib the uploaded enum approach in spirit and implementation:

- `StrEnumMixin` for string-valued identifiers where the value is canonical and JSON-safe.
- `IntEnumMixin` for ordered severities/urgencies/priorities where comparison is meaningful.
- `try_coerce_enum()` for tolerant fixture/replay/config readers.
- Strict construction paths should fail fast; tolerant paths should only be used for debugging/replay/migration.

Recommended initial enum module: `omni/core/enum.py`.

## Package layout

```text
omni/
  core/
    enum.py
    ids.py
    time.py
    colors.py
    serialization.py
  domain/
    base.py
    teams.py
    athletes.py
    baseball.py
    football.py
    basketball.py
    hockey.py
    golf.py
  events/
    base.py
    baseball.py
    football.py
    basketball.py
    hockey.py
    golf.py
  cards/
    base.py
    baseball.py
    football.py
    basketball.py
    hockey.py
    golf.py
  queue/
    delay_buffer.py
    priority.py
    scheduler.py
  panels/
    profiles.py
    geometry.py
    layout_contract.py
  providers/
    base.py
    mlb_statsapi.py
    espn.py
    datagolf.py
    sportsdataio.py
  renderers/
    base.py
    profile_single_64x32.py
    profile_stack_64x64.py
    profile_quad_128x64.py
  preview/
    fixture_replay.py
    snapshot.py
    web.py
```

This can be introduced incrementally inside the upstream tree. Do not attempt a giant rewrite before the emulator works.

## Core enums

```python
from enum import Enum, IntEnum
from omni.core.enum import StrEnumMixin, IntEnumMixin

class Sport(StrEnumMixin, str, Enum):
    BASEBALL = "baseball"
    FOOTBALL = "football"
    BASKETBALL = "basketball"
    HOCKEY = "hockey"
    GOLF = "golf"

class League(StrEnumMixin, str, Enum):
    MLB = "mlb"
    NFL = "nfl"
    NBA = "nba"
    NHL = "nhl"
    PGA = "pga"
    NCAAF = "ncaaf"
    NCAAB = "ncaab"

    @property
    def sport(self) -> Sport:
        return {
            League.MLB: Sport.BASEBALL,
            League.NFL: Sport.FOOTBALL,
            League.NBA: Sport.BASKETBALL,
            League.NHL: Sport.HOCKEY,
            League.PGA: Sport.GOLF,
            League.NCAAF: Sport.FOOTBALL,
            League.NCAAB: Sport.BASKETBALL,
        }[self]

class PanelProfile(StrEnumMixin, str, Enum):
    SINGLE_64X32 = "single_64x32"
    STACK_64X64 = "stack_64x64"
    QUAD_128X64 = "quad_128x64"

class GameStatus(StrEnumMixin, str, Enum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    LIVE = "live"
    DELAYED = "delayed"
    SUSPENDED = "suspended"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"

class DisplayPriority(IntEnumMixin, IntEnum):
    BACKGROUND = 0
    NORMAL = 10
    FAVORITE = 20
    HIGH_LEVERAGE = 30
    ALERT = 40
    STICKY = 50

class UpdateUrgency(IntEnumMixin, IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    LIVE_CRITICAL = 3
```

## Event type enums

Keep event type enums sport-specific. A baseball event and a football event may both be “score”, but the downstream concerns are different.

```python
class BaseballGameEventType(StrEnumMixin, str, Enum):
    GAME_SCHEDULED = "game_scheduled"
    GAME_STARTED = "game_started"
    PITCH = "pitch"
    BALL = "ball"
    CALLED_STRIKE = "called_strike"
    SWINGING_STRIKE = "swinging_strike"
    FOUL = "foul"
    BALL_IN_PLAY = "ball_in_play"
    STRIKEOUT_LOOKING = "strikeout_looking"
    STRIKEOUT_SWINGING = "strikeout_swinging"
    WALK = "walk"
    HIT_BY_PITCH = "hit_by_pitch"
    SINGLE = "single"
    DOUBLE = "double"
    TRIPLE = "triple"
    HOME_RUN = "home_run"
    SAC_FLY = "sac_fly"
    SAC_BUNT = "sac_bunt"
    DOUBLE_PLAY = "double_play"
    TRIPLE_PLAY = "triple_play"
    RUN_SCORED = "run_scored"
    RBI = "rbi"
    PITCHING_CHANGE = "pitching_change"
    CHALLENGE = "challenge"
    ABS_CHALLENGE = "abs_challenge"
    INNING_START = "inning_start"
    HALF_INNING_END = "half_inning_end"
    GAME_FINAL = "game_final"
    NO_HITTER_ACTIVE = "no_hitter_active"
    NO_HITTER_BROKEN = "no_hitter_broken"
    PERFECT_GAME_ACTIVE = "perfect_game_active"
    PERFECT_GAME_BROKEN = "perfect_game_broken"
```

NFL/NBA/NHL/PGA should get parallel sport-specific enums, not a giant universal enum.

## Value object policy

Use frozen, slotted dataclasses for durable domain values.

```python
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Final

@dataclass(frozen=True, slots=True)
class SourceRef:
    name: str              # e.g. "mlb_statsapi", "espn", "datagolf"
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
```

Use `typing.NewType` only for very small internal distinctions. Prefer real value objects when behavior/validation/serialization is needed.

## Competitors: teams and golfers

Golf means not every league has teams. Model the more general concept first.

```python
from dataclasses import dataclass
from typing import Protocol

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
    logo: "LogoAsset"

    def best_text_color_on_primary(self) -> RGBColor:
        white = RGBColor(255, 255, 255)
        black = RGBColor(0, 0, 0)
        return white if white.contrast_ratio(self.primary_color) >= black.contrast_ratio(self.primary_color) else black

@dataclass(frozen=True, slots=True)
class BaseballTeam(Team):
    division: str | None = None
    league_side: str | None = None  # AL/NL, intentionally typed later if desired

@dataclass(frozen=True, slots=True)
class Golfer:
    id: LeagueScopedId
    display_name: str
    short_name: str
    country: str | None = None
```

A provider should resolve raw team/player IDs through a registry and return these objects. Renderers should not know ESPN/MLB raw IDs.

## Contest model

Use `Contest` instead of assuming team-vs-team `Game` everywhere.

```python
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
    away: Team
    home: Team

    def __post_init__(self) -> None:
        if self.away == self.home:
            raise ValueError("home and away teams must differ")

@dataclass(frozen=True, slots=True)
class GolfTournament(Contest):
    tournament_name: str = ""
    round_number: int | None = None
    cut_line: int | None = None
```

## GameEvent redesign

Generic event base:

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

EventTypeT = TypeVar("EventTypeT", bound=Enum)
PayloadT = TypeVar("PayloadT")

@dataclass(frozen=True, slots=True)
class EventImportance:
    priority: DisplayPriority
    urgency: UpdateUrgency
    leverage: float
    rarity: float
    favorite_relevance: float
    reasons: tuple[str, ...] = ()

    def combined_score(self) -> float:
        return (
            int(self.priority)
            + int(self.urgency) * 5
            + self.leverage * 20
            + self.rarity * 15
            + self.favorite_relevance * 20
        )

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
```

Baseball specialization:

```python
@dataclass(frozen=True, slots=True)
class BaseballCount:
    balls: int
    strikes: int
    outs: int

@dataclass(frozen=True, slots=True)
class BaseballBaseState:
    first: BaseballTeam | None = None  # replace with Runner once modeled
    second: BaseballTeam | None = None
    third: BaseballTeam | None = None

@dataclass(frozen=True, slots=True)
class BaseballPlayPayload:
    inning: int
    half: str  # later: HalfInning enum
    count: BaseballCount | None
    description: str
    rbi: int = 0
    pitch_type: str | None = None
    fielder_sequence: tuple[int, ...] = ()

@dataclass(frozen=True, slots=True)
class BaseballGameEvent(GameEvent[BaseballGameEventType, BaseballPlayPayload]):
    pass
```

Important: `fielder_sequence` is structured as `tuple[int, ...]`, not a string like `"9-6-4-"`. Rendering converts it to a string late and can avoid truncating dangling delimiters.

## ScoreboardCard redesign

`ScoreboardCard` should be a renderable domain object, not a loose payload bag.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

CardPayloadT = TypeVar("CardPayloadT")

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
    compromise_notes: tuple[str, ...] = ()
    # fallback_card_kind is deferred until the queue needs card substitution; the
    # implemented renderers honor all three profiles directly (see omni/renderers).

    def supports(self, profile: PanelProfile) -> bool:
        return profile in self.profiles

@dataclass(frozen=True, slots=True)
class CardPriority:
    band: DisplayPriority
    score: float
    reasons: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ScoreboardCard(Generic[CardPayloadT]):
    id: CardId
    kind: "CardKind"
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
```

## Card payload examples

```python
@dataclass(frozen=True, slots=True)
class LiveBaseballCardPayload:
    score: "ScoreState"
    count: BaseballCount
    base_state: BaseballBaseState
    inning: int
    half: str
    batter_name: str | None
    pitcher_name: str | None
    last_play: str | None
    no_hitter_badge: bool = False

@dataclass(frozen=True, slots=True)
class GolfLeaderboardCardPayload:
    tournament: GolfTournament
    leaders: tuple["GolfLeaderboardRow", ...]
    favorite_rows: tuple["GolfLeaderboardRow", ...]
    cut_line: int | None
```

## Provider boundaries

Provider modules own raw API shape. The rest of the system never sees raw JSON.

```python
class Provider(Protocol):
    source: SourceRef
    league: League

    def refresh(self) -> "ProviderUpdate": ...
```

Provider update should contain typed domain objects:

```python
@dataclass(frozen=True, slots=True)
class ProviderUpdate:
    source: SourceRef
    observed_at: datetime
    contests: tuple[Contest, ...]
    events: tuple[GameEvent, ...]
    warnings: tuple[str, ...] = ()
```

## Queue responsibilities

- `DelayBuffer`: owns TV-delay semantics.
- `PriorityScorer`: converts events/domain states into `CardPriority`.
- `CardFactory`: converts events into typed card payloads.
- `InterleavedCardQueue`: picks the next eligible card with fairness across leagues/contests.

Do not put delay logic in renderers.
Do not put provider polling logic in card classes.
Do not let one league's provider starve the display.

## Rendering contract

```python
class Renderer(Protocol[CardPayloadT]):
    supported_profiles: frozenset[PanelProfile]

    def render(
        self,
        card: ScoreboardCard[CardPayloadT],
        profile: PanelProfile,
        canvas: "Canvas",
    ) -> None: ...
```

Renderers are pure-ish: given a card, profile, and frame time, they draw a frame. They should not fetch data.

## Type-checking and code-quality policy

Start with incremental strictness rather than boiling the ocean.

Recommended practices:

- `from __future__ import annotations` in all new files.
- `@dataclass(frozen=True, slots=True)` for value/domain objects unless mutation is intentional.
- `Protocol` for provider/renderer interfaces.
- `typing.override` where Python version supports it, or `typing_extensions.override`.
- `assert_never` for exhaustive enum matching where practical.
- `Final` for constants.
- No `Any` in domain code without a comment explaining the boundary.
- No raw API JSON beyond `providers/*`.
- No renderer dependency on ESPN/MLB/StatsAPI field names.
- Keep serialization explicit: enums use `to_json_value()`.
- Use pytest fixture replay and golden-image snapshot tests.

Potential toolchain:

- `mypy` or `pyright` for new `omni/*` modules.
- `ruff` for linting and formatting if introduced carefully.
- `pytest` for fixtures and snapshots.
- Avoid Pydantic as a runtime dependency until Pi performance and packaging are checked. Pydantic is attractive for config/provider boundaries, but dataclasses plus explicit parser functions may be enough.

## Migration strategy

1. Add typed modules alongside upstream code.
2. Wrap existing upstream game data into typed objects without changing rendering.
3. Add typed cards for one MLB card.
4. Add renderer contract for that card across all three profiles.
5. Expand card-by-card.
6. Only then generalize for NFL/NBA/NHL/PGA.

## Anti-patterns to avoid

- Giant universal `GameEventType` enum covering all sports.
- Dict payloads that leak everywhere.
- Event priorities as unexplained floats.
- Hard-coded team strings in renderers.
- Cropping 128x64 cards down to 64x32.
- TV-delay implemented separately per sport.
- `git pull` cron with no rollback.
- Paid data as a default dependency.
