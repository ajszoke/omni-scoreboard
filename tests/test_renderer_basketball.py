"""The NBA cards: a golden per card per profile, both halves where a live turn splits, from the locked design.

Each frame is drawn the way a board draws it: through a lane clock that a card's earlier frames went up on, so the
turn's half and every scrolling line read how long the card (and each line) has been up.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from omni.cards.base import CardKind, ScoreboardCard
from omni.cards.basketball import BasketballGameCardPayload, BestPlayers, CaptionPanel, PlayerLinesPanel
from omni.core.clock import MonotonicTime
from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.baseball import WinLossRecord
from omni.domain.basketball import BasketballPhase, BySide, ScoringRun
from omni.domain.contest import GolfTournament
from omni.events.basketball import TimeoutTaken
from omni.panels.geometry import geometry_for
from omni.renderers.basketball_pregame import BasketballPregameRenderer
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.live_basketball import _SECONDARY as SECONDARY
from omni.renderers.live_basketball import LiveBasketballRenderer, TurnHalf, turn_half
from omni.renderers.pillow_canvas import PillowCanvas
from omni.renderers.text_lane import LaneClock, LaneFrame, Marquee
from tests.basketball_fixtures import SPECIMENS, T, ZONE, Specimen, game_card, live, player, side

GOLDEN_DIR = Path(__file__).parent / "golden"
LOGOS = LogoStore()
_START = MonotonicTime(5000.0)  # an arbitrary monotonic moment for the card's first frame
_PRESENTATION = "card"
QUAD, STACK, SINGLE = PanelProfile.QUAD_128X64, PanelProfile.STACK_64X64, PanelProfile.SINGLE_64X32
PROFILES = sorted(LiveBasketballRenderer.supported_profiles)


def _render(card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
    if card.kind is CardKind.PREGAME:
        BasketballPregameRenderer().render(card, context, canvas)
    else:
        LiveBasketballRenderer().render(card, context, canvas)


def _context(spec: Specimen, profile: PanelProfile, lanes: LaneFrame) -> RenderContext:
    return RenderContext(profile=profile, now=spec.now, logos=LOGOS, timezone=ZONE, marquee=Marquee(clock=lanes))


def draw_frames(spec: Specimen, profile: PanelProfile) -> list[tuple[str, Image.Image]]:
    """Each of a specimen's frames on ``profile``, named as its golden is, 1 LED = 1 px.

    For each moment, a fresh lane clock takes the turn's earlier cards and this one as they went up, then the frame
    is drawn at the moment.
    """
    width, height = geometry_for(profile).size
    frames = []
    for name, moment in spec.frames():
        clock = LaneClock()
        for card, at in (*spec.earlier, (spec.card, spec.up_at)):
            lanes = clock.frame(_PRESENTATION, _START + timedelta(seconds=at))
            _render(card, _context(spec, profile, lanes), RecordingCanvas(width, height))
            lanes.commit()
        canvas = PillowCanvas(width, height)
        drawn = clock.frame(_PRESENTATION, _START + timedelta(seconds=moment))
        _render(spec.card, _context(spec, profile, drawn), canvas)
        frames.append((name, canvas.image()))
    return frames


def golden_name(frame: str, profile: PanelProfile) -> str:
    return f"nba_{frame.replace('-', '_')}_{profile.to_json_value()}.png"


def _assert_matches_golden(image: Image.Image, name: str) -> None:
    path = GOLDEN_DIR / name
    if os.environ.get("OMNI_REGEN_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return
    assert path.exists(), f"missing golden {name}; regenerate with OMNI_REGEN_GOLDEN=1"
    expected = Image.open(path).convert("RGB")
    assert image.convert("RGB").tobytes() == expected.tobytes(), f"render differs from golden {name}"


_CASES = [
    pytest.param(spec, profile, id=f"{spec.key}-{profile.to_json_value()}")
    for spec in SPECIMENS
    for profile in PROFILES
    if profile in spec.profiles
]


@pytest.mark.parametrize("spec, profile", _CASES)
def test_golden_per_card_and_profile(spec: Specimen, profile: PanelProfile) -> None:
    for name, image in draw_frames(spec, profile):
        _assert_matches_golden(image, golden_name(name, profile))


# --- behavior ------------------------------------------------------------------------------------------------


def _lanes(up: float) -> LaneFrame:
    """A frame of a card that has been up ``up`` seconds."""
    clock = LaneClock()
    clock.frame(_PRESENTATION, _START).commit()
    return clock.frame(_PRESENTATION, _START + timedelta(seconds=up))


def _ops(payload: BasketballGameCardPayload, profile: PanelProfile, *, up: float = 1.0) -> RecordingCanvas:
    width, height = geometry_for(profile).size
    canvas = RecordingCanvas(width, height)
    context = RenderContext(profile=profile, now=T, timezone=ZONE, marquee=Marquee(clock=_lanes(up)))
    LiveBasketballRenderer().render(game_card(payload), context, canvas)
    return canvas


def _texts(payload: BasketballGameCardPayload, profile: PanelProfile, *, up: float = 1.0) -> list[str]:
    return [op.text for op in _ops(payload, profile, up=up).texts()]


_RECORDS = (WinLossRecord(wins=6, losses=8), WinLossRecord(wins=9, losses=5))
_Q3 = (side(58, 4, False, (26, 25, 7)), side(68, 5, True, (26, 33, 9)))
_HALF = (side(51, 5, False, (26, 25)), side(59, 4, True, (26, 33)))
_FINAL = (side(98, 2, True, (26, 25, 22, 25)), side(106, 1, True, (26, 33, 28, 19)))


def _live(**changes: Any) -> BasketballGameCardPayload:
    return live(*_Q3, 3, 3670, **{"home_win": 84.0, "records": _RECORDS, **changes})


def _halftime(**changes: Any) -> BasketballGameCardPayload:
    return live(
        *_HALF, 2, None, **{"home_win": 74.0, "records": _RECORDS, "phase": BasketballPhase.HALFTIME, **changes}
    )


def _final(**changes: Any) -> BasketballGameCardPayload:
    return live(*_FINAL, 4, None, **{"home_win": None, "records": _RECORDS, "phase": BasketballPhase.FINAL, **changes})


@pytest.mark.parametrize("profile", PROFILES)
def test_a_turns_first_half_names_the_teams_and_its_second_shows_what_they_have_left(profile: PanelProfile) -> None:
    timeouts = {"4×TO", "5×TO", "4TO", "5TO"}
    for up in (0.0, 5.9, 12.0, 17.5):
        texts = set(_texts(_live(), profile, up=up))
        assert {"POR", "MIN", "6-8", "9-5"} <= texts and not timeouts & texts
    for up in (6.0, 11.9, 18.0):
        texts = set(_texts(_live(), profile, up=up))
        assert {"6-8", "9-5"}.isdisjoint(texts) and len(timeouts & texts) == 2


def test_without_a_lane_clock_the_halves_run_off_the_render_clock() -> None:
    turn = datetime(2026, 11, 17, 2, 39, 36, tzinfo=timezone.utc)
    assert turn.timestamp() % 12 == 0  # a turn's start, as the render clock counts them
    assert turn_half(RenderContext(profile=QUAD, now=turn + timedelta(seconds=5.9))) is TurnHalf.FIRST
    assert turn_half(RenderContext(profile=QUAD, now=turn + timedelta(seconds=6))) is TurnHalf.SECOND


@pytest.mark.parametrize("profile", PROFILES)
def test_halftime_and_a_final_name_the_teams_all_turn(profile: PanelProfile) -> None:
    for payload in (_halftime(), _final()):
        assert {"POR", "MIN"} <= set(_texts(payload, profile, up=7.0))


@pytest.mark.parametrize("profile", PROFILES)
def test_a_side_whose_timeouts_the_feed_hasnt_given_reads_a_dash(profile: PanelProfile) -> None:
    unknown = live(side(58, None, False, (26, 25, 7)), _Q3[1], 3, 3670, home_win=None)
    assert any(text in ("-×TO", "-TO") for text in _texts(unknown, profile, up=7.0))


@pytest.mark.parametrize("profile", PROFILES)
def test_with_no_records_the_lane_names_the_team_alone(profile: PanelProfile) -> None:
    named = set(_texts(_live(), profile)) - set(_texts(_live(records=(None, None)), profile))
    assert named == {"6-8", "9-5"}


@pytest.mark.parametrize("profile", PROFILES)
def test_a_clock_the_feed_hasnt_given_reads_dashes(profile: PanelProfile) -> None:
    assert "--:--" in _texts(live(*_Q3, 3, None, home_win=None), profile)


@pytest.mark.parametrize("profile", PROFILES)
def test_no_market_reading_draws_no_meter_until_a_final_settles_it(profile: PanelProfile) -> None:
    assert len(_ops(_live(), profile).rects()) > len(_ops(_live(home_win=None), profile).rects())
    settled = _ops(_final(), profile).rects()
    assert settled == _ops(_final(home_win=37.0), profile).rects()  # a final's meter reads the result, not a market


@pytest.mark.parametrize("profile", PROFILES)
def test_a_final_dims_its_loser(profile: PanelProfile) -> None:
    colors = {op.text: op.color for op in _ops(_final(), profile).texts() if op.text in ("98", "106")}
    assert colors["98"] != colors["106"]


_NO_END_WORDS = {QUAD: ["HALF"], STACK: ["HALFTIME"], SINGLE: ["HALF", "TIME"]}


@pytest.mark.parametrize("profile", PROFILES)
def test_halftime_counts_down_to_its_end_when_its_known(profile: PanelProfile) -> None:
    assert any("12:35" in text for text in _texts(_halftime(break_ends_at=T + timedelta(seconds=754.6)), profile))
    assert any("0:00" in text for text in _texts(_halftime(break_ends_at=T - timedelta(seconds=30)), profile))
    plain = _texts(_halftime(), profile)
    assert all(word in plain for word in _NO_END_WORDS[profile]) and not any(":" in text for text in plain)


@pytest.mark.parametrize("profile", PROFILES)
def test_the_end_of_an_overtime_names_it(profile: PanelProfile) -> None:
    tied = (side(105, 2, False, (26, 25, 22, 25, 7)), side(105, 1, True, (26, 33, 28, 11, 7)))
    payload = live(*tied, 5, None, home_win=50.0, phase=BasketballPhase.END_OF_PERIOD)
    assert "OT" in " ".join(_texts(payload, profile))


def test_past_three_overtimes_the_quads_line_score_gives_up_its_earliest_periods() -> None:
    periods = ((26, 25, 22, 25, 7, 6, 5, 9), (26, 33, 28, 11, 7, 6, 5, 4))
    payload = live(side(125, 1, False, periods[0]), side(120, 0, True, periods[1]), 8, 400, home_win=80.0)
    texts = _texts(payload, QUAD)
    assert "Q1" not in texts and {"Q2", "OT1", "OT4"} <= set(texts)


_EVEN = player("J.Doe", 0, 4, 5, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)  # level, and no three tried


@pytest.mark.parametrize("profile", [profile for profile in PROFILES if profile is not SINGLE])
def test_a_player_line_reads_an_even_plus_minus_in_gray_and_its_field_goals_alone(profile: PanelProfile) -> None:
    panel = PlayerLinesPanel(rank=0, players=BySide(_EVEN, None))
    texts = _ops(_live(bottom=panel), profile).texts()
    assert any(op.text.strip() == "0" and op.color == SECONDARY for op in texts)
    assert any("8 PTS 4/5 FG" in op.text for op in texts)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_non_team_contest_is_refused(profile: PanelProfile) -> None:
    golf = GolfTournament(
        id=LeagueScopedId(League.PGA, SourceRef("x"), "t"),
        league=League.PGA,
        status=GameStatus.LIVE,
        scheduled_start=T,
        tournament_name="Open",
    )
    card = dataclasses.replace(game_card(_live()), contest=golf)
    with pytest.raises(TypeError):
        LiveBasketballRenderer().render(card, RenderContext(profile=profile, now=T), RecordingCanvas(128, 64))


# --- stack ---------------------------------------------------------------------------------------------------


def _bottom(payload: BasketballGameCardPayload, *, up: float = 1.0) -> list[str]:
    """The stack's two lines under the divider."""
    return [op.text for op in _ops(payload, STACK, up=up).texts() if op.y > 50]


def test_the_stacks_player_lines_show_one_team_a_half_and_nothing_for_a_team_with_no_one_there() -> None:
    panel = PlayerLinesPanel(rank=0, players=BySide(_EVEN, None))
    assert "8 PTS 4/5 FG" in _bottom(_live(bottom=panel))
    assert _bottom(_live(bottom=panel), up=7.0) == []


def test_a_run_takes_the_stacks_panel_from_the_line_score_but_never_from_a_caption() -> None:
    run = ScoringRun(team=HomeAway.HOME, scored=9, allowed=2, span_seconds=95)
    assert _bottom(_live(run=run)) == ["MIN 9-2", "LAST 1:35"]
    timeout = CaptionPanel(caption=TimeoutTaken(side=HomeAway.AWAY, left=3))
    assert _bottom(_live(run=run, bottom=timeout)) == ["TIMEOUT POR", "3 LEFT"]


def test_a_finals_best_player_line_takes_as_many_numbers_as_fit() -> None:
    quiet = player("J.Doe", 4, 4, 5, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1)  # a steal is too few to mention
    best = BestPlayers(players=BySide(None, quiet))
    named = dataclasses.replace(_final(), best=best)
    assert _bottom(named)[-1] == "8 PTS 1 REB"
    assert _bottom(named, up=7.0) == []  # the loser has no line
    assert _bottom(_final()) == []  # no best players named
