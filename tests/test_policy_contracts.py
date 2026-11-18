from datetime import datetime, timedelta, timezone

import pytest

from omni.core.clock import ClockReading, MonotonicTime
from omni.core.enum import League
from omni.core.ids import LeagueScopedId, SourceRef
from omni.domain.contest import TeamGame
from omni.domain.breaks import BreakConfidence, BreakEvidence, BreakId, BreakKind
from omni.policy.leverage import Leverage, ModelStatus

_AT = ClockReading(datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc), MonotonicTime(100.0))
_MLB_GAME = LeagueScopedId(League.MLB, SourceRef("mlb-statsapi"), "401")


def test_every_break_kind_names_its_league() -> None:
    assert {kind.league for kind in BreakKind} == {League.MLB, League.NFL, League.NBA}
    assert BreakKind.NFL_TWO_MINUTE_WARNING.league is League.NFL
    assert BreakKind.NBA_BEFORE_OVERTIME.league is League.NBA


def test_a_break_reads_its_kind_and_says_which_break_it_is() -> None:
    evidence = BreakEvidence(
        id=BreakId(_MLB_GAME, BreakKind.MLB_INNING_BREAK, "top 5 middle"),
        received=_AT,
        confidence=BreakConfidence.EXPLICIT,
        stated=timedelta(seconds=120),
    )
    assert evidence.kind is BreakKind.MLB_INNING_BREAK
    assert str(evidence.id) == "mlb:mlb-statsapi:401 mlb inning break @ top 5 middle"


@pytest.mark.parametrize(
    "kind, stated, revision, message",
    [
        (BreakKind.NFL_TEAM_TIMEOUT, None, 0, "can't stop a mlb game"),
        (BreakKind.MLB_INNING_BREAK, timedelta(0), 0, "must be positive"),
        (BreakKind.MLB_INNING_BREAK, None, -1, "can't be negative"),
    ],
)
def test_break_evidence_refuses_what_cant_be(
    kind: BreakKind, stated: timedelta | None, revision: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BreakEvidence(
            id=BreakId(_MLB_GAME, kind, "x"),
            received=_AT,
            confidence=BreakConfidence.INFERRED,
            stated=stated,
            revision=revision,
        )


def test_only_a_validated_model_qualifies_an_action_interrupt() -> None:
    assert Leverage(g=0.8, z=0.9, status=ModelStatus.VALIDATED, model_id="mlb-wp/1").qualifies_action
    assert not Leverage(g=0.8, z=0.9, status=ModelStatus.REDUCED, model_id="mlb-wp/1").qualifies_action
    assert not Leverage(g=0.8, status=ModelStatus.PROXY).qualifies_action
    assert not Leverage(g=0.2, status=ModelStatus.UNKNOWN).qualifies_action


@pytest.mark.parametrize(
    "g, z, status, model_id, message",
    [
        (1.2, None, ModelStatus.PROXY, None, "within 0..1"),
        (0.5, -0.1, ModelStatus.VALIDATED, "m", "within 0..1"),
        (0.5, None, ModelStatus.PROXY, "m", "names its model"),
        (0.5, 0.5, ModelStatus.VALIDATED, None, "names its model"),
    ],
)
def test_a_leverage_reading_refuses_what_cant_be(
    g: float, z: float | None, status: ModelStatus, model_id: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Leverage(g=g, z=z, status=status, model_id=model_id)


def test_an_nfl_reading_keeps_the_answer_and_when_it_arrived() -> None:
    from omni.app.nfl_feeds import NflReading
    from omni.core.enum import GameStatus
    from omni.core.time import DurationSeconds
    from omni.domain.contest import TeamGame
    from omni.providers.nfl_teams import NflTeamRegistry
    from omni.providers.nflmeta import NflLiveSnapshot, NflPhase

    teams = NflTeamRegistry.default(source=SourceRef("nflmeta"))
    game = TeamGame(
        id=LeagueScopedId(League.NFL, SourceRef("nflmeta"), "401"),
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=_AT.utc,
        away=teams.resolve("CAR"),
        home=teams.resolve("ATL"),
    )
    snapshot = NflLiveSnapshot(
        game=game,
        phase=NflPhase.IN,
        state=None,
        events=(),
        caught_up=False,
        break_ends_at=None,
        observed_at=None,
        poll_every=DurationSeconds(10),
    )
    reading = NflReading(snapshot, _AT)
    assert reading.snapshot.game is game and reading.arrived == _AT


def _nfl_game(raw: str = "401") -> TeamGame:
    from omni.core.enum import GameStatus
    from omni.domain.contest import TeamGame
    from omni.providers.nfl_teams import NflTeamRegistry

    teams = NflTeamRegistry.default(source=SourceRef("nflmeta"))
    return TeamGame(
        id=LeagueScopedId(League.NFL, SourceRef("nflmeta"), raw),
        league=League.NFL,
        status=GameStatus.LIVE,
        scheduled_start=_AT.utc,
        away=teams.resolve("CAR"),
        home=teams.resolve("DAL"),
    )


def test_a_policy_view_files_each_game_s_released_facts_and_the_offers_beside_them() -> None:
    from omni.cards.base import DedupeKey
    from omni.events.break_detection import ActionEvidence
    from omni.policy.promotion import PromotionCandidate, PromotionKey, PromotionKind
    from omni.policy.view import EMPTY_POLICY_VIEW, GameFacts, PolicyView

    game = _nfl_game()
    evidence = BreakEvidence(
        id=BreakId(game.id, BreakKind.NFL_TEAM_TIMEOUT, "play 40"), received=_AT, confidence=BreakConfidence.EXPLICIT
    )
    facts = GameFacts(
        game=game,
        leverage=Leverage(g=0.4, status=ModelStatus.PROXY),
        fresh=True,
        breaks=(evidence,),
        action=ActionEvidence(contest=game.id, marker="play 39", received=_AT),
    )
    offer = PromotionCandidate(
        key=PromotionKey(game.id, "play 41"),
        kind=PromotionKind.REMINDER,
        eligible=_AT.monotonic,
        card=DedupeKey("nfl:nflmeta:401:live"),
    )
    view = PolicyView({game.id: facts}, (offer,))
    assert view.facts(game.id) is facts and view.facts(_MLB_GAME) is None
    assert view.offers == (offer,)
    assert EMPTY_POLICY_VIEW.facts(game.id) is None and EMPTY_POLICY_VIEW.offers == ()


def test_game_facts_refuse_another_game_s_evidence_and_a_view_files_facts_by_their_own_game() -> None:
    from omni.events.break_detection import ActionEvidence
    from omni.policy.view import GameFacts, PolicyView

    game, other = _nfl_game(), _nfl_game("402")
    leverage = Leverage(g=0.4, status=ModelStatus.PROXY)
    elsewhere = BreakEvidence(
        id=BreakId(other.id, BreakKind.NFL_TEAM_TIMEOUT, "play 4"), received=_AT, confidence=BreakConfidence.EXPLICIT
    )
    with pytest.raises(ValueError, match="can't describe"):
        GameFacts(game=game, leverage=leverage, fresh=True, breaks=(elsewhere,))
    with pytest.raises(ValueError, match="can't describe"):
        GameFacts(
            game=game,
            leverage=leverage,
            fresh=True,
            action=ActionEvidence(contest=other.id, marker="play 4", received=_AT),
        )
    with pytest.raises(ValueError, match="filed under"):
        PolicyView({other.id: GameFacts(game=game, leverage=leverage, fresh=True)})


def test_mutes_cover_a_league_or_either_team_and_read_in_a_fixed_order() -> None:
    from omni.policy.settings import Mutes

    game = _nfl_game()
    assert not Mutes() and str(Mutes()) == "none" and not Mutes().mutes(game)
    assert Mutes.of([League.NFL]).mutes(game)
    by_team = Mutes.of(teams={League.NFL: ["DAL"], League.MLB: []})
    assert by_team and by_team.mutes(game) and not Mutes.of(teams={League.NFL: ["ATL"]}).mutes(game)
    assert str(Mutes.of([League.NBA, League.MLB], {League.NFL: ["DAL", "CAR"]})) == "mlb, nba, nfl:CAR, nfl:DAL"


def test_policy_settings_choose_a_shared_board_policy_within_safe_weights_and_turns() -> None:
    from omni.app.versions import PolicyName
    from omni.policy.settings import PolicySettings

    settings = PolicySettings(league_weights={League.NFL: 1.5})
    assert settings.policy is PolicyName.BOARD_V2 and settings.promotions
    assert settings.weight(League.NFL) == 1.5 and settings.weight(League.MLB) == 1.0
    assert PolicySettings(policy=PolicyName.BOARD_V1).policy is PolicyName.BOARD_V1
    with pytest.raises(ValueError, match="isn't a shared-board policy"):
        PolicySettings(policy=PolicyName.NFL_SOLO_V1)
    with pytest.raises(ValueError, match="outside 0.5-2.0"):
        PolicySettings(league_weights={League.MLB: 3.0})
    with pytest.raises(ValueError, match="outside 8-30s"):
        PolicySettings(slot=timedelta(seconds=5))
