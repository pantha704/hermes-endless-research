---
name: endless-research
description: Use when research must dig until it is found.
version: 0.2.15
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [research, orchestration, durable, frontier, delegation, checkpoint]
    related_skills: [hermes-agent]
---

# Endless Research Engine

A "keep digging until the answer is found" research protocol. The brain lives on
disk in a project `.research/` directory, **not** in chat context. Any run — live
chat, a delegated subagent, or a cron tick — reads state, resumes the highest-value
unfinished frontier clue, and writes a checkpoint. The next run continues exactly
where the last one stopped. This is what makes "until the end of the world" real:
no single session needs to run forever, the *campaign* does.

Companion CLI: `python3 ~/.hermes/skills/research/endless-research/scripts/research_project.py` (init / status / resignal / reset). See `references/protocol.md` for the full spec; this file is the operating playbook.

## When to Use

- User asks to research something "until it's found" / "dig to the end of the world"
  / "leave no stone unturned" / "keep going no matter what."
- A research objective has acceptance criteria you must satisfy with traceable evidence.
- Any task that should survive restarts or a context-token wall (use cron).

**Don't use for:** quick factual lookups, single-pass searches, everyday coding.

## Project layout (the external brain)

```
<project>/
  HERMES.md                     # sticky instruction — auto-loaded by cron job with workdir
  .research/
    objective.md                # exact finding, questions, acceptance/failure/success, constraints
    state.json                  # machine-readable state (current_state, last_checkpoint, priority queue)
    frontier.jsonl              # pending clues, each with a priority score
    criteria.jsonl              # acceptance criteria the SUCCESS gate verifies
    edges.jsonl                 # evidence graph: typed relationships between nodes
    questions.jsonl             # open-question nodes (Q-*)
    people.jsonl                # person/organisation/entity nodes (P-*)
    scope.json                  # crawl/scope policy (budgets, not max-depth)
    claims.jsonl                # established claims, each linked to sources
    sources.jsonl               # every source record (url, title, type, accessed)
    contradictions.jsonl        # conflicting evidence
    dead-ends.jsonl             # failed branches + reopen conditions
    search-log.jsonl            # every query tried and its outcome
    unresolved.md               # open questions, ranked by importance
    checkpoints/                # one checkpoint snapshot per substantial round
    final-report.md             # the success deliverable
```

## The states (only these)

- **CONTINUE** — meaningful clues/uncertainties/untried strategies remain. Work it.
- **CHECKPOINT** — run must end (context/token/iterations/rate limit). Save state,
  write exact resume instructions. **Not** an answer.
- **BLOCKED** — external obstacle (auth, paywall, inaccessible source, rate limit).
  Record the obstacle + alternative routes in state.json `blockers`, stay BLOCKED.
- **EXHAUSTED** — all known strategies and high-value clues genuinely exhausted and the
  objective is NOT met. Still not success. Explain exactly what's missing. Trigger user.
- **SUCCESS** — acceptance criteria met, key claims traceable, contradictions investigated,
  primary sources used where reasonable. This is the only terminal "done."
- **DORMANT** — all current clues exhausted, but the *topic may yield new information
  later* (developing story, unreleased source, awaited filing). Unlike EXHAUSTED
  (terminal), DORMANT is **resumable**: when new info/terminology/sources emerge,
  `resignal <dir> CONTINUE` to re-awaken the campaign. Keeps developing topics alive
  instead of permanently ending them.

Rule: **"I searched a lot" is never SUCCESS.** A plausible answer is not verified truth.

## Operating loop

1. **Load state.** Read `objective.md`, `state.json`, `unresolved.md`, tail of
   `search-log.jsonl`, newest checkpoint. If files are missing/corrupt, re-scaffold.
2. **Resume.** Pick the highest-priority `pending` frontier item (deterministic: use
   `scripts/research_project.py status` for the sorted queue — don't eyeball-sort).
3. **Dig.** Run searches / web_extract / read sources. Extract claims, citations,
   authors, organisations, datasets, repos, identifiers, links, dates, **contradictions**.
4. **Sample frontier.** Record every new clue as a frontier entry (do NOT auto-enqueue
   everything — quality-gate). Only high-value, genuinely-new, plausible clues.
5. **Track sources/claims.** Append to the jsonl files. Never fabricate a citation,
   URL, quote, or search result. Never claim a source supports more than it does.
6. **Reprioritise.** Recompute the moving **priority score** (0-100):
   `0.30*relevance + 0.20*primary_source_likelihood + 0.20*info_gain + 0.15*resolves_uncertainty + 0.10*novelty + 0.05*ease`. Down-rank: repeated info, SEO pages, unsourced summaries, circular citations, content farms. Up-rank: primary docs, code/commits, filings, archives, original interviews, contradictions.
7. **Checkpoint** after each substantial round — even mid-progress. A future run must
   resume purely from the files. Never let an approaching limit force a rushed conclusion.
8. **Broader delegation.** Use `orchestrator` children for broad branches, `leaf`
   workers for individual clues. Only delegate high-value branches (width×depth costs
   real tokens). Each delegated task MUST include: objective, exact branch goal, known
   evidence, prior attempts/dead-ends, expected output format, verification standard.
   Validate and merge children's findings against their cited evidence — never trust a
   child summary on faith.
9. **Resolve state.** `SUCCESS` (must pass the deterministic gate — write criteria,
   final-report, then `verify_success`; SUCCESS is otherwise blocked) / `CONTINUE`
   (loop) / `CHECKPOINT` (save + note exact next action) / `BLOCKED` (record obstacle) /
   `DORMANT` (clues spent but topic alive — keep resumable; this parks the research job
   until the daily watcher re-awakens it) / `EXHAUSTED` (tell user what's still missing).
   Persist everything to `.research/`.

**The SUCCESS gate.** `resignal <dir> SUCCESS` is automatically blocked unless
`verify_success` passes: every acceptance criterion in `criteria.jsonl` is `met` (or
explicitly excepted), evidence source IDs resolve to `sources.jsonl`, `primary_hard`
criteria have primary evidence, corroboration-required criteria have ≥2 independent
sources, no critical contradiction is unresolved, and `final-report.md` is substantive.
`--force` bypasses it. This stops a model from prematurely declaring SUCCESS.

**Two-job model (the heartbeat).** The aggressive **research job** fires only during
`CONTINUE`/`CHECKPOINT`/`BLOCKED` and auto-pauses on `DORMANT`/`SUCCESS`/`EXHAUSTED`.
A cheap daily **dormant watcher** independently probes DORMANT campaigns for genuinely
new evidence (≤3 searches, no full campaign) and re-awakens them via
`resignal <dir> CONTINUE --cron <research-job-id>`, which auto-resumes the research job.

**Explicit evidence graph (v0.2.0).** Research is a graph, not a linear search. Record
typed relationships with `edge <dir> <from> <relationship> <to> [--context]` (e.g.
`SRC-001 cites SRC-002`, `SRC-001 supports CLM-001`, `SRC-002 contradicts CLM-001`,
`CLUE-001 derived_from SRC-002`). `edge` enforces referential integrity — from/to must
resolve to real nodes. View with `graph <dir>`. Before digging a new URL, run
`inspect <dir> <url>` to see its canonical form + the campaign scope (no blind crawling),
and use `clarify <dir> <url> --goal "..."` to compile a clear/vague/ambiguous goal into
a research contract.

**Concurrency.** Run every tick through the atomic lock primitive so two ticks can
never mutate the project simultaneously: `research_project.py tick <dir> [--cmd DIG]`.
Hermes' cron scheduler also natively skips a job whose prior run is still in flight
("already running — skipping"), so overlap is prevented both at the scheduler and at
the project level. A `tick` that finds the project locked exits code 2 (skip, not fail).

## Search diversification (never repeat a dead query verbatim)

Exact phrases, synonyms, abbreviations, alternate spellings, older terminology,
translated terms, author/org names, document titles, unique quoted fragments, file
types, site-restricted search, date windows, repo/search-API, paper/dataset IDs,
archive.org/wayback, contradictory formulations, disconfirming queries. Log every
meaningful query + result to `search-log.jsonl`.

## Common pitfalls

1. **Declaring SUCCESS too early** after a few plausible pages. Multiple pages citing
   the same original source are NOT independent confirmation — verify the original.
2. **Auto-enqueuing every clue** → frontier explodes. Quality-gate: only novel,
   plausible, on-objective clues get an entry.
3. **Skipping the deterministic priority sort** and hand-picking clues. Use the script.
4. **Losing the thread on CHECKPOINT** — always write the exact next action.
5. **Letting delegation needlessly multiply** — a 5-wide × depth-3 tree is expensive.
   Delegate only genuinely independent high-value branches.
6. **Not checking `state.json` after a contestable verdict** — a prior run may have left
   a BLOCKER or a nearly-successful frontier that a fresh context would miss.

## Verification checklist

- [ ] Read all core `.research/` files before acting.
- [ ] Resumed the highest-priority *pending* clue (script-sorted), not a random one.
- [ ] Every source added to `sources.jsonl` with type + accessed date.
- [ ] Every claim linked to a source in `claims.jsonl`.
- [ ] every query logged in `search-log.jsonl`.
- [ ] Dead branches recorded in `dead-ends.jsonl` with reopen conditions.
- [ ] Frontier re-prioritised after the round.
- [ ] State + checkpoint written before finishing; exact next action recorded.
- [ ] Terminal verdict is one of the five states, honestly applied.

## One-shot recipe: seed a campaign

```
# scaffold a project (creates HERMES.md + full .research/ tree from templates)
python3 ~/.hermes/skills/research/endless-research/scripts/research_project.py \
  init ~/research/<project> --objective "<the exact finding>" \
  --success "<what proves it found>" --failure "<what proves it is not found>"

# check queue + state any time
python3 .../research_project.py status ~/research/<project>

# then point a durable cron campaign at it (see scripts/research_project.py --help
# and the cron section in references/protocol.md for the exact cronjob invocation)
```
