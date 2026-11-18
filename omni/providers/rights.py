"""What each outside source's terms let us do with its data, and how long a recording of it may be kept.

`MANIFEST` holds one `SourceRights` per source the project reads or ships. Each says whether we may:
- **display** it: draw it on a board, holding the latest answer in memory while it's shown;
- **cache** it: "normal application caching", keeping answers in a store outside the board's own
  process (on disk, or a shared service like Omni's market service) to serve them again;
- **archive** it: keep a recording past its use. `Archive.DIAGNOSIS` allows one only to diagnose a
  problem, deleted after the entry's `retention`; `Archive.KEEP` allows keeping it;
- **train** models on it, and **redistribute** it;
- share one **key** between boards.
It also gives the attribution the source requires, what happens to what we hold when access ends, and
where and when we read the terms. A permission the terms don't speak to is `Permission.UNSTATED`, which
counts as refused: no permission is assumed.

Telemetry comes in two classes (`Telemetry`). **Our own decision telemetry** (ids, kinds, reasons, timings,
versions, counts) may be kept. **Third-party content** (raw rows, play text, injury text, anything a
provider worded) follows its source's archive rule. A recording is third-party content: each recording
folder gets a `RETENTION.json` stamp when it's made (`stamp_recording`), naming the source, when it was
made, when it must be deleted by, and the terms' date. `recordings_due` lists recording folders with their
stamps, reading nothing but the stamps.

The data expires on its own: `sweep_expired` deletes each stamped folder past its delete-by, after offering
it to its source's `Miner`, which may keep our own telemetry from it first. A miner's failure never keeps an
overdue folder: the terms bind, and mining is only our benefit. The sweep never deletes an unstamped folder
(that's the user's step, by the source's rule), never one whose source's terms allow keeping it, and never
anything a link under its root points to.

Beside each source's terms stands the project's own use of its data (`ProjectUse`), and the user's ruling
on that use (`RULINGS`). The one use so far is `ProjectUse.FAMILY_BOARD_DISPLAY`: showing a source's data on
the family's own boards. Each entry answers whether its data may reach a family board that way
(`FamilyDisplay`), and says where the answer comes from: the source's terms, or the user's ruling. The
terms stay as written; the ruling never turns a refusal in them into a permission.

`READS` says which sources each module reads or ships, so a new provider without an entry fails a test.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol

from omni.core.enum import StrEnumMixin

__all__ = [
    "FAMILY_DISPLAY_RULING",
    "MANIFEST",
    "READS",
    "RETENTION_FILE",
    "RULINGS",
    "Archive",
    "Basis",
    "DataSource",
    "DueRecording",
    "DueStatus",
    "FamilyDisplay",
    "Miner",
    "Mining",
    "OnTermination",
    "Permission",
    "ProjectUse",
    "RetentionStamp",
    "Ruling",
    "SourceRights",
    "SweepAction",
    "SweepReport",
    "SweptRecording",
    "Telemetry",
    "read_stamp",
    "recordings_due",
    "retention_stamp",
    "stamp_recording",
    "sweep_expired",
]

RETENTION_FILE = "RETENTION.json"
_STAMP_VERSION = 1


class DataSource(StrEnumMixin, str, Enum):
    """An outside source the project reads or ships. A provider's `SourceRef` name is its value."""

    MLB_STATSAPI = "mlb_statsapi"
    NFLMETA = "nflmeta"
    BALLDONTLIE = "balldontlie"
    POLYMARKET_US = "polymarket_us"
    BOVADA = "bovada"
    OMNI_MARKET_SERVICE = "omni_market_service"
    NFLVERSE = "nflverse"
    RETROSHEET = "retrosheet"
    WIKIPEDIA_COLORS = "wikipedia_colors"
    TEAM_LOGOS = "team_logos"


class Permission(StrEnumMixin, str, Enum):
    """What a source's terms say about one use."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNSTATED = "unstated"  # the terms we read don't say: counts as refused

    @property
    def granted(self) -> bool:
        return self is Permission.ALLOWED


class Archive(StrEnumMixin, str, Enum):
    """Whether a source's data may be kept past its use."""

    NONE = "none"
    DIAGNOSIS = "diagnosis"  # only to diagnose a problem, and deleted after the entry's retention
    KEEP = "keep"


class OnTermination(StrEnumMixin, str, Enum):
    """What happens to what we hold of a source when our access ends."""

    DELETE = "delete"
    KEEP = "keep"


class Telemetry(StrEnumMixin, str, Enum):
    """The two classes of what the project writes down."""

    OWN_DECISIONS = "own_decisions"  # ids, kinds, reasons, timings, versions, counts: may be kept
    THIRD_PARTY_CONTENT = "third_party_content"  # anything a provider worded: its source's archive rule


class ProjectUse(StrEnumMixin, str, Enum):
    """A use the project makes of a source's data, which its terms may not name as such."""

    # Showing a source's data on the family's own boards, from a short-lived copy that expires: never
    # archived, never sold, never shown to the public.
    FAMILY_BOARD_DISPLAY = "family_board_display"


@dataclass(frozen=True, slots=True, kw_only=True)
class Ruling:
    """The user's ruling on one of the project's uses: when it was made, what it holds, the condition it rests
    on, and when to look at it again."""

    use: ProjectUse
    ruled: date
    holds: str
    condition: str  # what every reliance on the ruling must keep true
    revisit: str


FAMILY_DISPLAY_RULING = Ruling(
    use=ProjectUse.FAMILY_BOARD_DISPLAY,
    ruled=date(2026, 11, 17),
    holds="Friends and family seeing a source's data briefly on their own boards isn't a redistribution of it.",
    condition="the copy that reaches a board expires quickly: never archived, sold or shown to the public",
    revisit="revisit before any commercial use",
)
RULINGS: Mapping[ProjectUse, Ruling] = {FAMILY_DISPLAY_RULING.use: FAMILY_DISPLAY_RULING}


class Basis(StrEnumMixin, str, Enum):
    """Where an answer about a use comes from."""

    TERMS = "terms"  # the source's own terms settle it
    RULING = "ruling"  # the terms grant display or leave it open, and the user's ruling settles the rest


@dataclass(frozen=True, slots=True, kw_only=True)
class FamilyDisplay:
    """Whether a source's data may reach a family board as `ProjectUse.FAMILY_BOARD_DISPLAY`, on what basis,
    and how soon its longest-lived copy on the way expires.

    An answer that rests on the ruling keeps the ruling's condition, so it records a bounded expiry: the
    longest any copy the use makes (a cache's row, a diagnosis recording, a run log) is kept.
    """

    allowed: bool
    basis: Basis
    expiry: timedelta | None = None
    why: str

    def __post_init__(self) -> None:
        if self.basis is Basis.RULING and self.allowed and (self.expiry is None or self.expiry <= timedelta(0)):
            raise ValueError("a family display that rests on the ruling keeps its condition: a bounded expiry")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRights:
    """One source's terms, as far as they bear on what the project does with its data, and the answer for the
    family's boards."""

    source: DataSource
    terms_url: str | None  # None when no single page holds them
    terms_read: date
    display: Permission
    cache: Permission
    archive: Archive
    retention: timedelta | None = None  # how long a diagnosis recording may be kept; only for DIAGNOSIS
    train: Permission
    redistribute: Permission
    attribution: str | None  # the text the source requires, or None
    on_termination: OnTermination
    key_shared: Permission | None  # one key on several boards; None when the source takes no key
    notes: str
    family_display: FamilyDisplay

    def __post_init__(self) -> None:
        if (self.archive is Archive.DIAGNOSIS) != (self.retention is not None):
            raise ValueError(f"{self.source}: a retention goes with a diagnosis archive, and only with one")
        answer = self.family_display
        if answer.allowed and self.display is Permission.DENIED:
            raise ValueError(f"{self.source}: its terms bar display, so no family board may show it")
        if answer.allowed and answer.basis is Basis.TERMS and not self.display.granted:
            raise ValueError(f"{self.source}: its terms don't grant display, so they can't be what allows it")


_RETROSHEET_NOTICE = (
    "The information used here was obtained free of charge from and is copyrighted by Retrosheet.  Interested"
    ' parties may contact Retrosheet at "www.retrosheet.org".'
)

_NFLMETA_RETENTION = timedelta(days=7)  # the user's rule for NFLMeta's recordings and run logs, since 2026-11-15
# The market service's longest-lived copy: its S3 objects expire a day after they're written. Its table rows
# expire after an hour, and a board ignores a price more than a minute old.
_MARKET_SERVICE_COPIES = timedelta(days=1)

# Where each term comes from: "SOURCES.md" is docs/agent_context/SOURCES.md, already verified there;
# "read 2026-11-17" is a read of the terms themselves on that day, for this manifest.
_ENTRIES = (
    SourceRights(
        source=DataSource.MLB_STATSAPI,
        terms_url="http://gdx.mlb.com/components/copyright.txt",  # the notice StatsAPI's `copyright` field names
        terms_read=date(2026, 11, 17),  # read 2026-11-17
        display=Permission.ALLOWED,  # read 2026-11-17: "individual, non-commercial, non-bulk use" is permitted
        cache=Permission.UNSTATED,  # read 2026-11-17: the notice doesn't speak to it
        archive=Archive.NONE,  # read 2026-11-17: any other use needs MLBAM's written authorization; stricter
        train=Permission.DENIED,  # read 2026-11-17: bulk use isn't permitted
        redistribute=Permission.DENIED,  # read 2026-11-17: only individual, non-commercial use
        attribution=None,  # read 2026-11-17: the notice asks for none
        on_termination=OnTermination.DELETE,  # no agreement to end; the stricter value
        key_shared=None,
        notes="MLBAM's notice: individual, non-commercial, non-bulk use only; every other use needs its consent.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.TERMS,
            why="Individual, non-commercial use: each board asks for its own owner and holds only what it shows.",
        ),
    ),
    SourceRights(
        source=DataSource.NFLMETA,
        terms_url="https://nflmeta.org/terms",
        terms_read=date(2026, 11, 17),  # read 2026-11-17 (last updated 2026-09-19); SOURCES.md read 2026-11-15
        display=Permission.ALLOWED,  # SOURCES.md: Free is personal, non-commercial; paid plans may display scores
        cache=Permission.ALLOWED,  # SOURCES.md: "normal application caching", never a standing copy
        archive=Archive.DIAGNOSIS,  # SOURCES.md: no standing copy, no systematic accumulation
        retention=_NFLMETA_RETENTION,
        train=Permission.DENIED,  # read 2026-11-17: "does not include model training or fine-tuning"
        redistribute=Permission.DENIED,  # read 2026-11-17: no redistributing, republishing or reselling
        attribution=None,  # read 2026-11-17: none of its own; material from nflverse carries nflverse's
        on_termination=OnTermination.DELETE,  # SOURCES.md: termination doesn't entitle keeping; a breach deletes
        key_shared=Permission.UNSTATED,  # read 2026-11-17: keys stay server-side and the holder answers for them
        notes="No standing copy and no training; a recording is for a diagnosis, deleted within 7 days.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.RULING,
            expiry=_NFLMETA_RETENTION,
            why=(
                "Its terms bar redistributing it; the ruling holds the family seeing it briefly isn't that. Boards"
                " hold it in memory, and its words land only in stamped folders deleted within 7 days."
            ),
        ),
    ),
    SourceRights(
        source=DataSource.BALLDONTLIE,
        terms_url="https://www.balldontlie.io/terms",
        terms_read=date(2026, 11, 16),  # SOURCES.md: terms last updated 2026-10-20, re-checked 2026-11-16
        display=Permission.ALLOWED,  # SOURCES.md: §6, any lawful personal or commercial use
        cache=Permission.ALLOWED,  # SOURCES.md: copying, caching, storing and archiving are allowed
        archive=Archive.KEEP,  # SOURCES.md: no retention deadline
        train=Permission.ALLOWED,  # BOARD_POLICY.md §2: its terms allow fitting a model on its archive
        redistribute=Permission.DENIED,  # SOURCES.md: §6 bars a substitute feed; serving our own boards is allowed
        attribution=None,  # SOURCES.md: no attribution is required
        on_termination=OnTermination.KEEP,  # SOURCES.md: §17, data may be kept after access ends
        key_shared=Permission.DENIED,  # SOURCES.md: §5 and §7(b) bar sharing keys
        notes="One key in a central poller may serve every Omni board; a key on each board would be sharing it.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.TERMS,
            why="§6 allows any lawful personal or commercial use, and serving Omni's own boards isn't a substitute feed.",
        ),
    ),
    SourceRights(
        source=DataSource.POLYMARKET_US,
        terms_url="https://www.polymarketexchange.com/files/legal/latest/participant-agreement",
        terms_read=date(2026, 11, 1),  # SOURCES.md files it with the odds research of 2026-11-01
        display=Permission.UNSTATED,  # SOURCES.md: the agreement read binds key holders; nothing on display
        cache=Permission.UNSTATED,  # SOURCES.md: nothing on caching
        archive=Archive.NONE,  # nothing allows keeping it; the stricter value
        train=Permission.UNSTATED,  # SOURCES.md: nothing on training
        redistribute=Permission.DENIED,  # SOURCES.md: clause 22, "may not be redistributed except as PMUS ... permits"
        attribution=None,
        on_termination=OnTermination.DELETE,  # nothing allows keeping it; the stricter value
        key_shared=Permission.UNSTATED,  # SOURCES.md: the agreement binds each account holder
        notes="Public market data needs no key; the keyed stream's key stays in Omni's market service.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.RULING,
            expiry=_MARKET_SERVICE_COPIES,
            why=(
                "Clause 22 bars redistributing it and nothing in the agreement speaks to display; the ruling holds"
                " the family seeing a price briefly isn't redistribution. The market service's copies expire"
                " within a day, and a board ignores a price a minute old."
            ),
        ),
    ),
    SourceRights(
        source=DataSource.BOVADA,
        terms_url="https://www.bovada.lv/terms-of-service",
        terms_read=date(2026, 11, 2),  # SOURCES.md: read through a summarizing fetch
        display=Permission.ALLOWED,  # SOURCES.md: 8.1, content licensed for private personal use
        cache=Permission.DENIED,  # SOURCES.md: 9.3, no storing in another electronic retrieval system
        archive=Archive.NONE,  # SOURCES.md: 9.3
        train=Permission.DENIED,  # SOURCES.md: licensed for private personal use only; 9.9, no commercial use
        redistribute=Permission.DENIED,  # SOURCES.md: 9.3
        attribution=None,
        on_termination=OnTermination.DELETE,  # nothing allows keeping it; the stricter value
        key_shared=None,
        notes="Private personal use: a board shows pregame lines for its owner, and the cloud poller leaves it off.",
        family_display=FamilyDisplay(
            allowed=False,
            basis=Basis.TERMS,
            why="9.3 bars storing it in another system, so it can't reach a board from a cache; 8.1 still lets a"
            " board show its owner's pregame lines.",
        ),
    ),
    SourceRights(
        source=DataSource.OMNI_MARKET_SERVICE,
        terms_url="https://github.com/ajszoke/omni-scoreboard/blob/main/cloud/README.md",  # our own service
        terms_read=date(2026, 11, 17),  # read 2026-11-17: cloud/README.md
        display=Permission.ALLOWED,
        cache=Permission.ALLOWED,  # read 2026-11-17: its rows expire after an hour, and its S3 copies after a day
        archive=Archive.NONE,  # what it holds is Polymarket US's prices, so Polymarket's rule
        train=Permission.UNSTATED,  # Polymarket US's rule
        redistribute=Permission.DENIED,  # it serves Omni's boards only
        attribution=None,
        on_termination=OnTermination.DELETE,
        key_shared=Permission.DENIED,  # read 2026-11-17: one token per board
        notes="Omni's own relay of market prices to its boards; what it holds follows the market's source.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.RULING,
            expiry=_MARKET_SERVICE_COPIES,
            why="It relays Polymarket US's prices, so Polymarket's answer: rows expire after an hour, S3 copies a day.",
        ),
    ),
    SourceRights(
        source=DataSource.NFLVERSE,
        terms_url="https://github.com/nflverse#licensing",
        terms_read=date(2026, 11, 17),  # read 2026-11-17; SOURCES.md: the team table is CC BY 4.0
        display=Permission.ALLOWED,  # read 2026-11-17: CC BY 4.0, any use with attribution
        cache=Permission.ALLOWED,
        archive=Archive.KEEP,
        train=Permission.ALLOWED,
        redistribute=Permission.ALLOWED,  # with the attribution
        attribution="Data from nflverse (https://github.com/nflverse), CC BY 4.0.",
        on_termination=OnTermination.KEEP,
        key_shared=None,
        notes="FTN's charting columns are CC BY-SA 4.0 (share-alike), so they're excluded; nothing here reads them.",
        family_display=FamilyDisplay(
            allowed=True, basis=Basis.TERMS, why="CC BY 4.0 allows any use, with the attribution."
        ),
    ),
    SourceRights(
        source=DataSource.RETROSHEET,
        terms_url="https://www.retrosheet.org/notice.txt",
        terms_read=date(2026, 11, 17),  # read 2026-11-17
        display=Permission.ALLOWED,  # read 2026-11-17: "free to make any desired use of the information"
        cache=Permission.ALLOWED,
        archive=Archive.KEEP,
        train=Permission.ALLOWED,  # read 2026-11-17: including "producing a commercial product"
        redistribute=Permission.ALLOWED,  # read 2026-11-17: "selling it, giving it away"
        attribution=_RETROSHEET_NOTICE,  # read 2026-11-17: verbatim, and it "must appear prominently"
        on_termination=OnTermination.KEEP,
        key_shared=None,
        notes="Not read yet: the planned MLB win-probability table's source. Its notice ships with it and in About.",
        family_display=FamilyDisplay(allowed=True, basis=Basis.TERMS, why="Its notice allows any use, shown with it."),
    ),
    SourceRights(
        source=DataSource.WIKIPEDIA_COLORS,
        terms_url="https://en.wikipedia.org/wiki/Module:Sports_color/basketball",
        terms_read=date(2026, 11, 16),  # SOURCES.md: CC BY-SA 4.0, pinned at revision 1386095329
        display=Permission.ALLOWED,  # SOURCES.md: CC BY-SA 4.0
        cache=Permission.ALLOWED,
        archive=Archive.KEEP,
        train=Permission.ALLOWED,  # share-alike: what's built from it carries the same license
        redistribute=Permission.ALLOWED,  # with the attribution, under the same license
        attribution="Colors from Wikipedia's Module:Sports color/basketball, by Wikipedia contributors, CC BY-SA 4.0.",
        on_termination=OnTermination.KEEP,
        key_shared=None,
        notes="The NBA palette's starting colors; share-alike.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.TERMS,
            why="CC BY-SA 4.0 allows any use, with the attribution, under the same license.",
        ),
    ),
    SourceRights(
        source=DataSource.TEAM_LOGOS,
        terms_url=None,  # the teams' trademarks, wherever they're hosted
        terms_read=date(2026, 11, 16),  # SOURCES.md: the user's ruling of 2026-11-16
        display=Permission.ALLOWED,  # SOURCES.md: league CDN and ESPN-hosted logo files are fine tile sources
        cache=Permission.ALLOWED,  # the committed tiles the boards draw
        archive=Archive.KEEP,  # kept only as those tiles
        train=Permission.DENIED,  # display only
        redistribute=Permission.DENIED,  # never beyond the boards
        attribution=None,
        on_termination=OnTermination.DELETE,  # no agreement to end; the stricter value
        key_shared=None,
        notes="Team trademarks: display only, as the boards' committed tiles; never redistributed beyond the boards.",
        family_display=FamilyDisplay(
            allowed=True,
            basis=Basis.TERMS,
            why="Display only, as the boards' committed tiles, never beyond the boards.",
        ),
    ),
)

MANIFEST: Mapping[DataSource, SourceRights] = {entry.source: entry for entry in _ENTRIES}

# Which outside sources each module reads or ships, by its path from the repo root. Every module in the
# provider packages has an entry, an empty one when it reads none, so a new provider can't go unlisted.
READS: Mapping[str, frozenset[DataSource]] = {
    "omni/providers/__init__.py": frozenset(),
    "omni/providers/base.py": frozenset(),
    "omni/providers/bovada_odds.py": frozenset({DataSource.BOVADA}),
    "omni/providers/market_service.py": frozenset({DataSource.OMNI_MARKET_SERVICE}),
    "omni/providers/mlb_palette.py": frozenset({DataSource.TEAM_LOGOS}),  # each tile's background
    "omni/providers/mlb_statsapi.py": frozenset({DataSource.MLB_STATSAPI}),
    "omni/providers/mlb_teams.py": frozenset({DataSource.MLB_STATSAPI}),
    "omni/providers/nba_palette.py": frozenset({DataSource.WIKIPEDIA_COLORS}),
    "omni/providers/nba_teams.py": frozenset({DataSource.BALLDONTLIE}),  # names, conferences, divisions
    "omni/providers/nfl_palette.py": frozenset({DataSource.NFLVERSE}),
    "omni/providers/nfl_teams.py": frozenset({DataSource.NFLVERSE}),
    "omni/providers/nflmeta.py": frozenset({DataSource.NFLMETA}),
    "omni/providers/polymarket_us.py": frozenset({DataSource.POLYMARKET_US}),
    "omni/providers/rights.py": frozenset(),
    "cloud/market_service/__init__.py": frozenset(),
    "cloud/market_service/api.py": frozenset(),
    "cloud/market_service/api_handler.py": frozenset(),
    "cloud/market_service/auth.py": frozenset(),
    "cloud/market_service/poller.py": frozenset({DataSource.POLYMARKET_US}),
    "cloud/market_service/poller_handler.py": frozenset(
        {DataSource.MLB_STATSAPI, DataSource.POLYMARKET_US, DataSource.BOVADA}
    ),
    "cloud/market_service/quote_stream.py": frozenset({DataSource.POLYMARKET_US}),
    "cloud/market_service/readings.py": frozenset(),
    "cloud/market_service/slate.py": frozenset({DataSource.MLB_STATSAPI, DataSource.POLYMARKET_US}),
    "cloud/market_service/store.py": frozenset(),
    # Readers outside the provider packages.
    "omni/app/football.py": frozenset({DataSource.NFLMETA}),
    "tools/balldontlie_record.py": frozenset({DataSource.BALLDONTLIE}),
    "tools/fetch_mlb_logos.py": frozenset({DataSource.TEAM_LOGOS}),
    "tools/fetch_nba_logos.py": frozenset({DataSource.TEAM_LOGOS, DataSource.WIKIPEDIA_COLORS}),
    "tools/fetch_nfl_logos.py": frozenset({DataSource.NFLVERSE, DataSource.TEAM_LOGOS}),
    "tools/nflmeta_record.py": frozenset({DataSource.NFLMETA}),
    "tools/nflmeta_replay_trace.py": frozenset({DataSource.NFLMETA}),  # reads a recording
}


# --- retention --------------------------------------------------------------------------------------------


class DueStatus(StrEnumMixin, str, Enum):
    """Where a recording stands against its stamp."""

    OVERDUE = "overdue"
    CURRENT = "current"
    KEPT = "kept"  # its source allows keeping it
    UNSTAMPED = "unstamped"
    UNREADABLE = "unreadable"  # a stamp that can't be read
    LINKED = "linked"  # a link, which nothing here follows


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionStamp:
    """What a recording folder's `RETENTION.json` says: its source, when it was made, and when it goes."""

    source: DataSource
    created: datetime
    archive: Archive
    delete_by: datetime | None  # None when its source allows keeping it
    terms_read: date

    @property
    def manifest(self) -> str:
        """Where the source's entry is."""
        return f"omni/providers/rights.py#{self.source}"

    def record(self) -> dict[str, Any]:
        return {
            "retention": _STAMP_VERSION,
            "source": self.source.value,
            "created": self.created.isoformat(),
            "archive": self.archive.value,
            "delete_by": self.delete_by.isoformat() if self.delete_by is not None else None,
            "terms_read": self.terms_read.isoformat(),
            "terms_url": MANIFEST[self.source].terms_url,
            "manifest": self.manifest,
        }

    def status(self, now: datetime) -> DueStatus:
        """Where the recording stands at ``now``. A source whose terms allow keeping is kept, whatever a stamp
        says of a deadline."""
        if self.delete_by is None or MANIFEST[self.source].archive is Archive.KEEP:
            return DueStatus.KEPT
        return DueStatus.OVERDUE if now >= self.delete_by else DueStatus.CURRENT


def retention_stamp(source: DataSource, created: datetime) -> RetentionStamp:
    """The stamp for a recording of ``source`` made at ``created``: a diagnosis's is due its retention later.

    Raises `ValueError` for a source whose terms allow no recording, or a naive instant.
    """
    if created.tzinfo is None:
        raise ValueError("a recording's creation instant carries its offset")
    rights = MANIFEST[source]
    if rights.archive is Archive.NONE:
        raise ValueError(f"{source}'s terms allow no recording")
    delete_by = created + rights.retention if rights.retention is not None else None
    return RetentionStamp(
        source=source, created=created, archive=rights.archive, delete_by=delete_by, terms_read=rights.terms_read
    )


def stamp_recording(folder: Path, source: DataSource, now: datetime) -> RetentionStamp:
    """Make a recording folder with its stamp, or keep the stamp it has: the stamp in force.

    A folder already stamped for another source raises `ValueError`: one folder holds one source.
    """
    existing = read_stamp(folder)
    if existing is not None:
        if existing.source is not source:
            raise ValueError(f"{folder} already holds a recording of {existing.source}")
        return existing
    stamp = retention_stamp(source, now)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / RETENTION_FILE).write_text(json.dumps(stamp.record(), indent=2) + "\n", encoding="utf-8")
    return stamp


def read_stamp(folder: Path) -> RetentionStamp | None:
    """A folder's stamp, or None if it has none; `ValueError` for one that can't be read."""
    path = folder / RETENTION_FILE
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        delete_by = record["delete_by"]
        return RetentionStamp(
            source=DataSource(record["source"]),
            created=_instant(record["created"]),
            archive=Archive(record["archive"]),
            delete_by=_instant(delete_by) if delete_by is not None else None,
            terms_read=date.fromisoformat(record["terms_read"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path}: {type(exc).__name__}: {exc}") from None


@dataclass(frozen=True, slots=True)
class DueRecording:
    """One recording under the recordings folder, with its stamp and where it stands."""

    path: PurePosixPath  # from the recordings folder
    status: DueStatus
    stamp: RetentionStamp | None = None

    def line(self) -> str:
        """The recording as the due-list prints it: its name and its stamp, never its content."""
        stamp = self.stamp
        if self.status is DueStatus.LINKED:
            return f"{self.path}  a link: not followed"
        if stamp is None:
            why = "stamp unreadable" if self.status is DueStatus.UNREADABLE else "unstamped"
            return f"{self.path}  {why}: follow the source's rule by hand"
        if stamp.delete_by is None or self.status is DueStatus.KEPT:
            return f"{self.path}  {stamp.source}  kept: its terms allow keeping"
        return f"{self.path}  {stamp.source}  delete by {_due(stamp.delete_by)}" + (
            "  OVERDUE" if self.status is DueStatus.OVERDUE else ""
        )


def recordings_due(root: Path, now: datetime) -> tuple[DueRecording, ...]:
    """Every recording under ``root``, by its path, with where it stands at ``now``. Deletes nothing.

    A folder with a stamp is a recording, and nothing under it is looked at. A folder with no stamp and no
    folders in it is an unstamped recording, and so is a file lying beside folders; an empty folder is
    nothing. A link to a folder under ``root`` is listed and never followed. Only stamps are read.
    """
    if not root.is_dir():
        return ()
    return tuple(sorted(_walk(root, root, now), key=lambda due: due.path))


def _walk(folder: Path, root: Path, now: datetime) -> Iterator[DueRecording]:
    name = PurePosixPath(folder.relative_to(root).as_posix())
    try:
        stamp = read_stamp(folder)
    except ValueError:
        yield DueRecording(name, DueStatus.UNREADABLE)
        return
    if stamp is not None:
        yield DueRecording(name, stamp.status(now), stamp)
        return
    children = sorted(folder.iterdir())
    if not children:
        return  # an empty folder holds nothing: one the sweep emptied, say
    if folder != root and not any(child.is_dir() for child in children):
        yield DueRecording(name, DueStatus.UNSTAMPED)
        return
    for child in children:
        path = PurePosixPath(child.relative_to(root).as_posix())
        if child.is_symlink():
            yield DueRecording(path, DueStatus.LINKED)
        elif child.is_dir():
            yield from _walk(child, root, now)
        else:
            yield DueRecording(path, DueStatus.UNSTAMPED)


# --- the expiry sweep -------------------------------------------------------------------------------------


class Miner(Protocol):
    """Keeps our own telemetry from a recording (ids, kinds, reasons, timings, counts) before it's deleted, as
    a digest outside the recording's folder."""

    def digest_for(self, folder: Path, stamp: RetentionStamp) -> Path:
        """Where ``folder``'s digest goes. Once it exists, the folder has been mined."""
        ...

    def mine(self, folder: Path, stamp: RetentionStamp) -> Path | None:
        """Write ``folder``'s digest; its path, or None when the folder held nothing to mine."""
        ...


class Mining(StrEnumMixin, str, Enum):
    """What came of offering an overdue recording to its source's miner."""

    MINED = "mined"
    ALREADY_MINED = "already mined"  # its digest was there: not mined twice
    NOTHING_TO_MINE = "nothing to mine"
    FAILED = "mining failed"  # reported; the recording is deleted anyway
    NO_MINER = "no miner"  # its source has none


class SweepAction(StrEnumMixin, str, Enum):
    """What the sweep did with a recording."""

    DELETED = "deleted"
    KEPT = "kept"  # not due, kept by its terms, or not the sweep's to judge (unstamped, unreadable, a link)
    DELETE_FAILED = "delete failed"


@dataclass(frozen=True, slots=True)
class SweptRecording:
    """One recording the sweep looked at: where it stood, what the sweep did, and what its miner did."""

    due: DueRecording
    action: SweepAction
    mining: Mining | None = None  # for an overdue recording
    digest: Path | None = None  # the digest its miner wrote, or had already
    # What failed, by its type, never its message: a message could quote the recording.
    mining_failed: str | None = None
    delete_failed: str | None = None

    def line(self) -> str:
        """The recording as the sweep reports it: its name, its stamp and what was done; never its content."""
        stamp = self.due.stamp
        if self.action is SweepAction.KEPT or stamp is None or stamp.delete_by is None:
            return self.due.line()
        done = "deleted" if self.action is SweepAction.DELETED else f"not deleted ({self.delete_failed})"
        mined = self.mining.value if self.mining is not None else Mining.NO_MINER.value
        if self.digest is not None:
            mined += f" to {self.digest}"
        elif self.mining_failed is not None:
            mined += f" ({self.mining_failed})"
        return f"{self.due.path}  {stamp.source}  {done}: due {_due(stamp.delete_by)}; {mined}"


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What one sweep of a root did, recording by recording."""

    root: Path
    recordings: tuple[SweptRecording, ...]

    def count(self, action: SweepAction) -> int:
        return sum(1 for swept in self.recordings if swept.action is action)

    def summary(self) -> str:
        """A line of counts for the root: what was deleted, kept, left to the user, and what failed."""
        by_hand = sum(1 for swept in self.recordings if swept.due.stamp is None)
        failed = self.count(SweepAction.DELETE_FAILED)
        mining = sum(1 for swept in self.recordings if swept.mining is Mining.FAILED)
        return (
            f"swept {self.root}: {self.count(SweepAction.DELETED)} deleted, {self.count(SweepAction.KEPT)} kept"
            f" ({by_hand} without a readable stamp, left to the source's rule by hand)"
            + (f", {failed} failed to delete" if failed else "")
            + (f", {mining} failed to mine" if mining else "")
        )


_NO_MINERS: Mapping[DataSource, Miner] = MappingProxyType({})


def sweep_expired(
    root: Path,
    now: datetime,
    *,
    miners: Mapping[DataSource, Miner] = _NO_MINERS,
    remove: Callable[[Path], None] = shutil.rmtree,
) -> SweepReport:
    """Delete each stamped recording under ``root`` past its delete-by at ``now``, offering it first to its
    source's miner in ``miners``; what was done with each recording.

    A miner's failure is reported and the recording deleted anyway, and one whose digest already exists isn't
    mined again. A recording the sweep can't delete is reported and kept. Only stamps are read (a miner reads
    what it mines), and only what ``remove`` is given is deleted: overdue stamped folders under ``root``, and
    never a link or what it points to.
    """
    swept: list[SweptRecording] = []
    for due in recordings_due(root, now):
        stamp = due.stamp
        if due.status is not DueStatus.OVERDUE or stamp is None:
            swept.append(SweptRecording(due, SweepAction.KEPT))
            continue
        folder = root / str(due.path)
        mining, digest, mining_failed = _mine(folder, stamp, miners.get(stamp.source))
        action, delete_failed = SweepAction.DELETED, None
        try:
            remove(folder)
        except OSError as exc:
            action, delete_failed = SweepAction.DELETE_FAILED, type(exc).__name__
        swept.append(SweptRecording(due, action, mining, digest, mining_failed, delete_failed))
    return SweepReport(root, tuple(swept))


def _mine(folder: Path, stamp: RetentionStamp, miner: Miner | None) -> tuple[Mining, Path | None, str | None]:
    """Offer an overdue recording to its miner: what came of it, the digest, and what failed, if anything."""
    if miner is None:
        return Mining.NO_MINER, None, None
    try:
        digest = miner.digest_for(folder, stamp)
        if digest.exists():
            return Mining.ALREADY_MINED, digest, None
        written = miner.mine(folder, stamp)
    except Exception as exc:  # whatever the miner does wrong, the overdue recording still goes
        return Mining.FAILED, None, type(exc).__name__
    return (Mining.MINED, written, None) if written is not None else (Mining.NOTHING_TO_MINE, None, None)


def _due(instant: datetime) -> str:
    return f"{instant.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"


def _instant(raw: str) -> datetime:
    instant = datetime.fromisoformat(raw)
    if instant.tzinfo is None:
        raise ValueError(f"an instant without its offset: {raw}")
    return instant
