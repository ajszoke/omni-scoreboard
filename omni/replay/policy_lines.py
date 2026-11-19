"""The board policy's decisions as a replay's decision lines (`BOARD_POLICY.md` §8).

A replay under `omni-board-policy/2` lists, beside what went up and down, what the policy decided and why, one line
per decision (`omni.tower.decisions`), each after the board's own lines of the same step. They carry the trace's
fields (`omni.app.trace`) in the replay's own shape: a word for the kind, padded as the board's lines are, and every
instant in seconds since the replay's start on its running clock, as the board's lines are timed. The policy's name
is left out: the replay's header names it.

- ``base``: a base turn at its first frame, or its game winning again (``again``): its game's ``G`` and what it
  rests on, ``F``, ``T`` and ``Q``; its target share of the base airtime, its league's, and within its league; its
  actual share so far; its debt and its league's; a rescue's misses; the favorites' break it's a guest of; and the
  "why this card" sentence;
- ``promote``: a promoted card's confirmed start, with its ``I``, the bar and its context, the hold's deadline, the
  cooldown it starts and why; ``wait``: why a candidate didn't start, each time that changes; ``unspent``: a cued
  promotion rejected at its first frame;
- ``lease``: a favorite's break lease opened (what's left, its guests, provisional), revoked, ended, lapsed, or
  refused and why; ``guest``: a card admitted into a break, or refused and why; ``yield``: a guest giving the board
  back when its host's break closed;
- ``card``: an ordinary card's turn and why; ``cap``: the non-base cap binding; ``clamp``: a debt held at the limit.
"""

from __future__ import annotations

from omni.core.clock import MonotonicTime
from omni.policy.ledger import DebtClamp
from omni.tower.decisions import (
    BaseTurnNote,
    CapNote,
    CardNote,
    ClampNote,
    GuestNote,
    LeaseEvent,
    LeaseNote,
    PolicyDecision,
    PromotionNote,
    UnspentNote,
    WaitNote,
    YieldNote,
    event_words,
)

__all__ = ["policy_line"]


def policy_line(note: PolicyDecision) -> str:
    """One decision as a replay's line, without its time."""
    if isinstance(note, BaseTurnNote):
        return _base(note)
    if isinstance(note, PromotionNote):
        return _promotion(note)
    if isinstance(note, CardNote):
        return f"card    {note.card.raw}  ({note.role.value}){_guest(note.hosts)} | why: {note.why}"
    if isinstance(note, WaitNote):
        bar = f" | bar {note.bar:.2f} {note.context.value}" if note.bar is not None and note.context else ""
        return f"wait    {note.key} r{note.revision}  ({note.reason.value}){bar}"
    if isinstance(note, UnspentNote):
        return f"unspent {note.card.raw}  ({note.reason.value})"
    if isinstance(note, LeaseNote):
        return _lease(note)
    if isinstance(note, GuestNote):
        hosts = ", ".join(str(host) for host in note.hosts)
        if note.refusal is None:
            return f"guest   admitted {note.card.raw} in {hosts}"
        return f"guest   refused {note.card.raw} in {hosts}  ({note.refusal.value})"
    if isinstance(note, YieldNote):
        return f"yield   {note.card.raw}  (its host's break closed {_at(note.closed)})"
    if isinstance(note, CapNote):
        return f"cap     refused {', '.join(rung.name.lower() for rung in note.refused)}"
    assert isinstance(note, ClampNote)
    return "clamp   " + "; ".join(_clamp(clamp) for clamp in note.clamps)


def _base(note: BaseTurnNote) -> str:
    weight = note.weight
    reason = note.reason.value + (", again" if note.continues else "")
    actual = f"{note.actual:.2%}" if note.actual is not None else "-"
    line = (
        f"base    {note.card.raw}  ({reason})"
        f" | G {weight.g:.3f} {weight.status.value} F {weight.f:.2f} T {weight.t:.3f} Q {weight.q:.3f}"
        f" | target {note.target:.2%} (league {note.league_target:.2%}, {note.within_league:.2%} within)"
        f" | actual {actual} | debt {note.debt:+.2f}s (league {note.league_debt:+.2f}s)"
    )
    if note.rescue is not None:
        line += f" | rescue {note.rescue.misses}/{note.rescue.threshold} misses"
    return line + _guest(note.hosts) + f" | why: {note.why}"


def _promotion(note: PromotionNote) -> str:
    value = note.value
    worth = "I -" if value is None else f"I {value.value:.3f} {event_words(note.kind, value)} on {value.basis.value}"
    hold = f" | hold until {_at(note.hold)}" if note.hold is not None else ""
    return (
        f"promote {note.card.raw}  ({note.kind.value}) | {worth} | bar {note.bar:.2f} {note.context.value}{hold}"
        f" | cooldown until {_at(note.cooldown_until)}{_guest(note.hosts)} | why: {note.why}"
    )


def _lease(note: LeaseNote) -> str:
    line = f"lease   {note.event.value} {note.id}"
    if note.event is LeaseEvent.REFUSED and note.refusal is not None:
        return line + f"  ({note.refusal.value})"
    if note.event is not LeaseEvent.OPENED:
        return line
    left = f"{note.remaining.total_seconds():.1f}s left" if note.remaining is not None else "open-ended"
    guests = "any guests that fit" if note.turns is None else f"{note.turns} guest" + ("s" if note.turns > 1 else "")
    return line + f" | {left} | {guests}" + (" | provisional" if note.provisional else "")


def _guest(hosts: tuple[object, ...]) -> str:
    return f" | guest of {', '.join(str(host) for host in hosts)}" if hosts else ""


def _clamp(clamp: DebtClamp) -> str:
    whose = str(clamp.contest) if clamp.contest is not None else f"{clamp.league.value} league"
    return f"{whose} debt {clamp.debt:+.2f}s held at {clamp.limit:+.2f}s"


def _at(instant: MonotonicTime) -> str:
    """An instant on the replay's running clock, which reads zero at its start."""
    return f"{instant.seconds:.1f}"
