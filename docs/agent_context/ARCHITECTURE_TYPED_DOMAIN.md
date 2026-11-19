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

### As-built reconciliation (2026-06-23, after PRs #1–#13)

The package above is the *proposed* layout. It landed faithfully for `core/`, `domain/`, `events/`
(all sport taxonomies), `cards/`, **`queue/` (exact)**, and `providers/`. The deviations below are
deliberate; the renderer axis and sequencing are flagged for external review, not silently settled.

- **`renderers/` — per-card, not per-profile.** Built `base.py` + a hardware-agnostic `Canvas`
  protocol (`canvas.py`) with `RecordingCanvas`/`PillowCanvas`/`MatrixCanvas` adapters + `font.py`,
  and one renderer per *card* (`live_baseball.py`) that handles all three profiles via
  `assert_never` dispatch — instead of the proposed `profile_single_64x32.py` / `_stack_64x64` /
  `_quad_128x64` modules. Rationale: a card's layout is cohesive across profiles, and the `Canvas`
  seam lets one renderer drive a golden image, a recording double, or a real panel. **Axis inversion —
  for the head to bless.**
- **`core/serialization.py` — absent.** Serialization is inline on enums (`to_json_value()`); no
  central module was needed yet.
- **`panels/` — `geometry.py` only.** `PanelProfile` lives in `core/enum.py` (subsumes `profiles.py`);
  the `layout_contract.py` concept is realized as `cards/base.py::LayoutSupport` + a renderer's
  `supported_profiles`.
- **`preview/` — `cli.py` + `scenario.py` + `__main__.py`.** `python -m omni.preview` replaces the
  proposed `web.py`/`fixture_replay.py`; golden snapshots live in `tests/` (run via pytest) rather
  than a `snapshot.py` module / `omni snapshot` CLI.
- **`CardFactory` in `cards/factory.py`**, not `queue/` (it produces cards; the queue consumes them).
- **Baseball value types** (`HalfInning`, `BaseballCount`, `BaseballBaseState`) + the live
  `BaseballGameState` snapshot live in `domain/baseball.py` (relocated there in PR #9, re-exported
  from `events`/`cards` for back-compat). **`providers/mlb_teams.py`** (the team registry) was added.
- **Sequencing:** the queue (`omni/queue/`) was built before MLB feature-completeness, inverting the
  kernel roadmap's "MLB polish → queue" order. MLB is live-card-only so far. **For the head to
  sequence.**

The frozen original layout is preserved at `docs/agent_context/kernel/`.

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

Provider modules own raw API shape. The rest of the system never sees raw JSON. As built since H5
(2026-11-17; `omni/providers/base.py`, `omni/app/contest_store.py`):

- **Scopes.** A schedule is asked scope by scope: `PollScope(source, league, window)`, where the window is
  a `LocalDate` or a `CurrentSlate`. Each answer is a `ScopeObservation`: `COMPLETE`, `PARTIAL` (the
  parser skipped rows, so what it leaves out isn't refuted) or `FAILED`, stamped with its receipt as a
  `ClockReading`. A `ScopeSource` (MLB's is `PlannedMlb`) reports each pass's `ScopeSpan` (its current
  scope and the earlier ones still in its window) and its observations. A plain `Provider` with
  `refresh(now) -> ProviderUpdate` is wrapped by `ProviderSupervisor` as one `CurrentSlate` scope.
- **The store** keeps each scope's membership and reconciles the union by full id:
  - observations are ordered by **monotonic receipt** per scope, never UTC;
  - a complete answer replaces its scope's members, and an empty one removes only those;
  - a partial answer adds and updates; a failed one keeps what was there;
  - the newest receipt wins across scopes;
  - rolled-off scopes retire under `RollingDates`, keeping a game still in play, and a final through
    `FINAL_HOLD`.
- **Health is per scope** (`ScopeHealth`, on the monotonic clock). A league's health is its current
  scope's (`LeagueHealth`), so one league can be offline while another is live. The board draws one
  health today, its healthiest league's; per-league dimming lands with the shared board.
- **Identity.** Every key that names a game uses its full `LeagueScopedId` (`league:source:raw`): card
  ids, dedupe keys, booking, traces, market caches. Favorites are typed and ranked per league
  (`omni/preferences/favorites.py`; `--favorite mlb:ATL`, a bare abbreviation meaning MLB). A market link
  (`MarketLink`) is confirmed by slug, start time and side, and fails closed.
- **What a value rests on.** Game states carry a `CoverageMap` of `FieldCoverage` per field (`REPORTED`,
  `INFERRED`, `DEFAULTED`, `UNAVAILABLE`, `NOT_APPLICABLE`; `omni/domain/coverage.py`). Displayed values
  keep their stand-ins; `state.known(field)` is what a leverage model reads.
- **Schedule facts** on `Contest`: `stage` (`SeasonStage`), `game_number`, `start_tbd`, `provider_date`,
  `neutral_site`, `national_broadcast` and `resumed_from`, each unknown by default.

- **Each league reaches its own card pipeline.** Baseball goes to `LiveBaseballPipeline`, NFL to
  `LiveFootballPipeline` (`omni/app/nfl_pipeline.py`), both into one queue; any other league's games are
  members and count toward its health, but have no cards (`AppLoop.refresh`). NFL's slate and feeds come from
  `PlannedNfl` (`omni/app/planned_nfl.py`) on the same `PollPlan` and quota ledger as MLB: the day's list once,
  each favorite's game at 10 s with its plays, the others in one `phase=in` read every 45 s, and one read per
  other final. `--leagues mlb,nfl` (opt-in; the default is `mlb`) and `NFLMETA_API_KEY` put NFL on the board.
- **Quota, per provider account** (`omni/app/quota.py`, `quota_store.py`):
  - **Accounts and limits:** each account the board spends (`ProviderAccount`: StatsAPI, NFLMeta,
    BALLDONTLIE, Polymarket US, Bovada, Omni's market service) has declared limits: a minute, a month's
    requests, a month's rows, asks at once. NFLMeta's follow `--nflmeta-plan` (Free unless the user says
    otherwise).
  - **Charging:** `QuotaLedger` counts every HTTP response where it's made (`Meter`, `MeteredHttp`), from
    the rows header when there is one. The provider's own "remaining" can raise the count, never lower it.
  - **Admission:** `PollPlan` asks the ledger before each ask goes out. A key's account comes from an
    explicit `AccountMap`, so an NFL slate in MLB's plan charges NFLMeta. Every wire request is admitted too
    (`QuotaLedger.admit_request`): an ask's first request uses its admission's place, and each later page or
    retry is admitted on its own, in the ask's class; a refused one is never sent (`QuotaRefused`). A request
    that got no answer is charged as one. Both NFL entry points (`PlannedNfl`, the solo runner's `GameFeed`) fetch
    inside `Admission.asking()`.
  - **One process per account:** NFLMeta takes an exclusive file lock (`nflmeta.lock` under `--state-dir`,
    `quota_store.FileLocks`), so a second process sharing the state dir refuses to poll it
    (`Refusal.ELSEWHERE`, naming the holder); the solo runner exits saying so, and `tools/nflmeta_record.py`
    takes the same lock.
  - **Shedding:** as an account runs down, it sheds by class (`SHEDDING`). Backfill goes above 50% left.
    Background goes above 25%, then at double cadence to 10%. Normal goes above 5%, and focus until the
    month is out. The last 20% of each minute is held for normal and focus.
  - **What a refusal says:** a refused ask stays due, and its reason reaches the league's health, which
    carries each league's quota reason apart (`LeagueHealth.quota`); the trace writes it on a line of its own
    when it changes, so NFL's stays visible beside a healthy MLB.
  - **NFL's day list:** goes as focus on a board with an NFL favorite (so a cold start near the month's end
    still finds its games), and is rechecked as background: hourly in the 3 hours before the first kickoff,
    and 30 minutes after each window's kickoffs while a later one is ahead (`NflSchedulePolicy`). A game a
    recheck drops before kickoff leaves its date; one dropped after kickoff is still read.
  - **Persistence:** monthly usage persists under `--state-dir`, written atomically at most once a minute
    and at shutdown.
- **Versions** (`omni/app/versions.py`): every run names its policy (`omni-board-policy/1` for the shared
  board by default, `/2` with `--board-policy 2`, `omni-nfl-solo/1` for the NFL runner), its config schema and the digest of the settings
  that change decisions, its rules (`omni/domain/rules.py`: MLB extra innings and NFL overtime by stage and
  season), its models (none yet) and its code's commit. Traces open with them, and replay decisions with
  all but the commit. Config migrations are typed and keep the original entry
  (`ConfigMigration.BARE_FAVORITE_IS_MLB`).
- **Rights** (`omni/providers/rights.py`): a typed manifest of each source's terms. It covers display,
  cache, archive (none, a diagnosis with a retention period, or kept), training, redistribution,
  attribution, termination and key sharing; an unstated term counts as refused. Every recording folder
  gets a `RETENTION.json` with its delete-by date, and the expiry sweep (`omni/app/expiry.py`;
  `tools/recordings_due.py --expire`, and the NFL runner at start and exit) mines an overdue NFLMeta folder
  into `build/telemetry/` (`omni/telemetry/`), then deletes it. Our decision telemetry (ids, kinds, reasons,
  timings) is ours to keep; a provider's wording follows its source's archive rule, and lands only in a
  stamped folder. Beside the terms sits the user's ruling: showing data on the family's boards from a
  short-lived copy that expires is display, not redistribution (`ProjectUse.FAMILY_BOARD_DISPLAY`).

Plays travel with each game's feed, not in `ProviderUpdate`.

## The board policy (`omni-board-policy/2`, as built)

`docs/agent_context/BOARD_POLICY.md` is the spec. The pieces:

- **Released inputs** (`omni/policy/view.py`): each refresh, the league pipelines describe every carded game
  from released state only (`GameFacts`: leverage, current or stale, records frozen at the start, the break
  evidence and newest action that have cleared the viewer's delay) and offer cards for promotion, as one
  `PolicyView` published with the cards (`BoardInput.policy`).
- **Leverage** (`omni/policy/leverage.py`): the labeled `PROXY` from score, time and progress until a league's
  model passes validation; a model's `z` and `R` tracker are ready for when one does.
- **Allocation** (`allocation.py`, `ledger.py`, `pool.py`): game weights (`G`, rank `F`, stakes `T`, records
  `Q`), league and game shares, the favorite floor scaled by leverage, and turns chosen by debt on confirmed
  airtime, with clamps and rescue.
- **Interrupts and breaks** (`interrupts.py`, `promotion.py`, `leases.py`, `ladder.py`): interrupt value
  against the favorites' bar, the 24 s hold, the 60 s cooldown and lifetimes; break leases from typed
  `BreakEvidence` (`omni/domain/breaks.py`, detected per league in `omni/events/mlb_breaks.py` and
  `nfl_breaks.py`); the ordinary-card ladder and the 30 s non-base cap.
- **The director** (`omni/tower/director.py`): owns that state for one board and answers the board's two
  questions, what next and whether the card up yields, keeping the board's contract (dwell from the
  commit, protected minimums, hand-off to a final, frames). Its decisions reach the trace with their
  factors and a plain "why this card" line (`omni/app/trace.py`).
- **Settings** (`omni/policy/settings.py`): `--board-policy 1|2`, `--mute`, `--league-weight`,
  `--no-promotions`, `--slot`. A loop built without settings keeps the old bands, as the older replays do.
- **Promotions name their event:** every scored offer names a card fixed for that event's revision (an MLB
  big-play card; NFL's per-play card, `<game>:play:<moment>:r<revision>`, the field its answer showed with the
  play as caption). A view that no longer carries an offer withdraws it; a story that aired is never promoted
  again. An unseen decisive event or feat is kept as a `ResultSummary` (`omni/tower/summary.py`) and leads its
  final as an 8 s still, fresh for 5 minutes after the final is eligible.
- **Leases at the physical start:** a lease is anchored at its evidence's pinned release less the age the break
  had when it was detected; a guest's grant (`LeaseGrant`) is stamped at hand-off and checked again when its
  first frame lands (`PolicyDirector.grant_refusal`); a lapsed one is booked as an accidental display.

## Output and interruptions

- **Receipts:** a synchronous sink (the emulator) books a frame when its commit returns. A sink that
  acknowledges frames (`StreamDisplaySink`, `--sink-command` on both apps) books a card's first frame only on
  the receiver's matching `SHOWN`, at the time it was heard; a frame replaced before it was confirmed is never
  credited, and one landing after its latest start or its grant is an accidental display (`LANDED_LATE`,
  `GRANT_LAPSED`). An unacknowledging (older) receiver is noted once; policy 2's choices then count for nothing
  and policy 1's bands rotate.
- **The receiver** (`tools/board_frame_sink.py`, logic in `omni/app/receiver.py`): announces a boot id; takes
  sessioned frames and heartbeats (`OMN3`, `OMNH`), keeps one waiting frame, rejects an older session, and
  blanks the panel 8 s after the host's last word, even on an unchanged frame. An older end on either side
  falls back to `OMN2` without the watchdog.
- **Interruptions** (`omni/tower/interruption.py`): a host suspend (suspend-inclusive clock minus monotonic
  over 5 s; without that clock, wall minus monotonic over 30 s, clock untrusted), a process stall (over 30 s
  past the 5 s health tick) or an output loss (a new receiver session, a `BLANK`, 8 s of silence). On a host
  gap, polls asked before it file nothing, the pipelines drop what they held (states, plays, breaks, captions,
  offers, per-play cards), draw the field stale until a state answered after it clears the delay, and
  distrust the clock so the next reads wait their full delay; the director closes its leases and withdraws
  pending promotions. On an output loss, accounting stops at the receiver's last word and the unknown time is
  recorded; cooldowns, debt and an owed recovery stand.

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
