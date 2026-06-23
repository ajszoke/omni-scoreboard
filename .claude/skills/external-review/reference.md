# External-review cadence — reference

Detailed conventions for the [external-review](SKILL.md) skill. Skeletons in [templates.md](templates.md).

---

## 1. The plan-trio (`~/omni-review/plans/`)

Three sibling files sharing an `omni-<slug>` handle. The split keeps the main brief scannable.

| File | Holds | Keep it… |
|---|---|---|
| `omni-<slug>.md` | The review brief / plan: posture, what was built vs. the kernel, the drift catalog with dispositions, scope (current-ship vs deferred), the questions for the head, next-phase proposal | **Scannable.** The head should grok the shape in 2 minutes. |
| `omni-<slug>-SUPPLEMENT.md` | The evidence locker: the full **drift table** (kernel item → as-built → verdict), `file:line` touchpoints, in-code verifications, test/coverage data | Dense is fine. |
| `omni-<slug>-REVIEWS.md` | One section per round: briefing summary, **verbatim verdict**, per-finding decisions, the head's open questions, convergence checklist | Append-only across rounds. |

Tag claims **[verified]** (read this session) vs **[reported]** (secondhand) in the SUPPLEMENT.

---

## 2. The round-zip (`~/omni-review/rounds/`)

```
round-N-prompt.md     ← authored FIRST, then a copy is bundled
round-N-verdict.md    ← the flagship's verbatim verdict (archived after the round)
round-N.zip           ← plans/ (trio snapshot) + sources/ + round-N-prompt.md + round-(N-1)-verdict.md
```

- **1-indexed, no timestamps** — the round number is the version. **Never overwrite a round.**
- Build with `zip -r -q round-N.zip plans/ sources/ round-N-prompt.md`. **Verify** with `zipinfo -1`
  before handing off: the trio snapshot must be current and no junk leaked in.
- Copy the final zip + prompt to a **user-reachable** location (e.g. `/mnt/c/Users/<user>/Downloads/
  omni_review_round-N/`) so the user can upload them to the flagship.

---

## 3. Selecting `sources/`

Bundle what lets the head reason from the actual repo, not training-data guesses:

- **The frozen kernel** (`docs/agent_context/kernel/`) — the baseline it's judging fidelity against.
- **Current living docs:** `AGENTS.md`, `docs/agent_context/STATUS.md`, `ROADMAP.md`, the *as-built*
  `ARCHITECTURE_TYPED_DOMAIN.md`, `BACKLOG.yaml`.
- **The `omni/` tree** (`find omni -name '*.py'`) and the **key modules the brief cites** (full files
  ≤ ~5k lines — let the head navigate, don't excerpt).
- **A test/quality snapshot** — `pytest` summary, `mypy`/`black` status, `--cov=omni --cov-branch`
  numbers — as a `.md`, pre-empting "is it actually tested?".
- When iterating, the **prior round's verbatim verdict**.
- Round 2+ typically *adds* the specific files the head asked for, carried alongside round-1 sources.

Exclude bulk/opaque junk and anything git-ignored (secrets, `config.json`, `.venv/`).

---

## 4. Anatomy of a good `round-N-prompt.md`

**Round 1 — fidelity + open-ended planning.** You want the framing pressure-tested, not a rubber stamp.

1. **Posture + ground rules** — head/hand split; "you seeded the kernel; I implemented; treat bundled
   sources as authoritative; cite `path:line`."
2. **What was built** — the phases/PRs shipped, in one scannable pass, mapped to the kernel's roadmap.
3. **Bundle manifest** — every file with a one-line "why it's here."
4. **The drift catalog** — each deviation from the kernel with your disposition (faithful / deliberate—
   here's the rationale / fixed-this-round). This is the core of a fidelity round.
5. **Fidelity questions** — ask the head to confirm the build is faithful and to **bless or redirect
   each deliberate deviation** (especially axis/sequencing calls).
6. **Next-phase planning** — present the open sequencing decision (e.g. MLB feature-completeness vs.
   running-app orchestration vs. league expansion) and ask the head to sequence it.
7. **What do *you* need from us?** — ask the head to list its own unknowns / files it wants next round.
8. **Response format** — overall fidelity read; per-deviation verdict (bless/redirect); next-phase plan
   with sequencing; severity-tagged findings (Critical/High/Medium/Low/Nit); its own open questions.

**Round 2+ — convergence.** What changed since last round; **report your in-code verification of the
head's claims back to it**; answer its prior questions; then a tight set of sign-off questions.

---

## 5. Adjudication (the REVIEWS "Decisions" block)

After pasting the verbatim verdict, work every finding:

- **Per finding:** `**<title> (severity):** <restatement> → **Decision:** accepted / deferred / disagreed because …`
- **Re-verify load-bearing claims in-code first** — a severity can move once you read the cited code
  (confirmed → fold the fix; resolved → already handled; downgraded/refuted → cite contradicting code).
- **Capture the head's open questions** as a table with dispositions (answer-in-code / next-round / decided).
- **Record user decisions** made mid-cadence in REVIEWS *and* fold them into the plan + STATUS.

---

## 6. Closure & after

- **Converge** on a clean "Proceed" with no open framing questions — don't manufacture rounds.
- **One framing question left?** Close it with a short followup in the same round, not a fresh cycle.
- **After convergence:** fold the head's next-phase plan into STATUS → Next, then implement per the
  normal per-PR + QC workflow. When a step both de-risks *and* generates data a later round wanted,
  prefer it first.

---

## 7. The kernel-diff method (omni-specific)

The frozen baseline is `docs/agent_context/kernel/`. To catalog drift each round:

```bash
K=docs/agent_context/kernel
diff "$K/AGENTS.md" AGENTS.md
for f in ARCHITECTURE_TYPED_DOMAIN.md ROADMAP.md BACKLOG.yaml SOURCES.md; do diff "$K/$f" "docs/agent_context/$f"; done
# structure drift: compare the proposed layout to as-built
diff <(sed -n '/```text/,/```/p' "$K/ARCHITECTURE_TYPED_DOMAIN.md") <(find omni -name '*.py' | sort)
```

Classify each diff: **benign enrichment** (docs grew consistently), **deliberate deviation** (defend
it to the head), or **accidental drift** (fix it yourself). Doc-vs-reality mismatches (a doc citing
modules that don't exist) are accidental — fix before the round.

---

## 8. Memory linkage

Keep a `project_omni_review.md` auto-memory pointing at the trio, the current round, and the hard-won
reframings, so the cadence survives across sessions. Add a one-line pointer in `MEMORY.md`.
