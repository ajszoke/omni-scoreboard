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

from omni.core.enum import League
from omni.core.ids import SourceRef
from omni.providers.rights import (
    MANIFEST,
    READS,
    RETENTION_FILE,
    Archive,
    DataSource,
    DueRecording,
    DueStatus,
    OnTermination,
    Permission,
    SourceRights,
    read_stamp,
    recordings_due,
    retention_stamp,
    stamp_recording,
)
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


def test_the_due_tool_prints_names_and_stamps_and_never_reads_a_recording(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _recordings(tmp_path)
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(opener: Any) -> Any:
        def wrapped(file: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(file))
            return opener(file, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(io, "open", spy(real_io_open))
    assert due_tool.main(["--root", str(tmp_path)], now=NOW) == 0
    monkeypatch.undo()
    out = capsys.readouterr().out
    assert "PROVIDER PLAY TEXT" not in out and "raw rows" not in out
    assert out.splitlines()[-1] == f"6 recordings under {tmp_path}: 1 overdue, 3 without a readable stamp"
    assert "nflmeta/2026-11-08-LAC-BAL  nflmeta  delete by 2026-11-15 21:00 UTC  OVERDUE" in out
    assert opened and all(Path(path).name == RETENTION_FILE for path in opened)
