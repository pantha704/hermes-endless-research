# Endless Research — Full Protocol Reference

Deep spec for the durable research campaign. The SKILL.md is the operating playbook;
this reference holds the machinery details: cron wiring, delegation rules, priority
formula, and state machine.

## v0.2.0 — Explicit evidence graph

The research world is a graph (sources, claims, clues, questions, entities, dead-ends,
contradictions). A flat list tells you *what was found*; a graph tells you *where it
came from, what it supports, what contradicts it, and how things connect*.

### Nodes

| Prefix | File | Kind |
|--------|------|------|
| `SRC-*` | `sources.jsonl` | Source (URL, title, type, accessed) |
| `CLM-*` | `claims.jsonl` | Claim (status, confidence, sources) |
| `CLUE-*` | `frontier.jsonl` | Clue / frontier item |
| `Q-*` | `questions.jsonl` | Open question |
| `P-*` | `people.jsonl` | Person / organisation / entity |
| `DE-*` | `dead-ends.jsonl` | Dead end (with reopen conditions) |
| `X-*` | `contradictions.jsonl` | Contradiction |

### Edges — `edges.jsonl`

An edge is an explicit typed relationship between two nodes. Recorded with `edge`:

```json
{
  "edge_id": "EDGE-<uuid>",
  "from_id": "SRC-0012",
  "relationship": "cites",
  "to_id": "SRC-0047",
  "context": "Listed as the original technical report",
  "discovered_at": "2026-08-05T12:00:00Z"
}
```

**Allowed relationships:** `links_to`, `cites`, `authored_by`, `published_by`,
`supports`, `contradicts`, `answers`, `depends_on`, `derived_from`, `investigates`,
`duplicate_of`, `archived_version_of`, `blocks`.

**Machine-enforced referential integrity:** `edge` rejects any `from_id`/`to_id` that
does not resolve to a real node in the project files (unless it is a fresh `Q-`/`P-`
id), and enforces domain rules (e.g. `supports`/`contradicts` require a `CLM` as
`to_id`; `answers` requires a `CLM` as `from_id`).

**View the graph:** `graph <dir>` prints nodes-by-kind and edges-by-relationship, and
optionally warns about dangling references. This is the machine-checkable view of the
evidence graph.

### URL intelligence (no blind crawling)

- **`inspect <dir> <url>`** — shows the canonical URL, a content fingerprint, and the
  campaign scope rules, *before any fetching*. Run this before deciding to dig.
- **URL canonicalisation** (`canonicalize_url`) — **conservative by default**:
  always strips only `utm_*`, `fbclid`, `gclid`; strips the fragment; sorts remaining
  params. It does NOT strip `www.` or `ref`/`source`/`from`/`share` by default (those
  can be semantically meaningful on some sites, so stripping them could merge distinct
  pages). Opt into stronger collapsing (`strip_www`, `conditional_params`) only when
  you have verified the domain's semantics. Always keep the originally-encountered URL
  (`url`) alongside the canonical form (`canonical_url`) for provenance.
- **Content fingerprinting** (`content_fingerprint`): a `sha256:` hash of whitespace-
  normalised text for duplicate detection (`duplicate_of` / `archived_version_of` edges).

### Scope rules — `scope.json`

Budget-based control, NOT a strict max-depth (a primary source may be six links away).
Per campaign: `follow_internal_links`, `follow_external_links`, `allowed_domains`,
`blocked_domains`, `max_pages_per_domain`, `relevance_budget`, `page_budget`,
`allow_archives`, `allow_repositories`, `allow_documents`.

The engine is a **targeted researcher, not a crawler**: it understands why each link
matters, scores links against the objective, and follows only high-value branches. It
records both "discovered" and "investigated" links — a link can be discovered without
being worth investigating. It avoids calendric-infinite, pagination, filter, tag,
login-redirect, tracking, duplicate/mobile, legal/privacy, auto-generated, and circular
pages. Use diminishing-information-gain + relevance budget + page budget rather than
`max_depth`.

### Objective clarifier — `clarify <dir> <url> [--goal TXT]`

A short, intelligent **objective compiler** that turns URL + vague intent into a
measurable research contract — without over-interrupting:

| Input | Behaviour |
|-------|-----------|
| **Clear** goal (e.g. "how it works + credible") | Compile the research contract now, no questions |
| **Vague** goal (e.g. "learn everything important") | Infer sensible defaults (purpose, architecture, creators, verify claims, limitations, trace to primary), record assumptions, start |
| **Materially ambiguous** goal (empty / "tell me about") | Ask only the essential questions (what to understand / strict-vs-external links / evidence standard / what counts as success) |

`--mode auto|clear|vague|ambiguous` forces behaviour. `--no-write` avoids overwriting
`objective.md`. The clarifier canonicalises the seed URL and writes the compiled
contract (Seed URL + Goal aspects + Scope + Success-conditions placeholder) to
`objective.md`.

## State machine

```
            ┌────────────────────────────────────────────┐
            │                 START (init)               │
            └───────────────┬────────────────────────────┘
                            ▼
                       CONTINUE ────► (dig, verify) ──────────► SUCCESS (done)
                            ▲             │                         ▲
                            │             │ write checkpoint         │
                            │             ▼                         │
                            │        CHECKPOINT ──(later run)───────┘
                            │             │
                            │  external obstacle     clues spent,
                            │                         topic alive
                            ▼                         │
                        BLOCKED          ┌────────────▼───────────┐
                        (obstacle +      │        DORMANT          │◄┐
                         detour)         │ (resumable; new info    │ │
                                         │  will reopen)───────────┘ │
                                         │ resignal CONTINUE         │
                                         └────────────┬───────────┘  │
                                                      │              │
                                                      ▼              │
                                                   EXHAUSTED          │
                                                   (topic truly       │
                                                    dead / terminal)  │
                                                      │   new info    │
                                                      └───────────────┘
```

- **CONTINUE** = more to dig. Loop.
- **CHECKPOINT** = must stop now; resume later. Never an answer.
- **BLOCKED** = external wall; record it and the detour, don't spin.
- **DORMANT** = clues all spent but the topic may yield new information later
  (developing story, unreleased source, awaited filing). Resumable: `resignal <dir>
  CONTINUE` when new info/terminology/sources emerge. Keeps the campaign alive,
  unlike EXHAUSTED.
- **EXHAUSTED** = all reasonable strategies genuinely tried, objective unmet, and no
  future information is reasonably expected. Report precisely what's missing.
  **Not** SUCCESS.
- **SUCCESS** = acceptance criteria met with traceable, verified evidence. Terminal.

Set/reset via `research_project.py resignal <dir> <STATE>`.

## Priority score (0–100, moving)

| Component | Weight | Meaning |
|---|---|---|
| relevance | 0.30 | on-objective |
| primary_source_likelihood | 0.20 | will it reach the original? |
| info_gain | 0.20 | new evidence expected |
| resolves_uncertainty | 0.15 | closes an open question |
| novelty | 0.10 | different from what we have |
| ease | 0.05 | investigation cost (higher = easier) |

`score = 0.30r + 0.20p + 0.20g + 0.15u + 0.10n + 0.05e`

Decay/re-rank signals:
- **Down-rank:** repeated info, SEO pages, unsourced summaries, circular citations,
  content farms, already-fully-explored sources.
- **Up-rank:** primary documents, papers/datasets, source code + commit history,
  official filings, archived pages, original interviews, technical docs, and anything
  that contradicts the current conclusion.

The script computes the sort deterministically (`status`). Do not hand-sort.

## Frontier entry shape

```json
{
  "clue_id": "CLUE-0001",
  "description": "…",
  "parent": "SRC-0001",         // parent source or clue
  "depth": 1,
  "relevance": 8, "primary_source_likelihood": 7, "info_gain": 8,
  "resolves_uncertainty": 6, "novelty": 7, "ease": 5,
  "status": "pending",          // pending | done_proven | done_closed | dead
  "attempts": 0
}
```

`status` governs lifecycle:
- `pending` → available to pick.
- `done_proven` → verified, evidence in claims.jsonl.
- `done_closed` → investigated, no payoff.
- `dead` → recorded in dead-ends.jsonl with reopen conditions.

## Source / claim tracking

- `sources.jsonl` — one record per useful source: `{src_id, url, title, author, org,
  publisher, accessed, type: primary|secondary|tertiary, discovered_from}`.
- `claims.jsonl` — one record per claim: `{claim_id, text, source_ids:[], confidence:
  verified|strong_conclusion|probable_inference|speculation|unresolved,
  contradiction_ids:[]}`.
- `contradictions.jsonl` — conflicting evidence with which sources support each side.
- `dead-ends.jsonl` — `{branch, attempted, queries, sources, why_failed,
  may_reopen, reopen_conditions}`.
- `search-log.jsonl` — `{ts, query, strategy, results, outcome}`. Also the anti-loop
  record: never re-run a verbatim dead query.

## Backward-tracing patterns (go to the original)

article → cited report → dataset → methodology → paper → references → foundational
work → source code/commits/diffs → issues/PRs → filings/specifications → archived
versions → original speaker/document. Do not stop at the first page that merely
repeats a claim.

## Delegation rules

- Parent = research director, owns the objective.
- Use `orchestrator` children for broad branches with several subquestions; `leaf`
  workers for individual clues.
- Only delegate genuinely independent, high-value branches. A 5-wide × depth-3 tree
  multiplies real tokens quickly.
- Every delegated task MUST receive: objective, exact branch goal, known evidence,
  prior attempts/dead-ends, expected output format, verification standard.
- Every worker MUST return: findings, exact source URLs, claims↔source links,
  source-quality assessment, contradictions, new clues, failed approaches, next
  actions, confidence.
- Parent validates and merges; never accept a child's summary on faith. Verify its
  cited evidence yourself.

## Concurrency: how overlap is prevented (defense-in-depth)

Two independent layers stop two ticks from mutating the same project:

1. **Hermes cron scheduler (native).** Verified in `cron/scheduler.py`: an in-process
   `_running_job_ids` set guarded by a lock. At dispatch (`_submit_with_guard`), if a
   job id is already in the set, the run is logged `"<name> already running — skipping"`
   and **not started**. The id is only released when that run fully finishes (its
   `finally` block), so a second due fire can never overlap the first. A `.tick.lock`
   file additionally guarantees only one scheduler sweep runs at a time across
   processes. Interval jobs also schedule the next fire off completion, so the 2h
   cadence is from-finish, not wall-clock.
2. **Per-mutation owned project lock (atomic).** Every shared-state write goes through
   `_owned_project_lock(args)`: it acquires the exclusive `flock` on
   `<proj>/.research/.lock` and validates lease ownership INSIDE the critical section,
   then performs the mutation (and mints any automatic id) within the same lock. This
   closes the check-then-lock race — a command can never observe "no lease" and then
   write after a cron gate has created one; the lease decision and the mutation are one
   atomic step. Covered operations: `source add`, `claim add`, `frontier add/update`,
   `edge`, `search-log add`, `dead-end add`, `criterion add/update`,
   `contradiction add/resolve`, `report write`, `resignal`, `checkpoint`, `tick`, `reset`,
   `clarify`. A locked command that finds the project already locked exits code 2; one
   that lacks ownership under a live lease exits code 3. The agent's browsing (web search /
   extraction) is not itself wrapped, but every file under `.research/` is written by a
   serialized, ownership-checked critical section.

3. **Worker lease (one worker per campaign).** A cron PRE-RUN script
   (`scripts/campaign-lease-gate.py`, attached to the research job as `script=`) decides
   whether to spawn an agent at all. It is TOKEN-based (a random `run_id`), NOT PID-based
   — the gate process exits before the Hermes session runs, so PID liveness cannot
   represent the agent. CHECK acquires the project flock and, reading-deciding-writing
   under one lock, emits `{"wakeAgent": false}` (skip, zero model tokens) when a live
   lease (within TTL) exists or the state is DORMANT/SUCCESS/EXHAUSTED/missing; otherwise
   it writes a fresh lease and emits `{"run_id": ...}` (wake). The agent calls
   `HEARTBEAT <proj> --run-id <R>` to renew ownership past TTL, and releases ownership at
   the end via `RELEASE <proj> --run-id <R>` (STRICT: only the matching run_id clears it;
   there is no anonymous release). An expired lease is automatically recoverable after a
   crash. This closes a gap the Hermes scheduler in-flight guard (which only blocks the
   SAME cron job id) cannot: a manual run, a second cron job, another profile, or a
   separately launched script.

   Lease safety is FAIL-CLOSED: an absent lease allows manual writes; a readable but
   expired lease allows; a readable live lease requires the matching `--run-id`;
   a lease that is PRESENT but unreadable/corrupt REFUSES all mutations (unless
   `--operator-override`), because we cannot prove the worker is gone. The gate writes
   the lease atomically (temp file + `os.replace`) so a crash cannot leave a truncated
   lease. Explicit `--id` must be unique (duplicate ids are rejected with exit 4).

4. **Identifier schema is type-aware.** Frontier (`CLUE-*`) and dead-end (`DE-*`) records
   store their id under `clue_id`; all other node types use `id`. `_mint_id` and
   `_node_exists` respect `NODE_ID_KEYS = {"CLUE": "clue_id", "DE": "clue_id"}` so
   automatic ID allocation is collision-free and CLUE/DE nodes resolve in the evidence
   graph (no false "unknown node" / dangling-endpoint errors).

Layer 1 is the primary overlap guard; layers 2-4 are belt-and-suspenders.

## Cron campaign wiring — TWO jobs (the "until the end of the world" heartbeat)

Two separate cron jobs split the work so the aggressive digger parks when it matters
and a cheap watcher keeps developing topics alive. Each run is a fresh session with
`skip_memory=True` and no chat context, so prompts MUST be self-contained and rely
entirely on the on-disk `.research/` state + HERMES.md.

### Job 1 — Research job (aggressive digger)

- **Cadence:** fast (best `every 2h`, from-finish). The heavy lifter.
- **Fires ONLY during active digging states** `CONTINUE` / `CHECKPOINT` / `BLOCKED`.
- **Auto-pauses** (via `resignal <dir> DORMANT|SUCCESS|EXHAUSTED --cron <research-job-id>`)
  when the campaign enters `DORMANT` (clues spent, parked), `SUCCESS`, or `EXHAUSTED`.
  When re-entering `CONTINUE`/`CHECKPOINT`/`BLOCKED` it re-resumes. This is handled
  deterministically by the CLI, not by the model.
- Each tick = one research dig under the atomic lock (see `tick`).

### Job 2 — Dormant watcher (cheap, daily)

- **Cadence:** slow (`0 12 * * *` = daily noon; or weekly). Very low cost.
- **Scope:** ONLY detects when genuinely new evidence has emerged for a DORMANT
  campaign. It NEVER runs the full research campaign.
- **Behavior per tick:**
  1. Read `state.json`. If NOT `DORMANT` → output `[SILENT]`, do nothing.
  2. If `DORMANT`: do ONE bounded probe (≤3 targeted searches) for material new
     evidence using fresh terminology/sources that were unavailable when it went dormant.
  3. If material new evidence found → `resignal <dir> CONTINUE --note "..." --cron
     <research-job-id>` (which re-resumes the research job) and report what/where.
  4. If nothing material → stay DORMANT, `[SILENT]`.
- Gate the watcher too: never fabricate a page; if not DORMANT, output ONLY `[SILENT]`.

### The SUCCESS gate (deterministic, blocks premature SUCCESS)

`research_project.py verify_success <dir>` is a hard gate run automatically whenever
`resignal <dir> SUCCESS` is issued. It blocks SUCCESS (exit 1) unless **all** pass:

1. Every acceptance criterion in `criteria.jsonl` is marked `met` OR has an explicit
   `exception`.
2. Every `evidence_source_ids` on a `met` criterion resolves to a real source in
   `sources.jsonl`.
3. `primary_hard` criteria are backed by a primary (type starts with `primary`)
   source, OR `exception_primary` is set.
4. `corroboration_required` criteria have ≥ `--min-corroboration` (default 2)
   independent sources, OR `exception_corroboration` is set.
5. No `critical` contradiction in `contradictions.jsonl` is `resolved:false`.
6. `final-report.md` exists, is substantive (>300 chars), and is not the template stub.

`--force` bypasses the gate explicitly (only if you accept the risk). This reduces the
risk of a model prematurely issuing `resignal SUCCESS`.

### Cron invariants to respect

- cron sessions are fresh (no chat context) and `skip_memory=True` — self-contained prompt,
  all state from disk.
- ~3-minute hard interrupt per run: design each tick to checkpoint quickly and not
  overreach; use delegation sparingly inside a tick.
- `context_from` can chain a "status watcher" job → the digger, but usually unnecessary.
- To watch for SUCCESS cheaply, a `no_agent=True` watchdog script can poll
  `.research/state.json` and only ping when it flips to SUCCESS/EXHAUSTED/BLOCKED.

```
cronjob action=create
  schedule="every <N>h"        # your cadence; e.g. every 2h
  name="<project> research"
  workdir="<abs path to project>"   # loads HERMES.md as the sticky instruction
  skills=["endless-research"]       # loads the protocol skill
  prompt="<see below>"
  deliver="origin"             # or 'local' for silent, or 'all' for fan-out
```

Recommendations:
- `deliver` = `origin` so progress lands back in your chat; use `local` if you want
  it fully silent until SUCCESS. For a fire-and-forget failsafe, `local` + check logs.
- Cadence: aggressive (30m–2h) for a hard hunt, slower (6–24h) for long-horizon digs.
- The cron run's job is: load state → resume highest-priority pending clue → dig to a
  natural stopping point → checkpoint → set state → output a short delivery. It is
  NOT expected to finish the whole objective in one tick.

Self-contained prompt template (adjust `<PLACEHOLDERS>`):

```
Run one research tick on this project per the HERMES.md protocol and the
endless-research skill. Objective: .research/objective.md.

0. CONCURRENCY (Design 2): perform EVERY shared-state write through a lock-protected
   CLI command. NEVER edit any .research/*.jsonl or state.json with a text editor / raw
   write. Locked write primitives (each acquires <proj>/.research/.lock):
     research_project.py source   add <dir> --url U [--title T] ...
     research_project.py claim    add <dir> --claim "..." --sources SRC-1,SRC-2 ...
     research_project.py frontier add <dir> --description "..." [--parent SRC-1]
     research_project.py frontier update <dir> <CLUE-ID> [--status ...] [--attempt]
     research_project.py edge <dir> <from> <relationship> <to> [--context "..."]
     research_project.py resignal <dir> <STATE> [--note "..."] [--cron <id>]
     research_project.py checkpoint <dir> [--note "..."]
   Browsing (web search/extract) does NOT need wrapping. If a locked command exits
   code 2, the project is locked by a prior run — skip that write / stop cleanly.

1. Read .research/state.json. If current_state is SUCCESS, EXHAUSTED, or DORMANT,
   STOP and report that verdict — do not continue digging. (SUCCESS/EXHAUSTED are
   terminal: the campaign has finished. DORMANT means clues are spent but the topic
   may reopen later; it stays silent/armed until resignalled to CONTINUE.)
2. Resume the highest-priority PENDING frontier clue (use
   `python3 .../research_project.py status .` for the deterministic sort).
3. Dig with search/extract; register sources (`source add`), create edges (`edge`),
   record claims (`claim add`). If you find the answer and it satisfies the objective's
   SUCCESS criteria with traceable evidence and no material contradiction, run
   `verify_success .` (must be UNBLOCKED), then `resignal . SUCCESS --cron <id>` and
   write .research/final-report.md.
4. Otherwise, `frontier add` quality-gated new clues, log queries, `checkpoint .`, and
   leave state as CONTINUE (or BLOCKED/DORMANT/EXHAUSTED only if genuinely appropriate)
   via `resignal . <STATE>`.
5. Deliver a short progress note: current state, what was checked this tick, the new
   frontier summary, and the next clue to investigate.

Rules: never fabricate a citation/URL. Multiple pages citing the same original are not
independent confirmation. Never declare SUCCESS just because you searched a lot. Never
claim "engine success == research success". Do not edit or schedule other cron jobs.
```

### Cron invariants to respect

- cron sessions are fresh (no chat context) and `skip_memory=True` — self-contained prompt,
  all state from disk.
- ~3-minute hard interrupt per run: design each tick to checkpoint quickly and not
  overreach; use delegation sparingly inside a tick.
- `context_from` can chain a "status watcher" job → the digger, but usually unnecessary.
- To watch for SUCCESS cheaply, a `no_agent=True` watchdog script can poll
  `.research/state.json` and only ping when it flips to SUCCESS/EXHAUSTED/BLOCKED.
