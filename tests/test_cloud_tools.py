"""The operator tools' secret handling, since a slip here would lose or expose one."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cloud.tools.issue_board_token import updated_env_text, write_private
from cloud.tools.load_polymarket_key import store_parameter
from omni.core.env import read_env_file


def test_settings_replace_their_old_lines_and_leave_the_rest_alone() -> None:
    existing = "# my keys\nPOLYMARKET_KEY_ID=abc\nexport OMNI_CLOUD_TOKEN=old\n\nOTHER=1\n"
    text = updated_env_text(existing, {"OMNI_CLOUD_TOKEN": "new", "OMNI_CLOUD_URL": "https://x"})
    assert text == "# my keys\nPOLYMARKET_KEY_ID=abc\nOMNI_CLOUD_TOKEN=new\n\nOTHER=1\nOMNI_CLOUD_URL=https://x\n"


def test_what_gets_written_reads_back(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_private(path, updated_env_text("", {"OMNI_CLOUD_TOKEN": "den.secret=="}))
    assert read_env_file(path) == {"OMNI_CLOUD_TOKEN": "den.secret=="}


def test_the_file_is_private_even_when_it_replaces_an_open_one(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OLD=1\n")
    path.chmod(0o644)
    write_private(path, "NEW=1\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "NEW=1\n"
    assert not (tmp_path / ".env.tmp").exists()


def test_a_parameters_value_goes_on_stdin_and_never_on_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "1", "")

    monkeypatch.setattr(subprocess, "run", run)
    store_parameter("/omni/test/secret-key", "s3cret==", profile="omni")
    command, kwargs = calls[0]
    assert kwargs["input"] == "s3cret=="
    assert not any("s3cret" in part for part in command)
    # The CLI refuses --cli-input-json from stdin, though it takes a single parameter's value this way.
    assert "--cli-input-json" not in command
    assert command[command.index("--value") + 1] == "file:///dev/stdin"
    assert command[command.index("--type") + 1] == "SecureString"
