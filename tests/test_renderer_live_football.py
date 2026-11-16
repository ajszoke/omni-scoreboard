"""The live football card: goldens for each profile across the states a game passes through."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import GolfTournament
from omni.domain.baseball import WinProbability
from omni.domain.football import (
    FootballGameState,
    FootballInterval,
    PlaySummary,
    PlayTone,
    TeamChallenges,
    TeamTimeouts,
)
from omni.panels.geometry import geometry_for
from omni.renderers.canvas import RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_football import LiveFootballRenderer, clock_text, period_parts
from omni.renderers.pillow_canvas import PillowCanvas
from tests.football_fixtures import ALL_PROFILES, T, live_football_card, nfl_game

GOLDEN_DIR = Path(__file__).parent / "golden"
LOGOS = LogoStore()  # resolves the committed NFL tiles
A, H = HomeAway.AWAY, HomeAway.HOME

STATES = {
    "drive": (
        FootballGameState(
            period=3,
            clock_seconds=754,
            away_score=17,
            home_score=14,
            possession=A,
            down=3,
            distance=7,
            yards_to_goal=38,
            timeouts=TeamTimeouts(away=2, home=3),
            challenges=TeamChallenges(away=2, home=1),
        ),
        PlaySummary(tone=PlayTone.STANDARD, headline="12 yd pass", detail="A.Rodgers to D.Metcalf"),
        WinProbability(home=42.0, away=58.0),
    ),
    "red_zone": (
        FootballGameState(
            period=4,
            clock_seconds=161,
            away_score=24,
            home_score=21,
            possession=H,
            down=1,
            distance=8,
            yards_to_goal=8,
            timeouts=TeamTimeouts(away=1, home=2),
            challenges=TeamChallenges(away=0, home=1),
        ),
        PlaySummary(tone=PlayTone.ALERT, headline="PIT PENALTY", detail="J.Porter Jr., Pass Interference (22 yds)"),
        WinProbability(home=55.0, away=45.0),
    ),
    "turnover": (
        FootballGameState(
            period=2,
            clock_seconds=48,
            away_score=10,
            home_score=7,
            possession=A,
            down=1,
            distance=10,
            yards_to_goal=31,
            timeouts=TeamTimeouts(away=3, home=1),
            challenges=TeamChallenges(away=2, home=2),
        ),
        PlaySummary(tone=PlayTone.TURNOVER, headline="INTERCEPTION", detail="J.Burrow, by J.Porter Jr. to CIN 31"),
        WinProbability(home=36.0, away=64.0),
    ),
    "halftime": (
        FootballGameState(
            period=2,
            clock_seconds=0,
            away_score=13,
            home_score=7,
            interval=FootballInterval.HALFTIME,
            timeouts=TeamTimeouts(away=3, home=3),
            challenges=TeamChallenges(away=2, home=1),
        ),
        PlaySummary(tone=PlayTone.SCORE, headline="FG GOOD", detail="C.Boswell (52 yds)"),
        WinProbability(home=31.0, away=69.0),
    ),
    "overtime": (
        FootballGameState(
            period=5,
            clock_seconds=432,
            away_score=20,
            home_score=20,
            possession=A,
            down=2,
            distance=3,
            yards_to_goal=50,
            timeouts=TeamTimeouts(away=2, home=2),
            challenges=TeamChallenges(away=1, home=1),
        ),
        PlaySummary(tone=PlayTone.STANDARD, headline="4 yd rush", detail="J.Warren"),
        WinProbability(home=48.0, away=52.0),
    ),
}


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


@pytest.mark.parametrize("state_name", list(STATES))
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_golden_image_per_state_and_profile(state_name: str, profile: PanelProfile) -> None:
    state, play, win = STATES[state_name]
    width, height = geometry_for(profile).size
    canvas = PillowCanvas(width, height)
    card = live_football_card(state, last_play=play, win_probability=win)
    LiveFootballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), canvas)
    _assert_matches_golden(canvas.image(), f"live_football_{state_name}_{profile.to_json_value()}.png")


# --- behavior ------------------------------------------------------------------------------------


def _texts(state: FootballGameState, profile: PanelProfile, **card: object) -> list[str]:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    LiveFootballRenderer().render(live_football_card(state, **card), RenderContext(profile=profile, now=T), canvas)  # type: ignore[arg-type]
    return [op.text for op in canvas.texts() if op.text]


def _rects(state: FootballGameState, profile: PanelProfile) -> int:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    LiveFootballRenderer().render(live_football_card(state), RenderContext(profile=profile, now=T), canvas)
    return len(canvas.rects())


@pytest.mark.parametrize(
    "period, parts",
    [(1, ("1", "st")), (2, ("2", "nd")), (3, ("3", "rd")), (4, ("4", "th")), (5, ("OT", "")), (6, ("2OT", ""))],
)
def test_periods_read_the_way_a_broadcast_writes_them(period: int, parts: tuple[str, str]) -> None:
    assert period_parts(period) == parts


@pytest.mark.parametrize("seconds, text", [(754, "12:34"), (245, "4:05"), (7, "0:07"), (0, "0:00")])
def test_the_clock_reads_the_way_a_broadcast_writes_it(seconds: int, text: str) -> None:
    assert clock_text(seconds) == text


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_an_end_of_quarter_shows_which_quarter_and_no_clock_or_down(profile: PanelProfile) -> None:
    state = FootballGameState(
        period=1, clock_seconds=0, away_score=7, home_score=0, interval=FootballInterval.END_OF_QUARTER
    )
    texts = _texts(state, profile)
    assert "END" in " ".join(texts) and "1st" in " ".join(texts)
    assert "0:00" not in texts and "&" not in texts


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_with_no_next_snap_there_is_no_down_or_spot(profile: PanelProfile) -> None:
    kickoff = FootballGameState(period=1, clock_seconds=900, away_score=0, home_score=0)
    texts = _texts(kickoff, profile)
    assert "&" not in texts and "15:00" in texts


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_a_try_after_a_score_shows_the_spot_but_no_down(profile: PanelProfile) -> None:
    extra_point = FootballGameState(
        period=2, clock_seconds=300, away_score=6, home_score=0, possession=HomeAway.AWAY, yards_to_goal=15
    )
    texts = _texts(extra_point, profile)
    assert "&" not in texts and "15" in texts


def test_a_long_overtime_label_drops_the_clock_a_size_to_fit_the_quad() -> None:
    double_overtime = FootballGameState(period=6, clock_seconds=600, away_score=20, home_score=20)
    width, height = geometry_for(PanelProfile.QUAD_128X64).size
    canvas = RecordingCanvas(width, height)
    card = live_football_card(double_overtime)
    LiveFootballRenderer().render(card, RenderContext(profile=PanelProfile.QUAD_128X64, now=T), canvas)
    clock = next(op for op in canvas.texts() if op.text == "10:00")
    assert clock.font == "6x10"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_unknown_allowances_draw_no_pips(profile: PanelProfile) -> None:
    bare = FootballGameState(period=2, clock_seconds=300, away_score=7, home_score=3)
    known = FootballGameState(
        period=2,
        clock_seconds=300,
        away_score=7,
        home_score=3,
        timeouts=TeamTimeouts(away=3, home=3),
        challenges=TeamChallenges(away=2, home=2),
    )
    assert _rects(known, profile) > _rects(bare, profile)


def test_a_play_with_no_detail_draws_only_its_headline() -> None:
    state, _, _ = STATES["drive"]
    texts = _texts(
        state, PanelProfile.QUAD_128X64, last_play=PlaySummary(tone=PlayTone.ALERT, headline="TIMEOUT", detail=None)
    )
    assert "TIMEOUT" in texts


def test_the_strip_is_empty_with_no_last_play() -> None:
    state, _, _ = STATES["drive"]
    assert not any("pass" in text for text in _texts(state, PanelProfile.QUAD_128X64))


def test_a_card_whose_contest_isnt_a_team_game_is_refused() -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("x"), "t"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=T,
        tournament_name="Open",
    )
    card = live_football_card(STATES["drive"][0], game=nfl_game())
    card = type(card)(**{**{f: getattr(card, f) for f in card.__dataclass_fields__}, "contest": golf})
    with pytest.raises(TypeError):
        LiveFootballRenderer().render(
            card, RenderContext(profile=PanelProfile.QUAD_128X64, now=T), RecordingCanvas(128, 64)
        )


@pytest.mark.parametrize("profile", [PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64])
def test_a_club_whose_tile_is_missing_falls_back_to_a_color_bar_and_its_abbreviation(profile: PanelProfile) -> None:
    state, play, win = STATES["drive"]
    width, height = geometry_for(profile).size
    with_tiles, without = RecordingCanvas(width, height), RecordingCanvas(width, height)
    card = live_football_card(state, last_play=play, win_probability=win)
    LiveFootballRenderer().render(card, RenderContext(profile=profile, now=T, logos=LOGOS), with_tiles)
    LiveFootballRenderer().render(card, RenderContext(profile=profile, now=T), without)
    assert len(with_tiles.images()) == 2 and not without.images()

    def row_labels(canvas: RecordingCanvas) -> set[str | None]:
        # The team rows: left of the pips and above the play strip. The spot ("CIN 38") also names a club.
        return {op.text for op in canvas.texts() if op.x is not None and op.y is not None and op.x < 20 and op.y < 40}

    assert row_labels(without) == {"PIT", "CIN"}
    assert not row_labels(with_tiles)  # the logo names the club
