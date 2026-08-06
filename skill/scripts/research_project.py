#!/usr/bin/env python3
"""
Endless Research project helper.

Deterministic companion to the endless-research skill. Handles project
scaffolding, state/queue inspection, state (re)signalling, and reset.
Keeps the priority queue machine-sorted so the agent never hand-picks clues.

Commands
--------
  init <dir> [--objective TXT] [--success TXT] [--failure TXT]
        Scaffold a research project with the full .research/ tree.
  status <dir>
        Print state, blocker, and the priority-sorted pending frontier.
  resignal <dir> <STATE> [--note TXT]
        Set the current_state (CONTINUE|CHECKPOINT|BLOCKED|EXHAUSTED|SUCCESS).
  reset <dir> [--state <STATE>|--frontier|--all]
        Re-open a project (default: mark all pending ready, keep evidence).
  checkpoint <dir> [--note TXT]
        Take a checkpoint snapshot now.
  tick <dir> [--cmd CMD] [--note TXT] [--lock-timeout N]
        Run ONE research tick under an atomic project lock, then auto-checkpoint.
        --cmd runs a subcommand (the dig work) while the lock is held; omit it
        for a no-op/dry tick. Returns exit code 2 if the project is locked.
  verify_success <dir> [--min-corroboration N]
        Deterministic SUCCESS gate; blocks premature SUCCESS.
  edge <dir> <from> <relationship> <to> [--context TXT]
        Add an explicit typed edge to the evidence graph (edges.jsonl).
  graph <dir> [--recent N] [--no-validate]
        Summarise the evidence graph: nodes by kind, edges by relationship.
  inspect <dir> <url>
        URL intelligence: canonical form + campaign scope rules (no fetching).
  clarify <dir> <url> [--goal TXT] [--mode auto|clear|vague|ambiguous] [--no-write]
        Objective compiler: turn a URL + vague goal into a research contract.
        clear -> compile now; vague -> infer defaults + record assumptions;
        ambiguous -> ask the essential 1-3 questions.

States: CONTINUE | CHECKPOINT | BLOCKED | EXHAUSTED | SUCCESS | DORMANT

DORMANT — clues all exhausted but the topic may yield new information later
(e.g. a developing story, a source yet to publish). Unlike EXHAUSTED (terminal),
DORMANT keeps the campaign alive to be re-awoken: resignal it back to CONTINUE
when new information/terminology/sources emerge, so a later tick can reopen it.

Evidence graph (v0.2.0): sources, claims, clues, questions, people, dead-ends and
contradictions are NODES; edges.jsonl holds typed RELATIONSHIPS (links_to, cites,
supports, contradicts, answers, ...) between them. The `edge` command enforces
referential integrity (from/to must reference real nodes) so the implicit graph
becomes explicit and machine-checkable.

Exit code 0 on success, 1 on missing/corrupt project, 2 = tick skipped (locked).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Atomic project lock — fcntl on Unix, msvcrt fallback on Windows.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

# Exit code when another researcher holds the project lock.
EXIT_ALREADY_LOCKED = 2


@contextlib.contextmanager
def _project_lock(proj: Path, timeout: float = 0.0):
    """Acquire an exclusive, atomic lock on a research project.

    Prevents two ticks (cron or manual) from mutating the same .research/
    directory simultaneously — defense-in-depth on top of Hermes' native
    cron in-flight guard. Creates/uses ``<proj>/.research/.lock``.

    - Unix: ``fcntl.flock`` (advisory, released automatically on close/exit).
    - Windows: ``msvcrt.locking`` on a dedicated byte.
    - ``timeout<=0``: block indefinitely; ``timeout>0``: bounded wait, then
      raise a ``BlockingIOError`` so the caller can exit EXIT_ALREADY_LOCKED.
    """
    research = proj / ".research"
    research.mkdir(parents=True, exist_ok=True)
    lockfile = research / ".lock"
    with open(lockfile, "a+b") as fh:
        if fcntl is not None:
            op = fcntl.LOCK_EX | fcntl.LOCK_NB if timeout > 0 else fcntl.LOCK_EX
            deadline = time.time() + timeout if timeout > 0 else None
            while True:
                try:
                    fcntl.flock(fh.fileno(), op)
                    break
                except OSError as e:
                    if getattr(e, "errno", None) not in (11,):  # EWOULDBLOCK/EAGAIN
                        raise
                    if timeout <= 0:
                        raise  # non-blocking request that didn't get the lock
                    if time.time() >= (deadline or 0):
                        raise BlockingIOError("project lock timeout")
                    fh.seek(0)
                    fh.write(b"\0")  # touch so it is never seen as stale
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            fh.seek(0)
            if fh.read(1) == b"":
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            yield  # no locking primitive available; run unlocked


VALID_PRIORITY_FIELDS = [
    "relevance", "primary_source_likelihood", "info_gain",
    "resolves_uncertainty", "novelty", "ease",
]
WEIGHTS = {
    "relevance": 0.30, "primary_source_likelihood": 0.20, "info_gain": 0.20,
    "resolves_uncertainty": 0.15, "novelty": 0.10, "ease": 0.05,
}
STATES = {"CONTINUE", "CHECKPOINT", "BLOCKED", "EXHAUSTED", "SUCCESS", "DORMANT"}

TEMPLATES = {
    "objective.md": """# Objective

## The exact finding being sought
{objective}

## Questions that must be answered
- (_edit_)

## Evidence required for acceptance
- (_edit_ — what would prove it's found_)

## What counts as FAILURE
{failure}

## What counts as SUCCESS
{success}

## Constraints / scope
- (_edit_)

## Terms requiring clarification
- (_edit_)

## Likely alternative terminology
- (_edit_)
""",
    "state.json": """{{
  "current_state": "CONTINUE",
  "state_updated": "{now}",
  "last_checkpoint": null,
  "next_action": "Scaffold objective; begin first search on the highest-priority frontier item.",
  "blockers": [],
  "rounds_completed": 0,
  "frontier_count": 0,
  "root_objective": "{objective}"
}}
""",
    "frontier.jsonl": "",
    "claims.jsonl": "",
    "sources.jsonl": "",
    "edges.jsonl": "",   # explicit evidence graph: nodes linked by typed relationships
    "contradictions.jsonl": "",
    "dead-ends.jsonl": "",
    "search-log.jsonl": "",
    "questions.jsonl": "",   # QUESTION nodes (open questions, rankable)
    "people.jsonl": "",      # PERSON / organisation / entity nodes
    "scope.json": """{
  "follow_internal_links": true,
  "follow_external_links": true,
  "allowed_domains": [],
  "blocked_domains": [],
  "max_pages_per_domain": 100,
  "relevance_budget": 100,
  "page_budget": 200,
  "allow_archives": true,
  "allow_repositories": true,
  "allow_documents": true,
  "notes": "Budget-based crawling control; do not use a strict max_depth. See protocol.md."
}
""",
    "criteria.jsonl": """{"id":"C-001","description":"REPLACE — acceptance criterion.","evidence_required":"primary_or_exception","corroboration_required":true,"met":false,"evidence_source_ids":[],"exception":null}
""",
    "unresolved.md": "# Unresolved questions\n\nRanked by importance.\n\n1. (_pending_)\n",
    "final-report.md": "# Final Report\n\n_Completed only on SUCCESS. See protocol for the 10 required sections._\n",
}

HERMES_MD = """# {name}

## Persistent Recursive Research Protocol

You are the research director for this project. Continue investigating the objective
in .research/objective.md until its explicit SUCCESS criteria are satisfied. Searching
extensively is NOT itself success.

RESUME — at the start of every run (live chat, cron tick, or spawn):

1. Read .research/objective.md
2. Read .research/state.json
3. Read .research/unresolved.md
4. Load pending entries from .research/frontier.jsonl (script-sorted: use
   `python3 {script} status .`)
5. Review the newest checkpoint and the tail of .research/search-log.jsonl
6. Resume the highest-priority unfinished clue.
   NEVER restart from zero unless the research files are missing or corrupt.

Follow the protocol in the `endless-research` skill. Terminal state must be one of:
CONTINUE, CHECKPOINT, BLOCKED, DORMANT, EXHAUSTED, SUCCESS.

- DORMANT = clues all spent but the topic may yield new information later; stay
  armed/resumable (`resignal . DORMANT`), re-awaken via `resignal . CONTINUE`.
- SUCCESS / EXHAUSTED are TERMINAL — the campaign is done. When setting either,
  pass `--cron <job-id>` so the recurring cron job auto-pauses (deterministic
  self-stop) and stops firing.

Never convert "searched a lot" into SUCCESS. After a substantial round, always
checkpoint (`tick` or `checkpoint`) so the next run resumes purely from disk.
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _score(item: dict) -> float:
    return sum(WEIGHTS[f] * float(item.get(f, 0) or 0) for f in VALID_PRIORITY_FIELDS)


def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def cmd_init(args) -> int:
    if args.now:
        pass  # placeholder to avoid unused warnings
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    research.mkdir(parents=True, exist_ok=True)
    checkpoints = research / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)

    objective = args.objective or "_unset — edit .research/objective.md_"
    success = args.success or "_unset_"
    failure = args.failure or "_unset_"

    with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
        # If the project is ALREADY initialized (state.json exists), re-running init is a
        # mutation of an active campaign: enforce lease ownership unless an operator
        # overrides. Fresh init (no state.json yet) proceeds normally.
        if (research / "state.json").exists():
            if not _check_lease_owner(args, proj, research):
                _owned_cm_fail(args, research, "init(re-run)", 3)
                return 3
        for name, content in TEMPLATES.items():
            target = research / name
            if target.exists():
                continue
            filled = (
                content.format(objective=objective, success=success, failure=failure,
                               now=_now())
                if name in ("objective.md", "state.json")
                else content
            )
            target.write_text(filled)
        # Write HERMES.md inside the SAME owned critical section so init is atomic (no
        # window where a gate could acquire a lease between scaffold sections).
        script = Path(os.path.abspath(__file__))
        hermes_md = HERMES_MD.format(name=proj.name, script=script)
        hfile = proj / "HERMES.md"
        if not hfile.exists():
            hfile.write_text(hermes_md)

    print(f"Scaffolded research project at {proj}")
    print("  Edit .research/objective.md, then enqueue initial clues in frontier.jsonl")
    print("  or ask the agent to begin. States live in .research/state.json.")
    return 0


def cmd_status(args) -> int:
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    state_f = research / "state.json"
    if not state_f.exists():
        print(f"No state.json at {research}. Run `init` first.", file=sys.stderr)
        return 1
    state = json.loads(state_f.read_text())
    print("== State ==")
    print(f"  current      : {state.get('current_state')}")
    print(f"  updated      : {state.get('state_updated')}")
    print(f"  rounds done  : {state.get('rounds_completed')}")
    print(f"  next action  : {state.get('next_action')}")
    blockers = state.get("blockers") or []
    if blockers:
        print(f"  blockers     : {blockers}")
    if state.get("last_checkpoint"):
        print(f"  checkpoint   : {state.get('last_checkpoint')}")

    front = _load_jsonl(research / "frontier.jsonl")
    pending = [f for f in front if (f.get("status") or "pending") == "pending"]
    done = sum(1 for f in front if (f.get("status") or "").startswith("done"))
    dead = sum(1 for f in front if (f.get("status") or "") == "dead")
    pending.sort(key=_score, reverse=True)
    print(f"\n== Frontier ({len(front)} total; {len(pending)} pending / {done} done / {dead} dead) ==")
    if not pending:
        print("  (no pending clues)")
    for i, f in enumerate(pending, 1):
        cid = f.get("clue_id", "?")
        desc = (f.get("description") or "")[:90]
        depth = f.get("depth", "?")
        attempts = f.get("attempts", 0)
        print(f"  {i:>2}. [{cid}] d={depth} a={attempts} score={_score(f):5.1f}  {desc}")
    return 0


def _pause_cron_job(jobid: str) -> None:
    """Pause a Hermes cron job via the CLI (deterministic self-stop)."""
    import subprocess
    try:
        r = subprocess.run(["hermes", "cron", "pause", jobid],
                           capture_output=True, text=True, timeout=60)
        print(f"[resignal] cron {jobid} pause -> exit {r.returncode}: "
              f"{(r.stdout or r.stderr or '').strip()[:200]}")
    except Exception as e:
        print(f"[resignal] WARNING: could not pause cron {jobid}: {e}")


def _resume_cron_job(jobid: str) -> None:
    """Resume a Hermes cron job via the CLI (re-arm for CONTINUE/DORMANT)."""
    import subprocess
    try:
        r = subprocess.run(["hermes", "cron", "resume", jobid],
                           capture_output=True, text=True, timeout=60)
        print(f"[resignal] cron {jobid} resume -> exit {r.returncode}: "
              f"{(r.stdout or r.stderr or '').strip()[:200]}")
    except Exception as e:
        print(f"[resignal] WARNING: could not resume cron {jobid}: {e}")


TERMINAL_STATES = {"SUCCESS", "EXHAUSTED"}

# The aggressive research-cron job should only run during active digging states
# (CONTINUE/CHECKPOINT/BLOCKED). It PAUSES on the terminal states (SUCCESS/EXHAUSTED)
# AND on DORMANT — because a dormant campaign has no clues to dig until the watcher
# re-awakens it. CONTINUE/CHECKPOINT/BLOCKED keep it firing; DORMANT parks it.
CRON_PAUSE_STATES = TERMINAL_STATES | {"DORMANT"}   # research cron is paused here
CRON_RUN_STATES = {"CONTINUE", "CHECKPOINT", "BLOCKED"}  # research cron fires here


def cmd_resignal(args) -> int:
    proj = Path(args.dir).expanduser().resolve()
    state_f = proj / ".research" / "state.json"
    if not state_f.exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    st = args.state.upper()
    if st not in STATES:
        print(f"Invalid state {st!r}. Valid: {sorted(STATES)}", file=sys.stderr)
        return 1

    # Deterministic SUCCESS gate: block premature SUCCESS unless verified or forced.
    if st == "SUCCESS" and not getattr(args, "force", False):
        print("== SUCCESS gate engaged ==")
        gate_ok = cmd_verify_success(args) == 0
        if not gate_ok:
            print("\nSUCCESS BLOCKED by gate. Fix the failures (or record explicit")
            print("exceptions) and re-run, or use `--force` if you accept the risk.")
            return 1
        print()

    # Atomic lease check + state write under one lock.
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "resignal", owned.exit_code)
            state = json.loads(state_f.read_text())
            prev = state.get("current_state", "CONTINUE")
            state["current_state"] = st
            state["state_updated"] = _now()
            if args.note:
                state["next_action"] = args.note
                if st == "BLOCKED":
                    bl = state.setdefault("blockers", [])
                    if args.note not in bl:
                        bl.append(args.note)
            state_f.write_text(json.dumps(state, indent=2))
    except BlockingIOError:
        return _owned_cm_fail(args, proj / ".research", "resignal", 2)
    print(f"State set to {st}.")

    # Deterministic cron self-(dis)arming on terminal vs resumable transitions.
    # The research cron fires only while state ∈ CRON_RUN_STATES; it pauses when
    # state ∈ CRON_PAUSE_STATES (SUCCESS/EXHAUSTED = done; DORMANT = parked until
    # the watcher re-awakens). Runs OUTSIDE the project lock (separate subsystem).
    if args.cron:
        if st in CRON_PAUSE_STATES and prev in CRON_RUN_STATES:
            _pause_cron_job(args.cron)          # entered a pause state -> stop firing
        elif st in CRON_RUN_STATES and prev in CRON_PAUSE_STATES:
            _resume_cron_job(args.cron)          # re-entered active digging -> fire again
    return 0


def cmd_reset(args) -> int:
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not research.exists():
        print("No .research dir. Run `init` first.", file=sys.stderr)
        return 1
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    # Atomic lease check + destructive rewrites under one lock. A reset while a worker
    # owns the campaign would erase its evidence, so it must prove ownership or use
    # --operator-override (deliberate emergency).
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "reset", owned.exit_code)
            front = _load_jsonl(research / "frontier.jsonl")
            if args.frontier or args.all:
                for f in front:
                    f["status"] = "pending"
                    f["attempts"] = 0
                (research / "frontier.jsonl").write_text(
                    "".join(json.dumps(f) + "\n" for f in front) or "")
                print("Frontier reset to pending.")
            if args.all:
                (research / "claims.jsonl").write_text("")
                (research / "dead-ends.jsonl").write_text("")
                (research / "search-log.jsonl").write_text("")
                print("Claims/dead-ends/search-log cleared.")
            state = json.loads((research / "state.json").read_text())
            state["current_state"] = "CONTINUE"
            state["state_updated"] = _now()
            state["blockers"] = []
            (research / "state.json").write_text(json.dumps(state, indent=2))
            print("State reset to CONTINUE.")
    except BlockingIOError:
        return _owned_cm_fail(args, research, "reset", 2)
    return 0


def _hermes_session_id() -> str:
    """The current Hermes session ID (exposed to tool subprocesses as $HERMES_SESSION_ID).

    This lets every audit record associate itself with the exact Hermes session that
    produced it, even when the database later prunes the full session. Falls back to ''.
    """
    return os.environ.get("HERMES_SESSION_ID", "")


# ---------------------------------------------------------------------------
# v0.2.12 — per-run audit journal (append-only .research/run-history.jsonl)
# ---------------------------------------------------------------------------
# Append-only lifecycle events rather than rewriting a single record, so a crash is
# visible: a "started" event with no matching "completed"/"aborted" means the session
# ended unexpectedly. Every event carries campaign_run_id + hermes_session_id so the
# campaign remains auditable even after Hermes session pruning.

AUDIT_SCHEMA = 1


def _history_path(research: Path) -> Path:
    return research / "run-history.jsonl"


def _append_history_event(research: Path, event: dict) -> None:
    """Append one audit event, fsynced for crash-durability (like the lease writer)."""
    p = _history_path(research)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _history_event_schema_ok(event: object) -> bool:
    """Semantic schema validation for a parsed audit-event row (mirrors the lease
    validator in _lease_schema_ok).

    A syntactically-valid JSON token that is not a well-formed audit event is treated as
    corrupt (fail-closed) — it must NOT be silently parsed (which could crash on .get())
    nor accepted as a valid event.
    """
    if not isinstance(event, dict):
        return False                        # null / [] / scalar -> corrupt
    if event.get("schema_version") != AUDIT_SCHEMA:
        return False
    if event.get("event") not in ("started", "completed", "aborted"):
        return False
    run_id = event.get("campaign_run_id")
    if not isinstance(run_id, str) or not run_id.startswith("RUN-"):
        return False
    if not isinstance(event.get("timestamp"), str) or not event["timestamp"]:
        return False
    # Event-specific required/typed fields.
    sess = event.get("hermes_session_id")
    if sess is not None and not isinstance(sess, str):
        return False
    cron = event.get("cron_job_id")
    if cron is not None and not isinstance(cron, str):
        return False
    ev = event.get("event")
    if ev == "started":
        rounds = event.get("rounds_completed")
        if isinstance(rounds, bool) or not isinstance(rounds, (int, type(None))):
            return False
    elif ev == "completed":
        if event.get("checkpoint") is not None and not isinstance(event.get("checkpoint"), str):
            return False
        for intf in ("sources_added", "claims_added", "edges_added"):
            if not isinstance(event.get(intf, 0), int) or isinstance(event.get(intf, 0), bool):
                return False
    elif ev == "aborted":
        if event.get("reason") is not None and not isinstance(event.get("reason"), str):
            return False
    return True


def _load_history_strict(research: Path):
    """Load run-history rows, tracking any unparseable OR schema-invalid line numbers.
    Returns (rows, corrupt_line_numbers).
    """
    rows = []
    corrupt = []
    p = _history_path(research)
    if p.exists():
        for idx, line in enumerate(p.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                corrupt.append(idx)
                continue
            # Semantic validation: a valid-JSON-but-malformed event is also corrupt.
            if not _history_event_schema_ok(obj):
                corrupt.append(idx)
                continue
            rows.append(obj)
    return rows, corrupt


def _start_for_run(events, run_id: str):
    return next((e for e in events if e.get("event") == "started"
                 and e.get("campaign_run_id") == run_id), None)


def _terminal_for_run(events, run_id: str):
    return [e for e in events if e.get("event") in ("completed", "aborted")
            and e.get("campaign_run_id") == run_id]


def _check_match(ev, run_id, session_id, cron_job_id, label) -> int:
    """Validate a finish/abort against its start event. Returns 0 to proceed, else refusal
    code (4 = no/invalid start, 5 = session mismatch, 6 = cron mismatch, 7 = already
    terminal). STRICT: when the start event recorded a non-empty session/cron id, the
    terminal event must supply a matching one (an empty/missing supplied value is treated
    as a mismatch, not silently allowed)."""
    start = _start_for_run(ev, run_id)
    if start is None:
        print(f"REFUSED: {label}: no 'started' event for run {run_id}. "
              f"Call `run start` first.", file=sys.stderr)
        return 4
    stored_sess = start.get("hermes_session_id")
    if stored_sess and session_id != stored_sess:
        print(f"REFUSED: {label}: session mismatch for run {run_id}. "
              f"start session={stored_sess} vs provided {session_id or '(none)'}.",
              file=sys.stderr)
        return 5
    stored_cron = start.get("cron_job_id")
    if stored_cron and cron_job_id != stored_cron:
        print(f"REFUSED: {label}: cron job mismatch for run {run_id}. "
              f"start cron={stored_cron} vs provided {cron_job_id or '(none)'}.",
              file=sys.stderr)
        return 6
    terms = _terminal_for_run(ev, run_id)
    if terms:
        print(f"REFUSED: {label}: run {run_id} already has terminal event "
              f"({terms[0].get('event')}).", file=sys.stderr)
        return 7
    return 0


def cmd_run_start(args) -> int:
    """Record a 'started' audit event (ATOMIC: read+validate+append under one lock)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    session_id = args.session_id or _hermes_session_id() or ""
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, research, "run start", owned.exit_code)
            events, corrupt_lines = _load_history_strict(research)
            if corrupt_lines:
                print(f"REFUSED: run start: run-history.jsonl has corrupt line(s) "
                      f"{corrupt_lines}. Refusing to mutate an incomplete journal — repair "
                      f"or remove it deliberately first.", file=sys.stderr)
                return 8
            # A live, un-finished run for the same campaign_run_id must not be double-started.
            if _start_for_run(events, args.run_id) is not None:
                print(f"REFUSED: run {args.run_id} already started.", file=sys.stderr)
                return 4
            event = {
                "schema_version": AUDIT_SCHEMA,
                "event": "started",
                "campaign_run_id": args.run_id,
                "hermes_session_id": session_id,
                "cron_job_id": args.cron_job_id or "",
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "state": getattr(args, "state", ""),
                "rounds_completed": getattr(args, "rounds", None),
            }
            _append_history_event(research, event)
    except BlockingIOError:
        return _owned_cm_fail(args, research, "run start", 2)
    print(f"run started -> {_history_path(research)}")
    return 0


def cmd_run_finish(args) -> int:
    """Record a 'completed' terminal event (strict start->terminal state machine, atomic)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    session_id = args.session_id or _hermes_session_id() or ""
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, research, "run finish", owned.exit_code)
            events, corrupt_lines = _load_history_strict(research)
            if corrupt_lines:
                print(f"REFUSED: run finish: run-history.jsonl has corrupt line(s) "
                      f"{corrupt_lines}. Refusing to mutate an incomplete journal — repair "
                      f"or remove it deliberately first.", file=sys.stderr)
                return 8
            rc = _check_match(events, args.run_id, session_id, args.cron_job_id, "run finish")
            if rc:
                return rc
            event = {
                "schema_version": AUDIT_SCHEMA,
                "event": "completed",
                "campaign_run_id": args.run_id,
                "hermes_session_id": session_id,
                "cron_job_id": args.cron_job_id or "",
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "checkpoint": args.checkpoint or "",
                "state": getattr(args, "state", ""),
                "sources_added": getattr(args, "sources", 0),
                "claims_added": getattr(args, "claims", 0),
                "edges_added": getattr(args, "edges", 0),
                "next_clue": args.next_clue or "",
                "result": args.result or "completed",
            }
            _append_history_event(research, event)
    except BlockingIOError:
        return _owned_cm_fail(args, research, "run finish", 2)
    print(f"run completed -> {_history_path(research)}")
    return 0


def cmd_run_abort(args) -> int:
    """Record an intentional 'aborted' terminal event (strict state machine, atomic)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    session_id = args.session_id or _hermes_session_id() or ""
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, research, "run abort", owned.exit_code)
            events, corrupt_lines = _load_history_strict(research)
            if corrupt_lines:
                print(f"REFUSED: run abort: run-history.jsonl has corrupt line(s) "
                      f"{corrupt_lines}. Refusing to mutate an incomplete journal — repair "
                      f"or remove it deliberately first.", file=sys.stderr)
                return 8
            rc = _check_match(events, args.run_id, session_id, args.cron_job_id, "run abort")
            if rc:
                return rc
            event = {
                "schema_version": AUDIT_SCHEMA,
                "event": "aborted",
                "campaign_run_id": args.run_id,
                "hermes_session_id": session_id,
                "cron_job_id": args.cron_job_id or "",
                "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reason": args.reason or "",
            }
            _append_history_event(research, event)
    except BlockingIOError:
        return _owned_cm_fail(args, research, "run abort", 2)
    print(f"run aborted -> {_history_path(research)}")
    return 0


def cmd_run_audit(args) -> int:
    """Reconcile the run-history journal under the project lock (consistent snapshot)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    # Take the ordinary project lock so a concurrent append cannot expose a partial line
    # and produce a transient false-corruption report.
    proj = Path(args.dir).expanduser().resolve()
    with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
        events, corrupt_lines = _load_history_strict(research)
        started = [e for e in events if e.get("event") == "started"]
        run_ids = sorted({e.get("campaign_run_id") for e in started if e.get("campaign_run_id")})
        crashed = []
        dup_terminal = []
        for rid in run_ids:
            terms = _terminal_for_run(events, rid)
            if not terms:
                crashed.append(rid)
            elif len(terms) > 1:
                dup_terminal.append(rid)
        summary = {
            "runs_started": len(started),
            "runs_completed": sum(1 for e in events if e.get("event") == "completed"),
            "runs_aborted": sum(1 for e in events if e.get("event") == "aborted"),
            "crashed_start_only": crashed,
            "duplicate_terminal_events": sorted(set(dup_terminal)),
            "corrupt_history_lines": corrupt_lines,
            "journal_integrity": "failed" if corrupt_lines else "ok",
            "events_total": len(events),
        }
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2))
    else:
        print("== run audit ==")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        if crashed:
            print("  WARNING: start-only (crashed) runs →", crashed)
        if corrupt_lines:
            print("  WARNING: corrupt journal lines →", corrupt_lines)
    return 0


def cmd_checkpoint(args) -> int:
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    state_f = research / "state.json"
    if not state_f.exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")  # microsecond suffix avoids same-second collisions
    cp = research / "checkpoints" / f"cp_{ts}.md"
    # Atomic lease check + state/checkpoint write under one lock.
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "checkpoint", owned.exit_code)
            state = json.loads(state_f.read_text())
            body = f"# Checkpoint {ts}\n\nstate       : {state.get('current_state')}\n"
            body += f"next_action : {state.get('next_action')}\nnote        : {args.note or ''}\n"
            # v0.2.12 — link the checkpoint back to its run/session/cron for auditability.
            body += f"campaign_run_id  : {getattr(args, 'run_id', '') or ''}\n"
            body += f"hermes_session_id: {_hermes_session_id()}\n"
            body += f"cron_job_id      : {getattr(args, 'cron_job_id', '') or ''}\n"
            cp.write_text(body)
            state["last_checkpoint"] = str(cp.relative_to(research))
            state_f.write_text(json.dumps(state, indent=2))
    except BlockingIOError:
        return _owned_cm_fail(args, research, "checkpoint", 2)
    print(f"Checkpoint written: {cp}")
    return 0


# ---------------------------------------------------------------------------
# Deterministic SUCCESS gate
# ---------------------------------------------------------------------------

def _load_sources(research: Path) -> dict:
    return {s.get("id"): s for s in _load_jsonl(research / "sources.jsonl")}


def _is_primary(source: dict) -> bool:
    st = (source.get("type") or "").lower()
    return st.startswith("primary")


def _load_criteria(research: Path) -> list:
    return _load_jsonl(research / "criteria.jsonl")


def cmd_verify_success(args) -> int:
    """Deterministic gate that BLOCKS premature SUCCESS.

    Checks every acceptance criterion in .research/criteria.jsonl plus the
    evidence trail, and reports PASS/FAIL for each. Intended to be run by the
    agent (or a human) BEFORE issuing `resignal SUCCESS`. If any required
    item fails, SUCCESS should NOT be issued.

    Checks (mirrors the operator's hardening list):
      1. Every criterion is marked met, OR has an explicit recorded exception.
      2. Every met criterion's evidence_source_ids reference real source IDs.
      3. primary_hard criteria are backed by a primary source OR exception.
      4. corroboration_required criteria have >= `min_corroboration`
         independent sources OR exception.
      5. No critical, unresolved contradiction remains in contradictions.jsonl.
      6. final-report.md exists, is non-empty, and is not the template stub.
    """
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    state_f = research / "state.json"
    if not state_f.exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    sources = _load_sources(research)
    source_ids = set(sources.keys())
    criteria = _load_criteria(research)
    contradictions = _load_jsonl(research / "contradictions.jsonl")
    final_report = research / "final-report.md"

    problems = []
    all_ok = True

    def fail(msg):
        nonlocal all_ok
        all_ok = False
        problems.append(msg)

    print("== SUCCESS gate report ==")
    print(f"criteria found      : {len(criteria)}")
    print(f"sources on record   : {len(sources)}")
    print(f"critical unresolved contradictions: "
          f"{sum(1 for c in contradictions if c.get('critical') and not c.get('resolved'))}\n")

    # 1. Every criterion met or excepted
    if not criteria:
        fail("No acceptance criteria defined in criteria.jsonl. Add them before SUCCESS.")
    else:
        for c in criteria:
            cid = c.get("id", "?")
            met = c.get("met", False)
            exc = c.get("exception")
            if met:
                continue
            if exc:
                print(f"  [EXCEPTION] {cid}: {c.get('description','')} — {exc}")
                continue
            fail(f"Criterion {cid} not met and no exception recorded: "
                 f"{c.get('description','')}")

        # 2. Evidence source ids resolve
        for c in criteria:
            if not c.get("met"):
                continue
            cid = c.get("id", "?")
            for sid in c.get("evidence_source_ids", []) or []:
                if sid not in source_ids:
                    fail(f"Criterion {cid} references unknown source {sid}")

        # 3. Primary-hard criteria backed by a primary source or exception
        for c in criteria:
            if not c.get("met"):
                continue
            cid = c.get("id", "?")
            eids = c.get("evidence_source_ids", []) or []
            if c.get("primary_hard") and not c.get("exception_primary"):
                if not any(_is_primary(sources.get(sid, {})) for sid in eids):
                    fail(f"Criterion {cid} requires PRIMARY evidence but none of "
                         f"{eids} is a primary source (or record exception_primary).")

        # 4. Corroboration
        min_coro = int(getattr(args, "min_corroboration", None) or 2)
        for c in criteria:
            if not c.get("met"):
                continue
            cid = c.get("id", "?")
            eids = c.get("evidence_source_ids", []) or []
            if c.get("corroboration_required") and not c.get("exception_corroboration"):
                independent = {sources.get(sid, {}).get("url") for sid in eids
                               if sid in sources}
                if len(eids) < min_coro or len(independent) < min_coro:
                    fail(f"Criterion {cid} needs >= {min_coro} independent sources "
                         f"(got {len(independent)}).")

        # 5. No critical unresolved contradiction
        for c in contradictions:
            if c.get("critical") and not c.get("resolved"):
                fail(f"Critical contradiction unresolved: {c.get('id', c.get('description',''))}")

        # 6. final-report present and substantive
        if not final_report.exists():
            fail("final-report.md does not exist.")
        else:
            body = final_report.read_text().strip()
            stub = "Completed only on SUCCESS"
            if len(body) < 300 or stub in body:
                fail("final-report.md is missing or is still the template stub.")

    verdict = "UNBLOCKED" if all_ok else "BLOCKED"
    print(f"\nVERDICT: {verdict}")
    for p in problems:
        print(f"  ✗ {p}")
    if all_ok:
        print("  All acceptance criteria satisfied. SUCCESS may be issued "
              "(`resignal <dir> SUCCESS --cron <jobid>`).")
    else:
        print("  Do NOT issue SUCCESS yet. Resolve the failures above, or record")
        print("  explicit exceptions, then re-run this gate.")
    return 0 if all_ok else 1


def cmd_tick(args) -> int:
    """Run ONE research tick under the atomic project lock.

    Acquires `<proj>/.research/.lock` exclusively, executes the provided
    subcommand (the actual dig work — search, extract, read), then writes a
    checkpoint for the whole tick. This is the single-entry primitive a cron
    (or manual) run should use so two ticks can never mutate the project
    simultaneously, guarding even against manual runs colliding with cron.

    ``--cmd`` is optional; when omitted the tick just touches state (marks a
    round) and checkpoints — useful as a dry run / no-op test.
    """
    import subprocess
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    state_f = research / "state.json"
    if not state_f.exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    timeout = getattr(args, "lock_timeout", 30)
    try:
        with _project_lock(proj, timeout=timeout):
            # INSIDE the lock: validate lease ownership (closes check-then-lock race).
            if not _check_lease_owner(args, proj, research):
                _owned_cm_fail(args, research, "tick", 3)
                return 3
            # Bump the round counter under the lock (single writer).
            state = json.loads(state_f.read_text())
            state["rounds_completed"] = state.get("rounds_completed", 0) + 1
            state["state_updated"] = _now()
            state_f.write_text(json.dumps(state, indent=2))
            print(f"[tick] lock acquired; round -> {state['rounds_completed']}")

            if args.cmd:
                result = subprocess.run(args.cmd, shell=True, cwd=proj)
                if result.returncode != 0:
                    print(f"[tick] subcommand exited {result.returncode}", file=sys.stderr)
                    return result.returncode

            # Auto-checkpoint the tick.
            note = args.note or f"tick round {state.get('rounds_completed')} (cmd={args.cmd!r})"
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")  # microsecond suffix avoids collisions
            cp = research / "checkpoints" / f"cp_tick_{ts}.md"
            s2 = json.loads(state_f.read_text())
            body = f"# Tick checkpoint {ts}\n\nstate : {s2.get('current_state')}\nnote  : {note}\n"
            cp.write_text(body)
            s2["last_checkpoint"] = str(cp.relative_to(research))
            state_f.write_text(json.dumps(s2, indent=2))
            print(f"[tick] checkpoint written: {cp}")
            print(f"[tick] lock released")
    except BlockingIOError:
        print("Project is locked by another researcher — skipping this tick "
              "(EXIT_ALREADY_LOCKED).", file=sys.stderr)
        return EXIT_ALREADY_LOCKED
    return 0


# ---------------------------------------------------------------------------
# Explicit evidence graph (v0.2.0)
# ---------------------------------------------------------------------------

# Typed relationships between nodes. From/to IDs must reference an existing node.
RELATIONSHIPS = {
    "links_to", "cites", "authored_by", "published_by",
    "supports", "contradicts", "answers", "depends_on",
    "derived_from", "investigates", "duplicate_of", "archived_version_of",
    "blocks",
}

# Node kinds and which files hold them (for machine-enforced referential integrity).
NODE_KINDS = {
    "SRC": "sources.jsonl",    # SOURCE
    "CLM": "claims.jsonl",     # CLAIM
    "CLUE": "frontier.jsonl",  # CLUE
    "Q": "questions.jsonl",    # QUESTION
    "P": "people.jsonl",       # PERSON / organisation / entity
    "DE": "dead-ends.jsonl",   # DEAD_END
    "X": "contradictions.jsonl",  # CONTRADICTION
}

# Identifier field used by each record type. CLUE and DE records store their id under
# "clue_id" (not "id"), so ID minting and node resolution must use this map.
NODE_ID_KEYS = {
    "CLUE": "clue_id",
    "DE": "clue_id",
}


def _node_exists(research: Path, node_id: str) -> bool:
    """True if node_id resolves to an existing node in the project files.

    Uses the correct identifier key per type (clue_id for CLUE/DE, id otherwise) so
    frontier/dead-end records (stored under "clue_id") are resolvable by the graph.
    """
    prefix = node_id.split("-")[0] if "-" in node_id else node_id
    fname = NODE_KINDS.get(prefix)
    if fname is None:
        return False
    p = research / fname
    if not p.exists():
        return False
    id_key = _node_id_key(prefix)
    return any(rec.get(id_key) == node_id for rec in _load_jsonl(p))


def _load_edges(research: Path) -> list:
    return _load_jsonl(research / "edges.jsonl")


def _append_edge(research: Path, edge: dict) -> None:
    path = research / "edges.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(edge) + "\n")


def _make_edge_id(edges: list) -> str:
    return f"EDGE-{uuid.uuid4().hex[:10]}"


def _append_node(research: Path, kind: str, node: dict) -> None:
    """Append a node record to its kind's file (append-only audit trail)."""
    fname = NODE_KINDS[kind]
    path = research / fname
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(node) + "\n")


def _mint_node_id(research: Path, kind: str, prefix: str) -> str:
    """Return the next unused node id for a kind, e.g. Q-0007 / P-0012."""
    existing = {n.get("id") for n in _load_jsonl(research / NODE_KINDS[kind])}
    i = 1
    while True:
        cand = f"{prefix}-{i:04d}"
        if cand not in existing:
            return cand
        i += 1


def cmd_edge(args) -> int:
    """Append an explicit typed edge to the evidence graph — ATOMICALLY.

    Enforces full referential integrity, with NO exemptions: every from_id/to_id
    must resolve to a real node at the end of the call. If an edge targets a
    question (Q-*) or person/entity (P-*) node that does not exist yet, that node
    is created atomically alongside the edge — BOTH succeed or NEITHER does.

    The node+edge create is wrapped in the project lock, so a concurrent tick can
    never observe a half-created edge or a dangling reference. relationship must
    be one of the known types and domain rules are enforced.
    """
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    rel = args.relationship.replace(" ", "_")
    if rel not in RELATIONSHIPS:
        print(f"Invalid relationship {rel!r}. Valid: {sorted(RELATIONSHIPS)}",
              file=sys.stderr)
        return 1

    # Domain rules: relationship-specific node-kind constraints.
    if rel in ("supports", "contradicts") and not args.to_id.startswith("CLM"):
        print(f"relationship '{rel}' requires a CLAIM as to_id (got {args.to_id})",
              file=sys.stderr)
        return 1
    if rel in ("answers",) and not args.from_id.startswith("CLM"):
        print(f"relationship 'answers' requires a CLAIM as from_id (got {args.from_id})",
              file=sys.stderr)
        return 1

    # Edge may point at a Q-* / P-* node that does not exist yet; mint it later.
    # Work entirely under the lock so node+edge creation is atomic.
    try:
        with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
            # INSIDE the lock: validate lease ownership (closes check-then-lock race).
            if not _check_lease_owner(args, proj, research):
                _owned_cm_fail(args, research, "edge", 3)
                return 3
            results = []
            ids_to_ensure = {args.from_id, args.to_id}

            # PHASE 1 — validate. Every non-Q/P endpoint must already exist; if any
            # is missing, abort BEFORE creating anything (true atomicity). Only Q-/P-
            # endpoints may be auto-created, and only once all existing nodes check out.
            for nid in ids_to_ensure:
                if _node_exists(research, nid):
                    continue
                kind = nid.split("-")[0]
                if kind not in ("Q", "P"):
                    print(f"Unknown node ID: {nid}. Add the node first (or use a Q-/P- id "
                          f"to have it created atomically with the edge).", file=sys.stderr)
                    return 1  # nothing has been created yet -> atomic abort

            # PHASE 2 — create missing Q-/P- nodes (all validation passed).
            for nid in ids_to_ensure:
                if _node_exists(research, nid):
                    continue
                kind = nid.split("-")[0]
                real_id = _mint_node_id(research, kind, kind)
                node = {
                    "id": real_id,
                    "kind": "question" if kind == "Q" else "entity",
                    "label": args.context or f"{kind} node",
                    "created_at": _now(),
                }
                _append_node(research, kind, node)
                # Substitute the real id in BOTH endpoints consistently.
                if nid == args.from_id:
                    args.from_id = real_id
                if nid == args.to_id:
                    args.to_id = real_id
                results.append(f"created {kind} node {real_id}")

            # PHASE 3 — append the edge (both endpoints now resolve to real nodes).
            edges = _load_edges(research)
            edge = {
                "edge_id": _make_edge_id(edges),
                "from_id": args.from_id,
                "relationship": rel,
                "to_id": args.to_id,
                "context": args.context or "",
                "discovered_at": _now(),
            }
            _append_edge(research, edge)
    except BlockingIOError:
        print("Project is locked by another researcher — edge not added.", file=sys.stderr)
        return 2
    except (FileNotFoundError, OSError) as e:
        print(f"Edge not added (atomic create-abort): {e}", file=sys.stderr)
        return 1

    for r in results:
        print(r)
    print(f"Added {edge['edge_id']}: {args.from_id} -[{rel}]-> {args.to_id}")
    return 0


# ---------------------------------------------------------------------------
# Locked mutation primitives (Design 2: lock every shared-state write)
# ---------------------------------------------------------------------------
# These make the flock cover ALL mutations of the project files, so even though the
# agent's browsing (web tool calls) is not itself wrapped in a flock, any write to
# sources.jsonl / claims.jsonl / frontier.jsonl / state.json / edges.jsonl is
# serialized by the project lock. This is the crash-resistant design: a state-changing
# operation is a single locked CLI call.

def _lease_schema_ok(lease) -> bool:
    """Semantic schema validation for a worker lease object.

    Returns True only if the object is a well-formed lease. A syntactically-valid JSON
    object that FAILS this schema is treated as corrupt (fail-closed) — it must NOT be
    misread as a valid-but-expired lease (which would let a new worker take over) nor
    cause a comparison error (e.g. expiry being a string).

    Required fields:
      run_id        non-empty string
      status        a recognized value ("running")
      expires_at    finite numeric timestamp
      heartbeat_at  finite numeric timestamp
      started_at    string (validated only when present)
    """
    if not isinstance(lease, dict):
        return False
    run_id = lease.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return False
    if lease.get("status") not in ("running",):
        return False
    for key in ("expires_at", "heartbeat_at"):
        val = lease.get(key)
        # finite numeric timestamp (int/float, not bool, not NaN/inf, not str)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False
        if isinstance(val, float) and (val != val or val in (float("inf"), float("-inf"))):
            return False
    if "started_at" in lease and not isinstance(lease.get("started_at"), str):
        return False
    return True


def _read_lease_fail_closed(research: Path):
    """Read `.worker-lease.json` with FAIL-CLOSED semantics.

    Returns:
      (None, None)                    -> lease file absent (allow manual mutation)
      (lease_dict, None)              -> lease readable and schema-valid
      (None, "unreadable")            -> lease present but unreadable OR schema-invalid
                                          (corrupt / malformed) -> MUST refuse (fail closed)
    """
    p = research / ".worker-lease.json"
    if not p.exists():
        return None, None
    try:
        lease = json.loads(p.read_text())
        if not isinstance(lease, dict):
            return None, "unreadable"
        if not _lease_schema_ok(lease):
            return None, "unreadable"   # syntactically valid JSON but not a valid lease
        return lease, None
    except Exception:
        return None, "unreadable"


def _check_lease_owner(args, proj: Path, research: Path) -> bool:
    """Enforce optional token ownership on a mutation (FAIL-CLOSED on corrupt lease).

    - Lease absent      -> permit manual/administrative mutation.
    - Lease readable+expired -> permit (the worker's claim has lapsed).
    - Lease readable+live   -> require --run-id == lease.run_id, else REFUSE
                               (unless --operator-override).
    - Lease present but UNREADABLE (corrupt/truncated) -> REFUSE (fail closed): we
      cannot prove the worker is gone, so we must not write under an unknown owner.
    """
    lease, err = _read_lease_fail_closed(research)
    if err == "unreadable":
        if getattr(args, "operator_override", False):
            return True  # deliberate emergency override
        print(f"REFUSED: .worker-lease.json is present but unreadable/corrupt. Refusing "
              f"to mutate under an unknown lease owner. Fix/remove the lease deliberately, "
              f"or use --operator-override.", file=sys.stderr)
        return False
    if lease is None:
        return True  # absent -> manual operation permitted

    # Lease readable: live? (running + not expired)
    lease_live = lease.get("status") == "running" and time.time() < lease.get("expires_at", 0)
    if not lease_live:
        return True  # expired/stale -> worker claim lapsed

    given = getattr(args, "run_id", None)
    if given and given == lease.get("run_id"):
        return True
    if getattr(args, "operator_override", False):
        return True
    print(f"REFUSED: campaign has a live worker lease (run_id={lease.get('run_id')}). "
          f"Pass --run-id <that token> to prove you own this run, or --operator-override to "
          f"force a manual write.", file=sys.stderr)
    return False


def _lease_guard(args) -> int:
    """Legacy pre-lock ownership check (kept for backward-compat callers).
    New/refactored code MUST use _owned_project_lock, which validates INSIDE the
    flock. This pre-lock version is race-prone and only retained temporarily."""
    proj = Path(args.dir).expanduser().resolve()
    if not _check_lease_owner(args, proj, proj / ".research"):
        return 3
    return 0


def _owned_cm_fail(args, research, label, exit_code):
    """Handle a refusal/lock-ABORT consistently for _owned_project_lock callers."""
    if exit_code == 3:
        print("REFUSED: campaign has a live worker lease. Pass --run-id <token> to prove "
              "you own this run, or --operator-override.", file=sys.stderr)
    elif exit_code == 2:
        print(f"Project locked by another researcher — {label} not written.", file=sys.stderr)
    elif exit_code == 1:
        print("No state.json. Run `init` first.", file=sys.stderr)
    return exit_code


def _find_id_in_file(research: Path, fname: str, id_value: str, id_key: str) -> bool:
    """True if id_value appears under id_key in .research/<fname>."""
    try:
        for row in _load_jsonl(research / fname):
            if str(row.get(id_key)) == str(id_value):
                return True
    except Exception:
        return False
    return False


def _locked_append(args, fname: str, node: dict, label: str,
                   expected_prefix: str = None) -> int:
    """Append `node` to `.research/<fname>` with ATOMIC ownership+append under one lock.

    If `node` carries an explicit id (id or clue_id), it is validated INSIDE the lock:
      1. The id's prefix must match `expected_prefix` (e.g. SRC- for a source). A
         mismatched prefix is rejected (exit 5) — this prevents a source being written
         with a CLM-* id, which would silent-break graph resolution.
      2. The id must not already exist in the TARGET file under its declared key
         (exit 4 = DUPLICATE_ID).
    Automatic minting (id: None) is handled by _mint_and_append_locked instead.
    """
    research = Path(args.dir).expanduser().resolve() / ".research"
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, research, label, owned.exit_code)
            explicit = node.get("id") or node.get("clue_id")
            if explicit:
                # 1) Prefix/type validation against the command's expected prefix.
                if expected_prefix:
                    if not str(explicit).startswith(expected_prefix + "-"):
                        print(
                            f"REFUSED: id {explicit} has wrong prefix for `{label}` — "
                            f"expected {expected_prefix}-*. Use a valid {expected_prefix}-* "
                            f"id (or omit --id to auto-mint).",
                            file=sys.stderr,
                        )
                        return 5   # WRONG_PREFIX
                # 2) Duplicate check against the TARGET file with its declared key.
                prefix = expected_prefix or (str(explicit).split("-")[0] if "-" in str(explicit) else None)
                id_key = _node_id_key(prefix) if prefix else "id"
                if _find_id_in_file(research, fname, str(explicit), id_key):
                    print(f"REFUSED: duplicate id {explicit} already exists in {fname}.",
                          file=sys.stderr)
                    return 4   # DUPLICATE_ID
            with open(research / fname, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(node) + "\n")
    except BlockingIOError:
        return _owned_cm_fail(args, research, label, 2)
    print(f"Added {node.get('id', node.get('clue_id', 'record'))} to {fname} (locked)")
    return 0


def _mint_and_append_locked(args, fname: str, prefix: str, label: str,
                            node: dict, id_key: str = None) -> int:
    """Atomically mint an id AND append `node` under one flock.

    `node` must carry `id_key: None` (or absent). The id is computed from the current
    file contents INSIDE the lock (closing the ID-allocation race), the node is filled,
    then appended within the same critical section (lease check + mint + append). The
    identifier key defaults to NODE_ID_KEYS[prefix] (clue_id for CLUE/DE).
    """
    if id_key is None:
        id_key = _node_id_key(prefix)
    research = Path(args.dir).expanduser().resolve() / ".research"
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, research, label, owned.exit_code)
            nid = _mint_id(research, fname, prefix)   # mint INSIDE lock
            node2 = dict(node); node2[id_key] = nid
            with open(research / fname, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(node2) + "\n")
    except BlockingIOError:
        return _owned_cm_fail(args, research, label, 2)
    print(f"Added {nid} to {fname} (locked)")
    return 0


def _owned_project_lock(args):
    """Context manager: ATOMIC lease-check-then-mutate under the project flock.

    Acquires the project flock FIRST, then validates lease ownership INSIDE the
    critical section, then yields. This closes the check-then-lock race: the lease
    read + ownership decision + the mutation share one lock.

    Uses fcntl only (Unix). On timeout raises BlockingIOError.

    Caller inspects .refused / .locked EXIT codes on the returned object, or lets
    exceptions propagate. Always use as:
        with _owned_project_lock(args) as owned:
            if owned.exit: return owned.exit      # REFUSED(3) or LOCKED(2) or ABORT(1)
            ... mutate ...
    """
    import fcntl as _fcntl
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    state_f = research / "state.json"
    if not state_f.exists():
        return _OwnedCM(proj, research, exit_code=1)  # ABORT: no state.json
    research.mkdir(parents=True, exist_ok=True)
    lockfile = research / ".lock"
    timeout = max(1.0, float(getattr(args, "lock_timeout", 10)))
    fh = open(lockfile, "a+b")
    deadline = time.time() + timeout
    acquired = False
    try:
        while True:
            try:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if getattr(e, "errno", None) not in (11,):  # EWOULDBLOCK/EAGAIN
                    raise
                if time.time() >= deadline:
                    raise BlockingIOError("project lock timeout")
                time.sleep(0.05)
        # INSIDE the flock: validate lease ownership
        if not _check_lease_owner(args, proj, research):
            cm = _OwnedCM(proj, research, exit_code=3)   # REFUSED by lease ownership
            cm.set_fh(fh)
            return cm   # caller must __exit__ to release the lock
        cm = _OwnedCM(proj, research, exit_code=0)
        cm.set_fh(fh)
        return cm
    except BaseException:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        fh.close()
        raise


class _OwnedCM:
    """Result/handle from _owned_project_lock. Exits the flock on __exit__."""
    __slots__ = ("proj", "research", "exit_code", "_fh")

    def __init__(self, proj, research, exit_code):
        self.proj = proj; self.research = research
        self.exit_code = exit_code; self._fh = None

    def set_fh(self, fh):
        self._fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            import fcntl as _fcntl
            try:
                _fcntl.flock(self._fh.fileno(), _fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        return False





def _node_id_key(prefix: str) -> str:
    """Return the identifier field name for a record prefix."""
    return NODE_ID_KEYS.get(prefix, "id")


def _mint_id(research: Path, fname: str, prefix: str) -> str:
    """Mint the next free id for `prefix` records in `fname`.

    Uses the correct identifier key per type (clue_id for CLUE/DE records, id otherwise)
    so frontier/dead-end records do not collide (CLUE-0001 / DE-0001 must not duplicate).
    """
    id_key = _node_id_key(prefix)
    existing = {n.get(id_key) for n in _load_jsonl(research / fname) if n.get(id_key)}
    i = 1
    while True:
        cand = f"{prefix}-{i:04d}"
        if cand not in existing:
            return cand
        i += 1


def cmd_source_add(args) -> int:
    """Append a source node (SRC-*) to sources.jsonl UNDER THE PROJECT LOCK.

    Every source write goes through here so the flock guards it. Keeps the original
    url (provenance) and the canonical_url.
    """
    canon = canonicalize_url(args.url) if args.url else None
    node = {
        "id": None, "url": args.url, "canonical_url": canon,
        "title": args.title or "", "author": args.author,
        "publisher": args.publisher, "type": args.type or "primary_author_text",
        "accessed": args.accessed or _now()[:10], "content_fingerprint": None,
        "note": args.note or "",
    }
    if getattr(args, "id", None):
        node["id"] = args.id
        return _locked_append(args, "sources.jsonl", node, "source", expected_prefix="SRC")
    return _mint_and_append_locked(args, "sources.jsonl", "SRC", "source", node)


def cmd_claim_add(args) -> int:
    """Append a claim (CLM-*) to claims.jsonl UNDER THE PROJECT LOCK (atomic mint)."""
    srcs = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    node = {
        "id": None, "claim": args.claim, "status": args.status or "strong",
        "confidence": args.confidence or "high", "sources": srcs,
        "technique": args.technique or "", "verification_note": args.note or "",
    }
    if getattr(args, "id", None):
        node["id"] = args.id
        return _locked_append(args, "claims.jsonl", node, "claim", expected_prefix="CLM")
    return _mint_and_append_locked(args, "claims.jsonl", "CLM", "claim", node)


def cmd_frontier_add(args) -> int:
    """Append a clue (CLUE-*) to frontier.jsonl UNDER THE PROJECT LOCK (atomic mint)."""
    node = {
        "clue_id": None,
        "description": args.description,
        "parent": args.parent,
        "depth": int(args.depth or 1),
        "relevance": int(args.relevance or 5),
        "primary_source_likelihood": int(args.primary_likelihood or 5),
        "info_gain": int(args.info_gain or 5),
        "resolves_uncertainty": int(args.resolves or 5),
        "novelty": int(args.novelty or 5),
        "ease": int(args.ease or 5),
        "status": "pending",
        "attempts": 0,
    }
    if getattr(args, "id", None):
        node["clue_id"] = args.id
        return _locked_append(args, "frontier.jsonl", node, "clue", expected_prefix="CLUE")
    return _mint_and_append_locked(args, "frontier.jsonl", "CLUE", "clue", node,
                                   id_key="clue_id")


def cmd_frontier_update(args) -> int:
    """Update a clue's status/attempts ATOMICALLY (lease check + rewrite under one lock)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "frontier", owned.exit_code)
            rows = _load_jsonl(research / "frontier.jsonl")
            hit = False
            for r in rows:
                if r.get("clue_id") == args.clue:
                    if args.status:
                        r["status"] = args.status
                    r["attempts"] = r.get("attempts", 0) + (1 if args.attempt else 0)
                    hit = True
            if not hit:
                print(f"clue {args.clue} not found", file=sys.stderr)
                return 1
            (research / "frontier.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))
    except BlockingIOError:
        return _owned_cm_fail(args, research, "frontier", 2)
    print(f"Updated {args.clue} (status={args.status or 'unchanged'})")
    return 0


# ---------------------------------------------------------------------------
# v0.2.4: lock EVERY shared-state mutation (search-log / dead-ends / criteria /
#         contradiction / final-report). No raw .research writes.
# ---------------------------------------------------------------------------

def cmd_searchlog_add(args) -> int:
    """Append a search-log entry (query + outcome) UNDER THE PROJECT LOCK."""
    node = {"ts": _now(), "query": args.query, "strategy": args.strategy or "",
            "results": args.results or 0, "outcome": args.outcome or ""}
    return _locked_append(args, "search-log.jsonl", node, "search-log")


def cmd_deadend_add(args) -> int:
    """Append a dead-end branch UNDER THE PROJECT LOCK (atomic mint)."""
    node = {"clue_id": None, "from_source": args.parent,
            "description": args.description,
            "attempted": args.attempted or "",
            "queries": args.queries or "",
            "sources": args.sources or "",
            "why_failed": args.why_failed or "",
            "may_reopen": bool(args.may_reopen),
            "reopen_conditions": args.reopen_conditions or ""}
    if getattr(args, "id", None):
        node["clue_id"] = args.id
        return _locked_append(args, "dead-ends.jsonl", node, "dead-end", expected_prefix="DE")
    return _mint_and_append_locked(args, "dead-ends.jsonl", "DE", "dead-end", node,
                                   id_key="clue_id")


def cmd_criterion_add(args) -> int:
    """Append an acceptance criterion UNDER THE PROJECT LOCK (atomic mint)."""
    node = {"id": None, "description": args.description,
            "evidence_required": args.evidence_required or "primary_or_exception",
            "corroboration_required": bool(args.corroboration),
            "primary_hard": bool(args.primary_hard),
            "met": False, "evidence_source_ids": [], "exception": None}
    if getattr(args, "id", None):
        node["id"] = args.id
        return _locked_append(args, "criteria.jsonl", node, "criterion", expected_prefix="C")
    return _mint_and_append_locked(args, "criteria.jsonl", "C", "criterion", node)


def cmd_criterion_update(args) -> int:
    """Update a criterion (met / evidence / exception) ATOMICALLY (lease check under lock)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    fname = research / "criteria.jsonl"
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "criterion", owned.exit_code)
            if not fname.exists():
                print("No criteria.jsonl. Run `init` first.", file=sys.stderr)
                return 1
            rows = _load_jsonl(fname)
            target = next((r for r in rows if r.get("id") == args.criterion), None)
            if target is None:
                print(f"criterion {args.criterion} not found", file=sys.stderr)
                return 1
            if args.met is not None:
                target["met"] = args.met in ("1", "true", "True", "yes")
            if args.evidence:
                target["evidence_source_ids"] = [s.strip() for s in args.evidence.split(",") if s.strip()]
            if args.exception is not None:
                target["exception"] = args.exception or None
            fname.write_text("".join(json.dumps(r) + "\n" for r in rows))
    except BlockingIOError:
        return _owned_cm_fail(args, research, "criterion", 2)
    print(f"Updated criterion {args.criterion}")
    return 0


def cmd_contradiction_add(args) -> int:
    """Append a contradiction node UNDER THE PROJECT LOCK (atomic mint)."""
    node = {"id": None, "critical": bool(args.critical), "resolved": False,
            "description": args.description, "side_a": args.side_a or "",
            "side_b": args.side_b or "", "resolution_notes": args.notes or ""}
    if getattr(args, "id", None):
        node["id"] = args.id
        return _locked_append(args, "contradictions.jsonl", node, "contradiction", expected_prefix="X")
    return _mint_and_append_locked(args, "contradictions.jsonl", "X", "contradiction", node)


def cmd_contradiction_resolve(args) -> int:
    """Mark a contradiction resolved (with note) ATOMICALLY (lease check under lock)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    fname = research / "contradictions.jsonl"
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "contradiction", owned.exit_code)
            if not fname.exists():
                print("No contradictions.jsonl.", file=sys.stderr)
                return 1
            rows = _load_jsonl(fname)
            target = next((r for r in rows if r.get("id") == args.contradiction), None)
            if target is None:
                print(f"contradiction {args.contradiction} not found", file=sys.stderr)
                return 1
            target["resolved"] = True
            target["resolution_notes"] = args.note or target.get("resolution_notes", "")
            fname.write_text("".join(json.dumps(r) + "\n" for r in rows))
    except BlockingIOError:
        return _owned_cm_fail(args, research, "contradiction", 2)
    print(f"Marked {args.contradiction} resolved")
    return 0


def cmd_report_write(args) -> int:
    """Write the final-report.md ATOMICALLY (lease check under lock)."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    body = args.content if getattr(args, "content", None) else ""
    try:
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "report", owned.exit_code)
            (research / "final-report.md").write_text(body)
    except BlockingIOError:
        return _owned_cm_fail(args, research, "report", 2)
    print(f"Wrote final-report.md ({len(body)} chars)")
    return 0


def cmd_cron_wrapper(args) -> int:
    """Generate a per-campaign cron pre-run lease-gate wrapper.

    WHY: Hermes runs a cron `script=` pre-run in the SCRIPT'S OWN directory
    (`{HERMES_HOME}/scripts/`), NOT the job's workdir (scheduler.py:2313:
    `_script_cwd = workdir or str(path.parent)`; the wake-gate caller passes no
    workdir). So a gate that relies on `cwd` to find the campaign will see
    `~/.hermes/scripts/` and wrongly emit `{"wakeAgent": false}` — the agent never
    starts. This wrapper bakes in the ABSOLUTE project path, so it works regardless
    of Hermes' cwd behaviour.
    """
    import os
    proj = Path(args.dir).expanduser().resolve()
    if not (proj / ".research" / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    # Directory for the generated wrapper. Defaults to the Hermes cron script sandbox;
    # an operator (or tests) can redirect with ENDLESS_SCRIPTS_DIR.
    base_dir = os.environ.get("ENDLESS_SCRIPTS_DIR") or os.path.expanduser("~/.hermes/scripts")
    scripts_dir = Path(base_dir)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    name = (args.name or proj.name or "campaign").strip()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    wrapper = scripts_dir / f"{safe}-lease-gate.sh"

    gate_candidates = [
        Path(os.path.expanduser("~/.hermes/scripts/campaign-lease-gate.py")),
        # repo root scripts/ (skill/scripts/research_project.py -> ../../scripts)
        Path(__file__).resolve().parent.parent.parent / "scripts" / "campaign-lease-gate.py",
        # sibling when running the gate from the repo scripts dir
        Path(__file__).resolve().parent / "campaign-lease-gate.py",
    ]
    gate = next((p for p in gate_candidates if p.exists()), None)
    if gate is None:
        print("campaign-lease-gate.py not found; install it first.", file=sys.stderr)
        return 1

    if args.run_id:
        exec_line = 'exec python3 "$GATE" CHECK "$PROJECT" --run-id "$RUN_ID"'
    else:
        exec_line = 'exec python3 "$GATE" CHECK "$PROJECT"'
    content = (
        f"#!/usr/bin/env bash\n"
        f"# Per-campaign lease gate for: {proj}\n"
        f"# Generated by `endless-research cron-wrapper`. Attach to a research cron job\n"
        f"# as:  --script \"{wrapper.name}\"\n"
        f"# so the gate finds the campaign regardless of Hermes' cwd behaviour.\n"
        f"GATE={gate}\n"
        f"PROJECT={proj}\n"
        f'RUN_ID="${{RUN_ID:-}}"\n'
        f"{exec_line}\n"
    )
    wrapper.write_text(content)
    try:
        os.chmod(wrapper, 0o755)
    except OSError:
        pass
    print(f"Wrote cron pre-run wrapper: {wrapper}")
    print(f"Attach to the research job as:  --script \"{wrapper.name}\"")
    return 0



def cmd_graph(args) -> int:
    """Summarise the evidence graph: nodes by kind, edges by relationship.

    This is the machine-checkable view that turns 'what was found' into 'where it
    came from, what it supports, what contradicts it, and how things connect'.
    """
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    edges = _load_edges(research)
    print("== Graph summary ==")
    nodes = {}
    for kind, fname in NODE_KINDS.items():
        n = len(_load_jsonl(research / fname))
        if n:
            nodes[kind] = n
    if nodes:
        print("  nodes:")
        for kind, n in nodes.items():
            print(f"    {kind:6} {n:>5}")
    else:
        print("  nodes: (none recorded yet)")

    by_rel = {}
    for e in edges:
        by_rel.setdefault(e.get("relationship"), []).append(e)
    if by_rel:
        print("  edges:")
        for rel in sorted(by_rel):
            print(f"    {rel:20} {len(by_rel[rel]):>5}")
    else:
        print("  edges: (none recorded — use `edge` to add typed relationships)")

    if args.recent:
        print("  recent edges:")
        for e in edges[-int(args.recent):]:
            print(f"    {e.get('edge_id')}: {e.get('from_id')} -[{e.get('relationship')}]-> {e.get('to_id')}")
    if not args.no_validate:
        def _node_present(nid):
            if not (nid and str(nid).strip()):
                return True  # blank endpoint — skip (not meaningful)
            return _node_exists(research, str(nid))
        # Audit BOTH endpoints to catch damaged / imported / legacy graph data.
        bad_from = [e for e in edges if not _node_present(e.get("from_id"))]
        bad_to = [e for e in edges if not _node_present(e.get("to_id"))]
        seen, bad = set(), []
        for e in bad_from + bad_to:          # dedupe by edge_id, keep order
            if e.get("edge_id") in seen:
                continue
            seen.add(e.get("edge_id")); bad.append(e)
        if bad:
            print(f"  WARN: {len(bad)} edge(s) reference a node (from_id or to_id) "
                  f"that is not present in the graph files.")
            for e in bad[:10]:
                print(f"    {e.get('edge_id')}: {e.get('from_id')} "
                      f"-[{e.get('relationship')}]-> {e.get('to_id')}")
        else:
            print("  validation: all edge endpoints resolve to existing nodes.")
    return 0


# ---------------------------------------------------------------------------
# URL intelligence (v0.2.0)
# ---------------------------------------------------------------------------

_FRAGMENT_ONLY = re.compile(r"^#")

# Parameters that are ALWAYS safe to strip — universally recognised tracking markers.
# These are unambiguous across sites. Do NOT add ref/source/from/share here: on some
# sites those values are semantically meaningful (routing, campaign identity, content
# selection), so stripping them could merge genuinely different pages.
_ALWAYS_STRIP_TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_campaignid", "fbclid", "gclid",
}

# Parameters that are USUALLY tracking but CAN be semantic on some sites. We do NOT
# remove these by default; they are only stripped when explicitly enabled via
# `aggressive=True` / scope policy. Their presence must not force two different pages
# to collide.
_CONDITIONALLY_TRACKING = {"ref", "source", "from", "share", "spm", "mc_cid", "igshid"}


def canonicalize_url(
    url: str,
    *,
    strip_www: bool = False,
    conditional_params: frozenset = frozenset(),
) -> str:
    """Canonicize a URL CONSERVATIVELY for duplicate detection.

    Default behaviour (safe on any site):
      - strips the URL fragment
      - always drops only the universally-safe tracking params (utm_*, fbclid, gclid)
      - strips empty query params
      - sorts remaining params for stable identity
      - lowercases scheme + host (case-insensitive host)

    It does NOT, by default:
      - strip ``www.`` (www.example.com and example.com can be different sites),
      - strip ``ref``/``source``/``from``/``share`` (semantically meaningful on some
        sites; a different value can mean different content),
      - normalise trailing slashes (path identity is intentional).

    To opt into stronger collapsing (used only for genuinely duplicate-detection where
    the operator has verified domain semantics), pass ``strip_www=True`` and/or put
    ``ref``/``source``/``from``/``share`` in ``conditional_params``.

    Provenance: because canonicalization intentionally loses information, callers that
    store a source SHOULD keep the originally-encountered URL alongside the canonical
    form (e.g. ``url`` + ``canonical_url``), never replace the original.
    """
    if not url:
        return url
    url = url.strip()
    if _FRAGMENT_ONLY.match(url):
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url

    scheme = (p.scheme or "").lower()
    netloc = (p.netloc or "").lower()
    if strip_www and netloc.startswith("www."):
        netloc = netloc[4:]

    # Never drop the semantically-meaningful params unless explicitly enabled.
    to_strip = set(_ALWAYS_STRIP_TRACKING) | set(conditional_params)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
          if k not in to_strip and v != ""]
    qs.sort()
    query = urlencode(qs)
    path = p.path or "/"
    return urlunparse((scheme, netloc, path, p.params, query, ""))


def content_fingerprint(text: str) -> str:
    """Return a stable content hash for duplicate detection."""
    blob = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def cmd_inspect(args) -> int:
    """Display URL intelligence for a seed URL without fetching: canonical form,
    candidate content fingerprint, and the campaign scope rules.

    The agent can run `inspect <url>` BEFORE deciding to dig, so it never blindly
    crawls: it sees the canonical target and the budget/domain policy up front.
    """
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    url = args.url
    canon = canonicalize_url(url)
    print(f"url                 : {url}")
    print(f"canonical_url       : {canon}")
    if canon != url:
        print(f"  (normalised: {url} -> {canon})")
    print(f"content_fingerprint : {content_fingerprint(url)} (hash of text to be provided)")
    scope = research / "scope.json"
    if scope.exists():
        print("scope:")
        for k, v in json.loads(scope.read_text()).items():
            print(f"  {k:24}: {v}")
    return 0


# ---------------------------------------------------------------------------
# Objective clarifier / compiler (v0.2.0)
# ---------------------------------------------------------------------------

_CLARIFY_FINE = {
    "understand": ["Understand the project/entity's purpose.",
                   "Understand how it technically works.",
                   "Map its architecture and key components."],
    "credibility": ["Evaluate whether its major claims are credible.",
                    "Trace important claims to primary sources.",
                    "Identify limitations and criticism."],
    "origin": ["Identify who created it and its dependencies.",
               "Trace provenance to the original source."],
    "safety": ["Assess safety, privacy, and risk considerations."],
    "reproduce": ["Locate source code / implementation and reproduction steps."],
    "business": ["Assess commercial viability, funding, and adoption."],
}
_CLARIFY_SCOPE = "External links will be followed when relevant, subject to scope.json."


def cmd_clarify(args) -> int:
    """Short, smart objective compiler.

    Turns a URL + a vague goal into a concrete research contract — but only asks
    focused questions when the objective is materially ambiguous. For clear goals it
    proposes well-scoped defaults and records assumptions, so it never over-interrupts.

    Modes:
      clear    -> compile immediately, no questions
      vague    -> propose sensible defaults, record assumptions, compile
      ambiguous-> ask the 1-3 essential questions (what/scope/evidence/success)
    """
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1

    url = args.url
    mode = (args.mode or "auto").lower()
    goal = (args.goal or "").strip().lower()

    # Which aspect clusters apply to the stated goal?
    aspects = []
    if any(w in goal for w in ("technic", "how it work", "architecture", "how does", "works")):
        aspects += _CLARIFY_FINE["understand"]
    if any(w in goal for w in ("credib", "claim", "true", "verify", "reliable", "legit")):
        aspects += _CLARIFY_FINE["credibility"]
    if any(w in goal for w in ("who", "create", "author", "creator", "origin", "provenance")):
        aspects += _CLARIFY_FINE["origin"]
    if any(w in goal for w in ("safe", "secur", "risk", "privacy")):
        aspects += _CLARIFY_FINE["safety"]
    if any(w in goal for w in ("reproduc", "implement", "code", "source", "install")):
        aspects += _CLARIFY_FINE["reproduce"]
    if any(w in goal for w in ("viab", "commerc", "business", "fund", "adopt", "money")):
        aspects += _CLARIFY_FINE["business"]

    if not aspects:
        # No signal: treat 'learn everything important' as the classic broad set.
        aspects = (_CLARIFY_FINE["understand"] + _CLARIFY_FINE["credibility"]
                   + _CLARIFY_FINE["origin"])

    # Scope default.
    scope = "Internal + external links when relevant (subject to scope.json)."

    print("== Objective Clarifier ==")
    print(f"seed_url  : {canonicalize_url(url)}")

    if mode == "ambiguous" or (mode == "auto" and goal in ("", "everything", "learn everything", "tell me about")):
        print("questions (please answer so I can sharpen the objective):")
        print("  1. What exactly do you want to understand about this?")
        print("  2. Strictly this site, or follow relevant external links too?")
        print("  3. What evidence standard do you need (casual | solid | strict)?")
        print("  4. What would count as a satisfactory answer for you?")
        return 0

    print("I interpret your goal as:")
    for a in aspects:
        print(f"  - {a}")
    print(f"  - scope: {scope}")
    print("assumptions recorded:")
    print(f"  - mode={mode} (clear/vague defaults applied; review & edit in objective.md)")

    if not args.no_write:
        obj_path = research / "objective.md"
        lines = [
            f"# Objective (compiled {_now()})",
            "",
            f"## Seed URL",
            f"{canonicalize_url(url)}",
            "",
            f"## Goal (compiled from: '{args.goal}')",
        ]
        lines += [f"- {a}" for a in aspects]
        lines += [
            "",
            f"## Scope",
            f"- {scope}",
            "",
            "## Success conditions (define these; the SUCCESS gate checks criteria.jsonl)",
            "- (_edit_)",
        ]
        with _owned_project_lock(args) as owned:
            if owned.exit_code:
                return _owned_cm_fail(args, owned.research, "clarify", owned.exit_code)
            obj_path.write_text("\n".join(lines) + "\n")
        print(f"compiled objective -> {obj_path}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="research_project.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="scaffold a project")
    pi.add_argument("dir")
    pi.add_argument("--objective", default=None)
    pi.add_argument("--success", default=None)
    pi.add_argument("--failure", default=None)
    pi.add_argument("--now", action="store_true", help="(reserved)")
    pi.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pi.add_argument("--run-id", default=None,
                    help="prove ownership when re-running init on an active campaign")
    pi.add_argument("--operator-override", action="store_true",
                    help="force a re-init despite a live lease (emergency)")
    pi.set_defaults(fn=cmd_init)

    ps = sub.add_parser("status", help="print state + priority-sorted queue")
    ps.add_argument("dir")
    ps.set_defaults(fn=cmd_status)

    pr = sub.add_parser("resignal", help="set current_state")
    pr.add_argument("dir")
    pr.add_argument("state", choices=sorted(STATES), type=str.upper)
    pr.add_argument("--note", default=None)
    pr.add_argument("--cron", default=None,
                    help="cron job id to auto-pause on SUCCESS/EXHAUSTED and re-resume "
                         "when leaving a terminal state (deterministic self-stop)")
    pr.add_argument("--force", action="store_true",
                    help="bypass the deterministic SUCCESS gate (use only if you accept "
                         "the risk of an unverified SUCCESS)")
    pr.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10,
                    help="seconds to wait for the project lock (default 10)")
    pr.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pr.add_argument("--operator-override", action="store_true",
                    help="force a manual write despite a live lease (emergency)")
    pr.set_defaults(fn=cmd_resignal)

    prs = sub.add_parser("reset", help="re-open a project")
    prs.add_argument("dir")
    prs.add_argument("--state", action="store_true", help="reset state to CONTINUE")
    prs.add_argument("--frontier", action="store_true", help="mark frontier pending")
    prs.add_argument("--all", action="store_true", help="reset frontier+evidence+state")
    prs.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    prs.add_argument("--operator-override", action="store_true",
                     help="force a reset despite a live worker lease (emergency)")
    prs.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    prs.set_defaults(fn=cmd_reset)

    pc = sub.add_parser("checkpoint", help="snapshot now")
    pc.add_argument("dir")
    pc.add_argument("--note", default=None)
    pc.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pc.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pc.add_argument("--cron-job-id", default=None, help="cron job id to embed in the checkpoint")
    pc.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pc.set_defaults(fn=cmd_checkpoint)

    pr = sub.add_parser("run", help="per-run audit journal (start/finish/abort/audit)")
    rsub = pr.add_subparsers(dest="run_op", required=True)
    rstart = rsub.add_parser("start", help="record a 'started' audit event")
    rstart.add_argument("dir")
    rstart.add_argument("--run-id", required=True)
    rstart.add_argument("--cron-job-id", default=None)
    rstart.add_argument("--state", default=None)
    rstart.add_argument("--rounds", type=int, default=None)
    rstart.add_argument("--session-id", default=None, help="override HERMES_SESSION_ID (tests/admin)")
    rstart.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    rstart.set_defaults(fn=cmd_run_start)
    rfin = rsub.add_parser("finish", help="record a 'completed' audit event")
    rfin.add_argument("dir")
    rfin.add_argument("--run-id", required=True)
    rfin.add_argument("--cron-job-id", default=None)
    rfin.add_argument("--checkpoint", default=None)
    rfin.add_argument("--state", default=None)
    rfin.add_argument("--sources", type=int, default=0)
    rfin.add_argument("--claims", type=int, default=0)
    rfin.add_argument("--edges", type=int, default=0)
    rfin.add_argument("--next-clue", default=None)
    rfin.add_argument("--result", default="completed")
    rfin.add_argument("--session-id", default=None)
    rfin.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    rfin.set_defaults(fn=cmd_run_finish)
    rab = rsub.add_parser("abort", help="record an intentional 'aborted' event")
    rab.add_argument("dir")
    rab.add_argument("--run-id", required=True)
    rab.add_argument("--cron-job-id", default=None)
    rab.add_argument("--reason", default=None)
    rab.add_argument("--session-id", default=None)
    rab.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    rab.set_defaults(fn=cmd_run_abort)
    raud = rsub.add_parser("audit", help="reconcile run history (crashed/duplicate)")
    raud.add_argument("dir")
    raud.add_argument("--json", action="store_true")
    raud.set_defaults(fn=cmd_run_audit)

    pt = sub.add_parser("tick", help="run ONE research tick under the atomic project lock")
    pt.add_argument("dir")
    pt.add_argument("--cmd", default=None, help="subcommand to run while holding the lock (the dig work)")
    pt.add_argument("--note", default=None, help="checkpoint note for this tick")
    pt.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=30,
                    help="seconds to wait for the project lock before giving up (default 30)")
    pt.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pt.add_argument("--operator-override", action="store_true",
                    help="force a tick despite a live lease (emergency)")
    pt.set_defaults(fn=cmd_tick)

    pv = sub.add_parser("verify_success", help="deterministic SUCCESS gate (blocks premature SUCCESS)")
    pv.add_argument("dir")
    pv.add_argument("--min-corroboration", type=int, default=2,
                    help="minimum independent sources for corroboration-required criteria")
    pv.set_defaults(fn=cmd_verify_success)

    # --- v0.2.0 graph / URL / clarifier commands ---
    pe = sub.add_parser("edge", help="add an explicit typed edge to the evidence graph (atomic node+edge)")
    pe.add_argument("dir")
    pe.add_argument("from_id")
    pe.add_argument("relationship", choices=sorted(RELATIONSHIPS))
    pe.add_argument("to_id")
    pe.add_argument("--context", default=None, help="why this edge matters / surrounding text")
    pe.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10,
                    help="seconds to wait for the project lock before giving up (default 10)")
    pe.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pe.add_argument("--operator-override", action="store_true",
                    help="force a manual write despite a live lease (emergency)")
    pe.set_defaults(fn=cmd_edge)

    pg = sub.add_parser("graph", help="summarise the evidence graph (nodes + edges)")
    pg.add_argument("dir")
    pg.add_argument("--recent", default=None,
                    help="print the N most recent edges")
    pg.add_argument("--no-validate", action="store_true",
                    help="skip referential-integrity warnings")
    pg.set_defaults(fn=cmd_graph)

    pi2 = sub.add_parser("inspect", help="URL intelligence: canonical form + scope rules")
    pi2.add_argument("dir")
    pi2.add_argument("url")
    pi2.set_defaults(fn=cmd_inspect)

    pc2 = sub.add_parser("clarify", help="objective compiler: turn URL+goal into a research contract")
    pc2.add_argument("dir")
    pc2.add_argument("url")
    pc2.add_argument("--goal", default="", help="what you want to understand")
    pc2.add_argument("--mode", default="auto", choices=["auto", "clear", "vague", "ambiguous"],
                     help="clear -> compile; vague -> defaults; ambiguous -> ask questions")
    pc2.add_argument("--no-write", action="store_true", help="do not overwrite objective.md")
    pc2.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pc2.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pc2.add_argument("--operator-override", action="store_true",
                     help="force the objective write despite a live lease (emergency)")
    pc2.set_defaults(fn=cmd_clarify)

    pwrap = sub.add_parser("cron-wrapper",
                           help="generate a per-campaign lease-gate wrapper for a cron `script=` field")
    pwrap.add_argument("dir")
    pwrap.add_argument("--name", default=None, help="short name -> <name>-lease-gate.sh")
    pwrap.add_argument("--run-id", default=None,
                       help="bake a token so the gate enforces token ownership")
    pwrap.set_defaults(fn=cmd_cron_wrapper)

    # --- v0.2.2 locked mutation primitives (Design 2) ---
    # source / claim / frontier writes all acquire the project lock, so every
    # shared-state mutation is flock-guarded even if browsing is not wrapped.
    psrc = sub.add_parser("source", help="add a source node (SRC-*) — LOCKED write")
    psrc_sub = psrc.add_subparsers(dest="op", required=True)
    psrc_a = psrc_sub.add_parser("add", help="append a source")
    psrc_a.add_argument("dir")
    psrc_a.add_argument("--url", required=True)
    psrc_a.add_argument("--title", default=None)
    psrc_a.add_argument("--author", default=None)
    psrc_a.add_argument("--publisher", default=None)
    psrc_a.add_argument("--type", default=None)
    psrc_a.add_argument("--id", default=None)
    psrc_a.add_argument("--accessed", default=None)
    psrc_a.add_argument("--note", default=None)
    psrc_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    psrc_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    psrc_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    psrc_a.set_defaults(fn=cmd_source_add)

    pcl = sub.add_parser("claim", help="add a claim node (CLM-*) — LOCKED write")
    pcl_sub = pcl.add_subparsers(dest="op", required=True)
    pcl_a = pcl_sub.add_parser("add", help="append a claim")
    pcl_a.add_argument("dir")
    pcl_a.add_argument("--claim", required=True)
    pcl_a.add_argument("--sources", default=None, help="comma-separated source ids")
    pcl_a.add_argument("--status", default=None)
    pcl_a.add_argument("--confidence", default=None)
    pcl_a.add_argument("--technique", default=None)
    pcl_a.add_argument("--id", default=None)
    pcl_a.add_argument("--note", default=None)
    pcl_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pcl_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pcl_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pcl_a.set_defaults(fn=cmd_claim_add)

    pfr = sub.add_parser("frontier", help="add/update frontier clues — LOCKED writes")
    pfr_sub = pfr.add_subparsers(dest="op", required=True)
    pfr_a = pfr_sub.add_parser("add", help="append a clue")
    pfr_a.add_argument("dir")
    pfr_a.add_argument("--description", required=True)
    pfr_a.add_argument("--parent", default=None)
    pfr_a.add_argument("--id", default=None)
    pfr_a.add_argument("--depth", default=1)
    pfr_a.add_argument("--relevance", default=5)
    pfr_a.add_argument("--primary-likelihood", default=5)
    pfr_a.add_argument("--info-gain", default=5)
    pfr_a.add_argument("--resolves", default=5)
    pfr_a.add_argument("--novelty", default=5)
    pfr_a.add_argument("--ease", default=5)
    pfr_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pfr_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pfr_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pfr_a.set_defaults(fn=cmd_frontier_add)
    pfr_u = pfr_sub.add_parser("update", help="update a clue status/attempts")
    pfr_u.add_argument("dir")
    pfr_u.add_argument("clue")
    pfr_u.add_argument("--status", default=None)
    pfr_u.add_argument("--attempt", action="store_true")
    pfr_u.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pfr_u.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pfr_u.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pfr_u.set_defaults(fn=cmd_frontier_update)

    # --- v0.2.4: lock the remaining mutations (search-log / dead-end / criterion / contradiction / report) ---
    pslog = sub.add_parser("search-log", help="add a search-log entry — LOCKED write")
    pslog_sub = pslog.add_subparsers(dest="op", required=True)
    pslog_a = pslog_sub.add_parser("add", help="append a search-log entry")
    pslog_a.add_argument("dir")
    pslog_a.add_argument("--query", required=True)
    pslog_a.add_argument("--strategy", default=None)
    pslog_a.add_argument("--results", type=int, default=0)
    pslog_a.add_argument("--outcome", default=None)
    pslog_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pslog_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pslog_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pslog_a.set_defaults(fn=cmd_searchlog_add)

    pde = sub.add_parser("dead-end", help="add a dead-end branch — LOCKED write")
    pde_sub = pde.add_subparsers(dest="op", required=True)
    pde_a = pde_sub.add_parser("add", help="append a dead-end")
    pde_a.add_argument("dir")
    pde_a.add_argument("--description", required=True)
    pde_a.add_argument("--parent", default=None)
    pde_a.add_argument("--id", default=None)
    pde_a.add_argument("--attempted", default=None)
    pde_a.add_argument("--queries", default=None)
    pde_a.add_argument("--sources", default=None)
    pde_a.add_argument("--why-failed", default=None)
    pde_a.add_argument("--may-reopen", action="store_true")
    pde_a.add_argument("--reopen-conditions", default=None)
    pde_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pde_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pde_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pde_a.set_defaults(fn=cmd_deadend_add)

    pcrit = sub.add_parser("criterion", help="add/update acceptance criteria — LOCKED writes")
    pcrit_sub = pcrit.add_subparsers(dest="op", required=True)
    pcrit_a = pcrit_sub.add_parser("add", help="append a criterion")
    pcrit_a.add_argument("dir")
    pcrit_a.add_argument("--description", required=True)
    pcrit_a.add_argument("--evidence-required", default=None)
    pcrit_a.add_argument("--corroboration", action="store_true")
    pcrit_a.add_argument("--primary-hard", action="store_true")
    pcrit_a.add_argument("--id", default=None)
    pcrit_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pcrit_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pcrit_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pcrit_a.set_defaults(fn=cmd_criterion_add)
    pcrit_u = pcrit_sub.add_parser("update", help="update a criterion")
    pcrit_u.add_argument("dir")
    pcrit_u.add_argument("criterion")
    pcrit_u.add_argument("--met", default=None, help="1/0")
    pcrit_u.add_argument("--evidence", default=None, help="comma-separated source ids")
    pcrit_u.add_argument("--exception", default=None)
    pcrit_u.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pcrit_u.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pcrit_u.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pcrit_u.set_defaults(fn=cmd_criterion_update)

    pcon = sub.add_parser("contradiction", help="add/resolve contradictions — LOCKED writes")
    pcon_sub = pcon.add_subparsers(dest="op", required=True)
    pcon_a = pcon_sub.add_parser("add", help="append a contradiction")
    pcon_a.add_argument("dir")
    pcon_a.add_argument("--description", required=True)
    pcon_a.add_argument("--critical", action="store_true")
    pcon_a.add_argument("--side-a", default=None)
    pcon_a.add_argument("--side-b", default=None)
    pcon_a.add_argument("--notes", default=None)
    pcon_a.add_argument("--id", default=None)
    pcon_a.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pcon_a.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pcon_a.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pcon_a.set_defaults(fn=cmd_contradiction_add)
    pcon_r = pcon_sub.add_parser("resolve", help="mark a contradiction resolved")
    pcon_r.add_argument("dir")
    pcon_r.add_argument("contradiction")
    pcon_r.add_argument("--note", default=None)
    pcon_r.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pcon_r.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    pcon_r.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    pcon_r.set_defaults(fn=cmd_contradiction_resolve)

    prep = sub.add_parser("report", help="write final-report.md — LOCKED write")
    prep_sub = prep.add_subparsers(dest="op", required=True)
    prep_w = prep_sub.add_parser("write", help="write the report body")
    prep_w.add_argument("dir")
    prep_w.add_argument("--content", required=True)
    prep_w.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    prep_w.add_argument("--run-id", default=None, help="prove ownership of the active worker lease")
    prep_w.add_argument("--operator-override", action="store_true", help="force a manual write despite a live lease (emergency)")
    prep_w.set_defaults(fn=cmd_report_write)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
