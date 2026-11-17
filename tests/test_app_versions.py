"""What a run decides with: the settings' digest, the versions line, and when the trace writes it."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from omni.app import __main__ as mlb_app
from omni.app import football as nfl_app
from omni.app.trace import DecisionTracer
from omni.app.versions import (
    CONFIG_SCHEMA,
    DecisionConfig,
    ModelVersion,
    PolicyName,
    SourceRevision,
    Versions,
    read_source_revision,
)
from omni.core.enum import League, PanelProfile
from omni.domain.rules import RULES
from omni.preferences.favorites import AppliedMigration, ConfigMigration, Favorites
from omni.providers.nflmeta import PlayFeedPace
from omni.renderers.text_lane import ScrollSpeed

COMMIT = SourceRevision("7518dd6")
MLB_BASE = ["--profile", "quad_128x64", "--emulated", "--favorite", "mlb:ATL", "--favorite", "mlb:COL"]
NFL_BASE = ["--profile", "quad_128x64", "--teams", "PIT,CIN", "--sink-command", "ssh omni-board sudo python3 sink.py"]


def _mlb(*flags: str) -> Versions:
    return mlb_app._versions(mlb_app._parse_args([*MLB_BASE, *flags]), COMMIT)


def _nfl(*flags: str, base: Sequence[str] = NFL_BASE) -> Versions:
    return nfl_app._versions(nfl_app._parse_args([*base, *flags]), COMMIT)


# --- the digest -------------------------------------------------------------------------------------------


def test_a_configuration_s_digest_is_its_canonical_rendering_hashed_and_the_same_every_time() -> None:
    config = DecisionConfig(
        profile=PanelProfile.QUAD_128X64,
        delay=timedelta(seconds=45),
        favorites=Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",)}),
        turn=timedelta(seconds=12),
        timezone=ZoneInfo("America/Denver"),
        meter=True,
        odds=False,
        scroll_speed=ScrollSpeed.MEDIUM,
        halftime_hold=timedelta(minutes=13),
        play_feed=PlayFeedPace.EVERY_POLL,
        meter_lag=timedelta(seconds=7.5),
    )
    assert config.canonical() == (
        "delay=45s;favorites=mlb:ATL,mlb:COL,nfl:CAR;halftime_hold=780s;meter=on;meter_lag=7.5s;odds=off;"
        "play_feed=every-poll;profile=quad_128x64;scroll_speed=medium;timezone=America/Denver;turn=12s"
    )
    assert config.digest == "e3b7afa83181"  # sha256 of the line above, its first 12 hex
    assert config.line() == f"config e3b7afa83181: {config.canonical()}"
    rebuilt = DecisionConfig(**{name: getattr(config, name) for name in DecisionConfig.__dataclass_fields__})
    assert rebuilt.digest == config.digest


def test_a_setting_a_runner_doesn_t_have_is_left_out_and_no_favorites_reads_none() -> None:
    bare = DecisionConfig(profile=PanelProfile.SINGLE_64X32, delay=timedelta(0), favorites=Favorites())
    assert bare.canonical() == "delay=0s;favorites=none;profile=single_64x32"


@pytest.mark.parametrize(
    "flags",
    [
        ["--profile", "stack_64x64"],
        ["--delay", "30"],
        ["--slot", "8"],
        ["--timezone", "America/Denver"],
        ["--meter"],
        ["--no-odds"],
        ["--scroll-speed", "medium"],
        ["--favorite", "nfl:CAR"],
    ],
)
def test_a_flag_that_changes_what_the_mlb_board_decides_changes_its_digest(flags: list[str]) -> None:
    assert _mlb(*flags).config.digest != _mlb().config.digest


def test_the_mlb_board_s_favorites_count_in_rank_order() -> None:
    swapped = ["--profile", "quad_128x64", "--favorite", "mlb:COL", "--favorite", "mlb:ATL"]
    assert mlb_app._versions(mlb_app._parse_args(swapped), COMMIT).config.digest != _mlb().config.digest


def test_where_the_mlb_board_draws_how_often_and_where_its_market_lives_don_t_change_its_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _mlb().config.digest
    monkeypatch.setenv("OMNI_CLOUD_URL", "https://example.invalid/market")
    without_emulator = [flag for flag in MLB_BASE if flag != "--emulated"]
    assert mlb_app._versions(mlb_app._parse_args(without_emulator), COMMIT).config.digest == before
    assert _mlb("--fps", "30").config.digest == before
    assert _mlb().config.digest == before


@pytest.mark.parametrize(
    "flags",
    [
        ["--profile", "single_64x32"],
        ["--delay", "20"],
        ["--meter-lag", "5"],
        ["--halftime-hold", "600"],
        ["--scroll-speed", "fast"],
        ["--timezone", "America/Chicago"],
        ["--play-feed", "every-poll"],
        ["--odds"],
    ],
)
def test_a_flag_that_changes_what_the_nfl_runner_decides_changes_its_digest(flags: list[str]) -> None:
    assert _nfl(*flags).config.digest != _nfl().config.digest


def test_the_nfl_runner_s_display_recording_frame_rate_and_game_don_t_change_its_digest() -> None:
    before = _nfl().config.digest
    assert _nfl("--sink-command", "cat").config.digest == before  # another host, another command
    emulated = ["--profile", "quad_128x64", "--teams", "PIT,CIN", "--emulated"]
    assert _nfl(base=emulated).config.digest == before
    assert _nfl("--record", "--fps", "4").config.digest == before
    other_game = ["--profile", "quad_128x64", "--teams", "LAC,BAL", "--sink-command", "cat"]
    assert _nfl(base=other_game).config.digest == before  # the game followed is the input, not a setting


def test_the_nfl_runner_s_digest_is_of_the_settings_in_effect() -> None:
    """A halftime hold given as the default is the default."""
    assert _nfl("--halftime-hold", "780").config == _nfl().config
    assert _nfl().config.halftime_hold == timedelta(minutes=13)


# --- the versions line ------------------------------------------------------------------------------------


def test_the_mlb_board_names_its_policy_rules_commit_and_settings() -> None:
    versions = _mlb()
    assert versions.policy is PolicyName.BOARD_V1 and versions.rules is RULES and versions.schema is CONFIG_SCHEMA
    assert versions.line() == (
        f"versions: policy omni-board-policy/1 | config omni-config/1 {versions.config.digest}"
        " | rules omni-rules/2026.1 | models none | source 7518dd6 | migrations none"
    )


def test_a_bare_favorite_is_the_same_setting_and_its_migration_is_recorded_with_the_entry_as_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bare = mlb_app._versions(mlb_app._parse_args(["--profile", "quad_128x64", "--favorite", "atl"]), COMMIT)
    qualified = mlb_app._versions(mlb_app._parse_args(["--profile", "quad_128x64", "--favorite", "mlb:ATL"]), COMMIT)
    assert bare.config == qualified.config
    assert bare.migrations == (
        AppliedMigration(ConfigMigration.BARE_FAVORITE_IS_MLB, original="atl", migrated="mlb:ATL"),
    )
    assert qualified.migrations == ()
    assert bare.line().endswith(" | migrations bare_favorite_is_mlb(atl->mlb:ATL)")
    assert capsys.readouterr().out == "omni.app: --favorite atl read as mlb:ATL (a team without a league is MLB's)\n"


def test_the_nfl_runner_names_its_own_policy() -> None:
    versions = _nfl()
    assert versions.policy is PolicyName.NFL_SOLO_V1
    assert versions.config.canonical() == (
        "delay=0s;halftime_hold=780s;meter=off;meter_lag=15s;play_feed=on-change;profile=quad_128x64;"
        "scroll_speed=slow;timezone=America/New_York"
    )
    assert versions.line().endswith(" | source 7518dd6 | migrations none")


def test_a_replay_s_header_leaves_out_the_commit_and_the_migrations_and_names_each_model() -> None:
    versions = Versions(
        policy=PolicyName.BOARD_V1,
        config=DecisionConfig(profile=PanelProfile.QUAD_128X64, delay=timedelta(0)),
        rules=RULES,
        models=(ModelVersion("mlb-li/0"), ModelVersion("nfl-wp/0")),
        source=COMMIT,
    )
    digest = versions.config.digest
    assert versions.header() == (
        f"versions: policy omni-board-policy/1 | config omni-config/1 {digest} | rules omni-rules/2026.1"
        " | models mlb-li/0, nfl-wp/0"
    )
    assert str(SourceRevision(None)) == "unknown"


# --- the trace --------------------------------------------------------------------------------------------


def test_the_trace_writes_the_versions_at_startup_and_again_only_on_a_change() -> None:
    lines: list[str] = []
    tracer = DecisionTracer(lines.append)
    versions = _mlb()
    tracer.versions(versions)
    assert lines == [versions.line(), versions.config.line()]  # the rendering, so the digest reads back
    tracer.versions(_mlb())
    assert len(lines) == 2  # nothing changed
    newer = Versions(policy=versions.policy, config=versions.config, rules=versions.rules, source=SourceRevision("abc"))
    tracer.versions(newer)
    assert lines[2:] == [newer.line()]  # the commit moved, the settings didn't
    delayed = _mlb("--delay", "60")
    tracer.versions(delayed)
    assert lines[3:] == [delayed.line(), delayed.config.line()]


# --- the source revision ----------------------------------------------------------------------------------


def test_the_source_revision_is_the_checked_out_commit_or_unknown() -> None:
    asked: list[Sequence[str]] = []

    def git(command: Sequence[str]) -> str:
        asked.append(command)
        return "7518dd6\n"

    assert read_source_revision(Path("/repo"), run=git) == SourceRevision("7518dd6")
    assert asked == [["git", "-C", "/repo", "rev-parse", "--short", "HEAD"]]
    assert read_source_revision(Path("/repo"), run=lambda command: "") == SourceRevision(None)


@pytest.mark.parametrize("error", [OSError("no git"), subprocess.TimeoutExpired("git", 5)])
def test_a_source_revision_git_can_t_give_is_unknown(error: Exception) -> None:
    def git(command: Sequence[str]) -> str:
        raise error

    assert str(read_source_revision(Path("/repo"), run=git)) == "unknown"
