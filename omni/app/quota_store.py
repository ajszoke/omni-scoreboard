"""Keeping each account's monthly usage between runs, so a restart doesn't hand the board a fresh month.

`UsageFile` is a small JSON file under the board's state directory (`--state-dir`; by default
``$XDG_STATE_HOME/omni``, else ``~/.local/state/omni``). It holds one entry per account: the billing month,
its requests and rows, and whether the provider said nothing was left. Nothing else, and nothing personal.

- **Writes are atomic:** the new file is written beside the old one and moved over it, so a crash mid-write
  leaves the old file whole. The ledger writes at most once a minute, and at shutdown, so an SD card isn't
  written once a request.
- **Two boards on one machine share the file:** a write keeps the larger count of each account's month, so
  one board's write never lowers another's count.
- **An unreadable file** is set aside (renamed with ``.unreadable``) with a warning, and the count starts
  from zero, until a provider's own count of what's left raises it.
- **A new month starts at zero:** a saved month older than the current one is ignored.

**One poller per account.** A count kept this way can't stop two processes from spending one account at once:
each would admit against its own count. So an account that needs it (NFLMeta's) is held by one process at a time
(`FileLocks`): an exclusive lock on ``<account>.lock`` beside the usage file, which the OS lets go of when the
process ends, however it ends. The process that holds it writes its pid and name there, so another can say who
does. The shared board and the NFL runner take it through their ledger (`open_ledger`), and the NFLMeta recorder
(`tools/nflmeta_record.py`) takes it before its first request.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

from omni.app.quota import (
    BalldontliePlan,
    BillingMonth,
    MonthUsage,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    QuotaPolicy,
    SHEDDING,
    account_limits,
    larger_usage,
)

__all__ = ["HELD_ONE_AT_A_TIME", "SCHEMA", "USAGE_FILE", "FileLocks", "UsageFile", "default_state_dir", "open_ledger"]

SCHEMA = "omni-quota-usage/1"
USAGE_FILE = "quota-usage.json"
HELD_ONE_AT_A_TIME = frozenset({ProviderAccount.NFLMETA})  # the accounts one process at a time may poll


def default_state_dir(environ: Mapping[str, str], home: Path) -> Path:
    """Where the board keeps its state: ``$XDG_STATE_HOME/omni``, or ``~/.local/state/omni`` without one."""
    xdg = environ.get("XDG_STATE_HOME")
    return Path(xdg) / "omni" if xdg else home / ".local" / "state" / "omni"


class _Unreadable(Exception):
    """The usage file isn't one this version can read."""


class UsageFile:
    """The usage file: read once at start, written atomically after."""

    def __init__(
        self,
        path: Path,
        *,
        log: Callable[[str], None] = print,
        replace: Callable[[str, Path], None] = os.replace,
    ) -> None:
        self._path = path
        self._log = log
        self._replace = replace  # moves the written file over the old one

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[ProviderAccount, MonthUsage]:
        """What the file holds; nothing when there's no file yet. An unreadable file is set aside."""
        try:
            return self._read()
        except FileNotFoundError:
            return {}
        except (OSError, _Unreadable) as exc:
            aside = self._path.with_name(self._path.name + ".unreadable")
            try:
                os.replace(self._path, aside)
            except OSError as moved:
                self._log(f"quota: {self._path} is unreadable ({exc}) and couldn't be set aside ({moved})")
            else:
                self._log(f"quota: {self._path} was unreadable ({exc}); set aside as {aside.name}, counting from zero")
            return {}

    def save(self, usage: Mapping[ProviderAccount, MonthUsage]) -> bool:
        """Write ``usage``, keeping any larger count the file already has for the same month; whether it was
        written. A failure is logged and leaves the old file as it was."""
        try:
            merged = larger_usage(self._read(), usage)
        except (OSError, _Unreadable):
            merged = dict(usage)  # nothing readable to keep
        document = {
            "schema": SCHEMA,
            "accounts": {
                account.value: {
                    "month": str(held.month),
                    "requests": held.requests,
                    "rows": held.rows,
                    "exhausted": held.exhausted,
                }
                for account, held in sorted(merged.items(), key=lambda item: item[0].value)
            },
        }
        written: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, written = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(document, out, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            self._replace(written, self._path)
        except OSError as exc:
            self._log(f"quota: couldn't save the usage to {self._path}: {exc}")
            if written is not None:
                Path(written).unlink(missing_ok=True)
            return False
        return True

    def _read(self) -> dict[ProviderAccount, MonthUsage]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema") != SCHEMA:
                raise _Unreadable(f"not a {SCHEMA} file")
            accounts = document["accounts"]
            return {ProviderAccount(name): _usage(entry) for name, entry in accounts.items()}
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _Unreadable(f"{type(exc).__name__}: {exc}") from exc


def _usage(entry: Any) -> MonthUsage:
    requests, rows, exhausted = entry["requests"], entry["rows"], entry["exhausted"]
    if not all(isinstance(count, int) and not isinstance(count, bool) for count in (requests, rows)):
        raise TypeError("a count is a whole number")
    if not isinstance(exhausted, bool):
        raise TypeError("exhausted is true or false")
    return MonthUsage(BillingMonth.parse(entry["month"]), requests=requests, rows=rows, exhausted=exhausted)


def _try_lock(fd: int) -> None:
    """Take an exclusive lock on the open file ``fd`` without waiting: `BlockingIOError` when another holds it."""
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class FileLocks:
    """Each of ``accounts`` held by one process on the machine at a time, through an exclusive lock on
    ``<account>.lock`` under ``state_dir``. ``who`` names this process to another that finds the lock held."""

    def __init__(
        self,
        state_dir: Path,
        accounts: Collection[ProviderAccount] = HELD_ONE_AT_A_TIME,
        *,
        who: str | None = None,
        lock: Callable[[int], None] = _try_lock,
    ) -> None:
        self._dir = state_dir
        self._accounts = frozenset(accounts)
        self._who = who if who is not None else Path(sys.argv[0]).name
        self._lock = lock
        self._held: dict[ProviderAccount, int] = {}  # each account held, by its lock file's descriptor

    def claim(self, account: ProviderAccount) -> str | None:
        """Hold ``account`` for this process: None once held (or when it needs no holding), else who holds it."""
        if account not in self._accounts or account in self._held:
            return None
        path = self._dir / f"{account.value}.lock"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            return f"its lock {path} can't be opened: {exc}"  # a lock that can't be had is no proof no one holds it
        try:
            self._lock(fd)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                return _holder(path)
            return f"its lock {path} can't be taken: {exc}"
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {self._who}\n".encode())
        self._held[account] = fd
        return None

    def release(self) -> None:
        """Let go of every account held: closing a lock's file lets go of its lock."""
        for fd in self._held.values():
            os.close(fd)
        self._held.clear()


def _holder(path: Path) -> str:
    """Who holds a lock, as its holder wrote it: "pid 4242, omni.app.football"."""
    try:
        pid, _, who = path.read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        return "another process"
    return f"pid {pid}, {who}" if pid and who else f"pid {pid}" if pid else "another process"


def open_ledger(
    state_dir: Path,
    nflmeta: NflMetaPlan,
    *,
    balldontlie: BalldontliePlan = BalldontliePlan.FREE,
    policy: QuotaPolicy = SHEDDING,
    log: Callable[[str], None] = print,
    who: str | None = None,
) -> QuotaLedger:
    """The board's ledger: every account at today's limits (NFLMeta's at ``nflmeta``, BALLDONTLIE's at
    ``balldontlie``), counting on from the
    usage saved under ``state_dir``, and saving back there. NFLMeta is polled by one process at a time: the ledger
    holds it under ``state_dir`` (`FileLocks`, as ``who``, by default the program's name) from its first ask on."""
    usage = UsageFile(state_dir / USAGE_FILE, log=log)
    return QuotaLedger(
        account_limits(nflmeta, balldontlie),
        policy=policy,
        usage=usage.load(),
        store=usage,
        locks=FileLocks(state_dir, who=who),
    )
