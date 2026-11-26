"""Redact the NFL runner's log into a log of our own, line for line; or check files for NFLMeta's text or ids.

    .venv/bin/python tools/redact_run_log.py build/logs/nflmeta/2026-11-19-NYJ-NE/runner.log \\
        --out build/telemetry/runner-log/2026-11-19-NYJ-NE.redacted.log --first-date 2026-11-19
    .venv/bin/python tools/redact_run_log.py --check sources/

Writing reads the runner's log, in any of its formats (`omni.telemetry.runner_log_mine`), and writes it redacted
(`omni.telemetry.runner_log_redact`): each line's stamp, kind and our own words kept, NFLMeta's ids numbered in the
order first seen, its words replaced. ``--first-date`` dates the log's nights in the header. It never overwrites a
file, and prints only counts: the lines kept as they were, those with words or ids replaced, and those not kept.

``--check`` is the gate before anything is bundled or shared: it exits 1 when any file it's given would change
under redaction, so still carries NFLMeta's words or ids. A folder is walked for its ``*.log`` files and the
``*.log`` members of its ``*.zip`` archives, and a ``.zip`` given is read in memory the same way; any other file
given is checked as a log. A path that can't be read fails the check. It prints each offending path with the
count of lines that would change, never their content.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # so `omni` resolves when this script is run directly

from omni.telemetry.runner_log_redact import lines_to_redact, redact

_LOG = ".log"
_ZIP = ".zip"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", nargs="?", type=Path, help="the runner's log to redact")
    parser.add_argument("--out", type=Path, help="where the redacted log goes; never overwritten")
    parser.add_argument(
        "--first-date", type=date.fromisoformat, help="the date of the log's first night, for the header"
    )
    parser.add_argument(
        "--check", nargs="+", type=Path, metavar="PATH", help="exit 1 if any log here still carries NFLMeta's words"
    )
    args = parser.parse_args(argv)
    if args.check is not None:
        if args.log is not None or args.out is not None or args.first_date is not None:
            parser.error("--check takes only the paths to check")
        return _check(args.check)
    if args.log is None or args.out is None:
        parser.error("give a log and --out to redact it, or --check PATH ...")
    return _write(args.log, args.out, args.first_date)


def _write(log: Path, out: Path, first_date: date | None) -> int:
    if out.exists():
        print(f"{out} is already there; not overwritten", file=sys.stderr)
        return 1
    redaction = redact(_lines(log.read_bytes()), first_date=first_date)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as written:
        written.write("\n".join(redaction.lines) + "\n")
    print(f"{out}: {redaction.kept} lines kept, {redaction.replaced} replaced, {redaction.not_kept} not kept")
    return 0


def _check(paths: Sequence[Path]) -> int:
    checked = failing = 0
    for name, lines in _logs(paths):
        checked += 1
        if lines is None:
            print(f"{name}: can't be read")
            failing += 1
        elif changed := lines_to_redact(lines):
            print(f"{name}: {changed} lines would change under redaction")
            failing += 1
    print(f"checked {checked} logs: {failing} carry NFLMeta text or ids, or can't be read")
    return 1 if failing else 0


def _logs(paths: Sequence[Path]) -> Iterator[tuple[str, list[str] | None]]:
    """Each log to check, by name, and its lines; None for one that can't be read."""
    for path in paths:
        if path.is_dir():
            for found in sorted(path.rglob("*")):
                if found.is_file() and found.suffix == _LOG:
                    yield str(found), _read(found)
                elif found.is_file() and found.suffix == _ZIP:
                    yield from _members(found)
        elif path.suffix == _ZIP:
            yield from _members(path)
        else:
            yield str(path), _read(path)


def _members(archive: Path) -> Iterator[tuple[str, list[str] | None]]:
    """Each ``*.log`` member of ``archive``, read in memory; the archive itself when it can't be read."""
    try:
        with zipfile.ZipFile(archive) as opened:
            members = [(name, opened.read(name)) for name in opened.namelist() if name.endswith(_LOG)]
    except (OSError, zipfile.BadZipFile):
        yield str(archive), None
        return
    for name, data in members:
        yield f"{archive}:{name}", _lines(data)


def _read(path: Path) -> list[str] | None:
    try:
        return _lines(path.read_bytes())
    except OSError:
        return None


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


if __name__ == "__main__":
    raise SystemExit(main())
