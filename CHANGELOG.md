# Changelog

All notable changes to **hermes-endless-research** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/), and this project
adheres to [Semantic Versioning](https://semver.org/) (with `v0.x` pre-1.0 semantics).

## [Unreleased]

## [0.2.16] — 2026-08-06
### Fixed (bool `rounds_completed` rejected in audit-event schema)
Tiny schema-precision fix: `bool` is a subclass of `int` in Python, so a started event with
`rounds_completed: true` slipped past the `int`-or-`null` check (unlike the completed-event
counters, which already reject booleans). `_history_event_schema_ok()` now explicitly
rejects a `bool` for `rounds_completed`, while still accepting a genuine integer `0`. The
existing schema tests were extended with both the rejection case and a `rounds_completed: 0`
positive case. Suite: 150, all green (incl. 3.9). Normal cron path unaffected.

## [0.2.15] — 2026-08-06
### Fixed (semantic audit-event schema validation — mirrors the v0.2.11 lease validator)
- **`_history_event_schema_ok()`** validates each parsed journal row is a well-formed
  audit event before it is trusted (analogous to `_lease_schema_ok`): must be a dict,
  `schema_version` == AUDIT_SCHEMA, `event` ∈ {started, completed, aborted}, `campaign_run_id`
  a string with `RUN-` prefix, `timestamp` a non-empty string, plus typed per-event fields
  (sources/claims/edges ints, checkpoint/reason/session/cron strings, started
  rounds_completed int-or-null). `_load_history_strict` now classifies any row failing this
  schema as **corrupt** (line recorded, skipped) rather than parsing a `null`, `[]`, or
  field-missing object that would later crash on `.get()` or be silently mis-linked.
- **Fail-closed is now semantic, not just syntactic**: `null`, `[]`, wrong schema_version,
  unknown event, missing `campaign_run_id`, and bad field types all block `run start` /
  `run finish` / `run abort` (exit **8**) and are reported in `corrupt_history_lines` /
  `journal_integrity`.
- **Tests:** new `test_v215_audit_schema.py` (5): schema valid/reject units, non-object rows
  reported as corrupt, run-start fail-closed on semantic corruption, and a dedicated
  `run abort` corrupt-journal refusal (the v0.2.14 test gap). One v0.2.13 duplicate-terminal
  test updated to append a schema-valid aborted row. Suite: 145 → **150**, all green (incl. 3.9).

## [0.2.14] — 2026-08-06
### Fixed (audit journal: fail-closed on corruption, locked audit, strict metadata)
- **Lifecycle commands fail closed on a corrupt journal.** `run start` / `run finish` /
  `run abort` now refuse (exit **8**) if `run-history.jsonl` has any unparseable line,
  instead of proceeding on incomplete history (a damaged line could conceal an earlier
  start/terminal event). They require deliberate repair/removal before any mutation.
- **`run audit` takes the project lock** for a consistent snapshot, so a concurrent append
  cannot expose a partial final line and trigger a transient false-corruption report.
- **Strict metadata matching.** When the `started` event recorded a non-empty session or
  cron job id, the terminal event must supply a matching one — an empty/missing value is a
  mismatch (session → exit 5, cron → exit 6), not silently allowed. The normal cron path
  (Hermes supplies `$HERMES_SESSION_ID`; the prompt supplies `--cron-job-id`) is correct.
- **Tests:** new `test_v214_audit_failclosed.py` (5): start/finish fail-closed on a corrupt
  journal, strict session/cron match (empty terminal value refused), locked clean-chain
  audit. Suite: 140 → **145**, all green (incl. 3.9).

## [0.2.13] — 2026-08-06
### Fixed (audit journal: atomicity, strict state machine, packaging)
The audit found six gaps in the v0.2.12 audit subsystem. All closed:

- **Atomic duplicate-start.** `run start` now reads the journal and validates the lifecycle
  state INSIDE `_owned_project_lock` (read → validate → append under one lock), closing the
  two-concurrent-start race. The broken `not ev.get("terminal")` check was replaced with a
  real search for an existing `started` event.
- **`--session-id` override precedence.** Now `args.session_id or _hermes_session_id()`
  (explicit argument wins over the env var), per documented behavior.
- **Strict start→terminal state machine.** `run finish`/`run abort` refuse when: there is no
  matching `started` event (exit 4), the session id mismatches (exit 5), the cron job id
  mismatches (exit 6), or the run already has a terminal event (exit 7). `run audit` flags a
  run with BOTH completed and aborted as a duplicate terminal, not just same-type dupes.
- **Checkpoint cron-job linkage.** `checkpoint` parser gained `--cron-job-id`, so checkpoints
  embed the cron job id (previously blank).
- **Corrupt-journal detection.** `run audit` now reports `corrupt_history_lines` and
  `journal_integrity: "failed"` rather than silently discarding unparseable lines. Audit
  appends are fsynced (temp-safe crash durability, like the lease writer).
- **Packaged cron prompt updated.** `templates/research-cron-prompt.md` now implements the
  full audit lifecycle (`run start` → research → `checkpoint --cron-job-id` → `run finish`
  → release); a fresh install now uses the journal automatically.
- **Tests:** new `test_v213_audit_state.py` (8): no-start/session/cron/already-terminal
  refusals, completed+aborted duplicate detection, `--session-id` override, corrupt-line
  reporting, checkpoint cron-job id. Suite: 132 → **140**, all green (incl. 3.9).

## [0.2.12] — 2026-08-06
### Added (per-run audit journal — run-history.jsonl + `run` CLI)
- **Append-only audit journal** `.research/run-history.jsonl`. `run start` / `run finish` /
  `run abort` record lifecycle events carrying `campaign_run_id`, `hermes_session_id`
  (auto-captured from `$HERMES_SESSION_ID`, overridable via `--session-id`), and
  `cron_job_id`. `run audit [--json]` reconciles the journal (crashed start-only runs,
  duplicate terminal events, counts). All writes go through `_owned_project_lock`.
- **Checkpoints enriched** with `campaign_run_id`, `hermes_session_id`, and `cron_job_id`.
- **Duplicate-start guard**: a second `run start` for an un-finished campaign_run_id is
  refused (exit 4). Ownership is still enforced on every audit write (wrong `--run-id` →
  exit 3).
- **Purpose:** the campaign remains auditable even after Hermes prunes/archives older cron
  sessions — each run maps to its exact Hermes session, campaign run, and cron job.
- **Tests:** new `test_v212_audit.py` (9): session-id capture, start/finish linkage,
  abort, ownership refusal (start & finish), crash start-only detection, audit JSON,
  duplicate-start, checkpoint id linkage. Suite: 123 → **132**, all green (incl. 3.9).

## [0.2.11] — 2026-08-06
### Fixed (semantic lease-schema validation — fail-closed on malformed-but-valid JSON)
The audit found the fail-closed reader only caught syntactically invalid JSON. A lease
that was valid JSON but missed required fields (e.g. `{}`, no `expires_at`) or had a bad
type (`expires_at: "tomorrow"`) was treated as expired/invalid and a new worker could take
over, or a comparison error could crash the check. Both the mutation layer and the gate
had the same gap.

- **`_lease_schema_ok()`** added to both `research_project.py` and `campaign-lease-gate.py`.
  A readable lease is only trusted if: `run_id` is a non-empty string, `status` is a
  recognized value (`running`), `expires_at` and `heartbeat_at` are finite numeric
  timestamps (not bool/NaN/Inf/str), and `started_at` is a string when present.
- **`_read_lease_fail_closed`** in both layers now returns `"unreadable"` (fail-closed) for
  any object that fails the schema — so a schema-invalid lease is treated as corrupt, never
  as a valid-but-expired lease. The gate cannot `CHECK`-takeover it, `HEARTBEAT`/`RELEASE`
  refuse, `STATUS` reports `corrupt`, and mutations refuse (unless `--operator-override`).
  The `expires_at: "tomorrow"` crash case is closed.
- **Tests:** new `test_v211.py` (7): schema valid/reject units, gate fail-closed on `{}`
  and on a str-`expires_at` lease, gate heartbeat/status on semantic leases, and mutation
  refusal. Suite: 116 → **123**, all green (incl. Python 3.9 parse check).

## [0.2.10] — 2026-08-06
### Fixed (gate fail-closed corrupt lease + explicit-ID prefix/type validation)
The audit found the cron gate still failed open on a corrupt lease, and explicit IDs were
not validated against their type (so `source add --id CLM-0001` could write a claim-prefixed
id, silently breaking graph resolution).

- **Gate is now fail-closed on a corrupt lease.** `campaign-lease-gate.py` uses the same
  three-state reader as the mutation layer (absent / readable / present-but-unreadable).
  `CHECK` on a corrupt lease emits `wakeAgent:false` + a recovery warning and does NOT
  auto-takeover (a second worker cannot start under an unknown owner). `HEARTBEAT`
  refuses, `STATUS` reports `corrupt`, and `RELEASE` refuses unless `--operator-override`
  (the explicit admin recovery). Expired-but-readable leases still re-acquire normally.
- **Explicit-ID prefix/type validation.** Each command now validates that an explicit
  `--id` carries the correct prefix for its type and checks for duplicates against the
  TARGET file with its declared identifier key (not a global guess):
  `source=SRC-`, `claim=CLM-`, `frontier=CLUE-`, `dead-end=DE-`, `criterion=C-`,
  `contradiction=X-`. A wrong prefix is rejected (exit **5**); a duplicate in the target
  file is rejected (exit **4**). A source can therefore no longer carry a `CLM-*` id.
- **Lease write is fsync-backed** (temp + flush + `os.fsync` + `os.replace` + parent-dir
  fsync), giving process-crash durability AND power-loss durability for the file/dir, and
  the wording is corrected to "process-crash-resistant" (not overclaiming "every failure
  mode").
- **Tests:** new `test_v210.py` (10): gate corrupt/absent/expired/live/heartbeat/status/
  release+override + wrong-prefix for all six commands + target-file duplicate + typed
  coexistence. Suite: 106 → **116**, all green (incl. Python 3.9 parse check).

## [0.2.9] — 2026-08-06
### Fixed (crash-consistency + uniqueness + deterministic barrier)
The audit found explicit `--id` could still duplicate, a corrupt lease failed open, init's
HERMES.md write was a separate (ungated) lock, and the barrier test still was not
deterministic. All closed:

- **Explicit-ID uniqueness.** `_locked_append` now checks, inside the owned lock, that an
  explicitly-supplied id does not already exist (type-aware key for CLUE/DE). Duplicate
  `--id` is rejected with exit **4** (DUPLICATE_ID); automatic minting is unchanged.
- **Crash-consistent lease writes.** `campaign-lease-gate.py` writes `.worker-lease.json`
  via a temp file + `os.replace`, so a mid-write crash cannot leave a truncated lease.
- **Corrupt-lease fail-closed.** `_check_lease_owner` distinguishes "lease absent"
  (allow manual) from "lease present but unreadable" (REFUSE unless `--operator-override`).
  A truncated lease can no longer let an unowned mutation through.
- **Atomic init.** HERMES.md is now written inside the SAME owned critical section as the
  `.research/` scaffold, closing the window where a gate could acquire a lease between the
  two sections.
- **Deterministic lock-ordering barrier test.** `test_owned_lock_barrier_interleaving` now
  holds the flock in the parent, launches the mutation subprocess (blocks on the lock),
  writes a live lease, releases, and asserts the mutation refuses (exit 3) with no write —
  no timing races, no dual-outcome branch.
- **Tests:** duplicate-id (SRC/CLUE/DE), corrupt-lease fail-closed + override, lease
  crash-consistency. Suite: 101 → **106**, all green (incl. Python 3.9 parse check).

## [0.2.8] — 2026-08-06
### Fixed (CLUE/DE identifier schema + graph-integrity + honest barrier test)
The audit found frontier/dead-end records store their id under `clue_id` (not `id`);
`_mint_id` and `_node_exists` only looked at `id`, so CLUE/DE allocation could duplicate
(e.g. repeated CLUE-0001) and the graph could not resolve CLUE/DE endpoints. Also, the
"barrier" race test did not actually use a barrier, and `init` re-run was ungated.

- **`NODE_ID_KEYS`** schema map (`{"CLUE":"clue_id","DE":"clue_id"}`). `_mint_id` and
  `_node_exists` now select the correct identifier key per type, so frontier/dead-end
  allocation is collision-free and CLUE/DE nodes resolve in the evidence graph.
- **`_mint_and_append_locked`** defaults the id key from `NODE_ID_KEYS[prefix]`.
- **Real `threading.Barrier` race test** (`test_owned_lock_barrier_interleaving`) that
  interleaves a running mutation with a gate creating a lease; replaces the overstated
  docstring-only claim.
- **`init` re-run is ownership-gated**: on an already-initialized campaign it refuses
  (exit 3) without `--run-id`/`--operator-override` when a worker is active.
- **Tests:** new `test_schema_id_keys.py` (8: CLUE/DE incremental + concurrent minting,
  node resolution, graph edges referencing CLUE/DE) + init-rerun + barrier tests.
  Suite: 91 → **101**, all green (incl. Python 3.9 parse check).

## [0.2.7] — 2026-08-06
### Fixed (atomic ownership contract — check-then-lock race closed)
The audit found ownership was validated BEFORE the flock was acquired (a race: a check
seeing 'no lease' could then write after a cron gate created one), plus `frontier update`,
`reset`, and `clarify` bypasses, and non-atomic ID minting. All closed:

- **`_owned_project_lock(args)`** — a context manager that acquires the project flock and
  validates lease ownership INSIDE the critical section, then yields. Every mutation
  routes through it (or an equivalent in-lock check), so check-then-mutate are one atomic
  step. A mutation can no longer observe a stale 'no lease'.
- **`frontier update`** now enforces ownership (was a direct `_project_lock` bypass).
- **`reset`** now holds the owned lock and requires `--run-id`/`--operator-override` when a
  worker is active (was an unprotected erase of frontier/claims/dead-ends/search-log/state).
- **`clarify`** now enforces ownership when writing `objective.md` (was locked but ungated).
- **Atomic ID allocation** — `_mint_id` moved inside the locked transaction
  (`_mint_and_append_locked`), so concurrent `source/claim/frontier/dead-end/criterion/
  contradiction add` calls cannot mint duplicate ids.
- **Tests:** new `test_atomic_ownership.py` (5: in-lock ownership, concurrent serialization,
  no-duplicate id minting, frontier-update refusal, reset refusal+override). Suite: 86 → **91**,
  all green (incl. Python 3.9 parse check).

## [0.2.6] — 2026-08-06
### Fixed (edge/resignal/tick ownership — found by the real cron lifecycle test)
The decisive real-Hermes-cron lifecycle test (gate → token → locked mutation → release)
surfaced an integration bug the unit suite had missed: `edge` (and `resignal` and `tick`)
called `_lease_guard` but their argparse parsers lacked `--run-id`/`--operator-override`,
so an edge could not be written under a live worker lease. Fixed by registering the two
flags on the `edge`, `resignal`, and `tick` subparsers. Added regression tests:
- `test_edge_writable_under_live_lease_with_run_id`
- `test_resignal_accepts_run_id`
Suite: 84 → **86 tests**, all green (incl. Python 3.9 parse check).

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
