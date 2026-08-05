# Changelog

All notable changes to **hermes-endless-research** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/) (with `v0.x` pre-1.0 semantics).

## [Unreleased]

## [0.2.5] — 2026-08-05
### Fixed (real Hermes cron integration + global worker contract)
- **Per-campaign cron gate (fixes the documented command not working).** Verified in
  `cron/scheduler.py` that a cron `script=` pre-run runs in the SCRIPT'S OWN directory
  (`_script_cwd = workdir or str(path.parent)`, and the wake-gate calls it with no
  workdir), so a gate that relies on `cwd` sees `~/.hermes/scripts/` and wrongly emits
  `{"wakeAgent": false}` — the research agent never starts. New
  `endless-research cron-wrapper <project> [--name N]` generates a per-campaign wrapper
  with the ABSOLUTE project path, to be attached as `--script "<name>-lease-gate.sh"`.
  README + research cron prompt updated to use it.
- **Global one-worker contract on mutations.** Runtime mutations (`source`, `claim`,
  `frontier`, `edge`, `search-log`, `dead-end`, `criterion`, `contradiction`, `report`,
  `resignal`, `checkpoint`, `tick`) now enforce token ownership: when a LIVE lease exists,
  the mutation must present `--run-id <token>` (matches the lease) or `--operator-override`
  (deliberate emergency bypass); a missing/wrong run_id is REFUSED (exit 3). With no live
  lease, manual/administrative writes are permitted. This closes the "ungated manual /
  independent worker" gap — the lease is now a system-wide ownership contract, not just a
  scheduling convention.
- **Locked the remaining writers.** `clarify` and `init` now hold the project lock while
  writing `objective.md` / scaffold files.
- **Graph validates BOTH endpoints.** `graph` now audits dangling `to_id` as well as
  `from_id` (catches damaged/imported/legacy graph data).
- **Tests:** new `test_v25.py` (9 tests: cron-wrapper path/cwd independence, mutation
  ownership refusal/allowed/override, graph to_id + clean audit). Suite: 75 → **84 tests**,
  all green (incl. Python 3.9 parse check).

## [0.2.4] — 2026-08-05
### Fixed (correct one-worker-per-campaign lease + complete mutation locking)
The audit found the v0.2.3 lease was PID-based (gate PID dies before the agent runs),
non-atomic (read-decide-write not under the flock), and RELEASE over-deleted
(unconditional unlink + allowed anonymous release). All corrected:

- **Token-based lease** (`secrets.run_id`); no reliance on the pre-run gate's PID, because
  that process exits before the Hermes agent session starts. Only TTL/heartbeat decide
  liveness, so a crashed/exited gate or agent is handled by expiry, not process-life.
- **Atomic acquisition.** CHECK holds the project flock while it reads state + lease and
  writes the new lease, so two gates starting at the same instant cannot both wake an
  agent (verified: 6 concurrent CHECKs -> exactly 1 owner). Matches the design's request
  to acquire the lock before creating the lease.
- **Strict RELEASE ownership.** `RELEASE` requires `--run-id` (no anonymous release) and
  only deletes the lease if the run_id matches; wrong/unknown run_id is refused and the
  lease is PRESERVED. (Fixes the "deleted before ownership check" and the
  "run_id None authorises deletion" bugs.)
- **New `HEARTBEAT` mode.** The research agent refreshes `heartbeat_at`/`expires_at` for
  its run_id during a long session so it does not lose ownership at TTL expiry.
- **Locked every remaining mutation.** New locked CLI commands cover the files the cron
  prompt previously let the agent edit directly: `search-log add`, `dead-end add`,
  `criterion add/update`, `contradiction add/resolve`, `report write`. The research prompt
  now routes ALL shared-state writes through locked CLI and forbids hand-editing any file
  under `.research/`.
- **Tests:** rewritten lease suite (13) covering simultaneous-acquire, gate-PID-dies,
  anonymous/wrong-run-id release refusal, heartbeat renewal; plus locked-mutation suite
  (6). Suite: 63 → **75 tests**, all green (incl. Python 3.9 parse check).

## [0.2.3] — 2026-08-05
### Added (one worker per campaign — cron pre-run lease gate)
- **`scripts/campaign-lease-gate.py`** — a Hermes cron `script=` pre-run gate that emits
  `{"wakeAgent": false}` to SKIP the agent run entirely (zero model tokens) when:
  - another **live worker** holds the campaign lease (recent heartbeat + alive PID), OR
  - the campaign state is **DORMANT / SUCCESS / EXHAUSTED**, OR
  - `state.json` is unreadable (conservative safety).
  Otherwise it acquires a recoverable lease (`run_id`, `pid`, `heartbeat_at`,
  `expires_at`) under `<project>/.research/.worker-lease.json`. Expired leases with a
  dead PID are treated as stale and recovered (a crash can't wedge the campaign).
  A `RELEASE` mode clears the lease at the end of a tick.
- This directly implements the **one-worker-per-campaign** guarantee. It complements
  (not replaces) Hermes' scheduler in-flight guard (`_running_job_ids`, which only
  blocks the SAME cron job); the lease also blocks manual runs, a second cron job,
  another profile, or a separately launched script.
- **Tests:** `test_campaign_lease.py` (7 tests) — active acquires lease, dormant/success
  skip, live-worker blocks concurrent, expired-lease recovery, release clears, missing
  state skips. Suite: 56 → **63 tests**.

### Changed
- `research-cron-prompt.md` now instructs the agent to **RELEASE its worker lease** at
  the end of each tick so the next cron fire can immediately re-acquire it.

## [0.2.2] — 2026-08-05
### Added (Design 2: lock every shared-state write)
- **Locked mutation primitives.** New `source add`, `claim add`, `frontier add`,
  `frontier update` CLI commands — each holds the project flock for the write, so a
  shared-state mutation is a single serialized critical section even though the agent's
  browser/web tool calls are not wrapped.
- `resignal` and `checkpoint` now also hold the project lock for their `state.json` /
  checkpoint-file writes (the cron pause/resume subprocess runs outside the lock).
- This closes the "full-session lock" gap: instead of trying to wrap the whole Hermes
  agent session in one flock (which `subprocess.run(shell=True)` cannot do for web tool
  calls), DESIGN 2 guarantees every mutation of shared state is flock-guarded. The
  engine's own scheduler in-flight guard + these per-mutation locks provide the safety.
- **Tests:** new `test_locked_mutations.py` (5 tests) — locked source/claim/frontier
  adds, concurrent serialization, lock-contention exit code. Suite: 51 → **56 tests**.

### Changed
- `research-cron-prompt.md` rewritten to Design 2: the agent must route ALL file/state
  writes through locked CLI commands and never hand-edit the `.research/*.jsonl` /
  state.json files. The prompt no longer claims a single whole-session flock.

## [0.2.1] — 2026-08-05

### Added
- **Atomic node+edge creation under the project lock** — creating a question/person
  node and its relationship edge is an all-or-nothing operation, so the graph can
  never hold a dangling reference. The old `Q-`/`P-` referential-integrity exemption is
  removed.
- **Dedicated automated tests** for the atomic-edge contract (rollback, concurrent
  creation, both-endpoints-missing, existing/question-id reuse, `supports`→claim rule,
  graph validation). Suite: 38 → **51 tests**. (The earlier commit only shipped the
  script change; now it is protected by tests.)

### Changed
- **Conservative URL canonicalisation.** `canonicalize_url` now ALWAYS strips only
  `utm_*`, `fbclid`, `gclid`; it does NOT strip `www.` or `ref`/`source`/`from`/`share`
  by default (those can be semantically meaningful on some sites and must not cause
  different pages to collide). Opt-in flags `strip_www` / `conditional_params` are
  available for verified duplicate detection, and provenance keeps the original URL.

### Removed
- `templates/cron-prompt.md` (combined research + watcher) → **split** into
  `templates/research-cron-prompt.md` and `templates/dormant-watcher-prompt.md` so a job
  can never accidentally receive both instructions. README + protocol updated to the
  split prompts.

### Fixed
- v0.2.0 tag predated the atomic-edge commit. **v0.2.1** now carries the atomic-edge +
  conservative-canonicalisation changes so the release tarball reflects `main`.

## [0.2.0] — 2026-08-05

### Added (data-layer upgrade — keeps the whole engine; no redesign/Neo4j)
- **Explicit evidence graph** (`edges.jsonl`) — sources, claims, clues, questions,
  people, dead-ends and contradictions are graph NODES connected by typed
  RELATIONSHIPS (`links_to`, `cites`, `supports`, `contradicts`, `answers`,
  `derived_from`, `duplicate_of`, `archived_version_of`, ...).
- **`edge` command** — appends a typed edge with **machine-enforced referential
  integrity** (from/to must resolve to real nodes) and domain rules (e.g. `supports`
  requires a claim as `to_id`).
- **`graph` command** — summarises the evidence graph: nodes by kind, edges by
  relationship, and dangling-reference warnings.
- **URL intelligence** — `canonicalize_url` (strips fragments/tracking params,
  normalises `www.`, sorts params), `content_fingerprint` (sha256 for duplicate
  detection), and `inspect` (canonical form + scope before fetching).
- **Scope rules** — `scope.json`: budget-based crawl control (`follow_internal/external`,
  allowed/blocked domains, `max_pages_per_domain`, `relevance_budget`, `page_budget`,
  allow archives/repos/documents). No strict `max_depth`; avoids blind crawling.
- **Objective clarifier** — `clarify <dir> <url> --goal "..."`: a short, smart objective
  compiler. Clear goal → compiles immediately; vague goal → infers defaults + records
  assumptions; materially ambiguous → asks the essential 1-4 questions.

### Changed
- `init` now scaffolds `edges.jsonl`, `scope.json`, `questions.jsonl`, `people.jsonl`.
- CLI now has 11 commands; SKILL.md/protocol.md document the graph model.

### Tests
- Added `test_evidence_graph.py`, `test_url_intelligence.py`, `test_clarifier.py`.
  Suite expanded 19 → **38 tests**, all passing.

## [0.1.0-beta] — 2026-08-05

### Added
- **Persistent disk-backed research brain** — a `.research/` directory (objective,
  state, frontier, criteria, claims, sources, search-log, dead-ends, contradictions,
  unresolved, checkpoints) that survives restarts, model changes, and context walls.
- **Deterministic CLI** (`research_project.py`): `init`, `status`, `resignal`, `reset`,
  `checkpoint`, `tick`, `verify_success`.
- **Six-state machine**: `CONTINUE`, `CHECKPOINT`, `BLOCKED`, `SUCCESS`, `EXHAUSTED`,
  and `DORMANT` (resumable; parks the research job until a watcher re-awakens it).
- **Atomic project lock** — `tick` holds an exclusive `flock` on `<proj>/.research/.lock`
  so two ticks can never mutate state simultaneously (exit code 2 = locked/skipped).
- **Deterministic SUCCESS gate** — `verify_success` blocks `resignal SUCCESS` unless
  every acceptance criterion in `criteria.jsonl` is met (or excepted) AND the evidence
  trail checks out (source IDs resolve, primary evidence present, corroboration met,
  no critical unresolved contradiction, substantive final-report).
- **Two-job cron model** — an aggressive research job (fires on
  CONTINUE/CHECKPOINT/BLOCKED, auto-pauses on DORMANT/SUCCESS/EXHAUSTED) plus a cheap
  daily dormant watcher that probes DORMANT campaigns for genuinely new evidence and
  re-awakens them.
- **Installer / uninstaller / verify-install** scripts for Hermes on Ubuntu/VPS.
- **Docker option** layering the skill over the official Hermes image with persistent
  `~/.hermes` and `/research` volumes.
- **Test suite** (pytest): state machine, locking, checkpoint, success gate.
- **CI**: test workflow; release workflow producing a tarball prerelease.
- **Templates**: `HERMES.md`, `objective.md`, `cron-prompt.md`; `.env.example`.

### Notes
- Experimental release. Source interpretation and anti-fabrication remain
  model-dependent; important conclusions need human review.
- Public repo stores code only — never personal research projects or credentials.
