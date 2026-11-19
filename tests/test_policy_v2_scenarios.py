"""The board policy's production scenarios (`BOARD_POLICY.md`), end to end under `omni-board-policy/2`.

Each runs through `AppLoop.refresh` and `paint` with the real pipelines over synthetic timed feeds: the replay harness
(`omni.replay.harness`, MLB alone, a refresh and a paint at every instant anything can change) or the mixed replay
(`omni.replay.mixed`, both leagues on one plan), each reading its decisions as the policy wrote them
(`omni.replay.policy_lines`). Every time is in seconds since the replay's start; each league's TV delay is 45 s.

- **MLB bid reminders:** when new, then every 60 open seconds, three at most, never into a favorite's action.
- **An MLB inning break** the feed states (read through the StatsAPI parse): guests for §9's airtime; a repeated
  same-half marker extends nothing.
- **Ordered finals with a decisive predecessor:** the walk-off, then its final, then the later final; a predecessor
  that ran out unseen unblocks its final, which says so; a correction updates the final and is no new game. (Marked:
  a big play retired unseen can't go on to the results queue, its card's window ending with its lifetime.)
- **NFL's official timeout,** from the feed's own row: one 12 s guest, not a commercial lease.
- **A 49-minute wake:** the host's suspend-inclusive clock tells the board it slept; what it held from before is
  history.
- **Clock, scope, quota and partial faults:** a board booted 60 s fast; a raw id both leagues use, and a wall-clock
  backstep, in the v2 day; NFLMeta's Free month spent; an answer missing its score or hits.
- **Standing offers:** each taken once, no refusal written twice.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from omni.app.display import RecordingDisplaySink
from omni.app.quota import NflMetaPlan, ProviderAccount, QuotaLedger, account_limits
from omni.app.runner import NflSource, build_loop
from omni.core.enum import GameStatus, HomeAway, League, PanelProfile
from omni.core.time import DurationSeconds
from omni.domain.football import PlaySummary, PlayTone
from omni.events.baseball import BaseballGameEventType, LiveBaseballFeed
from omni.events.football import FootballEvent, FootballEventKind
from omni.policy.settings import PolicySettings
from omni.preferences.favorites import Favorites
from omni.replay.harness import ReplayProvider, replay_decisions
from omni.replay.mixed import (
    ClockStep,
    Correction,
    MixedRun,
    MixedStep,
    MixedTimeline,
    SlateFrame,
    Wake,
    mixed_decisions,
    run_mixed,
)
from omni.replay.timeline import GameFrame, Timeline
from omni.cards.base import ScoreboardCard
from omni.core.clock import FakeClock
from omni.renderers.canvas import Canvas
from omni.renderers.context import RenderContext
from omni.renderers.registry import RendererRegistry, default_registry
from omni.tower.board import FrameKind, SourceHealth
from omni.tower.decisions import BaseTurnNote
from tests import mlb_break_feeds as statsapi
from tests import test_planned_nfl as planned_nfl
from tests.mlb_slate import _BOTTOM, _TOP, T0, _at, _Game, _Line
from tests.test_replay_mixed import _Game as _MixedGame
from tests.test_replay_mixed import _Line as _MixedLine
from tests.test_replay_policy_v2 import DAY, SCORECARD, SLATE
from tests.test_replay_mixed import SHORT, SUN, ZONE, _Football, _kickoff, _mlb_id, _playing, _run, _short, _snap

V2 = PolicySettings()
LAG = DurationSeconds(45)
PROFILES = list(PanelProfile)
NO_FAVORITES = Favorites()


def _decided(
    timeline: Timeline,
    *,
    until: float,
    favorites: Favorites = NO_FAVORITES,
    profile: PanelProfile = PanelProfile.QUAD_128X64,
) -> list[tuple[float, str, str]]:
    """The replay's lines after its header, each as (seconds, kind, the rest)."""
    lines = replay_decisions(
        timeline, profile=profile, until=_at(int(until)), broadcast_lag=LAG, favorites=favorites, settings=V2
    )
    parsed = []
    for line in lines[1:]:
        seconds, kind, rest = line.split(maxsplit=2)
        parsed.append((float(seconds), kind, rest))
    return parsed


def _when(decided: list[tuple[float, str, str]], kind: str, *words: str) -> list[float]:
    return [at for at, said, rest in decided if said == kind and all(word in rest for word in words)]


# --- MLB bid reminders ------------------------------------------------------------------------------------------


def _bid(*, until: int = 1500) -> _Game:
    """SF hitless at SD: through 5 at the start, through 6 from 300 (a bid the board sees begin), broken at 1500."""
    game = _Game("sf-sd", away=137, home=135)
    game.live(0, _Line(6, _TOP, 0, 2, 0, 6))
    game.every_minute(60, 300)
    game.live(300, _Line(7, _TOP, 0, 2, 0, 6))
    game.every_minute(360, until)
    game.play(until, BaseballGameEventType.SINGLE, _Line(8, _TOP, 0, 2, 1, 6, 1), "Chapman singles.", rbi=0)
    return game


def _other(raw: str = "hou-tex", away: int = 117, home: int = 140, *, until: int = 1600) -> _Game:
    game = _Game(raw, away=away, home=home)
    game.live(0, _Line(3, _TOP, 0, 0, 1, 1))
    game.every_minute(60, until)
    return game


@pytest.mark.parametrize("profile", PROFILES)
def test_a_new_bid_is_reminded_when_new_then_after_each_60_open_seconds_three_times_at_most(
    profile: PanelProfile,
) -> None:
    """The bid clears the delay at 345. Its reminders air then, and 60 s after each one shows (at the first legal
    boundary: a base turn's minimum), and never a fourth, though the bid stands until 1545."""
    timeline = Timeline(frames=tuple(_bid().frames + _other().frames))
    decided = _decided(timeline, until=1600, profile=profile)
    shown = _when(decided, "promote", "sf-sd:nohitter", "(reminder)")
    assert shown == [345.0, 409.0, 469.0]
    assert [later - earlier for earlier, later in zip(shown, shown[1:])] == [64.0, 60.0]  # 60 s, then the boundary


def test_a_bid_is_never_reminded_into_a_favorite_s_action_and_waits_for_open_time() -> None:
    """The Astros are a favorite in action until their game ends at 900: no reminder until then, and the open
    time that counts begins there."""
    astros = _other(until=900)
    astros.final(900, _Line(9, _BOTTOM, 0, 1, 4, 6, 3), decisions=None)  # type: ignore[arg-type]
    timeline = Timeline(frames=tuple(_bid().frames + astros.frames + _other("chc-stl", 112, 138).frames))
    decided = _decided(timeline, until=1600, favorites=Favorites({League.MLB: ["HOU"]}))
    shown = _when(decided, "promote", "sf-sd:nohitter", "(reminder)")
    assert len(shown) == 3 and shown[0] >= 945.0  # the Astros' final clears the delay at 945
    assert shown[1] - shown[0] >= 60.0 and shown[2] - shown[1] >= 60.0


# --- an MLB inning break the feed states ------------------------------------------------------------------------

_BREAK_AT = 120  # the top of the 5th ends: its break clears the delay at 165, a turn's boundary


def _rockies(stated: int) -> list[GameFrame]:
    """COL@LAD (the Rockies a favorite), read through StatsAPI's parse: in action, then the middle of the 5th, whose
    schedule states its break as ``stated`` seconds, read twice, then the bottom's first pitch at 320."""

    def read(raw: dict[str, Any]) -> GameFrame:
        state = statsapi.read(raw, inning_break_length=stated).state
        return GameFrame(at=at, game=statsapi.game(), state=state, dated=True)

    at = T0
    first = read(statsapi.answer(plays=(statsapi.play(38, complete=False, events=(statsapi.pitch(at=T0),)),)))
    at = T0 + timedelta(seconds=_BREAK_AT)
    third_out = statsapi.play(38, inning=5, half="top", ended=at, events=(statsapi.pitch(),))
    middle = read(statsapi.answer(inning=5, inning_state="Middle", plays=(third_out,)))
    at = T0 + timedelta(seconds=_BREAK_AT + 20)
    again = read(statsapi.answer(inning=5, inning_state="Middle", plays=(third_out,)))  # the same half's marker
    at = T0 + timedelta(seconds=320)
    pitch = statsapi.pitch(at=at)
    bottom = statsapi.play(39, inning=5, half="bottom", complete=False, events=(pitch,))
    resumed = read(statsapi.answer(inning=5, inning_state="Bottom", plays=(third_out, bottom)))
    return [first, middle, again, resumed]


@pytest.mark.parametrize(("stated", "airtime"), [(120, 96), (145, 120), (175, 144)])
@pytest.mark.parametrize("profile", PROFILES)
def test_an_inning_break_the_feed_states_lends_its_guests_the_airtime_section_9_gives_and_a_re_read_extends_nothing(
    stated: int, airtime: int, profile: PanelProfile
) -> None:
    """At zero detection age, a 120/145/175 s break lends 96/120/144 s: whole 12 s guests while what's left of its
    ceiling (the stated length less 15 s) fits a guest and the 6 s return margin; then the Rockies are back."""
    games = (_other(), _other("chc-stl", 112, 138))
    frames = tuple(_rockies(stated)) + tuple(frame for game in games for frame in game.frames)
    decided = _decided(Timeline(frames=frames), until=400, favorites=Favorites({League.MLB: ["COL"]}), profile=profile)
    released = _BREAK_AT + LAG.value
    opened = [rest for at, kind, rest in decided if kind == "lease" and rest.startswith("opened")]
    assert opened == [
        f"opened mlb:mlb_statsapi:700001 mlb inning break @ top 5 middle | {stated - 15:.1f}s left | any guests that fit"
    ]
    assert _when(decided, "lease", "opened") == [float(released)]  # never before the released state shows it
    guests = _when(decided, "guest", "admitted")
    assert guests == [float(released + 12 * turn) for turn in range(airtime // 12)]
    assert _when(decided, "guest", "refused", "(no_room)")[0] == released + airtime
    assert _when(decided, "base", "700001:live", "(return)") == [float(released + airtime)]  # the Rockies come back


# --- ordered finals with a decisive predecessor -----------------------------------------------------------------

M = timedelta(minutes=1)


def _finals(*, rockies: bool, rockies_end: timedelta = 10 * M) -> MixedTimeline:
    """A (MIN@CLE) ends first, on a walk-off homer at 12:04; B (BOS@NYY) ends at 12:04:30. With ``rockies``, COL@SF
    (the Rockies a favorite) is in action until its final, at ``rockies_end`` past 12:00 (12:10 unless said). A's
    final is corrected at 12:12."""
    a = _MixedGame("1", away=142, home=114, day=SUN, start=SHORT).live(SHORT, _MixedLine(9, _TOP, 1, 1, 6, 6))
    a.live(SHORT + 2 * M, _MixedLine(9, _BOTTOM, 1, 1, 6, 6, 1))
    a.home_run(SHORT + 4 * M, _MixedLine(9, _BOTTOM, 1, 2, 6, 7, 1))
    a.final(SHORT + 4 * M + 15 * S, _MixedLine(9, _BOTTOM, 1, 2, 6, 7, 1))
    b = _MixedGame("2", away=111, home=147, day=SUN, start=SHORT).live(SHORT, _MixedLine(9, _TOP, 3, 2, 8, 7))
    b.final(SHORT + 4 * M + 30 * S, _MixedLine(9, _TOP, 3, 2, 8, 7, 3))
    games = [a, b]
    if rockies:
        c = _MixedGame("3", away=115, home=137, day=SUN, start=SHORT).live(SHORT, _MixedLine(5, _TOP, 1, 1, 4, 4))
        c.live(SHORT + 5 * M, _MixedLine(6, _TOP, 1, 1, 5, 4))
        c.final(SHORT + rockies_end, _MixedLine(9, _BOTTOM, 2, 1, 8, 7, 3))
        games.append(c)
    correction = Correction(at=SHORT + 12 * M, game=_mlb_id("1"), away=1, home=3)
    return _short(*games, end=20 * M, faults=(correction,))


def _mixed(
    timeline: MixedTimeline, *, favorites: Favorites = NO_FAVORITES, profile: PanelProfile = PanelProfile.QUAD_128X64
) -> list[tuple[float, str, str]]:
    lines = mixed_decisions(timeline, profile=profile, favorites=favorites, broadcast_lag=LAG, settings=V2)
    return [_split(line) for line in lines[1:]]


def _split(line: str) -> tuple[float, str, str]:
    seconds, kind, rest = line.split(maxsplit=2)
    return float(seconds), kind, rest


@pytest.mark.parametrize("profile", PROFILES)
def test_finals_air_in_end_order_after_the_unseen_play_that_decided_the_first_and_a_correction_is_no_new_game(
    profile: PanelProfile,
) -> None:
    """B is on the board when A's walk-off clears the delay: the walk-off goes first (promoted), then A's final, then
    B's. A's later correction updates its final in place: it airs no second result, and A is no new game."""
    decided = _mixed(_finals(rockies=False), profile=profile)
    order = [
        rest.split()[0]
        for at, kind, rest in decided
        if kind in ("promote", "card") and "(result)" in rest or kind == "promote"
    ]
    assert order == ["mlb:mlb_statsapi:1:bigplay:1:ab:1", "mlb:mlb_statsapi:1:final", "mlb:mlb_statsapi:2:final"]
    [promoted] = _when(decided, "promote", "1:bigplay:1:ab:1")
    assert promoted in _when(decided, "down", "mlb:mlb_statsapi:2:live", "(next waiting")  # B was on the board
    [corrected] = _when(decided, "corrected", "mlb:mlb_statsapi:1:final")
    assert not [
        at
        for at, kind, rest in decided
        if at >= corrected and kind in ("final", "card") and "1:final  (result)" in rest
    ]
    assert [rest.split()[0] for at, kind, rest in decided if kind == "final"] == [
        "mlb:mlb_statsapi:1:final",
        "mlb:mlb_statsapi:2:final",
    ]


def test_a_final_whose_decisive_play_ran_out_unseen_is_unblocked_and_says_so_and_the_end_order_holds() -> None:
    """The Rockies act: A's walk-off can't cut in, retires unseen and its card runs out, and A's final waits for a
    break. When the Rockies' game ends, more than five minutes after A's final cleared the delay, A's final goes first,
    saying its deciding play is gone (its story is too old to lead it), then B's, then the Rockies'."""
    decided = _mixed(_finals(rockies=True), favorites=Favorites({League.MLB: ["COL"]}))
    assert _when(decided, "unseen", "1:bigplay:1:ab:1", "(too late to start)")
    results = [rest for at, kind, rest in decided if kind == "card" and "(result)" in rest]
    assert results == [
        "mlb:mlb_statsapi:1:final  (result) | why: Twins at Guardians: final; the play that decided it is gone",
        "mlb:mlb_statsapi:2:final  (result) | why: Red Sox at Yankees: final",
        "mlb:mlb_statsapi:3:final  (result) | why: Rockies: final",
    ]


@pytest.mark.parametrize("profile", PROFILES)
def test_a_big_play_whose_promotion_ran_out_still_airs_as_a_result(profile: PanelProfile) -> None:
    """§4: a candidate below the action bar stays break-only until its lifetime ends, then goes to the results queue.
    A's walk-off can't cut into the Rockies' action; its promotion and its card both run out unseen. The Rockies' game
    ends at 12:07, inside five minutes of A's final clearing the delay, so A's final, first in end order, airs first
    as the walk-off's still with the final score: one result, spending no promotion."""
    decided = _mixed(
        _finals(rockies=True, rockies_end=7 * M), favorites=Favorites({League.MLB: ["COL"]}), profile=profile
    )
    assert _when(decided, "unseen", "1:bigplay:1:ab:1", "(too late to start)")
    [summary] = [at for at, kind, rest in decided if kind == "up" and "1:bigplay:1:ab:1:result" in rest]
    results = [(at, rest) for at, kind, rest in decided if kind == "card" and "(result)" in rest]
    assert results[0] == (
        summary,
        "mlb:mlb_statsapi:1:final  (result) | why: Twins at Guardians: final, with the play that decided it",
    )
    assert [rest.split()[0] for _, rest in results] == [
        "mlb:mlb_statsapi:1:final",
        "mlb:mlb_statsapi:2:final",
        "mlb:mlb_statsapi:3:final",
    ]
    assert not [rest for at, kind, rest in decided if kind == "promote"]


# --- NFL's official timeout -------------------------------------------------------------------------------------

S = timedelta(seconds=1)


def _official_timeout() -> MixedTimeline:
    """NO@CAR (the Panthers a favorite) and DAL@PHI from 12:00, with MIN@CLE on MLB's side. At 12:05 the Panthers'
    feed writes an official timeout's row, read the moment it's written; play resumes at 12:08."""
    car = _Football("404", "NO", "CAR", SHORT).listed(SHORT).live(SHORT, _kickoff())
    car.live(SHORT + 120 * S, _snap(1, 700, 0, 0, HomeAway.AWAY, 2, 6, 50), _run(4))
    stopped = _snap(1, 600, 0, 3, HomeAway.HOME, 1, 10, 66)
    row = FootballEvent(
        key="404:official",
        kind=FootballEventKind.OFFICIAL_TIMEOUT,
        summary=PlaySummary(tone=PlayTone.STANDARD, headline="OFFICIAL TIMEOUT"),
        play=9,
        written_at=SHORT + 300 * S,
    )
    car.frames.append(SlateFrame(at=SHORT + 300 * S, game=car.team_game(GameStatus.LIVE), state=stopped, plays=(row,)))
    car.live(SHORT + 480 * S, _snap(1, 590, 0, 3, HomeAway.HOME, 2, 4, 60), _run(6))
    dal = _Football("401", "DAL", "PHI", SHORT).listed(SHORT).live(SHORT, _kickoff())
    dal.live(SHORT + 200 * S, _snap(1, 650, 7, 0, HomeAway.HOME, 1, 10, 70), _run(3))
    return MixedTimeline(
        zone=ZONE, start=SHORT, end=SHORT + 10 * M, mlb=tuple(_playing("1").frames), nfl=tuple(car.frames + dal.frames)
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_an_official_timeout_from_the_feed_s_own_row_lends_one_12_s_guest_not_a_commercial_break(
    profile: PanelProfile,
) -> None:
    """The row clears the delay at 345: a 30 s lease for one guest, never a TV timeout's 120 s. One game's card is its
    guest for 12 s; the next is refused, the turn spent, and the Panthers have the board back."""
    decided = _mixed(_official_timeout(), favorites=Favorites({League.NFL: ["CAR"]}), profile=profile)
    leases = [(at, rest) for at, kind, rest in decided if kind == "lease"]
    assert leases[:2] == [
        (345.0, "opened nfl:nflmeta:404 nfl official timeout @ play 9 | 30.0s left | 1 guest"),
        (375.0, "lapsed nfl:nflmeta:404 nfl official timeout @ play 9"),
    ]
    assert not any("tv timeout" in rest for _, rest in leases)
    [admitted] = _when(decided, "guest", "admitted", "in nfl:nflmeta:404")
    [refused] = _when(decided, "guest", "refused", "in nfl:nflmeta:404", "(turns_spent)")
    assert refused - admitted == 12.0
    assert _when(decided, "base", "nfl:nflmeta:404:live", "(return)") == [refused]


# --- a 49-minute wake -------------------------------------------------------------------------------------------

_ASLEEP = 350.0  # seconds on the running clock: the host sleeps at 12:05:50 for 49 minutes, and wakes at once
_FRESH = 405.0  # the first reading after the wake, at 360, clears the delay


def _sleepy() -> MixedTimeline:
    """NO@CAR (the Panthers a favorite) from 12:00 and MIN@CLE on MLB's side. The Panthers' feed writes an official
    timeout at 12:05, which clears the delay at 12:05:45 with its caption; the host sleeps at 12:05:50. Asleep, the
    feed writes 49 plays, a touchdown among them, and the board's first read after waking brings all of them."""
    car = _Football("404", "NO", "CAR", SHORT).listed(SHORT).live(SHORT, _kickoff())
    car.live(SHORT + 4 * M, _snap(1, 700, 0, 0, HomeAway.AWAY, 2, 6, 50), _run(4))
    row = FootballEvent(
        key="404:official",
        kind=FootballEventKind.OFFICIAL_TIMEOUT,
        summary=PlaySummary(tone=PlayTone.STANDARD, headline="OFFICIAL TIMEOUT"),
        play=99,
        written_at=SHORT + 5 * M,
    )
    stopped = _snap(1, 600, 0, 3, HomeAway.HOME, 1, 10, 66)
    car.frames.append(SlateFrame(at=SHORT + 5 * M, game=car.team_game(GameStatus.LIVE), state=stopped, plays=(row,)))
    touchdown = PlaySummary(tone=PlayTone.SCORE, headline="TOUCHDOWN", detail="A synthetic score.")
    for minute in range(6, 55):
        state = _snap(2 if minute > 30 else 1, 600, 7 if minute >= 30 else 0, 3, HomeAway.HOME, 1, 10, 60)
        car.live(SHORT + minute * M, state, touchdown if minute == 30 else _run(2))
    twins = _playing("1")
    for minute in range(10, 70, 10):
        twins.live(SHORT + minute * M, _MixedLine(8 if minute < 40 else 9, _TOP, 1, 0, 5 + minute // 10, 5))
    sleep = Wake(at=SHORT + 5 * M + 50 * S, slept=49 * M)
    return MixedTimeline(
        zone=ZONE, start=SHORT, end=SHORT + 60 * M, mlb=tuple(twins.frames), nfl=tuple(car.frames), faults=(sleep,)
    )


@dataclass(frozen=True)
class _Woken:
    run: MixedRun
    steps: list[MixedStep]

    def decided(self) -> list[tuple[float, str, str]]:
        return [_split(line) for line in self.run.decisions]

    def panthers(self, start: float, end: float) -> list[MixedStep]:
        """The steps that drew the Panthers' card, from ``start`` up to ``end``."""
        return [
            step
            for step in self.steps
            if start <= step.elapsed.total_seconds() < end
            and step.drawn is not None
            and step.drawn.contest.id.raw == "404"
        ]


@pytest.fixture(scope="module")
def woken() -> _Woken:
    steps: list[MixedStep] = []
    run = run_mixed(
        _sleepy(),
        profile=PanelProfile.QUAD_128X64,
        favorites=Favorites({League.NFL: ["CAR"]}),
        broadcast_lag=LAG,
        settings=V2,
        watch=steps.append,
    )
    return _Woken(run, steps)


def test_after_a_49_minute_wake_nothing_old_is_promoted_or_spoiled_and_the_second_league_carries_on(
    woken: _Woken,
) -> None:
    """The board notices the sleep (its suspend-inclusive clock ran 49 minutes while its monotonic clock stood
    still), and what it admits next waits the full delay. Of the 49 plays one read brings, none is promoted; nothing
    airs before its release; the fresh score first shows on the Panthers' first turn once the read after the wake
    clears the delay; and MLB keeps its turns."""
    decided = woken.decided()
    assert not [at for at, kind, _ in decided if kind == "promote"]
    assert woken.run.early == ()
    scores = [
        (step.elapsed.total_seconds(), step.drawn.payload.state.away_score)
        for step in woken.panthers(_ASLEEP, 600)
        if step.drawn is not None
    ]
    fresh = min(at for at, away in scores if away == 7)
    assert _FRESH <= fresh < _FRESH + 12  # the Twins' turn running at 405 ends first
    ups = [at for at in _when(decided, "up", "nfl:nflmeta:404:live") if at > _FRESH]
    assert ups[0] == round(fresh, 1)  # it's the card's own first turn after the release: nothing held it back
    assert [at for at, kind, rest in decided if kind == "base" and "mlb:mlb_statsapi:1:live" in rest and at > _ASLEEP]


def test_after_a_wake_the_field_is_drawn_stale_until_a_fresh_reading_clears_the_delay(woken: _Woken) -> None:
    drawn = woken.panthers(_ASLEEP, _FRESH)
    assert drawn and all(step.painted.stale for step in drawn)


def test_after_a_wake_no_caption_from_before_the_sleep_is_drawn(woken: _Woken) -> None:
    captions = [step.drawn.payload.last_play for step in woken.panthers(_ASLEEP, _FRESH) if step.drawn is not None]
    assert all(caption is None or caption.headline != "OFFICIAL TIMEOUT" for caption in captions)


def test_after_a_wake_a_lease_from_before_the_sleep_is_void(woken: _Woken) -> None:
    decided = woken.decided()
    assert not [
        rest for at, kind, rest in decided if _ASLEEP <= at < _FRESH and kind in ("lease", "guest") and "404" in rest
    ]


# --- clock, scope, quota and partial faults ---------------------------------------------------------------------


def _homer_day(*faults: ClockStep) -> MixedTimeline:
    """MIN@CLE from 12:00, its answers dated as StatsAPI's are: a home run at 12:03:20 and a final at 12:08."""
    game = _MixedGame("1", away=142, home=114, day=SUN, start=SHORT).live(SHORT, _MixedLine(6, _TOP, 0, 0, 3, 3))
    game.home_run(SHORT + 200 * S, _MixedLine(6, _TOP, 1, 0, 4, 3))
    game.final(SHORT + 8 * M, _MixedLine(9, _BOTTOM, 1, 0, 6, 5, 3))
    other = _playing("2")
    frames = tuple(dataclasses.replace(frame, dated=True) for frame in game.frames + other.frames)
    return MixedTimeline(zone=ZONE, start=SHORT, end=SHORT + 15 * M, mlb=frames, faults=faults)


def test_a_board_that_boots_60_s_fast_releases_nothing_earlier_and_decides_as_one_with_the_right_time() -> None:
    """A credible play's age is read on the source's own clock, and every hold is pinned on the monotonic clock, so a
    wall clock a minute fast changes no release: the same cards go up and down at the same moments, and none early."""
    kinds = ("up", "down", "drop", "base", "promote", "card", "wait", "unseen", "frame")

    def decided(*faults: ClockStep) -> tuple[list[tuple[float, str, str]], MixedRun]:
        run = run_mixed(_homer_day(*faults), profile=PanelProfile.QUAD_128X64, nfl=False, settings=V2)
        return [line for line in map(_split, run.decisions) if line[1] in kinds], run

    right, _ = decided()
    fast, run = decided(ClockStep(at=SHORT, by=60 * S))
    assert fast == right and run.early == ()
    assert _when(fast, "up", "1:bigplay:1:ab:1")  # the home run aired, at the same moment


def test_in_the_v2_day_a_raw_id_both_leagues_use_is_two_games_and_a_wall_clock_backstep_freezes_no_membership() -> None:
    """The mixed day's NFL and MLB games numbered 401 take their own turns and accounts; after the wall clock steps
    back ten minutes at 15:00, HOU@SEA still joins Sunday's slate and LAD@SD keeps its turns."""
    lines = [_split(line) for line in DAY.read_text().splitlines()[1:]]
    for shared in ("mlb:mlb_statsapi:401:live", "nfl:nflmeta:401:live"):
        assert _when(lines, "base", shared)
    backstep = 54600.0  # 15:00 on the running clock
    assert _when(lines, "scope", "mlb 2026-11-22", "+mlb:mlb_statsapi:776204")[0] > backstep
    assert [at for at in _when(lines, "base", "776203:live") if at > backstep]
    scores = SCORECARD.read_text()
    assert "| mlb:mlb_statsapi:401 |" in scores and "| nfl:nflmeta:401 |" in scores


def test_standing_offers_are_taken_once_each_and_no_refusal_is_written_twice() -> None:
    """The pipelines publish each offer at every refresh until its lifetime ends; the board takes each key and
    revision once, so no wait is written twice for one reason, and nothing is promoted twice."""
    for fixture in (SLATE, DAY):
        lines = [_split(line) for line in fixture.read_text().splitlines()[1:]]
        waits = [rest for _, kind, rest in lines if kind == "wait"]
        assert waits and len(waits) == len(set(waits))
        promoted = [rest.split()[0] for _, kind, rest in lines if kind == "promote"]
        assert len(promoted) == len(set(promoted))


def test_once_nflmeta_free_is_spent_nfl_degrades_with_the_reason_and_mlb_keeps_its_turns() -> None:
    """NFLMeta's month runs out at 15:00 with the Panthers and Steelers live: NFL's health goes stale naming the
    switch to Builder (then offline, ten minutes past its due read), its cards dim and come down, and MLB's game keeps
    its base turns, under omni-board-policy/2."""
    board = planned_nfl._board(
        planned_nfl._et(planned_nfl.SUNDAY, 14, 59), ledger=QuotaLedger(account_limits(NflMetaPlan.FREE))
    )
    planned_nfl._sunday(board, planned_nfl.CAR_ATL, planned_nfl.PIT_CIN)
    planned_nfl._start(board, 101, 102)
    start = board.clock.now()
    twins = _MixedGame("1", away=142, home=114, day=SUN, start=start)
    frame = GameFrame(at=start, game=twins.team_game(GameStatus.LIVE), state=_MixedLine(5, _TOP, 1, 0, 4, 3).state())
    timeline = Timeline(frames=(frame,))

    def fetch(game: Any, now: Any) -> LiveBaseballFeed:
        return LiveBaseballFeed(state=frame.state, status=GameStatus.LIVE) if frame.state is not None else _never()

    loop = build_loop(
        ReplayProvider(timeline),
        fetch,
        RecordingDisplaySink(PanelProfile.QUAD_128X64),
        nfl=NflSource(board.planned, board.planned, board.planned.held_back),
        nfl_delay=LAG,
        favorites=Favorites({League.NFL: ["CAR"]}),
        settings=V2,
    )
    seen: list[tuple[float, Any, Any]] = []  # (seconds, the refresh's leagues, the paint)

    def until(stop: Any, *, spend: int | None = None) -> None:
        if spend is not None:
            board.nflmeta.requests_left = spend
        while board.clock.now() < stop:
            loop.refresh(board.clock.read())
            board.poller.run_due()
            refresh = loop.refresh(board.clock.read())
            seen.append(((board.clock.now() - start).total_seconds(), refresh.leagues, loop.paint(board.clock.read())))
            board.clock.advance(5 * S)

    until(start + 2 * M)
    until(start + 2 * M + 30 * S, spend=10)  # NFLMeta says the month is nearly spent
    until(start + 14 * M)
    assert board.ledger.exhausted(ProviderAccount.NFLMETA, board.clock.read())
    spent = "quota: NFLMeta Free spent for 2026-11: switch to Builder"
    nfl = [
        (league.source, league.reason) for _, leagues, _ in seen for league in leagues if league.league is League.NFL
    ]
    assert (SourceHealth.STALE, f"no schedule for over 2m: {spent}") in nfl
    assert nfl[-1] == (SourceHealth.OFFLINE, f"no schedule for over 10m: {spent}")
    assert all(
        league.source is SourceHealth.FRESH
        for _, leagues, _ in seen
        for league in leagues
        if league.league is League.MLB
    )
    shown = [(at, painted.shown.raw, painted.stale) for at, _, painted in seen if painted.shown is not None]
    nfl_shown = [(at, stale) for at, card, stale in shown if card.startswith("nfl:")]
    assert any(stale for _, stale in nfl_shown)  # NFL's cards dim once their answers age
    late = start + 12 * M
    assert {card for at, card, _ in shown if at >= (late - start).total_seconds()} == {"mlb:mlb_statsapi:1:live"}
    turns = [decision for _, _, painted in seen for decision in painted.decisions if isinstance(decision, BaseTurnNote)]
    assert turns[-1].contest.league is League.MLB  # NFL's games have left: MLB's carries on, unwritten while alone


def _never() -> LiveBaseballFeed:  # pragma: no cover - every frame here carries its state
    raise AssertionError("no state")


class _Drawn(RendererRegistry):
    """Today's renderers, chosen as for drawing but not drawn; it keeps every card drawn."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.cards: list[ScoreboardCard[Any]] = []

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.renderer_for(card)
        self.cards.append(card)


@pytest.mark.parametrize("missing", ["runs", "hits"])
def test_an_answer_missing_its_score_or_hits_is_refused_never_read_as_zero(missing: str) -> None:
    """A StatsAPI answer whose linescore leaves out a side's runs or hits is refused at the parse: the board keeps
    the last whole answer, drawn stale and then taken down, never a zero score, zero hits or a no-hit bid from it,
    and the policy reads the game as not fresh."""
    whole = statsapi.answer(inning=8, inning_state="Top", plays=(statsapi.play(60, inning=8, complete=False),))
    partial = statsapi.answer(inning=8, inning_state="Top", plays=(statsapi.play(60, inning=8, complete=False),))
    for side in ("away", "home"):
        del partial["liveData"]["linescore"]["teams"][side][missing]
    answers = {"raw": whole}
    game = statsapi.game()
    state = statsapi.read(whole).state
    timeline = Timeline(frames=(GameFrame(at=statsapi.T, game=game, state=state),))

    def fetch(contest: Any, now: Any) -> LiveBaseballFeed:
        return statsapi.provider(answers["raw"]).fetch_live_feed(contest, now=now)

    registry = _Drawn()
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(ReplayProvider(timeline), fetch, sink, registry=registry, settings=V2)
    clock = FakeClock(statsapi.T)
    fresh: list[tuple[float, bool]] = []
    frames: list[FrameKind | None] = []
    for second in range(0, 400, 5):
        if second == 120:
            answers["raw"] = partial
        reading = clock.read()
        facts = loop.refresh(reading).policy.facts(game.id)
        if facts is not None:
            fresh.append((float(second), facts.fresh))
        frames.append(loop.paint(reading).frame)
        clock.advance(5 * S)
    lines = {(card.id.raw, card.payload.away_line.runs, card.payload.away_line.hits) for card in registry.cards}
    assert lines == {("mlb:mlb_statsapi:700001:live", 1, 3)}  # the whole answer's: never a zero, never a no-hit bid
    assert (115.0, True) in fresh and (125.0, False) in fresh
    assert frames[-1] is FrameKind.OFFLINE  # once the stale card came down: no feed for the game
