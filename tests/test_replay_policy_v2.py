"""Policy v2 through the production spine: the MLB slate and the mixed day replayed under `omni-board-policy/2`.

Both replays run the real pipelines over their timed feeds, the released generations, the board's policy director
and the display's commits, on all three profiles, and list every decision with the policy's own lines
(`omni.replay.policy_lines`). The mixed day here is `tests.mixed_slate_v2`: the mixed Sunday with the user's
favorites (`mlb:ATL`, `mlb:COL`, `nfl:CAR`) playing too. Its scorecard (`omni.replay.scorecard`) is recorded beside
its decisions.

The fixtures are behavior baselines, regenerated deliberately: run each replay as these tests do and write its lines
(and the scorecard's markdown, titled `SCORECARD_TITLE`) to the fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from omni.app.quota import ProviderAccount
from omni.core.enum import League, PanelProfile
from omni.core.time import DurationSeconds
from omni.policy.leverage import ModelStatus
from omni.policy.settings import PolicySettings
from omni.replay.harness import replay_decisions, replay_versions
from omni.replay.mixed import MixedRun, run_mixed
from omni.replay.scorecard import ScoreKeeper, Scorecard
from tests.mixed_slate_v2 import ATL, CAR, COL, FAVORITES, mixed_slate_v2
from tests.mlb_slate import END, LAG, slate
from tests.mlb_slate import FAVORITES as SLATE_FAVORITES

FIXTURES = Path(__file__).parent / "fixtures" / "replay"
SLATE = FIXTURES / "mlb-slate.v2.decisions.txt"
DAY = FIXTURES / "mixed-slate.v2.decisions.txt"
SCORECARD = FIXTURES / "mixed-slate.v2.scorecard.md"
SCORECARD_TITLE = "The mixed day under omni-board-policy/2"
V2 = PolicySettings()  # the app's default: omni-board-policy/2
QUAD = PanelProfile.QUAD_128X64
DAY_LAG = DurationSeconds(45)


@dataclass(frozen=True)
class _Day:
    run: MixedRun
    card: Scorecard


@pytest.fixture(scope="module", params=list(PanelProfile), ids=lambda profile: profile.value)
def day(request: pytest.FixtureRequest) -> _Day:
    """The v2 mixed day on one profile, run once and shared."""
    keeper = ScoreKeeper()
    profile: PanelProfile = request.param
    run = run_mixed(
        mixed_slate_v2(), profile=profile, favorites=FAVORITES, broadcast_lag=DAY_LAG, settings=V2, watch=keeper
    )
    return _Day(run=run, card=keeper.card(run))


# --- the recorded decisions ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", list(PanelProfile))
def test_the_mlb_slate_under_policy_v2_replays_to_its_recorded_decisions_on_every_profile(
    profile: PanelProfile,
) -> None:
    header, *decided = replay_decisions(
        slate(), profile=profile, until=END, broadcast_lag=LAG, favorites=SLATE_FAVORITES, settings=V2
    )
    recorded, *expected = SLATE.read_text().splitlines()
    assert decided == expected
    assert header == replay_versions(profile, broadcast_lag=LAG, favorites=SLATE_FAVORITES, settings=V2).header()
    assert recorded == replay_versions(QUAD, broadcast_lag=LAG, favorites=SLATE_FAVORITES, settings=V2).header()


def test_the_mixed_day_under_policy_v2_replays_to_its_recorded_decisions_on_every_profile(day: _Day) -> None:
    recorded, *expected = DAY.read_text().splitlines()
    assert list(day.run.decisions) == expected
    assert recorded == (
        replay_versions(QUAD, broadcast_lag=DAY_LAG, favorites=FAVORITES, settings=V2, nfl_delay=DAY_LAG).header()
    )


def test_the_v2_fixtures_name_the_policy_and_the_settings_that_made_them() -> None:
    versions = replay_versions(QUAD, broadcast_lag=DAY_LAG, favorites=FAVORITES, settings=V2, nfl_delay=DAY_LAG)
    assert versions.policy.value == "omni-board-policy/2"
    assert versions.config.canonical() == (
        "delay=45s;favorites=mlb:ATL,mlb:COL,nfl:CAR;league_weights=none;meter=off;mutes=none;nfl_delay=45s;"
        "odds=off;policy=omni-board-policy/2;profile=quad_128x64;promotions=on;scroll_speed=slow;takeover_cap=30s;"
        "turn=12s"
    )
    assert SLATE.read_text().splitlines()[0].startswith("versions: policy omni-board-policy/2 |")
    # Without settings, a replay decides by the bands and its header is as it always was.
    assert replay_versions(QUAD, broadcast_lag=LAG, favorites=SLATE_FAVORITES).policy.value == "omni-board-policy/1"


def test_every_line_the_policy_wrote_in_the_replays_is_one_of_its_kinds() -> None:
    kinds = {"base", "promote", "wait", "unspent", "lease", "guest", "yield", "card", "cap", "clamp"}
    board = {"up", "down", "drop", "frame", "stale", "unseen", "final", "note", "source", "health", "scope"}
    for fixture in (SLATE, DAY):
        written = {line.split()[1] for line in fixture.read_text().splitlines()[1:]}
        assert written <= kinds | board | {"corrected"}
        assert {"base", "card", "wait"} <= written
    assert "lease" in {line.split()[1] for line in DAY.read_text().splitlines()[1:]}


# --- the scorecard ------------------------------------------------------------------------------------------------


def test_the_mixed_day_s_scorecard_is_its_recorded_one_on_every_profile(day: _Day) -> None:
    assert day.card.markdown(title=SCORECARD_TITLE) == SCORECARD.read_text()


def test_each_league_s_actual_base_share_lands_within_a_point_of_its_target(day: _Day) -> None:
    card = day.card
    assert round(card.base, 1) == 16889.8
    shares = {each.league: (each.target / card.base, each.actual / card.base) for each in card.leagues}
    assert {league: (round(target, 4), round(actual, 4)) for league, (target, actual) in shares.items()} == {
        League.MLB: (0.4146, 0.4186),
        League.NFL: (0.5854, 0.5814),
    }
    for target, actual in shares.values():
        assert abs(actual - target) < 0.01


def test_the_favorites_get_their_share_and_no_game_waits_long_for_a_base_turn(day: _Day) -> None:
    card = day.card
    panthers, braves, rockies = card.game(CAR), card.game(ATL), card.game(COL)
    assert (panthers.favorite, braves.favorite, rockies.favorite) == (1, 1, 2)
    assert (round(panthers.target, 1), round(panthers.actual, 1)) == (4960.4, 4944.3)
    assert (round(braves.target, 1), round(braves.actual, 1)) == (1971.7, 1978.4)
    assert (round(rockies.target, 1), round(rockies.actual, 1)) == (2573.0, 2618.3)
    assert max(game.longest_wait for game in card.games) == pytest.approx(192.2, abs=0.05)
    assert sum(game.missed for game in card.games) == 6
    assert (card.floor_turns, card.clamps, card.rescues) == (4, 3, 8)


def test_breaks_lend_guests_with_no_overrun_and_none_before_the_released_state_shows_the_break(day: _Day) -> None:
    card = day.card
    assert card.lease_kinds == {
        "mlb inning break": 25,
        "mlb mound visit": 1,
        "nfl halftime": 3,
        "nfl official timeout": 1,
        "nfl quarter break": 1,
        "nfl team timeout": 1,
        "nfl two-minute warning": 1,
    }
    assert (card.guests, dict(card.guest_refusals)) == (19, {"no_room": 4})
    assert card.overruns == () and card.premature == ()


def test_the_day_spends_what_the_quota_ledger_counts_and_spoils_nothing(day: _Day) -> None:
    card = day.card
    assert card.usage == {ProviderAccount.MLB_STATSAPI: (7416, 0), ProviderAccount.NFLMETA: (2189, 4910)}
    assert set(card.leverage) == {ModelStatus.PROXY}  # no model has passed validation: every G is the labeled proxy
    assert len(card.unknown_records) == 10  # the replay's pregame details carry no records
    assert card.spoilers == 0 and day.run.early == ()


def test_the_day_s_promotions_waits_and_lapses_are_counted(day: _Day) -> None:
    card = day.card
    assert dict(card.promotions) == {}
    assert dict(card.waits) == {"already_shown": 1, "below_bar": 1, "below_every_bar": 3, "expired": 3}
    assert (card.held, card.ran_out) == (0, 3)
    assert dict(card.leases) == {"lapsed": 32, "opened": 33, "revoked": 31}
