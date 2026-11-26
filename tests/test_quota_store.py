"""Keeping the month's usage between runs: a small atomic file, written at most once a minute and at shutdown."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from omni.app.poll_plan import Demand, PollPriority
from omni.app.quota import (
    BalldontliePlan,
    BillingMonth,
    MonthUsage,
    NflMetaPlan,
    ProviderAccount,
    ProviderBalance,
    QuotaLedger,
    Refusal,
    account_limits,
    spent_notice,
)
from omni.app.quota_store import SCHEMA, USAGE_FILE, FileLocks, UsageFile, default_state_dir, open_ledger
from omni.core.clock import FakeClock

T0 = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)
NFL = ProviderAccount.NFLMETA
NOVEMBER = BillingMonth(2026, 11)


def _quiet(line: str) -> None:
    """A log nobody reads."""


def _file(tmp_path: Path, lines: list[str] | None = None) -> UsageFile:
    return UsageFile(tmp_path / USAGE_FILE, log=(lines if lines is not None else []).append)


def _ledger(store: UsageFile) -> QuotaLedger:
    return QuotaLedger(account_limits(), usage=store.load(), store=store)


def test_the_state_directory_follows_xdg_and_falls_back_to_the_home_directory() -> None:
    home = Path("/home/fan")
    assert default_state_dir({"XDG_STATE_HOME": "/var/state"}, home) == Path("/var/state/omni")
    assert default_state_dir({"XDG_STATE_HOME": ""}, home) == Path("/home/fan/.local/state/omni")
    assert default_state_dir({}, home) == Path("/home/fan/.local/state/omni")


def test_the_ledger_holds_balldontlie_to_the_tier_it_s_opened_with(tmp_path: Path) -> None:
    assert open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet).limits(ProviderAccount.BALLDONTLIE).per_minute == 5
    all_star = open_ledger(tmp_path, NflMetaPlan.FREE, balldontlie=BalldontliePlan.ALL_STAR, log=_quiet)
    assert all_star.limits(ProviderAccount.BALLDONTLIE).per_minute == 55


def test_the_usage_survives_a_restart(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    ledger = open_ledger(tmp_path / "state", NflMetaPlan.FREE, log=_quiet)
    ledger.charge(NFL, at=clock.read(), rows=14)
    ledger.charge(ProviderAccount.MLB_STATSAPI, at=clock.read())
    ledger.save(clock.read(), force=True)
    document = json.loads((tmp_path / "state" / USAGE_FILE).read_text())
    assert document["schema"] == SCHEMA
    assert document["accounts"]["nflmeta"] == {"month": "2026-11", "requests": 1, "rows": 14, "exhausted": False}
    assert set(document["accounts"]) == {"mlb_statsapi", "nflmeta"}  # by account and month, nothing else
    again = open_ledger(tmp_path / "state", NflMetaPlan.FREE, log=_quiet)
    assert again.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=1, rows=14)
    assert again.limits(NFL).requests_a_month == 5_000


def test_the_ledger_writes_at_most_once_a_minute_and_at_shutdown(tmp_path: Path) -> None:
    store = _file(tmp_path)
    ledger, clock = _ledger(store), FakeClock(T0)

    def saved() -> int:
        return store.load()[NFL].requests if store.path.exists() else 0

    ledger.charge(NFL, at=clock.read())
    assert saved() == 1  # the first charge writes
    clock.advance(30 * S)
    ledger.charge(NFL, at=clock.read())
    assert saved() == 1  # not again within the minute
    clock.advance(30 * S)
    ledger.charge(NFL, at=clock.read())
    assert saved() == 3  # a minute on
    clock.advance(5 * S)
    ledger.charge(NFL, at=clock.read())
    ledger.save(clock.read())
    assert saved() == 3  # an ordinary save waits out the minute too
    ledger.save(clock.read(), force=True)
    assert saved() == 4  # shutdown writes whatever is new
    mtime = store.path.stat().st_mtime_ns
    ledger.save(clock.read(), force=True)
    assert store.path.stat().st_mtime_ns == mtime  # nothing new: nothing written


def test_a_ledger_without_a_store_never_writes(tmp_path: Path) -> None:
    ledger, clock = QuotaLedger(account_limits()), FakeClock(T0)
    ledger.charge(NFL, at=clock.read())
    ledger.save(clock.read(), force=True)
    assert list(tmp_path.iterdir()) == []


def test_a_crash_between_writing_and_moving_leaves_the_old_file_whole(tmp_path: Path) -> None:
    lines: list[str] = []
    store = _file(tmp_path, lines)
    store.save({NFL: MonthUsage(NOVEMBER, requests=10, rows=50)})
    before = store.path.read_text()

    def crash(written: str, path: Path) -> None:
        raise OSError("power cut")

    crashing = UsageFile(store.path, log=lines.append, replace=crash)
    assert not crashing.save({NFL: MonthUsage(NOVEMBER, requests=11, rows=60)})
    assert store.path.read_text() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [USAGE_FILE]  # the half-done file is gone
    assert lines == [f"quota: couldn't save the usage to {store.path}: power cut"]


def test_a_state_directory_that_cant_be_made_is_reported_and_nothing_is_written(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file, not a directory")
    lines: list[str] = []
    store = UsageFile(tmp_path / "state" / USAGE_FILE, log=lines.append)
    assert not store.save({NFL: MonthUsage(NOVEMBER, requests=1)})
    assert len(lines) == 1 and lines[0].startswith("quota: couldn't save the usage to")


def test_a_save_that_fails_is_tried_again_at_the_next_chance(tmp_path: Path) -> None:
    attempts: list[str] = []

    def flaky(written: str, path: Path) -> None:
        attempts.append(written)
        if len(attempts) == 1:
            raise OSError("disk busy")
        Path(written).replace(path)

    store = UsageFile(tmp_path / USAGE_FILE, log=_quiet, replace=flaky)
    ledger, clock = QuotaLedger(account_limits(), store=store), FakeClock(T0)
    ledger.charge(NFL, at=clock.read())
    assert not store.path.exists()
    ledger.save(clock.read(), force=True)
    assert store.load()[NFL].requests == 1


def test_an_unreadable_file_is_set_aside_and_the_count_starts_over(tmp_path: Path) -> None:
    for broken in ("not json", json.dumps({"schema": "something/9"}), json.dumps([1, 2])):
        (tmp_path / USAGE_FILE).write_text(broken)
        lines: list[str] = []
        ledger = open_ledger(tmp_path, NflMetaPlan.FREE, log=lines.append)
        assert ledger.usage(NFL, FakeClock(T0).read()) == MonthUsage(NOVEMBER)
        assert (tmp_path / f"{USAGE_FILE}.unreadable").read_text() == broken
        assert not (tmp_path / USAGE_FILE).exists()
        assert len(lines) == 1 and "set aside as quota-usage.json.unreadable, counting from zero" in lines[0]


@pytest.mark.parametrize(
    "entry",
    [
        {"month": "2026-11", "requests": "7", "rows": 0, "exhausted": False},
        {"month": "2026-11", "requests": True, "rows": 0, "exhausted": False},
        {"month": "2026-11", "requests": 7, "rows": 0, "exhausted": "no"},
        {"month": "November", "requests": 7, "rows": 0, "exhausted": False},
        {"requests": 7, "rows": 0, "exhausted": False},
    ],
)
def test_an_entry_that_isn_t_what_was_written_makes_the_file_unreadable(tmp_path: Path, entry: object) -> None:
    (tmp_path / USAGE_FILE).write_text(json.dumps({"schema": SCHEMA, "accounts": {"nflmeta": entry}}))
    assert _file(tmp_path).load() == {}
    assert (tmp_path / f"{USAGE_FILE}.unreadable").exists()


def test_the_count_after_an_unreadable_file_starts_from_the_provider_s_own_figure(tmp_path: Path) -> None:
    (tmp_path / USAGE_FILE).write_bytes(b"\xff\xfe garbage")
    ledger, clock = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet), FakeClock(T0)
    ledger.charge(NFL, at=clock.read(), rows=2, balance=ProviderBalance(requests_left=4_000, rows_left=20_000))
    assert ledger.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=1_000, rows=5_000)


def test_a_file_that_cant_be_set_aside_is_reported_and_the_count_starts_over(tmp_path: Path) -> None:
    (tmp_path / USAGE_FILE).mkdir()  # a directory where the file should be: unreadable, and unmovable onto
    (tmp_path / f"{USAGE_FILE}.unreadable").mkdir()
    (tmp_path / f"{USAGE_FILE}.unreadable" / "keep").write_text("")
    lines: list[str] = []
    assert _file(tmp_path, lines).load() == {}
    assert len(lines) == 1 and "couldn't be set aside" in lines[0]


def test_a_new_month_starts_from_zero_and_its_first_save_replaces_the_old_month(tmp_path: Path) -> None:
    store = _file(tmp_path)
    store.save({NFL: MonthUsage(NOVEMBER, requests=4_999, rows=24_000, exhausted=True)})
    ledger, clock = _ledger(store), FakeClock(datetime(2026, 12, 1, 0, 0, 5, tzinfo=timezone.utc))
    assert ledger.usage(NFL, clock.read()) == MonthUsage(BillingMonth(2026, 12))
    assert not ledger.exhausted(NFL, clock.read())
    ledger.charge(NFL, at=clock.read(), rows=3)
    assert store.load()[NFL] == MonthUsage(BillingMonth(2026, 12), requests=1, rows=3)


def test_two_boards_sharing_the_file_never_lower_each_other_s_counts(tmp_path: Path) -> None:
    store = _file(tmp_path)
    store.save(
        {
            NFL: MonthUsage(NOVEMBER, requests=300, rows=900),  # the football board's
            ProviderAccount.BOVADA: MonthUsage(BillingMonth(2026, 12), requests=2),  # a later month already
        }
    )
    store.save(
        {
            NFL: MonthUsage(NOVEMBER, requests=5, rows=1_000, exhausted=True),
            ProviderAccount.BOVADA: MonthUsage(NOVEMBER, requests=40),
            ProviderAccount.MLB_STATSAPI: MonthUsage(NOVEMBER, requests=40),
        }
    )
    assert store.load() == {
        NFL: MonthUsage(NOVEMBER, requests=300, rows=1_000, exhausted=True),
        ProviderAccount.BOVADA: MonthUsage(BillingMonth(2026, 12), requests=2),
        ProviderAccount.MLB_STATSAPI: MonthUsage(NOVEMBER, requests=40),
    }


def test_a_month_spent_on_free_has_room_again_after_a_restart_on_builder_and_its_first_answer_reconciles(
    tmp_path: Path,
) -> None:
    clock = FakeClock(T0)
    free = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet)
    free.charge(NFL, at=clock.read(), rows=3, balance=ProviderBalance(requests_left=0, rows_left=24_000))
    assert free.exhausted(NFL, clock.read())  # NFLMeta says Free's month is spent
    assert spent_notice(free, clock.read()) == (
        "NFLMeta Free is spent for 2026-11: switch the account to Builder and restart with --nflmeta-plan builder"
    )
    free.save(clock.read(), force=True)
    saved = json.loads((tmp_path / USAGE_FILE).read_text())["accounts"]["nflmeta"]
    assert saved == {"month": "2026-11", "requests": 5_000, "rows": 1_000, "exhausted": True}
    assert open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet).exhausted(NFL, clock.read())  # still spent on Free
    clock.advance(timedelta(hours=1))
    builder = open_ledger(tmp_path, NflMetaPlan.BUILDER, log=_quiet)
    assert builder.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=5_000, rows=1_000)  # what NFLMeta bills
    assert not builder.exhausted(NFL, clock.read()) and spent_notice(builder, clock.read()) is None
    first = builder.admit(NFL, Demand(PollPriority.FOCUS, 1), clock.read())
    assert first.admitted and first.reason == "quota: NFLMeta focus admitted"
    builder.charge(NFL, at=clock.read(), rows=2, balance=ProviderBalance(requests_left=69_990, rows_left=248_000))
    assert builder.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=5_010, rows=2_000)  # NFLMeta's count
    builder.save(clock.read(), force=True)
    again = open_ledger(tmp_path, NflMetaPlan.BUILDER, log=_quiet)
    assert not again.exhausted(NFL, clock.read())  # the old word in the file doesn't spend Builder's month
    assert again.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=5_010, rows=2_000)


# --- one poller per account ---------------------------------------------------------------------------------


def test_one_process_at_a_time_holds_nflmeta_and_another_is_told_who_does(tmp_path: Path) -> None:
    board, recorder = FileLocks(tmp_path, who="omni.app"), FileLocks(tmp_path, who="nflmeta_record")
    assert board.claim(NFL) is None and board.claim(NFL) is None  # held, and held again at no cost
    assert (tmp_path / "nflmeta.lock").read_text() == f"{os.getpid()} omni.app\n"
    assert recorder.claim(NFL) == f"pid {os.getpid()}, omni.app"
    assert recorder.claim(ProviderAccount.MLB_STATSAPI) is None  # StatsAPI needs no holding
    board.release()
    assert recorder.claim(NFL) is None  # let go of at the holder's end, however it ends
    assert board.claim(NFL) == f"pid {os.getpid()}, nflmeta_record"
    recorder.release()


@pytest.mark.parametrize(
    ("written", "holder"),
    [("4242 omni.app.football\n", "pid 4242, omni.app.football"), ("4242", "pid 4242"), ("", "another process")],
)
def test_a_holder_is_named_as_far_as_its_lock_file_says(tmp_path: Path, written: str, holder: str) -> None:
    held = FileLocks(tmp_path)
    assert held.claim(NFL) is None
    (tmp_path / "nflmeta.lock").write_text(written)
    assert FileLocks(tmp_path).claim(NFL) == holder
    held.release()


def test_a_lock_file_that_can_t_be_read_names_no_holder(tmp_path: Path) -> None:
    def held(fd: int) -> None:
        os.unlink(tmp_path / "nflmeta.lock")  # gone before its holder could be read
        raise BlockingIOError("held")

    assert FileLocks(tmp_path, lock=held).claim(NFL) == "another process"


def test_a_lock_that_can_t_be_had_refuses_the_account_with_why(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file where the state directory goes")
    assert FileLocks(tmp_path / "state").claim(NFL) == (
        f"its lock {tmp_path / 'state' / 'nflmeta.lock'} can't be opened: [Errno 17] File exists:"
        f" '{tmp_path / 'state'}'"
    )

    def broken(fd: int) -> None:
        raise OSError(37, "No locks available")

    assert FileLocks(tmp_path, lock=broken).claim(NFL) == (
        f"its lock {tmp_path / 'nflmeta.lock'} can't be taken: [Errno 37] No locks available"
    )


def test_a_program_unnamed_is_named_by_how_it_was_started(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    assert locks.claim(NFL) is None
    assert (tmp_path / "nflmeta.lock").read_text() == f"{os.getpid()} {Path(sys.argv[0]).name}\n"
    locks.release()


def test_the_ledger_holds_nflmeta_from_its_first_ask_and_refuses_every_ask_while_another_holds_it(
    tmp_path: Path,
) -> None:
    clock = FakeClock(T0)
    board = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet, who="omni.app")
    runner = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet, who="omni.app.football")
    assert runner.admit(ProviderAccount.MLB_STATSAPI, Demand(PollPriority.FOCUS), clock.read()).admitted  # no lock
    assert board.admit(NFL, Demand(PollPriority.NORMAL), clock.read()).admitted
    refused = runner.admit(NFL, Demand(PollPriority.FOCUS), clock.read())
    assert refused.refusal is Refusal.ELSEWHERE and refused.held_back
    reason = f"quota: NFLMeta is polled by another process (pid {os.getpid()}, omni.app); not asking here"
    assert refused.reason == reason and runner.claim(NFL) == reason
    assert runner.admit_request(NFL, clock.read()).refusal is Refusal.ELSEWHERE
    board.close()
    assert runner.claim(NFL) is None and runner.admit(NFL, Demand(PollPriority.FOCUS), clock.read()).admitted
    runner.close()


def test_taking_nflmeta_over_counts_on_from_what_its_last_holder_saved(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    waiting = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet)  # started while the runner polled
    counted = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet)  # has counted the month already
    assert counted.usage(NFL, clock.read()).requests == 0
    runner = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet)
    assert runner.claim(NFL) is None
    runner.charge(NFL, at=clock.read(), requests=40, rows=90)
    runner.save(clock.read(), force=True)
    runner.close()
    for ledger in (waiting, counted):
        assert ledger.claim(NFL) is None
        assert ledger.usage(NFL, clock.read()) == MonthUsage(NOVEMBER, requests=40, rows=90)
        ledger.close()


def test_a_holder_s_save_from_another_month_is_left_as_it_is(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    later = open_ledger(tmp_path, NflMetaPlan.FREE, log=_quiet)
    assert later.usage(NFL, clock.read()).month == NOVEMBER  # this one counts November
    _file(tmp_path).save({NFL: MonthUsage(BillingMonth(2026, 10), requests=4_000)})  # October's, from a stale clock
    assert later.claim(NFL) is None
    assert later.usage(NFL, clock.read()) == MonthUsage(NOVEMBER)
    later.close()
    unlocked = QuotaLedger(account_limits())
    assert unlocked.claim(NFL) is None  # a ledger without locks holds nothing, and has nothing to let go of
    unlocked.close()
