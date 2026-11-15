"""The git-ignored .env reader."""

from __future__ import annotations

from pathlib import Path

from omni.core.env import read_env_file


def test_a_missing_file_means_no_settings(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / ".env") == {}


def test_it_reads_pairs_past_comments_exports_blanks_and_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("""
# a comment
PLAIN=one
export EXPORTED=two
DOUBLE="three four"
SINGLE='five'
EQUALS=a=b==
SPACED = six
not a setting
EMPTY=
LONE_QUOTE="
""")
    assert read_env_file(env) == {
        "PLAIN": "one",
        "EXPORTED": "two",
        "DOUBLE": "three four",
        "SINGLE": "five",
        "EQUALS": "a=b==",
        "SPACED": "six",
        "EMPTY": "",
        "LONE_QUOTE": '"',
    }
