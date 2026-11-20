"""``python -m omni.app`` — run the live scoreboard on the RGBMatrixEmulator, or on a board.

    python -m omni.app --emulated --profile quad_128x64 --favorite mlb:ATL --favorite mlb:COL --favorite nfl:CAR
    python -m omni.app --profile quad_128x64 --sink-command "ssh omni-board sudo python3 omni/board_frame_sink.py"

Polls the MLB StatsAPI and NFLMeta, runs each league's delay-safe pipeline, and rotates both leagues' cards on
the emulator (browser at http://localhost:8888/), the default. With ``--sink-command`` it drives a board's
receiver instead (`tools/board_frame_sink.py`, started by the command, restarted if it drops): frames go to it off
the frame loop with a heartbeat at least every 2 s, and a card's time on the board counts only from the receiver's
word that its first frame is on the panel (`omni.app.frame_stream`). This is the thin I/O shell around the
deterministic `AppLoop`; everything testable lives in `runner.py` / `loop.py`.

``--leagues`` names the leagues that run, ``mlb`` unless you say otherwise: NFL and NBA on the shared board are opt-in
(``--leagues mlb,nfl,nba``) until their live paths have passed a staffed dogfood, and the NFL runner
(`omni.app.football`) stays NFL's fallback. NFL reads NFLMeta with ``NFLMETA_API_KEY``, NBA BALLDONTLIE with
``BALLDONTLIE_API_KEY``, each from the environment or the git-ignored ``.env``; without its key, the app says once that
the league is off and why, and runs the others. NFL's games are held back by ``--nfl-delay``, NBA's by
``--nba-delay``, MLB's by ``--delay``. Every league asks through one poll plan, each ask spending its own provider's
account. ``--balldontlie-plan`` names the BALLDONTLIE tier the key is on, free unless you say otherwise: Free reads
the day's games alone, so NBA's cards have no captions or stat panels; ALL-STAR adds the players' lines.

The board policy's settings (`omni.policy.settings.PolicySettings`): ``--board-policy`` picks the policy
(``1``, `omni-board-policy/1`, unless you say ``2``: policy 2 is opt-in until its live path has passed a staffed
dogfood); ``--mute LEAGUE`` or ``--mute LEAGUE:TEAM`` keeps a league or a
team off the board (repeatable; a team is checked as a favorite is); ``--league-weight LEAGUE=W`` weighs a league's
share, from 0.5 to 2 (repeatable); ``--no-promotions`` keeps big moments elsewhere from cutting in; and ``--slot`` is
the turn, from 8 to 30 seconds. A setting out of bounds stops the app with a usage error that names it.

Each card stays up for its turn (``--slot``, held within the card's own minimum and maximum)
unless a bigger moment takes over, and frames draw ``--fps`` times a second, so scrolling lines
and countdowns move. Each change on the board is traced. The network is read by a poller on its
own threads, on the cadences in `omni.app.planned_mlb.MLB_POLICIES` and `omni.app.planned_nfl.NFL_POLICIES`,
pregame details and odds included; a refresh reads its latest answers whenever one lands, and never waits on a
request.

Each ``--favorite`` is a league and a team, and their order is the rank within each league: above, ATL
is MLB's first and COL its second. A bare team (``--favorite COL``) is read as MLB's, the only league
the flag once meant, and the app says so as it starts. An unknown league or team stops it with a usage
error that names the entry.

The trace opens with what the run decides with (`omni.app.versions`): the board policy, a digest of the
settings that change its decisions (spelled out on the line under it), the rule table, and the commit.

The live win meter is off unless ``--meter`` is given. With it, and with ``OMNI_CLOUD_URL`` and
``OMNI_CLOUD_TOKEN`` in the environment or a git-ignored ``.env``, the meter reads Omni's market
service (``cloud/``); without them it reads Polymarket's public quotes. Either way MLB's model takes
over when the market has nothing.

Every request is counted against its provider account's quota (`omni.app.quota`), and the month's counts
are kept under ``--state-dir`` so a restart doesn't forget them. ``--nflmeta-plan`` names the plan of the
NFLMeta key, free unless you say otherwise.
"""

from __future__ import annotations

import argparse
import os
import shlex
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

from omni.core.clock import SystemClock
from omni.app.display import MatrixDevice, MatrixDisplaySink
from omni.app.frame_stream import StreamDisplay, StreamDisplaySink, stream_display
from omni.app.planned_mlb import MarketReader
from omni.app.quota import BalldontliePlan, Meter, NflMetaPlan, ProviderAccount
from omni.app.runner import build_planned_loop, run_forever
from omni.app.versions import DecisionConfig, PolicyName, SourceRevision, Versions, read_source_revision
from omni.core.enum import League, PanelProfile
from omni.core.env import read_env_file
from omni.core.time import DurationSeconds
from omni.domain.rules import RULES
from omni.panels.geometry import geometry_for
from omni.policy.settings import Mutes, PolicySettings
from omni.preferences.favorites import AppliedMigration, Favorites, parse_favorites
from omni.renderers.image import LogoStore
from omni.renderers.text_lane import DEFAULT_MARQUEE, ScrollSpeed
from omni.tower.board import BoardPolicy
from omni.tower.interruption import InterruptionPolicy

if TYPE_CHECKING:
    from omni.app.planned_mlb import OddsReader
    from omni.app.poll_plan import AccountMap
    from omni.app.quota import QuotaLedger
    from omni.providers.market_service import ServiceTransport
    from omni.providers.mlb_statsapi import MlbStatsApiProvider

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOARD_LEAGUES = (League.MLB, League.NFL, League.NBA)  # the leagues the shared board can run
_POLICIES = {"1": PolicyName.BOARD_V1, "2": PolicyName.BOARD_V2}
# Each league that reads a keyed source, and the key's name in the environment or .env.
_KEYS = {League.NFL: "NFLMETA_API_KEY", League.NBA: "BALLDONTLIE_API_KEY"}


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="omni.app", description="Run the Omni scoreboard loop.")
    parser.add_argument("--profile", required=True, choices=[profile.value for profile in PanelProfile])
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--emulated", action="store_true", help="render on the RGBMatrixEmulator (the default)")
    display.add_argument(
        "--sink-command",
        metavar="COMMAND",
        help="drive a board's receiver: the command whose stdin takes the frames, e.g. \"ssh omni-board sudo"
        ' python3 omni/board_frame_sink.py" (see tools/board_frame_sink.py)',
    )
    parser.add_argument(
        "--favorite",
        action="append",
        default=[],
        metavar="LEAGUE:ABBR",
        help="a favorite team, e.g. mlb:ATL or nfl:CAR (repeatable; the order is the rank, and a bare ABBR is MLB)",
    )
    parser.add_argument(
        "--leagues",
        default="mlb",
        metavar="LEAGUE,...",
        help="the leagues on the board, of mlb, nfl and nba (default mlb; NFL needs NFLMETA_API_KEY, NBA"
        " BALLDONTLIE_API_KEY)",
    )
    parser.add_argument("--delay", type=int, default=45, help="TV broadcast delay in seconds (default 45)")
    parser.add_argument(
        "--nfl-delay",
        type=int,
        default=0,
        help="TV broadcast delay for NFL games in seconds (default 0, as the NFL runner's --delay)",
    )
    parser.add_argument(
        "--nba-delay",
        type=int,
        default=0,
        help="TV broadcast delay for NBA games in seconds (default 0, as NFL's)",
    )
    parser.add_argument(
        "--slot",
        "--tick",
        dest="slot",
        type=int,
        default=12,
        help="seconds a card in rotation stays up when another is waiting, within its own minimum and maximum"
        " (8 to 30, default 12)",
    )
    parser.add_argument(
        "--board-policy",
        choices=sorted(_POLICIES),
        default="1",
        help="the board policy that shares the airtime: 1, or 2 (leverage, ranked favorites, promotions; opt-in)"
        " (default 1)",
    )
    parser.add_argument(
        "--mute",
        action="append",
        default=[],
        metavar="LEAGUE[:ABBR]",
        help="a league, e.g. nba, or a team, e.g. nfl:DAL, never to show (repeatable)",
    )
    parser.add_argument(
        "--league-weight",
        action="append",
        default=[],
        metavar="LEAGUE=W",
        help="a league's weight in the board's share, 0.5 to 2, e.g. nfl=1.5 (repeatable; the others weigh 1)",
    )
    parser.add_argument(
        "--no-promotions", action="store_true", help="keep big moments elsewhere from cutting in (rotation only)"
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        metavar="IANA",
        help="IANA zone for the schedule day and board clocks, e.g. America/Denver (default America/New_York)",
    )
    parser.add_argument("--no-odds", action="store_true", help="leave sportsbook odds off the pregame card")
    parser.add_argument(
        "--meter",
        action="store_true",
        help="show the live win meter (the market, then MLB's model); off by default",
    )
    parser.add_argument(
        "--scroll-speed",
        choices=["slow", "medium", "fast"],
        default="slow",
        help="how fast scrolling lines move at their peak: slow, medium (1.25x) or fast (1.5x)",
    )
    parser.add_argument("--fps", type=float, default=10.0, help="frames drawn a second (marquees move this fast)")
    parser.add_argument(
        "--nflmeta-plan",
        choices=[plan.value for plan in NflMetaPlan],
        default=NflMetaPlan.FREE.value,
        help="the NFLMeta plan the key is on, whose limits the board keeps to (default free)",
    )
    parser.add_argument(
        "--balldontlie-plan",
        choices=[plan.value for plan in BalldontliePlan],
        default=BalldontliePlan.FREE.value,
        help="the BALLDONTLIE tier the key is on (default free: the day's games alone; all-star adds the players'"
        " lines, so the captions and the stat panels)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="where the month's quota usage is kept (default $XDG_STATE_HOME/omni, else ~/.local/state/omni)",
    )
    args = parser.parse_args(argv)
    migrations: list[AppliedMigration] = []

    def migrated(applied: AppliedMigration) -> None:
        migrations.append(applied)
        print(f"omni.app: {applied}")

    try:
        args.favorites = parse_favorites(args.favorite, note=migrated)
        args.leagues = _parse_leagues(args.leagues)
        args.settings = PolicySettings(
            policy=_POLICIES[args.board_policy],
            mutes=_parse_mutes(args.mute),
            league_weights=MappingProxyType(_parse_weights(args.league_weight)),
            slot=timedelta(seconds=args.slot),
            promotions=not args.no_promotions,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.migrations = tuple(migrations)
    return args


def _league(name: str, entry: str, flag: str) -> League:
    """The league ``name`` spells; `ValueError` naming the entry when it isn't one."""
    try:
        return League(name.strip().lower())
    except ValueError:
        known = ", ".join(league.value for league in League)
        raise ValueError(f"{flag} {entry}: unknown league {name!r} (one of {known})") from None


def _parse_leagues(entry: str) -> frozenset[League]:
    """The leagues ``--leagues`` names, each one the shared board can run."""
    leagues = frozenset(_league(name, entry, "--leagues") for name in entry.split(",") if name.strip())
    if not leagues:
        raise ValueError("--leagues: name at least one league")
    beyond = sorted(league.value for league in leagues - set(_BOARD_LEAGUES))
    if beyond:
        raise ValueError(f"--leagues {entry}: the board can't run {', '.join(beyond)} yet (only mlb, nfl and nba)")
    return leagues


def _parse_mutes(entries: Iterable[str]) -> Mutes:
    """The leagues and teams the ``--mute`` entries keep off the board; each team resolved as a favorite is."""
    leagues: set[League] = set()
    teams: dict[League, list[str]] = {}
    for entry in entries:
        name, colon, team = entry.partition(":")
        league = _league(name, entry, "--mute")
        if not colon:
            leagues.add(league)
            continue
        try:
            resolved = Favorites({league: [team]}).teams(league)
        except ValueError as exc:
            raise ValueError(f"--mute {entry}: {exc}") from None
        teams.setdefault(league, []).extend(resolved)
    return Mutes.of(leagues, teams)


def _parse_weights(entries: Iterable[str]) -> dict[League, float]:
    """Each league's weight the ``--league-weight`` entries set, the last for a league standing. A weight of 1 is
    the default, so it isn't kept."""
    weights: dict[League, float] = {}
    for entry in entries:
        name, equals, raw = entry.partition("=")
        league = _league(name, entry, "--league-weight")
        try:
            weight = float(raw) if equals else None
        except ValueError:
            weight = None
        if weight is None:
            raise ValueError(f"--league-weight {entry}: give a weight, e.g. {league.value}=1.5")
        weights[league] = weight
    return {league: weight for league, weight in weights.items() if weight != 1.0}


def _leagues_on(
    args: argparse.Namespace, keys: Mapping[League, str | None], say: Callable[[str], None]
) -> frozenset[League]:
    """The leagues this run puts on the board: those asked for, less each keyed league whose key ``keys`` lacks,
    which is said once. With nothing left to run, the app stops, saying why."""
    asked: frozenset[League] = args.leagues
    leagues = asked
    for league, name in _KEYS.items():
        if league not in asked or keys.get(league):
            continue
        leagues = leagues - {league}
        code = league.value.upper()
        off = f"omni.app: {code} is off: set {name} in the environment or .env to put {code} on the board"
        if not leagues:
            left = "it's the only league asked for" if len(asked) == 1 else "no league asked for is left"
            raise SystemExit(f"{off}, and {left}")
        say(off)
    return leagues


def _keys(leagues: Collection[League]) -> dict[League, str | None]:  # pragma: no cover - reads the env and .env
    """Each keyed league's key, for the leagues asked for, from the environment or the git-ignored ``.env``. The
    keys are only handed on, never printed."""
    env = {**read_env_file(_REPO_ROOT / ".env"), **os.environ}
    return {league: env.get(name) for league, name in _KEYS.items() if league in leagues}


def _versions(args: argparse.Namespace, source: SourceRevision) -> Versions:
    """What a run with these flags decides with. The display target and the frame rate aren't settings
    that change a decision, so they're left out."""
    board = _board_policy(args)
    settings: PolicySettings = args.settings
    return Versions(
        policy=settings.policy,
        config=DecisionConfig(
            profile=PanelProfile(args.profile),
            delay=timedelta(seconds=args.delay),
            favorites=args.favorites,
            turn=board.slot,
            takeover_cap=board.takeover_cap,
            timezone=ZoneInfo(args.timezone),
            meter=args.meter,
            odds=not args.no_odds,
            scroll_speed=ScrollSpeed(args.scroll_speed),
            policy=settings.policy,
            mutes=settings.mutes,
            league_weights=settings.league_weights,
            promotions=settings.promotions,
            nfl_delay=timedelta(seconds=args.nfl_delay),
            nba_delay=timedelta(seconds=args.nba_delay) if League.NBA in args.leagues else None,
            nba_plan=BalldontliePlan(args.balldontlie_plan) if League.NBA in args.leagues else None,
        ),
        rules=RULES,
        source=source,
        migrations=args.migrations,
    )


def _board_policy(args: argparse.Namespace) -> BoardPolicy:
    return BoardPolicy(slot=args.settings.slot)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - drives the live emulator + network
    args = _parse_args(argv)
    profile = PanelProfile(args.profile)
    keys = _keys(args.leagues)
    leagues = _leagues_on(args, keys, say=print)
    key = keys.get(League.NFL)

    # Lazy imports keep the network libraries and emulator out of import time and tests.
    from omni.app.football import NflMetaHttp
    from omni.app.planned_mlb import PlannedMlb
    from omni.app.poller import Poller
    from omni.app.quota_store import default_state_dir, open_ledger
    from omni.app.runner import NbaSource, board_plan, planned_nba_source, planned_nfl_source

    clock = SystemClock()
    state_dir = args.state_dir if args.state_dir is not None else default_state_dir(os.environ, Path.home())
    tier = BalldontliePlan(args.balldontlie_plan)
    ledger = open_ledger(
        state_dir,
        NflMetaPlan(args.nflmeta_plan),
        balldontlie=tier,
        log=lambda line: print(line, flush=True),
        who="omni.app",
    )
    zone = ZoneInfo(args.timezone)
    source = read_source_revision(_REPO_ROOT)

    mlb: _Mlb | None = _mlb(args, clock, ledger, zone, source) if League.MLB in leagues else None
    plan = board_plan(
        ledger=ledger,
        mlb=mlb.accounts if mlb is not None else None,
        nfl=League.NFL in leagues,
        nba=tier if League.NBA in leagues else None,
    )
    planned = None
    if mlb is not None:
        planned = PlannedMlb(
            plan,
            clock,
            mlb.provider,
            market=mlb.market,
            odds=mlb.odds,
            favorites=args.favorites,
            meter=Meter(ledger, ProviderAccount.MLB_STATSAPI, clock),
        )
    nfl = None
    if League.NFL in leagues:
        assert key is not None  # NFL runs only with a key
        nfl = planned_nfl_source(plan, clock, NflMetaHttp(key, None).get, ledger=ledger, favorites=args.favorites)
    nba = None
    if League.NBA in leagues:
        from omni.providers.balldontlie import BalldontlieHttp

        nba_key = keys.get(League.NBA)
        assert nba_key is not None  # NBA runs only with a key
        planned_nba = planned_nba_source(
            plan, clock, BalldontlieHttp(nba_key), ledger=ledger, tier=tier, log=lambda line: print(line, flush=True)
        )
        nba = NbaSource.of(planned_nba)

    board: StreamDisplay | None = None
    sink: MatrixDisplaySink | StreamDisplaySink
    if args.sink_command:
        board = stream_display(shlex.split(args.sink_command), profile, log=lambda line: print(line, flush=True))
        sink = board.sink
    else:
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions

        options = RGBMatrixOptions()
        width, height = geometry_for(profile).size
        options.cols, options.rows = width, height
        options.chain_length = 1
        options.parallel = 1
        matrix = RGBMatrix(options=options)

        # The emulator's RGBMatrix is the matrix API at runtime, but its SwapOnVSync is typed with
        # the emulator's own canvas type and extra args — broader than our minimal MatrixDevice
        # protocol. Assert the boundary match precisely instead of blanket-ignoring the whole call.
        sink = MatrixDisplaySink(cast(MatrixDevice, matrix), profile)
    loop = build_planned_loop(
        planned,
        sink,
        meter=args.meter,
        favorites=args.favorites,
        broadcast_lag=DurationSeconds(args.delay),
        nfl=nfl,
        nfl_delay=DurationSeconds(args.nfl_delay),
        nba=nba,
        nba_delay=DurationSeconds(args.nba_delay),
        logos=LogoStore(),  # resolve committed team tiles from assets/ (lazy, cached)
        display_timezone=zone,
        marquee=DEFAULT_MARQUEE.at(ScrollSpeed(args.scroll_speed)),
        policy=_board_policy(args),
        settings=args.settings,
        interruptions=InterruptionPolicy(),  # a sleep, a stall or a lost panel is noticed and taken in
    )
    on = ", ".join(league.value for league in League if league in leagues)
    target = f"the board's receiver ({args.sink_command})" if board is not None else "http://localhost:8888/"
    print(
        f"omni.app: {profile.value}, leagues {on}, delay {args.delay}s (nfl {args.nfl_delay}s, nba {args.nba_delay}s),"
        f" nba {tier.label}, favorites"
        f" {args.favorites}, meter {mlb.meter if mlb is not None else 'off'} — {target}"
    )
    if board is not None:
        board.start()
    try:
        run_forever(
            loop,
            clock,
            fps=args.fps,
            log=lambda line: print(line, flush=True),
            timezone=zone,
            poller=Poller(plan, clock),
            versions=_versions(args, source),
        )
    except KeyboardInterrupt:
        pass
    finally:
        if board is not None:
            board.stop()  # the receiver blanks the panel as its input ends
        ledger.save(clock.read(), force=True)  # the month's usage, for the next run
    return 0


@dataclass(frozen=True, slots=True)
class _Mlb:
    """MLB's sources for a run: StatsAPI, the sportsbook's odds and the meter's market, each counted, and the
    accounts they spend."""

    provider: MlbStatsApiProvider
    odds: OddsReader | None
    market: MarketReader | None
    meter: str  # which market the meter reads, for the startup line
    accounts: AccountMap


def _mlb(
    args: argparse.Namespace, clock: SystemClock, ledger: QuotaLedger, zone: ZoneInfo, source: SourceRevision
) -> _Mlb:  # pragma: no cover - drives the network
    """MLB's sources, from the flags: odds unless ``--no-odds``, the live meter's market with ``--meter``."""
    import statsapi  # noqa: F401  (ensures the dependency is present before we start)

    from omni.app.planned_mlb import mlb_accounts
    from omni.providers import bovada_odds
    from omni.providers.bovada_odds import BovadaOddsProvider
    from omni.providers.market_service import (
        HeartbeatSender,
        MarketServiceClient,
        MarketServiceConfig,
        UrllibTransport,
    )
    from omni.providers.mlb_statsapi import MlbStatsApiProvider
    from omni.providers.mlb_teams import MlbTeamRegistry
    from omni.providers.polymarket_us import PolymarketUsProvider, fetch_market_data

    provider = MlbStatsApiProvider(MlbTeamRegistry.from_color_file(), schedule_timezone=zone)
    # The sportsbook's coupon fetch, each request counted (the provider refreshes it at its own pace).
    sportsbook = Meter(ledger, ProviderAccount.BOVADA, clock).counted(bovada_odds._default_fetch)
    odds = None if args.no_odds else BovadaOddsProvider(fetch=sportsbook)

    # With the meter on, the shared market service when this board has a token for it, else the public quotes.
    market: MarketReader | None = None
    market_account = ProviderAccount.POLYMARKET_US
    meter = "off"
    if args.meter:
        cloud = MarketServiceConfig.from_env({**read_env_file(_REPO_ROOT / ".env"), **os.environ})
        if cloud is not None:
            transport = UrllibTransport(cloud)
            HeartbeatSender(transport, version=str(source)).start()
            service = _CountedTransport(transport, Meter(ledger, ProviderAccount.MARKET_SERVICE, clock))
            market, meter = MarketServiceClient(service).win_probability, "market service"
            market_account = ProviderAccount.MARKET_SERVICE
        else:
            quotes = Meter(ledger, ProviderAccount.POLYMARKET_US, clock).counted(fetch_market_data)
            market, meter = PolymarketUsProvider(fetch=quotes).win_probability, "Polymarket public quotes"
    accounts = mlb_accounts(provider.source, market=market_account)
    return _Mlb(provider, odds.lines_for if odds is not None else None, market, meter, accounts)


class _CountedTransport:  # pragma: no cover - wraps the real network
    """The market service's transport, each request it makes counted against the service's account."""

    def __init__(self, transport: ServiceTransport, meter: Meter) -> None:
        self._transport = transport
        self._meter = meter

    def get(self, path: str, *, timeout_s: float) -> Any:
        return self._meter.counted(self._transport.get)(path, timeout_s=timeout_s)

    def post(self, path: str, body: Mapping[str, Any], *, timeout_s: float) -> Any:
        return self._meter.counted(self._transport.post)(path, body, timeout_s=timeout_s)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
