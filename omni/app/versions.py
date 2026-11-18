"""What a run decides with: the board policy, the settings, the rules, the models and the code.

A decision can only be compared with another made under the same things, so every run names them in
one `Versions` record:
- **policy:** the board policy in force, by its published name (`PolicyName`);
- **config:** the settings that change what the board decides (`DecisionConfig`), under a schema version,
  as a short digest of their canonical rendering. The rendering itself is logged once at startup, so a
  digest can be read back. Keys, URLs, the display's command and the frame rate are left out: they change
  where things come from or go, not what is decided;
- **rules:** the version of the rule table (`omni.domain.rules`);
- **models:** none yet. The slot is here so the first one has a place;
- **source:** the running code's git commit, or "unknown" when it can't be read;
- **migrations:** each older setting a run read as today's, with the entry as it was given.

The running MLB board's trace writes the record when the run starts and again only if it changes
(`omni.app.trace`), the NFL runner writes it through its log, and a replay's decisions open with its
header, which leaves out the code's commit (it changes every commit, and replay fixtures must not) and
the migrations (a replay is given its settings typed).

The record lives with the running app because its settings are the app's: favorites, the scroll speed,
the play feed's pace. Replays build on the app the same way.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from omni.core.enum import PanelProfile, StrEnumMixin
from omni.domain.rules import RulesVersion
from omni.preferences.favorites import AppliedMigration, Favorites
from omni.providers.nflmeta import PlayFeedPace
from omni.renderers.text_lane import ScrollSpeed

__all__ = [
    "CONFIG_SCHEMA",
    "ConfigSchema",
    "DecisionConfig",
    "ModelVersion",
    "PolicyName",
    "SourceRevision",
    "Versions",
    "read_source_revision",
]

_DIGEST_LENGTH = 12  # hex characters of the sha256: plenty to tell a few configurations apart


class PolicyName(StrEnumMixin, str, Enum):
    """A board policy a run can decide with, by its published name."""

    BOARD_V1 = "omni-board-policy/1"  # the shared board's scheduler bands (`omni.queue.scheduler`)
    BOARD_V2 = (
        "omni-board-policy/2"  # `BOARD_POLICY.md`: leverage, ranked favorites, debt and interrupts (`omni.policy`)
    )
    NFL_SOLO_V1 = "omni-nfl-solo/1"  # the NFL single-game runner's own focus (`omni.app.football_board`)


class ConfigSchema(StrEnumMixin, str, Enum):
    """The version of `DecisionConfig`'s settings and their rendering."""

    V1 = "omni-config/1"


CONFIG_SCHEMA = ConfigSchema.V1  # the schema in force


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionConfig:
    """The settings that change what a board decides, as a run has them in effect.

    A setting the runner doesn't have stays None and is left out of the rendering, so adding one later
    leaves the digests of runs without it as they were.
    """

    profile: PanelProfile
    delay: timedelta  # the broadcast delay the board holds its feed back by
    favorites: Favorites | None = None  # in rank order
    turn: timedelta | None = None  # a rotation card's turn
    takeover_cap: timedelta | None = None  # takeover airtime back to back
    timezone: ZoneInfo | None = None  # the schedule's day and the board's clocks
    meter: bool | None = None  # the live win meter
    odds: bool | None = None  # sportsbook odds on the pregame card
    scroll_speed: ScrollSpeed | None = None
    halftime_hold: timedelta | None = None  # how long after halftime begins the feed isn't asked
    play_feed: PlayFeedPace | None = None
    meter_lag: timedelta | None = None  # how far the win meter trails the market

    def canonical(self) -> str:
        """The settings in force, sorted by name, e.g. ``delay=45s;favorites=mlb:ATL,mlb:COL;meter=off;...``."""
        settings = ((field.name, getattr(self, field.name)) for field in fields(self))
        return ";".join(f"{name}={_rendered(value)}" for name, value in sorted(settings) if value is not None)

    @property
    def digest(self) -> str:
        """A short sha256 of the canonical rendering: equal settings, equal digests."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]

    def line(self) -> str:
        """The rendering a digest stands for, as a log line: ``config <digest>: <canonical>``."""
        return f"config {self.digest}: {self.canonical()}"


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """A model a decision reads, by its registry id. None is built yet."""

    model_id: str


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """The running code's git commit; None when it couldn't be read."""

    commit: str | None

    def __str__(self) -> str:
        return self.commit if self.commit is not None else "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class Versions:
    """What a run decides with. Two runs that decide alike on the same input share all but `source`."""

    policy: PolicyName
    config: DecisionConfig
    rules: RulesVersion
    models: tuple[ModelVersion, ...] = ()
    source: SourceRevision = SourceRevision(None)
    migrations: tuple[AppliedMigration, ...] = ()
    schema: ConfigSchema = CONFIG_SCHEMA

    def line(self) -> str:
        """The run's versions line: the header's, then the code's commit and the migrations applied."""
        migrations = ", ".join(_migration(each) for each in self.migrations) or "none"
        return f"{self.header()} | source {self.source} | migrations {migrations}"

    def header(self) -> str:
        """A replay's first line: policy, config schema and digest, rules and models; nothing that changes
        with each commit."""
        models = ", ".join(model.model_id for model in self.models) or "none"
        return (
            f"versions: policy {self.policy} | config {self.schema} {self.config.digest} | rules {self.rules}"
            f" | models {models}"
        )


def read_source_revision(repo: Path, *, run: Callable[[Sequence[str]], str] | None = None) -> SourceRevision:
    """The commit checked out at ``repo``, read with ``git``; unknown if git can't say."""
    read = run if run is not None else _git
    try:
        commit = read(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]).strip()
    except (OSError, subprocess.SubprocessError):
        return SourceRevision(None)
    return SourceRevision(commit or None)


def _git(command: Sequence[str]) -> str:  # pragma: no cover - runs git
    return subprocess.run(command, capture_output=True, text=True, timeout=5).stdout


def _rendered(value: bool | timedelta | Favorites | ZoneInfo | Enum) -> str:
    """One setting as the canonical rendering writes it."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, timedelta):
        return f"{value.total_seconds():g}s"
    if isinstance(value, Favorites):
        return ",".join(f"{league}:{team}" for league, teams in value.ranked for team in teams) or "none"
    if isinstance(value, ZoneInfo):
        return value.key
    return str(value.value)


def _migration(applied: AppliedMigration) -> str:
    return f"{applied.migration}({applied.original}->{applied.migrated})"
