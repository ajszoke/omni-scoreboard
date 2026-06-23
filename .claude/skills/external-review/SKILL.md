---
name: external-review
description: Run a round of the head/hand external review — bundle the build state for the external flagship model (ChatGPT, "CGPT") to review fidelity-to-plan and steer the next phase, using the plan-trio + round-zip cadence. Use after a milestone/phase, before a framework-level direction change, or whenever the build needs the planner to bless deviations or sequence what's next. Produces a scannable plan-trio in ~/omni-review/plans/ and an immutable round-N.zip + prompt to hand off. NOT for routine PRs, bug fixes, or single-file edits.
---

# External-review cadence (head/hand, plan-trio + round-zips)

This project runs a **head / hand** split:

- **Head (planner):** the external flagship model (ChatGPT, "CGPT" / "external"). Owns conceptual
  reasoning, long-horizon planning, and design/fidelity review. It seeded the kernel
  (`docs/agent_context/kernel/`, frozen) and reviews each round's bundle.
- **Hand (implementer):** Claude Code (you). Carry out the build, **self-review against the kernel**,
  fix clear drifts yourself, and bundle each round for the head to review and re-plan.

A round packages the current build state into an immutable `round-N.zip` + a `round-N-prompt.md` the
**user** pastes/uploads to the flagship; the flagship returns a verdict + next-phase plan, which you
adjudicate and fold in. Adapted from the day-job `external-review` skill (Shopfine). Detail in
[reference.md](reference.md); skeletons in [templates.md](templates.md).

## When to run a round (and not)

| Run a round | Don't |
|---|---|
| After a phase/milestone — "was the build faithful; what's next?" | Routine PRs, bug fixes, single-file edits |
| Before a framework-level direction change or a big sequencing call | Mechanical refactors / renames / typos |
| When deviations from the kernel need the planner to bless or redirect | Anything where the right next step is already clear |
| Locking a multi-file design before building it | Backfills / test tweaks |

Always state explicitly what's **current-ship** vs **deferred** and ask the head to confirm the split. Don't overengineer; a clean green light ends the cadence.

## The artifacts

All review artifacts live in one durable WSL home, `~/omni-review/` (machine-local — *not* in the repo):

```
~/omni-review/
  plans/                       ← the living plan-trio, updated across rounds
    omni-<slug>.md             ← main plan / review brief, stays SCANNABLE
    omni-<slug>-SUPPLEMENT.md  ← evidence locker: file:line touchpoints, in-code verifications, drift table
    omni-<slug>-REVIEWS.md     ← per-round log: briefing + verbatim verdict + our adjudication
  rounds/                      ← immutable per-round bundles
    round-N-prompt.md          ← the briefing handed to the flagship (also bundled)
    round-N.zip                ← plans/ snapshot + prompt + sources/ + prior verdict
    round-N-verdict.md         ← the flagship's verbatim verdict, archived per round
```

`<slug>` is a stable memorable handle. **One zip per round, never overwrite — it's the audit trail.**
The home is reachable from Windows at `\\wsl.localhost\Ubuntu\home\<user>\omni-review\`, so the user can
upload `rounds/round-N.zip` + `round-N-prompt.md` to the flagship directly — no copy into Downloads.

## The loop

1. **Self-review against the kernel first.** Diff the as-built repo (`omni/`, `AGENTS.md`, `docs/`)
   against the frozen `docs/agent_context/kernel/`. Catalog every drift: **faithful**, **deliberate
   deviation (defend it)**, or **accidental (fix it)**. Verify load-bearing claims in-code, cite
   `file:line`, and distinguish **[verified]** (read this session) from **[reported]**.
2. **Fix the clear drifts yourself** (own PRs) before the round — don't ask the head to adjudicate
   what you can just correct (e.g. a doc that describes modules that don't exist). Leave the
   judgment calls (architecture axis, sequencing) for the head.
3. **Write the plan-trio** ([templates.md](templates.md)): main brief (scannable), SUPPLEMENT
   (drift table + in-code verifications), REVIEWS skeleton.
4. **Author `round-N-prompt.md`.** Round 1 is fidelity + open-ended planning: state the posture,
   summarize what was built vs. the kernel, present the drift catalog with your disposition on each,
   and ask the head to (a) confirm fidelity, (b) bless or redirect each deliberate deviation, and
   (c) plan/sequence the next phase. Later rounds report what changed and converge.
5. **Bundle `round-N.zip`** — `plans/` snapshot + the prompt + `sources/` (the frozen kernel, current
   `AGENTS.md`/`STATUS.md`/`ARCHITECTURE`, the `omni/` tree, key modules the brief cites ≤~5k lines,
   a test/coverage snapshot) + the prior round's verbatim verdict. **Verify contents** (`zipinfo -1`)
   before handing off.
6. **Hand off → the user runs the flagship → pastes the verbatim verdict back.** You cannot launch
   the flagship yourself.
7. **Record + adjudicate** in REVIEWS: archive the verdict verbatim (`round-N-verdict.md`), then per
   finding write **accept / defer / disagree-with-rationale** + a severity tag; capture the head's own
   open questions with a disposition.
8. **Verify the head's load-bearing claims in-code before folding them in** — re-read every `file:line`
   it leans on; confirm, downgrade, or refute each.
9. **Fold accepted findings + the next-phase plan into the trio + STATUS.** Then implement per the
   normal PR + QC workflow.
10. **Closure check.** Clean "Proceed" + no open framing questions → converge, don't manufacture
    rounds. Otherwise build `round-(N+1).zip` and loop from step 6.

## Standing rules (every round)

- **Verbatim, always.** The flagship's verdict goes into `round-N-verdict.md` unedited — audit trail.
- **Verify before you trust.** Re-read every `file:line` either side leans on.
- **Never overwrite a round.** Each `round-N.zip` is immutable history.
- **Never edit the kernel.** `docs/agent_context/kernel/` is the frozen baseline; drift is surfaced,
  not silently reconciled away.
- **Track state in memory.** Keep a `project_omni_review.md` auto-memory pointing at the trio + current
  round + the key reframings, so a future session resumes cleanly.
- **Right-size.** Round count scales with risk; a clean green light ends it.

See [reference.md](reference.md) for the full layout/sources/prompt-anatomy spec and [templates.md](templates.md) for skeletons.
