"""Tests for the app runner: build_loop wiring and the CLI arg parser."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omni.app.__main__ import _leagues_on, _parse_args
from omni.app.display import RecordingDisplaySink
from omni.app.pipeline import MeterReading
from omni.app.planned_mlb import PlannedMlb, mlb_accounts
from omni.app.poll_plan import PollSource
from omni.app.poller import Poller
from omni.app.quota import BalldontliePlan, NflMetaPlan, ProviderAccount, QuotaLedger, account_limits
from omni.app.runner import _no_baseball, board_plan, build_loop, build_planned_loop, paint_frame, planned_nfl_source
from omni.app.versions import PolicyName
from omni.cards.base import ScoreboardCard
from omni.core.clock import ClockReading, FakeClock, MonotonicTime
from omni.core.enum import GameStatus, League, PanelProfile
from omni.core.ids import LeagueScopedId, SourceRef
from omni.core.time import DurationSeconds
from omni.domain.baseball import BaseballBaseState, BaseballCount, BaseballGameState, InningPhase, WinProbability
from omni.domain.contest import TeamGame
from omni.events.baseball import LiveBaseballFeed
from omni.policy.settings import Mutes, PolicySettings
from omni.preferences.favorites import Favorites
from omni.providers.base import ProviderError, ProviderUpdate
from omni.providers.nflmeta import NFLMETA_SOURCE
from omni.providers.mlb_teams import MlbTeamRegistry
from omni.queue.scheduler import Pick
from omni.renderers.canvas import Canvas, RecordingCanvas
from omni.renderers.context import RenderContext
from omni.renderers.image import LogoStore
from omni.renderers.registry import RendererRegistry, default_registry
from omni.renderers.text_lane import TextLane
from omni.tower.lane import ChangeKind
from tests.test_planned_mlb import _FakeMlb
from tests.test_planned_nfl import SUNDAY, _et, _NflMeta, _row

_REG = MlbTeamRegistry.from_color_file()
SOURCE = SourceRef("mlb_statsapi", "https://statsapi.mlb.com")
T = datetime(2026, 6, 17, 23, 0, 0, tzinfo=timezone.utc)


def _at(moment: datetime) -> ClockReading:
    return ClockReading(moment, MonotonicTime((moment - T).total_seconds()))


def _game(raw: str = "g1") -> TeamGame:
    return TeamGame(
        id=LeagueScopedId(League.MLB, SOURCE, raw),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=T,
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )


class _Provider:
    source = SOURCE
    league = League.MLB

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=(_game(),))


def _fetch_feed(game: TeamGame, now: datetime) -> LiveBaseballFeed:
    return LiveBaseballFeed(
        state=BaseballGameState(
            away_score=1,
            home_score=2,
            inning=7,
            phase=InningPhase.BOTTOM,
            count=BaseballCount(balls=2, strikes=1, outs=1),
            bases=BaseballBaseState(first=True),
            away_hits=5,  # a normal mid-game state — hits present, so no no-hitter card pre-empts the live card
            home_hits=7,
        )
    )


def test_build_loop_produces_a_working_loop() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    result = loop.run_once(_at(T))
    assert result.shown is not None
    assert sink.committed == 1


def test_build_loop_wires_the_broadcast_delay() -> None:
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(30))
    assert loop.run_once(_at(T)).shown is None  # inside the delay — the held frame instead
    assert loop.run_once(_at(T + timedelta(seconds=30))).shown is not None
    assert sink.committed == 2


def test_build_loop_threads_logos_through_to_the_rendered_frame() -> None:
    # A LogoStore handed to build_loop must reach rendering: the quad live card blits team tiles.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), logos=LogoStore())
    loop.run_once(_at(T))
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert frame.images()  # tiles blitted — the store reached the renderer end to end


def test_build_loop_without_logos_falls_back_to_color_bars() -> None:
    # No store wired (a test/replay) → the renderer draws color bars, never a tile blit.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0))
    loop.run_once(_at(T))
    frame = sink.frames[-1]
    assert isinstance(frame, RecordingCanvas)
    assert not frame.images()  # color-bar fallback, no blits


def test_build_loop_threads_the_win_probability_fetcher() -> None:
    # A win-prob fetcher handed to build_loop must reach the pipeline's live path.
    sink = RecordingDisplaySink(PanelProfile.QUAD_128X64)
    calls: list[str] = []

    def fetch_wp(game: TeamGame, as_of: ClockReading) -> MeterReading | None:
        calls.append(game.id.raw)
        return MeterReading(WinProbability(home=70.0, away=30.0), as_of)

    loop = build_loop(_Provider(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), fetch_win_probability=fetch_wp)
    loop.run_once(_at(T))
    assert calls == ["g1"]  # the pipeline fetched win-prob for the live game


class _TwoGames:
    source = SOURCE
    league = League.MLB

    def refresh(self, now: datetime) -> ProviderUpdate:
        return ProviderUpdate(source=SOURCE, observed_at=now, contests=(_game("g1"), _game("g2")))


class _SlowSink(RecordingDisplaySink):
    """A display whose swaps take time: each commit lets the clock run on by the next of ``delays``."""

    def __init__(self, clock: FakeClock, *delays: float) -> None:
        super().__init__(PanelProfile.QUAD_128X64)
        self._clock = clock
        self._delays = list(delays)

    def commit(self, frame: Canvas) -> None:
        self._clock.advance(timedelta(seconds=self._delays.pop(0) if self._delays else 0.0))
        super().commit(frame)


_LONG = TextLane(x=0, y=0, width=8, font="4x6")


class _Scrolling(RendererRegistry):
    """Today's renderers, plus one line too long for its lane in every frame: when it went up, as each frame
    reads it."""

    def __init__(self) -> None:
        super().__init__()
        self._real = default_registry()
        self.elapsed: list[float | None] = []

    def render(self, card: ScoreboardCard[Any], context: RenderContext, canvas: Canvas) -> None:
        self._real.render(card, context, canvas)
        assert context.marquee.clock is not None
        self.elapsed.append(context.marquee.clock.elapsed(_LONG, "a line too long for its lane", overflows=True))


def test_a_slow_swap_moves_the_dwell_the_booking_and_the_marquee_to_when_the_frame_was_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(T)
    sink = _SlowSink(clock, 5.0)  # the first frame takes 5 s to reach the panel; the rest are instant
    scrolling = _Scrolling()
    loop = build_loop(_TwoGames(), _fetch_feed, sink, broadcast_lag=DurationSeconds(0), registry=scrolling)
    booked: list[ClockReading] = []
    book = loop._queue.book

    def spy(pick: Pick, at: ClockReading) -> bool:
        booked.append(at)
        return book(pick, at)

    monkeypatch.setattr(loop._queue, "book", spy)
    loop.refresh(clock.read())
    first = paint_frame(loop, clock)
    shown = T + timedelta(seconds=5)
    assert first.presented is not None and first.presented.at == shown and first.at == shown
    [up] = [change for change in first.changes if change.kind is ChangeKind.UP]
    assert up.at == ClockReading(shown, MonotonicTime(5.0))  # the dwell starts when the frame was shown
    assert booked == [up.at]  # and the turn is booked then
    assert loop.next_deadline(clock.read()) == MonotonicTime(13.0)  # its minimum, from delivery
    clock.advance(timedelta(seconds=0.1))
    assert paint_frame(loop, clock).shown == first.shown
    assert scrolling.elapsed == [0.0, pytest.approx(0.1)]  # the line started at delivery, not at drawing
    clock.advance(timedelta(seconds=11.8))  # 16.9 s: 11.9 s after delivery
    assert paint_frame(loop, clock).shown == first.shown
    clock.advance(timedelta(seconds=0.1))  # 12 s after delivery
    turned = paint_frame(loop, clock)
    assert turned.shown != first.shown
    [down] = [change for change in turned.changes if change.kind is ChangeKind.DOWN]
    assert down.aired == timedelta(seconds=12)


def test_a_paint_without_a_clock_credits_the_frame_when_it_was_drawn() -> None:
    clock = FakeClock(T)
    loop = build_loop(_TwoGames(), _fetch_feed, _SlowSink(clock, 5.0), broadcast_lag=DurationSeconds(0))
    loop.refresh(_at(T))
    painted = loop.paint(clock.read())
    assert painted.presented is not None and painted.presented.at == T  # a replay: no time passes in a paint


def test_parse_args_defaults() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--emulated"])
    assert ns.profile == "quad_128x64"
    assert ns.emulated is True
    assert ns.favorite == [] and ns.favorites == Favorites() and ns.delay == 45 and ns.slot == 12
    assert ns.nfl_delay == 0 and ns.nba_delay == 0  # as the NFL runner's --delay
    assert ns.balldontlie_plan == "free"  # the board never assumes a paid tier
    assert ns.timezone == "America/New_York"
    assert ns.meter is False and ns.no_odds is False  # the live meter is opt-in; pregame odds are on


def test_nfl_s_tv_delay_is_a_flag_of_its_own() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--delay", "45", "--nfl-delay", "30"])
    assert (ns.delay, ns.nfl_delay) == (45, 30)


def test_nba_s_tv_delay_and_balldontlie_s_tier_are_flags_of_their_own() -> None:
    ns = _parse_args(["--profile", "quad_128x64", "--nba-delay", "20", "--balldontlie-plan", "all-star"])
    assert (ns.nba_delay, ns.balldontlie_plan) == (20, "all-star")


def test_the_live_meter_is_asked_for_by_flag() -> None:
    assert _parse_args(["--profile", "quad_128x64", "--meter"]).meter is True


def test_parse_args_collects_favorites_and_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    ns = _parse_args(
        [
            "--profile",
            "single_64x32",
            "--favorite",
            "COL",
            "--favorite",
            "LAD",
            "--delay",
            "30",
            "--slot",
            "8",
            "--timezone",
            "America/Denver",
        ]
    )
    assert ns.favorite == ["COL", "LAD"]
    assert ns.favorites == Favorites({League.MLB: ("COL", "LAD")})  # the flag's older, bare form: MLB, in order
    assert capsys.readouterr().out.splitlines() == [
        "omni.app: --favorite COL read as mlb:COL (a team without a league is MLB's)",
        "omni.app: --favorite LAD read as mlb:LAD (a team without a league is MLB's)",
    ]
    assert ns.delay == 30 and ns.slot == 8
    assert _parse_args(["--profile", "quad_128x64", "--tick", "9"]).slot == 9  # the flag's old name still works
    assert ns.timezone == "America/Denver"


def test_favorites_on_the_command_line_are_ranked_per_league_in_the_order_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    flags = ["--favorite", "mlb:ATL", "--favorite", "nfl:CAR", "--favorite", "mlb:COL", "--favorite", "nba:CHA"]
    ns = _parse_args(["--profile", "quad_128x64", *flags])
    assert ns.favorites == Favorites({League.MLB: ("ATL", "COL"), League.NFL: ("CAR",), League.NBA: ("CHA",)})
    assert str(ns.favorites) == "mlb 1 ATL, 2 COL; nfl 1 CAR; nba 1 CHA"
    assert capsys.readouterr().out == ""  # nothing to say about a team given with its league


@pytest.mark.parametrize(
    ("entry", "message"),
    [("xfl:DAL", "unknown league 'xfl'"), ("nfl:XYZ", "no NFL team 'XYZ'"), ("ARI", "no MLB team 'ARI'")],
)
def test_an_unknown_favorite_stops_the_app_with_a_usage_error_naming_it(
    entry: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        _parse_args(["--profile", "quad_128x64", "--favorite", entry])
    assert exit_.value.code == 2
    assert f"error: --favorite {entry}: {message}" in capsys.readouterr().err


# --- which leagues run, and the board policy's settings ------------------------------------------------------------


def test_the_board_runs_mlb_alone_unless_nfl_is_asked_for() -> None:
    assert _parse_args(["--profile", "quad_128x64"]).leagues == {League.MLB}
    assert _parse_args(["--profile", "quad_128x64", "--leagues", "mlb,nfl"]).leagues == {League.MLB, League.NFL}
    assert _parse_args(["--profile", "quad_128x64", "--leagues", "mlb"]).leagues == {League.MLB}
    assert _parse_args(["--profile", "quad_128x64", "--leagues", " NFL , mlb"]).leagues == {League.MLB, League.NFL}
    three = _parse_args(["--profile", "quad_128x64", "--leagues", "mlb,nfl,nba"]).leagues
    assert three == {League.MLB, League.NFL, League.NBA}
    assert _parse_args(["--profile", "quad_128x64", "--leagues", "nba"]).leagues == {League.NBA}


@pytest.mark.parametrize(
    ("leagues", "message"),
    [
        ("mlb,nhl", "--leagues mlb,nhl: the board can't run nhl yet (only mlb, nfl and nba)"),
        ("xfl", "--leagues xfl: unknown league 'xfl'"),
        (",", "--leagues: name at least one league"),
    ],
)
def test_a_league_the_board_can_t_run_stops_the_app_with_a_usage_error_naming_it(
    leagues: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        _parse_args(["--profile", "quad_128x64", "--leagues", leagues])
    assert exit_.value.code == 2 and message in capsys.readouterr().err


def test_without_an_nflmeta_key_nfl_is_off_said_once_and_mlb_runs_alone() -> None:
    said: list[str] = []
    both = _parse_args(["--profile", "quad_128x64", "--leagues", "mlb,nfl"])
    assert _leagues_on(both, {League.NFL: None}, said.append) == {League.MLB}
    assert said == ["omni.app: NFL is off: set NFLMETA_API_KEY in the environment or .env to put NFL on the board"]
    said.clear()
    assert _leagues_on(both, {League.NFL: "a-key"}, said.append) == {League.MLB, League.NFL} and said == []
    assert _leagues_on(_parse_args(["--profile", "quad_128x64", "--leagues", "mlb"]), {}, said.append) == {League.MLB}
    assert said == []  # NFL wasn't asked for, so nothing's missing
    with pytest.raises(SystemExit, match="it's the only league asked for"):
        _leagues_on(_parse_args(["--profile", "quad_128x64", "--leagues", "nfl"]), {}, said.append)


def test_each_keyed_league_without_its_key_is_off_and_said_so_once_each() -> None:
    said: list[str] = []
    three = argparse.Namespace(leagues=frozenset({League.MLB, League.NFL, League.NBA}))
    assert _leagues_on(three, {League.NFL: "a-key", League.NBA: ""}, said.append) == {League.MLB, League.NFL}
    assert said == ["omni.app: NBA is off: set BALLDONTLIE_API_KEY in the environment or .env to put NBA on the board"]
    said.clear()
    keyed = argparse.Namespace(leagues=frozenset({League.NFL, League.NBA}))
    with pytest.raises(SystemExit, match="NBA is off: .*, and no league asked for is left"):
        _leagues_on(keyed, {}, said.append)
    assert said == ["omni.app: NFL is off: set NFLMETA_API_KEY in the environment or .env to put NFL on the board"]


def test_the_board_policy_s_settings_default_to_policy_one_with_promotions_and_nothing_muted_or_weighted() -> None:
    assert _parse_args(["--profile", "quad_128x64"]).settings == PolicySettings(policy=PolicyName.BOARD_V1)
    assert _parse_args(["--profile", "quad_128x64", "--board-policy", "2"]).settings == PolicySettings()


def test_the_board_policy_s_settings_come_from_their_flags() -> None:
    flags = ["--board-policy", "1", "--mute", "nba", "--mute", "nfl:dal", "--mute", "mlb:COL", "--slot", "10"]
    flags += ["--league-weight", "nfl=1.5", "--league-weight", "mlb=0.5", "--league-weight", "mlb=1", "--no-promotions"]
    settings = _parse_args(["--profile", "quad_128x64", *flags]).settings
    assert settings.policy is PolicyName.BOARD_V1 and not settings.promotions and settings.slot == timedelta(seconds=10)
    assert settings.mutes == Mutes.of({League.NBA}, {League.NFL: ["DAL"], League.MLB: ["COL"]})
    assert dict(settings.league_weights) == {League.NFL: 1.5}  # a weight of 1 is the default, and the last one stands


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--mute", "nfl:XYZ"], "--mute nfl:XYZ: no NFL team 'XYZ'"),
        (["--mute", "xfl"], "--mute xfl: unknown league 'xfl'"),
        (["--league-weight", "nfl=3"], "nfl's weight 3.0 is outside 0.5-2.0"),
        (["--league-weight", "nfl"], "--league-weight nfl: give a weight, e.g. nfl=1.5"),
        (["--league-weight", "nfl=lots"], "--league-weight nfl=lots: give a weight, e.g. nfl=1.5"),
        (["--slot", "5"], "a turn of 5s is outside 8-30s"),
        (["--board-policy", "3"], "--board-policy: invalid choice: '3'"),
    ],
)
def test_a_board_policy_setting_out_of_bounds_stops_the_app_with_a_usage_error_naming_it(
    flags: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        _parse_args(["--profile", "quad_128x64", *flags])
    assert exit_.value.code == 2 and message in capsys.readouterr().err


# --- both leagues on one plan ---------------------------------------------------------------------------------------


def test_the_app_s_board_asks_mlb_and_nfl_through_one_plan_each_ask_spending_its_own_account() -> None:
    clock = FakeClock(_et(SUNDAY, 13, 5))
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = board_plan(ledger=ledger, mlb=mlb_accounts(_FakeMlb.source), nfl=True)
    assert plan.accounts is not None
    assert plan.accounts.account(PollSource.SLATE, _FakeMlb.source) is ProviderAccount.MLB_STATSAPI
    assert plan.accounts.account(PollSource.LIVE_SCORES, NFLMETA_SOURCE) is ProviderAccount.NFLMETA
    statsapi = _FakeMlb(clock)
    baseball = TeamGame(
        id=LeagueScopedId(League.MLB, _FakeMlb.source, "g1"),
        league=League.MLB,
        status=GameStatus.LIVE,
        scheduled_start=_et(SUNDAY, 13),
        away=_REG.resolve(115),
        home=_REG.resolve(119),
    )
    statsapi.slates = {SUNDAY: (baseball,)}
    nflmeta = _NflMeta(clock)
    nflmeta.rows[101] = _row(101, "CAR", "ATL", _et(SUNDAY, 13), phase="in")
    nflmeta.rows[102] = _row(102, "PIT", "CIN", _et(SUNDAY, 13), phase="in")
    nfl = planned_nfl_source(plan, clock, nflmeta, ledger=ledger, favorites=Favorites({League.NFL: ["CAR"]}))
    planned = PlannedMlb(plan, clock, statsapi, favorites=Favorites({League.MLB: ["ATL"]}))
    loop = build_planned_loop(
        planned, RecordingDisplaySink(PanelProfile.QUAD_128X64), nfl=nfl, broadcast_lag=DurationSeconds(0)
    )
    poller = Poller(plan, clock)
    for _ in range(4):
        loop.refresh(clock.read())
        poller.run_due()
        clock.advance(timedelta(seconds=5))
    refresh = loop.refresh(clock.read())
    assert set(refresh.policy.games) == {
        baseball.id,
        LeagueScopedId(League.NFL, NFLMETA_SOURCE, "101"),
        LeagueScopedId(League.NFL, NFLMETA_SOURCE, "102"),
    }
    assert [league.league for league in refresh.leagues] == [League.MLB, League.NFL]
    assert ledger.usage(ProviderAccount.NFLMETA, clock.read()).requests == len(nflmeta.requests) > 0
    assert statsapi.asked  # MLB's reads went out on the same plan


def test_a_board_without_mlb_runs_nfl_alone_and_never_reads_a_baseball_feed() -> None:
    clock = FakeClock(_et(SUNDAY, 13, 5))
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = board_plan(ledger=ledger, mlb=None, nfl=True)
    nflmeta = _NflMeta(clock)
    nflmeta.rows[102] = _row(102, "PIT", "CIN", _et(SUNDAY, 13), phase="in")
    nfl = planned_nfl_source(plan, clock, nflmeta, ledger=ledger)
    loop = build_planned_loop(None, RecordingDisplaySink(PanelProfile.QUAD_128X64), nfl=nfl)
    poller = Poller(plan, clock)
    for _ in range(3):
        loop.refresh(clock.read())
        poller.run_due()
        clock.advance(timedelta(seconds=5))
    refresh = loop.refresh(clock.read())
    assert set(refresh.policy.games) == {LeagueScopedId(League.NFL, NFLMETA_SOURCE, "102")}
    assert [league.league for league in refresh.leagues] == [League.NFL]
    with pytest.raises(ProviderError, match="MLB isn't on this board"):
        _no_baseball(_game(), T)  # what its baseball pipeline would read, if a baseball game ever reached it
    with pytest.raises(ValueError, match="at least one league"):
        board_plan(ledger=ledger, mlb=None, nfl=False)


def test_on_one_plan_nfl_s_quotes_go_every_5_s_and_nba_s_every_15_s_each_on_the_exchange_s_account() -> None:
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = board_plan(ledger=ledger, mlb=None, nfl=True, nba=BalldontliePlan.FREE)
    assert plan.policy(PollSource.PLAY_MARKET).every == timedelta(seconds=5)
    assert plan.policy(PollSource.PUBLIC_MARKET).every == timedelta(seconds=15)
    assert plan.accounts is not None
    assert plan.accounts.account(PollSource.PLAY_MARKET, NFLMETA_SOURCE) is ProviderAccount.POLYMARKET_US
    assert plan.accounts.account(PollSource.PUBLIC_MARKET, NFLMETA_SOURCE) is ProviderAccount.POLYMARKET_US


def test_with_a_market_the_board_s_nfl_cards_carry_the_meter_read_through_the_plan_on_the_exchange_s_account() -> None:
    clock = FakeClock(_et(SUNDAY, 13, 5))
    ledger = QuotaLedger(account_limits(NflMetaPlan.BUILDER))
    plan = board_plan(ledger=ledger, mlb=mlb_accounts(_FakeMlb.source), nfl=True)
    assert plan.accounts is not None
    assert plan.accounts.account(PollSource.PLAY_MARKET, NFLMETA_SOURCE) is ProviderAccount.POLYMARKET_US
    nflmeta = _NflMeta(clock)
    nflmeta.rows[102] = _row(102, "PIT", "CIN", _et(SUNDAY, 13), phase="in")
    asked: list[datetime] = []

    def market(game: TeamGame, now: datetime) -> WinProbability | None:
        asked.append(now)
        return WinProbability(home=64.0, away=36.0)

    nfl = planned_nfl_source(plan, clock, nflmeta, ledger=ledger, market=market)
    loop = build_planned_loop(None, RecordingDisplaySink(PanelProfile.QUAD_128X64), nfl=nfl)
    poller = Poller(plan, clock)
    key = f"{LeagueScopedId(League.NFL, NFLMETA_SOURCE, '102')}:live"
    for _ in range(3):
        loop.refresh(clock.read())
        poller.run_due()
        clock.advance(timedelta(seconds=5))
    card = loop._queue.card(key)
    assert card is not None and card.payload.win_probability is None  # the first reading is too new for the score
    assert asked  # the card asked for it
    clock.advance(timedelta(seconds=45))  # the others' next read
    for _ in range(2):
        loop.refresh(clock.read())
        poller.run_due()
    card = loop._queue.card(key)
    assert card is not None and card.payload.win_probability == WinProbability(home=64.0, away=36.0)


def test_the_board_s_receiver_is_driven_by_a_sink_command_and_the_emulator_stays_the_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = "ssh omni-board sudo python3 omni/board_frame_sink.py"
    ns = _parse_args(["--profile", "quad_128x64", "--sink-command", command])
    assert ns.sink_command == command and ns.emulated is False
    assert _parse_args(["--profile", "quad_128x64"]).sink_command is None  # the emulator, by default
    with pytest.raises(SystemExit):
        _parse_args(["--profile", "quad_128x64", "--emulated", "--sink-command", command])
    assert "not allowed with argument" in capsys.readouterr().err
