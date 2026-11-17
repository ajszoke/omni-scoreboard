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
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omni.app.quota import (
    BillingMonth,
    MonthUsage,
    NflMetaPlan,
    ProviderAccount,
    QuotaLedger,
    QuotaPolicy,
    SHEDDING,
    account_limits,
)

__all__ = ["SCHEMA", "USAGE_FILE", "UsageFile", "default_state_dir", "open_ledger"]

SCHEMA = "omni-quota-usage/1"
USAGE_FILE = "quota-usage.json"


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
            merged = _merged(self._read(), usage)
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


def _merged(
    saved: Mapping[ProviderAccount, MonthUsage], usage: Mapping[ProviderAccount, MonthUsage]
) -> dict[ProviderAccount, MonthUsage]:
    """Each account's later month, and in the same month the larger counts of the two."""
    merged = dict(saved)
    for account, ours in usage.items():
        theirs = merged.get(account)
        if theirs is None or ours.month > theirs.month:
            merged[account] = ours
        elif ours.month == theirs.month:
            merged[account] = MonthUsage(
                ours.month,
                requests=max(ours.requests, theirs.requests),
                rows=max(ours.rows, theirs.rows),
                exhausted=ours.exhausted or theirs.exhausted,
            )
    return merged


def open_ledger(
    state_dir: Path,
    nflmeta: NflMetaPlan,
    *,
    policy: QuotaPolicy = SHEDDING,
    log: Callable[[str], None] = print,
) -> QuotaLedger:
    """The board's ledger: every account at today's limits (NFLMeta's at ``nflmeta``), counting on from the
    usage saved under ``state_dir``, and saving back there."""
    usage = UsageFile(state_dir / USAGE_FILE, log=log)
    return QuotaLedger(account_limits(nflmeta), policy=policy, usage=usage.load(), store=usage)
