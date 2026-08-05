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

States: CONTINUE | CHECKPOINT | BLOCKED | EXHAUSTED | SUCCESS | DORMANT

DORMANT — clues all exhausted but the topic may yield new information later
(e.g. a developing story, a source yet to publish). Unlike EXHAUSTED (terminal),
DORMANT keeps the campaign alive to be re-awoken: resignal it back to CONTINUE
when new information/terminology/sources emerge, so a later tick can reopen it.

Exit code 0 on success, 1 on missing/corrupt project, 2 = tick skipped (locked).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

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
    "contradictions.jsonl": "",
    "dead-ends.jsonl": "",
    "search-log.jsonl": "",
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
    print(f"State set to {st}.")

    # Deterministic cron self-(dis)arming on terminal vs resumable transitions.
    # The research cron fires only while state ∈ CRON_RUN_STATES; it pauses when
    # state ∈ CRON_PAUSE_STATES (SUCCESS/EXHAUSTED = done; DORMANT = parked until
    # the watcher re-awakens).
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
    state = json.loads(state_f.read_text())
    body = f"# Checkpoint {ts}\n\nstate       : {state.get('current_state')}\n"
    body += f"next_action : {state.get('next_action')}\nnote        : {args.note or ''}\n"
    cp.write_text(body)
    state["last_checkpoint"] = str(cp.relative_to(research))
    state_f.write_text(json.dumps(state, indent=2))
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

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
