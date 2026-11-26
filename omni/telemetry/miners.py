"""NFLMeta's miner for the expiry sweep: before an overdue folder is deleted, its measurements are kept.

The sweep (`omni.providers.rights.sweep_expired`) offers each overdue NFLMeta folder to this miner. A
recording (its scoreboards and play feed) is mined by `mine_recording`; a run's own log folder (``runner.log``)
by `mine_runner_log`, each night of it to a digest of its own, with the night's log redacted beside it
(`write_redacted_nights`: line for line, our own words only, ``runner-log/<run>/<date>.redacted.log``). Digests
and redacted logs go under the telemetry root, outside every folder the sweep deletes, and hold nothing of the
provider's.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from omni.app.run_log import RUN_LOG_FILE
from omni.providers.rights import RetentionStamp
from omni.telemetry.nflmeta_mine import mine_recording
from omni.telemetry.runner_log_mine import mine_runner_log
from omni.telemetry.runner_log_redact import write_redacted_nights

__all__ = ["NflMetaMiner"]


class NflMetaMiner:
    """Mines an NFLMeta recording, or a run's log, into digests under ``root``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def digest_for(self, folder: Path, stamp: RetentionStamp) -> Path:
        """Where ``folder``'s digest goes: a recording's by its name; a run log's first night's in a folder of its
        own, named for the run."""
        if self._is_run_log(folder):
            return self._root / "runner-log" / folder.name / f"{_first_date(folder, stamp).isoformat()}.json"
        return self._root / "nflmeta" / f"{folder.name}.json"

    def mine(self, folder: Path, stamp: RetentionStamp) -> Path | None:
        """Write ``folder``'s digests (and a run log's redacted nights); the first digest's path, or None when it
        holds nothing to mine. The digests go last: the first one's being there says the folder was mined."""
        if self._is_run_log(folder):
            out, first = self._root / "runner-log" / folder.name, _first_date(folder, stamp)
            write_redacted_nights(folder / RUN_LOG_FILE, out, first)
            paths = mine_runner_log(folder / RUN_LOG_FILE, out, first)
            return paths[0] if paths else None
        return mine_recording(folder, self._root / "nflmeta")

    @staticmethod
    def _is_run_log(folder: Path) -> bool:
        return (folder / RUN_LOG_FILE).is_file()


def _first_date(folder: Path, stamp: RetentionStamp) -> date:
    """The run's first night: the date the folder is named for, or the day it was stamped."""
    try:
        return date.fromisoformat(folder.name[:10])
    except ValueError:
        return stamp.created.date()
