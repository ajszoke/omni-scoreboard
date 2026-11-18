"""The rights manifest, the retention stamp on each recording, and the due-list that reads only stamps."""

from __future__ import annotations

import builtins
import importlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from cloud.market_service.readings import Reading, ReadingKind
from cloud.market_service.store import _READING_TTL, ReadingArchive
from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.providers.market_service import _MAX_AGE
from omni.providers.rights import (
    FAMILY_DISPLAY_RULING,
    MANIFEST,
    READS,
    RETENTION_FILE,
    RULINGS,
    Archive,
    Basis,
    DataSource,
    DueRecording,
    DueStatus,
    FamilyDisplay,
    OnTermination,
    Mining,
    Permission,
    ProjectUse,
    RetentionStamp,
    SourceRights,
    SweepAction,
    read_stamp,
    recordings_due,
    retention_stamp,
    stamp_recording,
    sweep_expired,
)
from omni.app.expiry import EXPIRY_ROOTS, MINERS, NFLMETA_RUN_LOGS, RECORDINGS, expire
from omni.app.football import _expiry_roots, _parse_args
from tools import recordings_due as due_tool

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)
PROVIDER_PACKAGES = ("omni/providers", "cloud/market_service")


# --- the manifest -----------------------------------------------------------------------------------------


def test_every_source_has_one_entry_under_its_own_name() -> None:
    assert set(MANIFEST) == set(DataSource)
    assert all(source is entry.source for source, entry in MANIFEST.items())


@pytest.mark.parametrize("package", PROVIDER_PACKAGES)
def test_every_module_in_a_provider_package_says_which_sources_it_reads(package: str) -> None:
    """A new provider without an entry fails here: add it to `READS`, with the sources it reads."""
    modules = {path.relative_to(REPO).as_posix() for path in (REPO / package).glob("*.py")}
    assert sorted(modules - set(READS)) == []


def test_every_reader_listed_exists_and_every_source_it_reads_has_an_entry() -> None:
    assert [path for path in READS if not (REPO / path).is_file()] == []
    assert set().union(*READS.values()) <= set(MANIFEST)


@pytest.mark.parametrize("package", PROVIDER_PACKAGES)
def test_every_source_a_provider_names_has_an_entry(package: str) -> None:
    """A provider's `SourceRef` names its source; one named for a league is that league's own id space."""
    leagues = {league.value for league in League}
    named: set[str] = set()
    for path in sorted((REPO / package).glob("*.py")):
        module = importlib.import_module(path.relative_to(REPO).with_suffix("").as_posix().replace("/", "."))
        named |= {value.name for value in vars(module).values() if isinstance(value, SourceRef)}
    assert named, "the providers name their sources"
    assert sorted(name for name in named - leagues if name not in set(DataSource)) == []


def test_nflmeta_may_be_kept_only_to_diagnose_and_never_trained_on() -> None:
    nflmeta = MANIFEST[DataSource.NFLMETA]
    assert (nflmeta.archive, nflmeta.retention) == (Archive.DIAGNOSIS, timedelta(days=7))
    assert not nflmeta.train.granted and not nflmeta.redistribute.granted and nflmeta.cache.granted
    assert nflmeta.on_termination is OnTermination.DELETE


def test_balldontlie_may_be_kept_and_trained_on_but_its_key_is_never_shared() -> None:
    bdl = MANIFEST[DataSource.BALLDONTLIE]
    assert bdl.archive is Archive.KEEP and bdl.retention is None and bdl.train.granted
    assert bdl.on_termination is OnTermination.KEEP and bdl.key_shared is Permission.DENIED
    assert bdl.attribution is None


def test_what_the_terms_don_t_say_counts_as_refused() -> None:
    polymarket = MANIFEST[DataSource.POLYMARKET_US]
    assert polymarket.cache is Permission.UNSTATED and not polymarket.cache.granted
    assert polymarket.redistribute is Permission.DENIED and polymarket.archive is Archive.NONE
    assert MANIFEST[DataSource.BOVADA].cache is Permission.DENIED  # no storing it in another system


def test_attributions_are_carried_as_required() -> None:
    assert MANIFEST[DataSource.RETROSHEET].attribution == (
        "The information used here was obtained free of charge from and is copyrighted by Retrosheet.  Interested"
        ' parties may contact Retrosheet at "www.retrosheet.org".'
    )
    nflverse = MANIFEST[DataSource.NFLVERSE]
    assert nflverse.attribution is not None and "CC BY 4.0" in nflverse.attribution
    assert "FTN" in nflverse.notes and "excluded" in nflverse.notes
    assert MANIFEST[DataSource.WIKIPEDIA_COLORS].attribution == (
        "Colors from Wikipedia's Module:Sports color/basketball, by Wikipedia contributors, CC BY-SA 4.0."
    )
    logos = MANIFEST[DataSource.TEAM_LOGOS]
    assert logos.display.granted and not logos.redistribute.granted and logos.terms_url is None


def test_a_retention_goes_with_a_diagnosis_archive_and_only_with_one() -> None:
    entry = MANIFEST[DataSource.BALLDONTLIE]
    fields: dict[str, Any] = {name: getattr(entry, name) for name in SourceRights.__dataclass_fields__}
    with pytest.raises(ValueError, match="a retention goes with a diagnosis archive"):
        SourceRights(**{**fields, "retention": timedelta(days=1)})
    with pytest.raises(ValueError, match="a retention goes with a diagnosis archive"):
        SourceRights(**{**fields, "archive": Archive.DIAGNOSIS})


# --- the family's boards ----------------------------------------------------------------------------------


def test_the_user_s_ruling_on_the_family_s_boards_is_recorded_with_its_date_its_condition_and_its_revisit() -> None:
    assert list(ProjectUse) == [ProjectUse.FAMILY_BOARD_DISPLAY]
    assert RULINGS == {ProjectUse.FAMILY_BOARD_DISPLAY: FAMILY_DISPLAY_RULING}
    assert FAMILY_DISPLAY_RULING.ruled == date(2026, 11, 17)
    assert "isn't a redistribution" in FAMILY_DISPLAY_RULING.holds
    assert "expires quickly" in FAMILY_DISPLAY_RULING.condition
    assert FAMILY_DISPLAY_RULING.revisit == "revisit before any commercial use"


def test_each_source_answers_for_the_family_s_boards_and_says_where_the_answer_comes_from() -> None:
    answers = {source: (entry.family_display.allowed, entry.family_display.basis) for source, entry in MANIFEST.items()}
    assert answers == {
        DataSource.MLB_STATSAPI: (True, Basis.TERMS),
        DataSource.NFLMETA: (True, Basis.RULING),
        DataSource.BALLDONTLIE: (True, Basis.TERMS),
        DataSource.POLYMARKET_US: (True, Basis.RULING),
        DataSource.BOVADA: (False, Basis.TERMS),  # it bars storing its content elsewhere: no cache may carry it
        DataSource.OMNI_MARKET_SERVICE: (True, Basis.RULING),
        DataSource.NFLVERSE: (True, Basis.TERMS),
        DataSource.RETROSHEET: (True, Basis.TERMS),
        DataSource.WIKIPEDIA_COLORS: (True, Basis.TERMS),
        DataSource.TEAM_LOGOS: (True, Basis.TERMS),
    }
    assert all(entry.family_display.why for entry in MANIFEST.values())


def test_every_source_shown_on_the_ruling_records_a_bounded_expiry_and_keeps_its_terms_as_written() -> None:
    relying = {source: entry for source, entry in MANIFEST.items() if entry.family_display.basis is Basis.RULING}
    assert {source: entry.family_display.expiry for source, entry in relying.items()} == {
        DataSource.NFLMETA: timedelta(days=7),  # its recordings and run logs, stamped
        DataSource.POLYMARKET_US: timedelta(days=1),  # the market service's longest-lived copy
        DataSource.OMNI_MARKET_SERVICE: timedelta(days=1),
    }
    assert all(not entry.redistribute.granted for entry in relying.values())  # the ruling flips no term
    assert MANIFEST[DataSource.POLYMARKET_US].redistribute is Permission.DENIED
    assert MANIFEST[DataSource.NFLMETA].family_display.expiry == MANIFEST[DataSource.NFLMETA].retention


def _with(source: DataSource, **fields: Any) -> SourceRights:
    entry = MANIFEST[source]
    current: dict[str, Any] = {name: getattr(entry, name) for name in SourceRights.__dataclass_fields__}
    return SourceRights(**{**current, **fields})


def test_a_source_whose_terms_bar_display_can_t_be_marked_for_the_family_s_boards() -> None:
    on_terms = FamilyDisplay(allowed=True, basis=Basis.TERMS, why="x")
    on_ruling = FamilyDisplay(allowed=True, basis=Basis.RULING, expiry=timedelta(hours=1), why="x")
    for answer in (on_terms, on_ruling):
        with pytest.raises(ValueError, match="its terms bar display"):
            _with(DataSource.MLB_STATSAPI, display=Permission.DENIED, family_display=answer)
    with pytest.raises(ValueError, match="can't be what allows it"):
        _with(DataSource.POLYMARKET_US, family_display=on_terms)  # its terms leave display open: only the ruling
    refused = FamilyDisplay(allowed=False, basis=Basis.TERMS, why="x")
    assert not _with(DataSource.MLB_STATSAPI, display=Permission.DENIED, family_display=refused).family_display.allowed


@pytest.mark.parametrize("expiry", [None, timedelta(0)])
def test_a_family_display_on_the_ruling_without_a_bounded_expiry_is_refused(expiry: timedelta | None) -> None:
    with pytest.raises(ValueError, match="a bounded expiry"):
        FamilyDisplay(allowed=True, basis=Basis.RULING, expiry=expiry, why="x")
    assert not FamilyDisplay(allowed=False, basis=Basis.RULING, why="x").allowed


def _resource(template: str, name: str) -> list[str]:
    """One resource's lines in the CloudFormation template, stripped: up to the next line at its indentation."""
    lines = template.splitlines()
    start = lines.index(f"  {name}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line.strip())
    return block


def test_the_market_service_s_copies_expire_within_what_its_entry_records() -> None:
    """The table's rows within an hour, the archive's objects within a day, and a board ignores a minute-old one."""
    recorded = MANIFEST[DataSource.OMNI_MARKET_SERVICE].family_display.expiry
    assert recorded == timedelta(days=1) and _READING_TTL == timedelta(hours=1)
    archive = _resource((REPO / "cloud" / "template.yaml").read_text(), "Archive")
    assert {"Status: Enabled", "Prefix: readings/", "ExpirationInDays: 1"} <= set(archive)

    class _S3:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            self.keys.append(kwargs["Key"])
            return {}

    s3 = _S3()
    ReadingArchive(s3, "bucket").write([Reading(game_id="g", kind=ReadingKind.WIN, observed_at=NOW, body={})], NOW)
    assert s3.keys and all(key.startswith("readings/") for key in s3.keys)  # every object falls under the rule
    assert _MAX_AGE == timedelta(seconds=60)


# --- the stamp --------------------------------------------------------------------------------------------


def test_an_nflmeta_recording_is_due_seven_days_after_it_was_made() -> None:
    stamp = retention_stamp(DataSource.NFLMETA, NOW)
    assert stamp.delete_by == NOW + timedelta(days=7) and stamp.archive is Archive.DIAGNOSIS
    assert stamp.terms_read == date(2026, 11, 17) and stamp.manifest == "omni/providers/rights.py#nflmeta"
    assert stamp.status(NOW + timedelta(days=7) - timedelta(seconds=1)) is DueStatus.CURRENT
    assert stamp.status(NOW + timedelta(days=7)) is DueStatus.OVERDUE


def test_a_balldontlie_recording_may_be_kept() -> None:
    stamp = retention_stamp(DataSource.BALLDONTLIE, NOW)
    assert stamp.delete_by is None and stamp.status(NOW + timedelta(days=3650)) is DueStatus.KEPT


def test_a_source_whose_terms_allow_no_recording_can_t_be_stamped() -> None:
    with pytest.raises(ValueError, match="bovada's terms allow no recording"):
        retention_stamp(DataSource.BOVADA, NOW)
    with pytest.raises(ValueError, match="carries its offset"):
        retention_stamp(DataSource.NFLMETA, NOW.replace(tzinfo=None))


def test_a_recording_folder_is_made_with_its_stamp_which_says_its_source_dates_and_entry(tmp_path: Path) -> None:
    folder = tmp_path / "nflmeta" / "2026-11-15-PIT-CIN"
    stamp = stamp_recording(folder, DataSource.NFLMETA, NOW)
    assert json.loads((folder / RETENTION_FILE).read_text()) == {
        "retention": 1,
        "source": "nflmeta",
        "created": "2026-11-17T21:00:00+00:00",
        "archive": "diagnosis",
        "delete_by": "2026-11-24T21:00:00+00:00",
        "terms_read": "2026-11-17",
        "terms_url": "https://nflmeta.org/terms",
        "manifest": "omni/providers/rights.py#nflmeta",
    }
    assert read_stamp(folder) == stamp


def test_a_folder_recorded_into_again_keeps_the_stamp_it_was_made_with(tmp_path: Path) -> None:
    folder = tmp_path / "nflmeta" / "game"
    first = stamp_recording(folder, DataSource.NFLMETA, NOW)
    assert stamp_recording(folder, DataSource.NFLMETA, NOW + timedelta(hours=3)) == first  # its deadline holds
    with pytest.raises(ValueError, match="already holds a recording of nflmeta"):
        stamp_recording(folder, DataSource.BALLDONTLIE, NOW)


@pytest.mark.parametrize(
    "written",
    [
        "{not json",
        json.dumps({"source": "nflmeta"}),
        json.dumps({"source": "espn", "created": "x", "archive": "keep", "delete_by": None, "terms_read": "x"}),
        json.dumps(
            {
                "source": "nflmeta",
                "created": "2026-11-17T21:00:00",
                "archive": "diagnosis",
                "delete_by": None,
                "terms_read": "2026-11-17",
            }
        ),
        json.dumps(["a list"]),
    ],
)
def test_a_stamp_that_can_t_be_read_says_so(tmp_path: Path, written: str) -> None:
    (tmp_path / RETENTION_FILE).write_text(written)
    with pytest.raises(ValueError, match=RETENTION_FILE):
        read_stamp(tmp_path)
    assert read_stamp(tmp_path / "elsewhere") is None


# --- the due-list -----------------------------------------------------------------------------------------


def _recordings(root: Path) -> None:
    """A recordings folder: overdue, current and kept recordings, unstamped ones and an unreadable stamp."""
    stamp_recording(root / "nflmeta" / "2026-11-08-LAC-BAL", DataSource.NFLMETA, NOW - timedelta(days=9))
    stamp_recording(root / "nflmeta" / "2026-11-16-PIT-CIN", DataSource.NFLMETA, NOW - timedelta(days=1))
    stamp_recording(root / "balldontlie" / "nba" / "2026-11-16", DataSource.BALLDONTLIE, NOW - timedelta(days=1))
    (root / "nflmeta" / "2026-11-15-PIT-CIN").mkdir(parents=True)  # made before stamps
    (root / "balldontlie" / "nba" / "probe-20261116T200000Z.jsonl").write_text('{"body": "raw rows"}\n')
    (root / "nflmeta" / "broken").mkdir()
    (root / "nflmeta" / "broken" / RETENTION_FILE).write_text("{")
    for folder in ("2026-11-08-LAC-BAL", "2026-11-16-PIT-CIN", "2026-11-15-PIT-CIN", "broken"):
        (root / "nflmeta" / folder / "play-changes.jsonl").write_text('{"description": "PROVIDER PLAY TEXT"}\n')
    (root / "nflmeta" / "2026-11-08-LAC-BAL" / "replays").mkdir()  # a stamped folder's own: not looked into


def test_the_due_list_names_each_recording_with_its_source_and_delete_by_marking_the_overdue(tmp_path: Path) -> None:
    _recordings(tmp_path)
    due = recordings_due(tmp_path, NOW)
    assert [(str(entry.path), entry.status) for entry in due] == [
        ("balldontlie/nba/2026-11-16", DueStatus.KEPT),
        ("balldontlie/nba/probe-20261116T200000Z.jsonl", DueStatus.UNSTAMPED),
        ("nflmeta/2026-11-08-LAC-BAL", DueStatus.OVERDUE),  # what's under a stamped folder is its own
        ("nflmeta/2026-11-15-PIT-CIN", DueStatus.UNSTAMPED),
        ("nflmeta/2026-11-16-PIT-CIN", DueStatus.CURRENT),
        ("nflmeta/broken", DueStatus.UNREADABLE),
    ]
    assert [entry.line() for entry in due] == [
        "balldontlie/nba/2026-11-16  balldontlie  kept: its terms allow keeping",
        "balldontlie/nba/probe-20261116T200000Z.jsonl  unstamped: follow the source's rule by hand",
        "nflmeta/2026-11-08-LAC-BAL  nflmeta  delete by 2026-11-15 21:00 UTC  OVERDUE",
        "nflmeta/2026-11-15-PIT-CIN  unstamped: follow the source's rule by hand",
        "nflmeta/2026-11-16-PIT-CIN  nflmeta  delete by 2026-11-23 21:00 UTC",
        "nflmeta/broken  stamp unreadable: follow the source's rule by hand",
    ]
    assert all((tmp_path / str(entry.path)).exists() for entry in due)  # nothing deleted


def test_a_missing_or_empty_recordings_folder_has_nothing_due(tmp_path: Path) -> None:
    assert recordings_due(tmp_path / "missing", NOW) == ()
    assert recordings_due(tmp_path, NOW) == ()
    assert DueRecording(PurePosixPath("x"), DueStatus.UNSTAMPED).stamp is None


def _spy_on_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every file opened from now on, by its path, until the monkeypatch is undone."""
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(opener: Any) -> Any:
        def wrapped(file: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(file))
            return opener(file, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_io_open))
    return opened


def test_the_due_tool_prints_names_and_stamps_and_never_reads_a_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _recordings(tmp_path)
    opened = _spy_on_opens(monkeypatch)
    assert due_tool.main(["--root", str(tmp_path)], now=NOW) == 0
    monkeypatch.undo()
    out = capsys.readouterr().out
    assert "PROVIDER PLAY TEXT" not in out and "raw rows" not in out
    assert out.splitlines()[-1] == f"6 recordings under {tmp_path}: 1 overdue, 3 without a readable stamp"
    assert "nflmeta/2026-11-08-LAC-BAL  nflmeta  delete by 2026-11-15 21:00 UTC  OVERDUE" in out
    assert opened and all(Path(path).name == RETENTION_FILE for path in opened)


def test_a_source_whose_terms_allow_keeping_is_kept_whatever_its_stamp_says(tmp_path: Path) -> None:
    folder = tmp_path / "balldontlie" / "edited"
    stamp_recording(folder, DataSource.NFLMETA, NOW - timedelta(days=30))
    record = json.loads((folder / RETENTION_FILE).read_text()) | {"source": "balldontlie"}  # a stamp edited by hand
    (folder / RETENTION_FILE).write_text(json.dumps(record))
    (due,) = recordings_due(tmp_path, NOW)
    assert (
        due.status is DueStatus.KEPT and due.line() == "balldontlie/edited  balldontlie  kept: its terms allow keeping"
    )
    assert sweep_expired(tmp_path, NOW).count(SweepAction.KEPT) == 1 and folder.is_dir()


# --- the expiry sweep -------------------------------------------------------------------------------------


class _Miner:
    """A miner that notes what it's offered and touches a digest for it under ``digests``, opening no file."""

    def __init__(self, digests: Path, *, fails: bool = False, finds_nothing: bool = False) -> None:
        self.digests = digests
        self.fails = fails
        self.finds_nothing = finds_nothing
        self.mined: list[tuple[str, DataSource]] = []

    def digest_for(self, folder: Path, stamp: RetentionStamp) -> Path:
        return self.digests / f"{folder.name}.digest"

    def mine(self, folder: Path, stamp: RetentionStamp) -> Path | None:
        assert folder.is_dir()  # offered before it's deleted
        self.mined.append((folder.name, stamp.source))
        if self.fails:
            raise RuntimeError("PROVIDER PLAY TEXT in a row it couldn't read")
        if self.finds_nothing:
            return None
        digest = self.digest_for(folder, stamp)
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.touch()
        return digest


def test_the_sweep_mines_each_overdue_stamped_recording_then_deletes_it_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recordings"
    _recordings(root)
    miner = _Miner(tmp_path / "digests")
    opened = _spy_on_opens(monkeypatch)
    report = sweep_expired(root, NOW, miners={DataSource.NFLMETA: miner})
    monkeypatch.undo()
    assert opened and all(Path(path).name == RETENTION_FILE for path in opened)  # only stamps were read
    assert miner.mined == [("2026-11-08-LAC-BAL", DataSource.NFLMETA)]
    assert [(str(swept.due.path), swept.action, swept.mining) for swept in report.recordings] == [
        ("balldontlie/nba/2026-11-16", SweepAction.KEPT, None),  # its terms allow keeping
        ("balldontlie/nba/probe-20261116T200000Z.jsonl", SweepAction.KEPT, None),  # unstamped: by hand
        ("nflmeta/2026-11-08-LAC-BAL", SweepAction.DELETED, Mining.MINED),
        ("nflmeta/2026-11-15-PIT-CIN", SweepAction.KEPT, None),  # unstamped: by hand
        ("nflmeta/2026-11-16-PIT-CIN", SweepAction.KEPT, None),  # not due yet
        ("nflmeta/broken", SweepAction.KEPT, None),  # a stamp that can't be read: by hand
    ]
    assert not (root / "nflmeta" / "2026-11-08-LAC-BAL").exists()
    assert all((root / path).exists() for path in ("balldontlie/nba/2026-11-16", "nflmeta/2026-11-15-PIT-CIN"))
    assert (root / "nflmeta" / "2026-11-16-PIT-CIN").is_dir() and (root / "nflmeta" / "broken").is_dir()
    digest = tmp_path / "digests" / "2026-11-08-LAC-BAL.digest"
    assert report.recordings[2].line() == (
        f"nflmeta/2026-11-08-LAC-BAL  nflmeta  deleted: due 2026-11-15 21:00 UTC; mined to {digest}"
    )
    assert report.recordings[3].line() == "nflmeta/2026-11-15-PIT-CIN  unstamped: follow the source's rule by hand"
    assert report.summary() == (
        f"swept {root}: 1 deleted, 5 kept (3 without a readable stamp, left to the source's rule by hand)"
    )


def _overdue(root: Path, name: str) -> Path:
    folder = root / "nflmeta" / name
    stamp_recording(folder, DataSource.NFLMETA, NOW - timedelta(days=8))
    (folder / "play-changes.jsonl").write_text('{"description": "PROVIDER PLAY TEXT"}\n')
    return folder


def test_a_miner_s_failure_is_reported_and_the_overdue_recording_is_deleted_anyway(tmp_path: Path) -> None:
    folder = _overdue(tmp_path, "2026-11-08-LAC-BAL")
    (swept,) = sweep_expired(tmp_path, NOW, miners={DataSource.NFLMETA: _Miner(tmp_path, fails=True)}).recordings
    assert (swept.action, swept.mining, swept.mining_failed) == (SweepAction.DELETED, Mining.FAILED, "RuntimeError")
    assert not folder.exists()
    assert "PROVIDER PLAY TEXT" not in swept.line()  # the failure's type, never its message
    assert swept.line().endswith("deleted: due 2026-11-16 21:00 UTC; mining failed (RuntimeError)")
    report = sweep_expired(tmp_path, NOW)  # the folder it emptied holds nothing to list
    assert report.recordings == () and report.summary() == (
        f"swept {tmp_path}: 0 deleted, 0 kept (0 without a readable stamp, left to the source's rule by hand)"
    )


def test_a_recording_already_mined_isn_t_mined_again_and_one_with_nothing_or_no_miner_still_goes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recordings"
    mined, empty, unmined = (_overdue(root, name) for name in ("mined", "empty", "unmined"))
    miner = _Miner(tmp_path / "digests")
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests" / "mined.digest").touch()  # a sweep that couldn't delete it mined it already
    report = sweep_expired(root, NOW, miners={DataSource.NFLMETA: miner})
    assert miner.mined == [("empty", DataSource.NFLMETA), ("unmined", DataSource.NFLMETA)]  # not "mined" again
    assert [swept.mining for swept in report.recordings] == [Mining.MINED, Mining.ALREADY_MINED, Mining.MINED]
    assert not any(folder.exists() for folder in (mined, empty, unmined))
    nothing = _Miner(tmp_path / "other", finds_nothing=True)
    folder = _overdue(root, "bare")
    (swept,) = sweep_expired(root, NOW, miners={DataSource.NFLMETA: nothing}).recordings
    assert (swept.mining, swept.digest, folder.exists()) == (Mining.NOTHING_TO_MINE, None, False)
    assert swept.line().endswith("; nothing to mine")
    folder = _overdue(root, "no-miner")
    (swept,) = sweep_expired(root, NOW).recordings
    assert (swept.mining, folder.exists()) == (Mining.NO_MINER, False) and swept.line().endswith("; no miner")


def test_a_recording_the_sweep_can_t_delete_is_reported_and_kept(tmp_path: Path) -> None:
    folder = _overdue(tmp_path, "stuck")

    def refuse(path: Path) -> None:
        raise PermissionError(f"{path}: PROVIDER PLAY TEXT")

    report = sweep_expired(tmp_path, NOW, miners={DataSource.NFLMETA: _Miner(tmp_path / "d")}, remove=refuse)
    (swept,) = report.recordings
    assert (swept.action, swept.delete_failed, folder.is_dir()) == (SweepAction.DELETE_FAILED, "PermissionError", True)
    assert swept.line().startswith("nflmeta/stuck  nflmeta  not deleted (PermissionError): due ")
    assert report.summary().endswith(", 1 failed to delete")
    failing = sweep_expired(tmp_path, NOW, miners={DataSource.NFLMETA: _Miner(tmp_path, fails=True)}, remove=refuse)
    assert failing.summary().endswith(", 1 failed to delete, 1 failed to mine")


def test_the_sweep_never_follows_a_link_out_of_its_root(tmp_path: Path) -> None:
    root, outside = tmp_path / "recordings", tmp_path / "elsewhere"
    target = _overdue(outside, "kept-elsewhere")
    (root / "nflmeta").mkdir(parents=True)
    (root / "nflmeta" / "linked").symlink_to(target, target_is_directory=True)  # overdue, but not ours
    inside = _overdue(root, "2026-11-08-LAC-BAL")
    (inside / "out").symlink_to(outside, target_is_directory=True)  # a link inside a recording that goes
    report = sweep_expired(root, NOW)
    assert [(str(swept.due.path), swept.due.status, swept.action) for swept in report.recordings] == [
        ("nflmeta/2026-11-08-LAC-BAL", DueStatus.OVERDUE, SweepAction.DELETED),
        ("nflmeta/linked", DueStatus.LINKED, SweepAction.KEPT),
    ]
    assert report.recordings[1].line() == "nflmeta/linked  a link: not followed"
    assert not inside.exists() and (root / "nflmeta" / "linked").is_symlink()
    assert target.is_dir() and (target / "play-changes.jsonl").is_file() and read_stamp(target) is not None
    (root / "solo").mkdir()
    (root / "solo" / "link").symlink_to(target, target_is_directory=True)  # a folder holding only a link
    (root / "solo" / "file").symlink_to(target / RETENTION_FILE)
    assert [(str(due.path), due.status) for due in recordings_due(root, NOW)] == [
        ("nflmeta/linked", DueStatus.LINKED),
        ("solo/file", DueStatus.LINKED),
        ("solo/link", DueStatus.LINKED),
    ]


def test_the_due_tool_expires_each_root_it_s_given_printing_names_and_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recordings, logs = tmp_path / "recordings", tmp_path / "logs"
    _recordings(recordings)
    _overdue(logs, "2026-11-09-PIT-CIN")
    miner = _Miner(tmp_path / "digests")
    argv = ["--root", str(recordings), "--root", str(logs), "--expire"]
    assert due_tool.main(argv, now=NOW, miners={DataSource.NFLMETA: miner}) == 0
    out = capsys.readouterr().out
    assert "PROVIDER PLAY TEXT" not in out and "raw rows" not in out
    assert (
        out.splitlines()[-1]
        == f"swept {logs}: 1 deleted, 0 kept (0 without a readable stamp, left to the source's rule by hand)"
    )
    assert f"swept {recordings}: 1 deleted, 5 kept" in out
    assert miner.mined == [("2026-11-08-LAC-BAL", DataSource.NFLMETA), ("2026-11-09-PIT-CIN", DataSource.NFLMETA)]
    assert due_tool.main(["--root", str(recordings)], now=NOW) == 0  # the default still only lists
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"5 recordings under {recordings}: 0 overdue, 3 without a readable stamp"
    )
    assert EXPIRY_ROOTS == (RECORDINGS, NFLMETA_RUN_LOGS) and dict(MINERS) == {}
    assert RECORDINGS == REPO / "build" / "recordings" and NFLMETA_RUN_LOGS == REPO / "build" / "logs" / "nflmeta"


def test_the_runner_s_expiry_logs_a_line_for_each_recording_deleted_under_both_its_roots(tmp_path: Path) -> None:
    args = _parse_args(["--profile", "quad_128x64", "--teams", "PIT,CIN", "--emulated", "--log-dir", str(tmp_path)])
    assert _expiry_roots(args) == (RECORDINGS, tmp_path)
    recordings, logs = tmp_path / "recordings", tmp_path / "logs"
    _recordings(recordings)
    _overdue(logs, "2026-11-09-PIT-CIN")
    lines: list[str] = []
    reports = expire((recordings, logs), NOW, lines.append, miners={DataSource.NFLMETA: _Miner(tmp_path / "d")})
    assert [report.root for report in reports] == [recordings, logs]
    assert lines == [
        f"expiry under {recordings}: nflmeta/2026-11-08-LAC-BAL  nflmeta  deleted: due 2026-11-15 21:00 UTC;"
        f" mined to {tmp_path / 'd' / '2026-11-08-LAC-BAL.digest'}",
        f"expiry under {logs}: nflmeta/2026-11-09-PIT-CIN  nflmeta  deleted: due 2026-11-16 21:00 UTC;"
        f" mined to {tmp_path / 'd' / '2026-11-09-PIT-CIN.digest'}",
    ]
    assert expire((recordings, logs), NOW, lines.append) and len(lines) == 2  # nothing more is due: no line
