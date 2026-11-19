"""The trace of the board's decisions under `omni-board-policy/2`: one line per decision, its factors as fields, and
the "why this card" sentence, written from what a board in a synthetic time loop decided."""

from __future__ import annotations

import dataclasses
from datetime import timedelta, timezone

from omni.app.trace import DecisionTracer
from omni.core.enum import League
from omni.domain.breaks import BreakKind
from omni.policy.allocation import AllocationPolicy
from omni.policy.ledger import DebtClamp
from omni.preferences.favorites import Favorites
from omni.tower.decisions import ClampNote, LeaseNote, PolicyDecision
from omni.tower.director import DirectorPolicy
from tests.board_policy import Run, action, at, big_play, break_id, evidence, facts, final, games, live, major, reminder


def _lines(notes: list[PolicyDecision]) -> list[str]:
    lines: list[str] = []
    DecisionTracer(lines.append, timezone=timezone.utc).decided(notes)
    return lines


def test_a_promotion_in_a_mound_visit_is_written_with_its_value_bar_hold_cooldown_and_why() -> None:
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run()
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4), facts(other, 0.4))
    run.offers.append(major(play, 1.2, play="play 9"))
    run.publish()

    def visit() -> None:
        run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=18 - 45),)))
        run.publish()

    run.on(18, visit)
    run.run(0, 31)
    assert _lines(run.notes) == [
        "wait 23:00:00: mlb:mlb_statsapi:g01 play 9 r0 (holding) | bar 1.10 action",
        "base 23:00:00 omni-board-policy/2: mlb:mlb_statsapi:g00:live (weighted) | G 0.400 proxy F 4.00 T 1.000"
        " Q 0.500 | target 80.00% (league 100.00%, 80.00% within) | actual - | debt +0.00s (league +0.00s)"
        " | why: Braves: favorite",
        "base 23:00:12 omni-board-policy/2: mlb:mlb_statsapi:g00:live (weighted, again) | G 0.400 proxy F 4.00"
        " T 1.000 Q 0.500 | target 80.00% (league 100.00%, 80.00% within) | actual 100.00% | debt -2.40s"
        " (league +0.00s) | why: Braves: favorite",
        "lease 23:00:18: opened mlb:mlb_statsapi:g00 mlb mound visit @ b1 | 36.0s left | 1 guest",
        "promotion 23:00:18 omni-board-policy/2: mlb:mlb_statsapi:g01:bigplay:p1 (major) | I 1.200 late winner on"
        " model | bar 0.85 micro_break | hold until 23:00:24 | cooldown until 23:01:18"
        " | guest of mlb:mlb_statsapi:g00 | why: Angels at D-backs: late winner; Braves in mound visit",
        "guest 23:00:18: admitted mlb:mlb_statsapi:g01:bigplay:p1 in mlb:mlb_statsapi:g00",
        "guest 23:00:30: refused mlb:mlb_statsapi:g01:live in mlb:mlb_statsapi:g00 (turns_spent)",
        "base 23:00:30 omni-board-policy/2: mlb:mlb_statsapi:g00:live (return) | G 0.400 proxy F 4.00 T 1.000"
        " Q 0.500 | target 80.00% (league 100.00%, 80.00% within) | actual 100.00% | debt -3.60s (league +0.00s)"
        " | why: Braves: favorite, back before its break ends",
    ]


def test_the_cap_and_a_result_in_a_favorite_s_break_are_written() -> None:
    slate = games(7)
    atl, other = slate[0], slate[1]
    finals = [final(each, ended=-50 + 10 * index) for index, each in enumerate(slate[2:6])]
    run = Run()
    run.stage(live(atl), live(other), *finals, when=0)
    in_break = evidence(atl, BreakKind.MLB_INNING_BREAK, received=68 - 45, stated=175)
    run.describe(facts(atl, 0.4, breaks=(in_break,)), facts(other, 0.4))
    run.publish()
    run.run(68, 100)
    lines = _lines(run.notes)
    assert lines[2] == (
        "card 23:01:08 omni-board-policy/2: mlb:mlb_statsapi:g02:final (result) | guest of mlb:mlb_statsapi:g00"
        " | why: Orioles at Red Sox: final; Braves in inning break"
    )
    assert "cap 23:01:40: the non-base cap is reached; refused initial_result" not in lines  # four finals only
    assert lines[-1].startswith("base 23:01:40 omni-board-policy/2: mlb:mlb_statsapi:g01:live (recovery)")
    capped = Run(favorites=Favorites())
    capped.stage(live(other), *(final(each, ended=-50 + 10 * index) for index, each in enumerate(slate[2:7])))
    capped.describe(facts(other, 0.4))
    capped.publish()
    capped.run(0, 32)
    assert "cap 23:00:32: the non-base cap is reached; refused initial_result" in _lines(capped.notes)


def test_an_unspent_cue_a_yield_and_lease_changes_are_written() -> None:
    atl, other = games(2)
    play = big_play(other, "p1")
    run = Run()
    run.stage(live(atl), live(other), play)
    run.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_MOUND_VISIT, received=-45),)), facts(other, 0.4))
    run.offers.append(major(play, 0.9, model=False, play="play 9"))
    run.publish()
    cue = run.board.cue(at(0))
    run.describe(
        facts(
            atl, 0.4, action=action(atl, "ab 41 p2", received=-44, closes=(break_id(atl, BreakKind.MLB_MOUND_VISIT),))
        )
    )
    run.publish()
    run.board.committed(cue, at(0.1))
    assert _lines(run.board.decisions()) == [
        "lease 23:00:00: opened mlb:mlb_statsapi:g00 mlb mound visit @ b1 | 36.0s left | 1 guest",
        "lease 23:00:00: revoked mlb:mlb_statsapi:g00 mlb mound visit @ b1",
        "unspent 23:00:00: mlb:mlb_statsapi:g01:bigplay:p1 (break_over)",
    ]

    closing = Run()
    closing.stage(live(atl), live(other), when=0)
    closing.describe(facts(atl, 0.4), facts(other, 0.4))
    closing.publish()

    def inning_break() -> None:
        closing.describe(facts(atl, 0.4, breaks=(evidence(atl, BreakKind.MLB_INNING_BREAK, received=90 - 45),)))
        closing.publish()

    def resumed() -> None:
        closing.describe(
            facts(
                atl,
                0.4,
                action=action(atl, "ab 41 p1", received=104 - 45, closes=(break_id(atl, BreakKind.MLB_INNING_BREAK),)),
            )
        )
        closing.publish()

    closing.on(90, inning_break)
    closing.on(104, resumed)
    closing.run(52, 108)
    assert "yield 23:01:48: mlb:mlb_statsapi:g01:live (its host's break closed 23:01:44; aired 8.0s)" in _lines(
        closing.notes
    )


def test_leases_refused_ended_open_ended_and_provisional_are_written_with_their_terms() -> None:
    atl, other = games(2)
    run = Run()
    run.stage(live(atl), live(other))
    stated = evidence(atl, BreakKind.MLB_INNING_BREAK, received=-45, stated=145, onset=None)
    review = evidence(other, BreakKind.MLB_REVIEW, received=-45, marker="r")
    run.describe(facts(atl, 0.4, breaks=(stated,)), facts(other, 0.4, breaks=(review,)))
    run.publish()
    run.step(0)
    run.describe(facts(atl, 0.4, breaks=(dataclasses.replace(stated, ended=True),)))
    run.publish()
    run.step(1)
    late = evidence(other, BreakKind.MLB_MOUND_VISIT, received=-45, onset=30, marker="v")
    rain = evidence(atl, BreakKind.MLB_WEATHER, received=-44, marker="w")
    run.describe(facts(other, 0.4, breaks=(late,)), facts(atl, 0.4, breaks=(rain,)))
    run.publish()
    run.step(2)
    assert _lines([note for note in run.notes if isinstance(note, LeaseNote)]) == [
        "lease 23:00:00: opened mlb:mlb_statsapi:g00 mlb inning break @ b1 | 115.0s left | any guests that fit"
        " | provisional",
        "lease 23:00:00: opened mlb:mlb_statsapi:g01 mlb review @ r | 48.0s left | 2 guests",
        "lease 23:00:01: ended mlb:mlb_statsapi:g00 mlb inning break @ b1",
        "lease 23:00:02: opened mlb:mlb_statsapi:g00 mlb weather @ w | open-ended | any guests that fit",
        "lease 23:00:02: refused mlb:mlb_statsapi:g01 mlb mound visit @ v (inside_open_break)",
    ]


def test_a_rescue_a_clamp_a_reminder_and_an_offer_kept_back_are_written() -> None:
    atl, other = games(2)
    rules = DirectorPolicy(allocation=AllocationPolicy(rescue_turns=0.3, debt_limit=timedelta(seconds=1)))
    run = Run(rules=rules)
    run.stage(live(atl), live(other))
    run.describe(facts(atl, 1.0), facts(other, 0.0))
    run.publish()
    run.run(0, 48)
    lines = _lines(run.notes)
    assert lines[1] == (
        "clamp 23:00:12: mlb:mlb_statsapi:g00 debt -1.09s held at -1.00s;"
        " mlb:mlb_statsapi:g01 debt +1.09s held at +1.00s"
    )
    assert lines[-1] == (
        "base 23:00:48 omni-board-policy/2: mlb:mlb_statsapi:g01:live (rescue) | G 0.000 proxy F 1.00 T 1.000"
        " Q 0.500 | target 9.09% (league 100.00%, 9.09% within) | actual 0.00% | debt +1.00s (league +0.00s)"
        " | rescue 4/4 misses | why: Angels at D-backs: lopsided game, overdue for a turn"
    )
    league = ClampNote(at=at(60), clamps=(DebtClamp(League.NFL, None, 25.5, 24.0),))
    assert _lines([league]) == ["clamp 23:01:00: nfl league debt +25.50s held at +24.00s"]

    slate = games(3)[1:]
    bid = big_play(slate[0], "bid")
    quiet = Run(favorites=Favorites())
    quiet.stage(live(slate[0]), live(slate[1]), bid)
    quiet.describe(facts(slate[0], 0.4), facts(slate[1], 0.4))
    quiet.offers.extend((reminder(bid), major(big_play(slate[1], "gone"), 0.9, play="play 3")))
    quiet.publish()
    quiet.step(0)
    assert _lines(quiet.notes) == [
        "wait 23:00:00: mlb:mlb_statsapi:g02 play 3 r0 (not_in_view)",
        "promotion 23:00:00 omni-board-policy/2: mlb:mlb_statsapi:g01:bigplay:bid (reminder) | I - | bar 0.35 open"
        " | cooldown until 23:01:00 | why: Angels at D-backs: reminder; no favorite in action",
    ]
