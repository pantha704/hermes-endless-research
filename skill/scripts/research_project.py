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

    # Read-modify-write state.json while holding the project lock (Design 2).
    try:
        with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
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
        print("Project locked by another researcher — state not changed.", file=sys.stderr)
        return 2
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
    front = _load_jsonl(research / "frontier.jsonl")
    if args.frontier or args.all:
        for f in front:
            f["status"] = "pending"
            f["attempts"] = 0
        research.joinpath("frontier.jsonl").write_text(
            "".join(json.dumps(f) + "\n" for f in front) or "")
        print("Frontier reset to pending.")
    if args.all:
        research.joinpath("claims.jsonl").write_text("")
        research.joinpath("dead-ends.jsonl").write_text("")
        research.joinpath("search-log.jsonl").write_text("")
        print("Claims/dead-ends/search-log cleared.")
    state = {}
    state_f = research / "state.json"
    if state_f.exists():
        state = json.loads(state_f.read_text())
    state["current_state"] = "CONTINUE"
    state["state_updated"] = _now()
    state["blockers"] = []
    state_f.write_text(json.dumps(state, indent=2))
    print("State reset to CONTINUE.")
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
    # Read-modify-write state.json + checkpoint file under the project lock (Design 2).
    try:
        with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
            state = json.loads(state_f.read_text())
            body = f"# Checkpoint {ts}\n\nstate       : {state.get('current_state')}\n"
            body += f"next_action : {state.get('next_action')}\nnote        : {args.note or ''}\n"
            cp.write_text(body)
            state["last_checkpoint"] = str(cp.relative_to(research))
            state_f.write_text(json.dumps(state, indent=2))
    except BlockingIOError:
        print("Project locked by another researcher — checkpoint skipped.", file=sys.stderr)
        return 2
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


def _node_exists(research: Path, node_id: str) -> bool:
    """True if node_id resolves to an existing node in the project files."""
    prefix = node_id.split("-")[0] if "-" in node_id else node_id
    fname = NODE_KINDS.get(prefix)
    if fname is None:
        return False
    p = research / fname
    if not p.exists():
        return False
    return any(rec.get("id") == node_id for rec in _load_jsonl(p))


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

def _locked_append(args, fname: str, node: dict, label: str) -> int:
    """Append `node` to `.research/<fname>` under the project lock."""
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    try:
        with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
            with open(research / fname, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(node) + "\n")
    except BlockingIOError:
        print(f"Project locked by another researcher — {label} not added.", file=sys.stderr)
        return 2
    print(f"Added {node.get('id', 'record')} to {fname} (locked)")
    return 0


def _mint_id(research: Path, fname: str, prefix: str) -> str:
    existing = {n.get("id") for n in _load_jsonl(research / fname)}
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
    research = Path(args.dir).expanduser().resolve() / ".research"
    sid = args.id or _mint_id(research, "sources.jsonl", "SRC")
    canon = canonicalize_url(args.url) if args.url else None
    node = {
        "id": sid,
        "url": args.url,
        "canonical_url": canon,
        "title": args.title or "",
        "author": args.author,
        "publisher": args.publisher,
        "type": args.type or "primary_author_text",
        "accessed": args.accessed or _now()[:10],
        "content_fingerprint": None,
        "note": args.note or "",
    }
    return _locked_append(args, "sources.jsonl", node, "source")


def cmd_claim_add(args) -> int:
    """Append a claim (CLM-*) to claims.jsonl UNDER THE PROJECT LOCK."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    cid = args.id or _mint_id(research, "claims.jsonl", "CLM")
    srcs = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    node = {
        "id": cid,
        "claim": args.claim,
        "status": args.status or "strong",
        "confidence": args.confidence or "high",
        "sources": srcs,
        "technique": args.technique or "",
        "verification_note": args.note or "",
    }
    return _locked_append(args, "claims.jsonl", node, "claim")


def cmd_frontier_add(args) -> int:
    """Append a clue (CLUE-*) to frontier.jsonl UNDER THE PROJECT LOCK."""
    research = Path(args.dir).expanduser().resolve() / ".research"
    cid = args.id or _mint_id(research, "frontier.jsonl", "CLUE")
    node = {
        "clue_id": cid,
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
    return _locked_append(args, "frontier.jsonl", node, "clue")


def cmd_frontier_update(args) -> int:
    """Update a clue's status/attempts UNDER THE PROJECT LOCK."""
    proj = Path(args.dir).expanduser().resolve()
    research = proj / ".research"
    if not (research / "state.json").exists():
        print("No state.json. Run `init` first.", file=sys.stderr)
        return 1
    try:
        with _project_lock(proj, timeout=max(1.0, float(getattr(args, "lock_timeout", 10)))):
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
            research.joinpath("frontier.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows))
    except BlockingIOError:
        print("Project locked by another researcher — frontier not updated.", file=sys.stderr)
        return 2
    print(f"Updated {args.clue} (status={args.status or 'unchanged'})")
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
        bad = [e for e in edges
               if e.get("from_id") and e.get("to_id")
               and not _node_exists(research, e["from_id"])
               and not e["from_id"].startswith(("Q.", "P."))]
        if bad:
            print(f"  WARN: {len(bad)} edge(s) reference a node not yet present in the graph files.")
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
    pr.set_defaults(fn=cmd_resignal)

    prs = sub.add_parser("reset", help="re-open a project")
    prs.add_argument("dir")
    prs.add_argument("--state", action="store_true", help="reset state to CONTINUE")
    prs.add_argument("--frontier", action="store_true", help="mark frontier pending")
    prs.add_argument("--all", action="store_true", help="reset frontier+evidence+state")
    prs.set_defaults(fn=cmd_reset)

    pc = sub.add_parser("checkpoint", help="snapshot now")
    pc.add_argument("dir")
    pc.add_argument("--note", default=None)
    pc.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pc.set_defaults(fn=cmd_checkpoint)

    pt = sub.add_parser("tick", help="run ONE research tick under the atomic project lock")
    pt.add_argument("dir")
    pt.add_argument("--cmd", default=None, help="subcommand to run while holding the lock (the dig work)")
    pt.add_argument("--note", default=None, help="checkpoint note for this tick")
    pt.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=30,
                    help="seconds to wait for the project lock before giving up (default 30)")
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
    pc2.set_defaults(fn=cmd_clarify)

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
    pfr_a.set_defaults(fn=cmd_frontier_add)
    pfr_u = pfr_sub.add_parser("update", help="update a clue status/attempts")
    pfr_u.add_argument("dir")
    pfr_u.add_argument("clue")
    pfr_u.add_argument("--status", default=None)
    pfr_u.add_argument("--attempt", action="store_true")
    pfr_u.add_argument("--lock-timeout", dest="lock_timeout", type=float, default=10)
    pfr_u.set_defaults(fn=cmd_frontier_update)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
