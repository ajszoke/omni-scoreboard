# External-review cadence — templates

Copy-paste skeletons for the [external-review](SKILL.md) skill. Replace `<…>`. Conventions in [reference.md](reference.md).

---

## A. Main brief — `~/omni-review/plans/omni-<slug>.md`

```markdown
# Omni external review — <slug> (round N)

> **Posture:** head (flagship/CGPT) plans · hand (Claude Code) implements + self-reviews.
> **Status:** <round-N pending | reviewed → verdict | converged>. Baseline: `docs/agent_context/kernel/` (frozen <date>).

## What was built (vs. the kernel roadmap)
<scannable: phases/PRs shipped, mapped to the kernel's roadmap order>

## Drift catalog (summary — full table in SUPPLEMENT)
| # | Kernel item | As-built | Class | Disposition |
|---|---|---|---|---|
| 1 | <…> | <…> | faithful / deliberate / accidental | fixed this round / defend / for the head |

## Scope calibration (current-ship vs deferred)
| Item | Disposition |
|---|---|
| <…> | **Current ship** / **Deferred** / **Open — round N** |

## Questions for the head (round N)
1. Fidelity: <…>
2. Bless or redirect: <deviation>
3. Sequence the next phase: <options>

## Next-phase options (for the head to sequence)
- <option A — e.g. MLB feature-completeness (M3)>
- <option B — running-app orchestration loop>
- <option C — league expansion (M6 NFL)>
```

---

## B. SUPPLEMENT — `~/omni-review/plans/omni-<slug>-SUPPLEMENT.md`

```markdown
# Omni external review — <slug> — SUPPLEMENT

Companion to `omni-<slug>.md`. Citations: **[verified]** = read this session; **[reported]** = secondhand.

## 1. Full drift table
| # | Kernel (file) | Plan said | As-built (`file:line`) | Verdict + rationale |

## 2. In-code fidelity verifications
1. <claim, e.g. "no raw JSON outside providers/"> — CONFIRMED/REFUTED — <evidence, file:line>

## 3. Test / quality snapshot
<pytest summary · mypy/black · omni coverage (--cov-branch)>

## 4. Touchpoint map for the next phase
<file:line for where the next work would land>
```

---

## C. REVIEWS — `~/omni-review/plans/omni-<slug>-REVIEWS.md`

```markdown
# Omni external review — <slug> — rounds

Per-round log. Bundles at `~/omni-review/rounds/round-N.zip`.

## Round N — [pending | date]
**Bundle:** `round-N.zip`  **Briefing:** `round-N-prompt.md`

### Briefing summary
<what we sent + focus questions>

### Verdict (verbatim archived in `round-N-verdict.md`)
<overall read + key extracts>

### Decisions (our adjudication)
- **<finding> (severity):** <restatement> → **Decision:** accepted / deferred / disagreed because …

### Our in-code verification of the head's claims
1. <claim> — CONFIRMED / RESOLVED / DOWNGRADED — `file:line` + rationale

### Head's open questions + disposition
| # | Question | Disposition (answer-in-code / next-round / decided) |

## Convergence checklist
Stops when: clean "Proceed" + no open framing questions + all accepted findings landed.
```

---

## D. Round-N briefing — `~/omni-review/rounds/round-N-prompt.md`

```markdown
# Omni Scoreboard — external review, round N (<fidelity + plan | converge>)

You are the **planning head** for Omni Scoreboard, a typed rebuild of MLB-LED-Scoreboard into an
`omni/` package (friends-and-family LED sports scoreboard; three panel profiles). You seeded this
project's kernel (bundled, frozen); I (Claude Code) am the **implementing hand**. Treat bundled
sources as authoritative; cite `path:line`. <Prior verdict at round-(N-1)-verdict.md.>

## 1. What I built (vs. your kernel roadmap)
<the phases/PRs, mapped to the roadmap order>

## 2. What's in the bundle
```
plans/    <trio>
sources/  kernel/ (frozen) · AGENTS.md · STATUS.md · ARCHITECTURE (as-built) · omni-tree.txt · <key modules> · test-snapshot.md
```

## 3. Drift catalog (my self-review)
<each deviation: kernel said → as-built → my disposition (faithful / deliberate+rationale / fixed)>

## 4. Fidelity questions
<ask: is the build faithful? bless or redirect each deliberate deviation?>

## 5. Next-phase planning
<the open sequencing decision + options; ask the head to sequence and scope current-ship vs deferred>

## 6. What do you need from me next round?
<ask the head to list its own unknowns / files it wants>

## 7. Response format
- Overall fidelity read • per-deviation verdict (bless/redirect) • next-phase plan + sequencing •
  severity-tagged findings (Critical/High/Medium/Low/Nit) • your own open questions

<Round 2+: replace 3–6 with "What changed", "My in-code verification of your claims (reported back)",
"Answers to your prior questions", and tight sign-off focus questions.>
```
