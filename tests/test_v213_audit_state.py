"""v0.2.13 tests: strict audit state machine + session/cron override + corrupt-journal."""
import json
import os
import subprocess
import sys
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _cli(*args, env_mult=None):
    env = dict(os.environ)
    if env_mult:
        env.update(env_mult)
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def _hist(project):
    p = project / ".research" / "run-history.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _acquire(project):
    GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"
    r = subprocess.run([sys.executable, str(GATE), str(project)],
                       capture_output=True, text=True)
    return json.loads(r.stdout).get("run_id")


# ---- strict state machine ----

def test_finish_without_start_refused(project):
    rid = _acquire(project)
    # acquire lease but never run start
    rc, out, err = _cli("run", "finish", project, "--run-id", rid,
                        env_mult={"HERMES_SESSION_ID": "sess"})
    assert rc == 4, f"expected no-start refusal (exit 4), got {rc}\n{err}"
    assert "no 'started' event" in (out + err)


def test_finish_session_mismatch_refused(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, env_mult={"HERMES_SESSION_ID": "sess-A"})
    rc, out, err = _cli("run", "finish", project, "--run-id", rid,
                        env_mult={"HERMES_SESSION_ID": "sess-B"})   # different session
    assert rc == 5, f"expected session-mismatch refusal (exit 5), got {rc}\n{err}"
    assert "session mismatch" in (out + err)


def test_finish_cron_mismatch_refused(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "JOB-1")
    rc, out, err = _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "JOB-2")
    assert rc == 6, f"expected cron mismatch (exit 6), got {rc}\n{err}"
    assert "cron job mismatch" in (out + err)


def test_double_terminal_refused(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "JOB")
    _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "JOB")
    # second terminal (abort) must be refused as already-terminal
    rc, out, err = _cli("run", "abort", project, "--run-id", rid, "--cron-job-id", "JOB")
    assert rc == 7, f"expected already-terminal refusal (exit 7), got {rc}\n{err}"
    assert "already has terminal event" in (out + err)


def test_audit_flags_completed_plus_aborted_as_duplicate(project):
    """One completed AND one aborted for the same run = 2 terminal events => flagged."""
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "JOB",
         env_mult={"HERMES_SESSION_ID": "S"})
    _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "JOB",
         env_mult={"HERMES_SESSION_ID": "S"})
    # Append a SCHEMA-VALID aborted event (simulates a journal that predates the strict
    # state machine) so it is counted as a second terminal event, not as corruption.
    with open(project / ".research" / "run-history.jsonl", "a") as fh:
        fh.write(json.dumps({
            "schema_version": 1, "event": "aborted", "campaign_run_id": rid,
            "hermes_session_id": "S", "cron_job_id": "JOB",
            "timestamp": "2026-08-06T00:00:00Z", "reason": "x"}) + "\n")
    rc, out, err = _cli("run", "audit", project, "--json")
    d = json.loads(out)
    assert d["journal_integrity"] == "ok", d
    assert rid in d["duplicate_terminal_events"], d


# ---- --session-id override precedence ----

def test_session_id_argument_overrides_env(project):
    rid = _acquire(project)
    # Even though env has a session id, the explicit --session-id must win.
    rc, out, err = _cli("run", "start", project, "--run-id", rid,
                        "--session-id", "arg-session",
                        env_mult={"HERMES_SESSION_ID": "env-session"})
    assert rc == 0, err
    ev = _hist(project)[0]
    assert ev["hermes_session_id"] == "arg-session", ev


# ---- corrupt journal detection ----

def test_audit_reports_corrupt_line_numbers(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)
    # Append a corrupt line to the journal.
    with open(project / ".research" / "run-history.jsonl", "a") as fh:
        fh.write("{ this is not valid json \n")
    rc, out, err = _cli("run", "audit", project, "--json")
    d = json.loads(out)
    assert d["journal_integrity"] == "failed", d
    assert d["corrupt_history_lines"] == [2], d  # line 2 is the corrupt one


# ---- checkpoint cron-job linkage ----

def test_checkpoint_records_cron_job_id(project):
    rid = _acquire(project)
    rc, out, err = _cli("checkpoint", project, "--note", "t", "--run-id", rid,
                        "--cron-job-id", "JOB-42",
                        env_mult={"HERMES_SESSION_ID": "sess"})
    assert rc == 0, err
    cp = sorted((project / ".research" / "checkpoints").glob("cp_*.md"))[-1]
    body = cp.read_text()
    assert "cron_job_id      : JOB-42" in body, body
