from datetime import date, datetime, timezone
from pathlib import Path

from omni.app.expiry import MINERS, TELEMETRY
from omni.app.run_log import RUN_LOG_FILE
from omni.providers.rights import DataSource, retention_stamp
from omni.telemetry.miners import NflMetaMiner
from omni.telemetry.runner_log_redact import lines_to_redact
from tests.test_telemetry_nflmeta import FOLDER, _game
from tests.nflmeta_recording import PHRASE
from tests.test_telemetry_runner_log import NIGHT_ONE

_STAMP = retention_stamp(DataSource.NFLMETA, datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc))


def test_the_sweep_mines_nflmeta_with_its_digests_kept_under_the_telemetry_root() -> None:
    miner = MINERS[DataSource.NFLMETA]
    assert isinstance(miner, NflMetaMiner)
    assert miner.digest_for(Path("2026-11-15-PIT-CIN"), _STAMP) == TELEMETRY / "nflmeta" / "2026-11-15-PIT-CIN.json"


def test_a_recording_is_mined_by_its_name_and_not_mined_twice(tmp_path: Path) -> None:
    miner = NflMetaMiner(tmp_path / "telemetry")
    folder = _game(tmp_path / "recordings")
    digest = miner.mine(folder, _STAMP)
    assert digest == miner.digest_for(folder, _STAMP) == tmp_path / "telemetry" / "nflmeta" / f"{FOLDER}.json"
    assert digest is not None and PHRASE not in digest.read_text()
    assert miner.mine(folder, _STAMP) == digest


def test_a_run_s_log_folder_is_mined_night_by_night_into_a_folder_of_its_own(tmp_path: Path) -> None:
    miner = NflMetaMiner(tmp_path / "telemetry")
    folder = tmp_path / "logs" / "2026-11-15-PIT-CIN"
    folder.mkdir(parents=True)
    (folder / RUN_LOG_FILE).write_text("\n".join(NIGHT_ONE) + "\n")
    digest = miner.mine(folder, _STAMP)
    expected = tmp_path / "telemetry" / "runner-log" / "2026-11-15-PIT-CIN" / "2026-11-15.json"
    assert digest == miner.digest_for(folder, _STAMP) == expected
    assert digest is not None and PHRASE not in digest.read_text()
    redacted = expected.with_name("2026-11-15.redacted.log").read_text().splitlines()  # the night, line for line
    assert len(redacted) == len(NIGHT_ONE) + 1 and lines_to_redact(redacted) == 0
    assert PHRASE not in "\n".join(redacted) and "22920" not in "\n".join(redacted)


def test_a_run_log_folder_without_a_dated_name_is_dated_by_its_stamp(tmp_path: Path) -> None:
    miner = NflMetaMiner(tmp_path / "telemetry")
    folder = tmp_path / "logs" / "a-run"
    folder.mkdir(parents=True)
    (folder / RUN_LOG_FILE).write_text(f"no run started: {PHRASE}\n")
    digest = miner.mine(folder, _STAMP)
    assert digest == miner.digest_for(folder, _STAMP)
    assert digest is not None and digest.name == f"{date(2026, 11, 15).isoformat()}.json"
    assert PHRASE not in digest.read_text()


def test_an_empty_run_log_mines_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "logs" / "2026-11-15-PIT-CIN"
    folder.mkdir(parents=True)
    (folder / RUN_LOG_FILE).write_text("")
    assert NflMetaMiner(tmp_path / "telemetry").mine(folder, _STAMP) is None


def test_a_folder_that_is_neither_holds_nothing_to_mine(tmp_path: Path) -> None:
    assert NflMetaMiner(tmp_path / "telemetry").mine(tmp_path, _STAMP) is None
