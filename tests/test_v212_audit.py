"""v0.2.12 tests: per-run audit journal (run-history.jsonl + run start/finish/abort/audit)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _cli(*args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def _hist(project):
    p = project / ".research" / "run-history.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _acquire(project):
    """Acquire a lease via the gate and return its run_id."""
    GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"
    r = subprocess.run([sys.executable, str(GATE), str(project)],
                       capture_output=True, text=True)
    return json.loads(r.stdout).get("run_id")


# ---- session-id capture + start/finish linkage ----

def test_run_start_captures_hermes_session_id(project):
    rid = _acquire(project)
    rc, out, err = _cli("run", "start", project, "--run-id", rid,
                        extra_env={"HERMES_SESSION_ID": "sess-abc123"})
    assert rc == 0, err
    events = _hist(project)
    assert events[0]["event"] == "started"
    assert events[0]["hermes_session_id"] == "sess-abc123"


def test_start_finish_share_run_and_session(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "JOB-X",
         extra_env={"HERMES_SESSION_ID": "sess-1"})
    rc, out, err = _cli("run", "finish", project, "--run-id", rid,
                        "--cron-job-id", "JOB-X", "--checkpoint", "cp.md",
                        "--sources", "3", "--claims", "2",
                        extra_env={"HERMES_SESSION_ID": "sess-1"})
    assert rc == 0, err
    events = _hist(project)
    started = [e for e in events if e["event"] == "started"]
    completed = [e for e in events if e["event"] == "completed"]
    assert started[0]["campaign_run_id"] == rid
    assert completed[0]["campaign_run_id"] == rid
    assert completed[0]["hermes_session_id"] == "sess-1"
    assert completed[0]["cron_job_id"] == "JOB-X"
    assert completed[0]["sources_added"] == 3


def test_run_abort_records_terminal_event(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)
    rc, out, err = _cli("run", "abort", project, "--run-id", rid, "--reason", "rate limit")
    assert rc == 0, err
    aborted = [e for e in _hist(project) if e["event"] == "aborted"]
    assert aborted and aborted[0]["reason"] == "rate limit"


# ---- ownership refusal ----

def test_run_start_refuses_wrong_lease_owner(project):
    rid = _acquire(project)          # someone owns the lease
    rc, out, err = _cli("run", "start", project, "--run-id", "RUN-other")  # wrong token
    assert rc == 3, f"expected ownership refusal (exit 3), got {rc}\n{err}"
    assert "REFUSED" in (out + err).upper() or "REFUSED" in (out + err)


def test_run_finish_refuses_wrong_lease_owner(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)
    rc, out, err = _cli("run", "finish", project, "--run-id", "RUN-wrong")
    assert rc == 3, err


# ---- crash detection (start-only) / audit ----

def test_audit_detects_crashed_start_only_run(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)   # no finish/abort -> simulated crash
    # Also a clean complete run for contrast:
    rid2 = _acquire(project)
    _cli("run", "start", project, "--run-id", rid2)
    _cli("run", "finish", project, "--run-id", rid2)
    rc, out, err = _cli("run", "audit", project, "--json")
    assert rc == 0, err
    d = json.loads(out)
    crashed = d["crashed_start_only"]
    assert rid in crashed, f"audit should flag crashed run {rid}, got {crashed}"
    assert rid2 not in crashed, f"clean run {rid2} must not be flagged as crashed"


def test_audit_json_reports_counts(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)
    _cli("run", "finish", project, "--run-id", rid)
    rc, out, err = _cli("run", "audit", project, "--json")
    d = json.loads(out)
    assert d["runs_started"] == 1
    assert d["runs_completed"] == 1
    assert d["crashed_start_only"] == []


def test_double_run_start_refused(project):
    """A second 'started' for the same un-finished run_id must be refused."""
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid)
    rc, out, err = _cli("run", "start", project, "--run-id", rid)
    assert rc == 4, f"expected duplicate-start refusal (exit 4), got {rc}\n{err}"
    assert "already started" in (out + err)


def test_checkpoint_records_audit_ids(project):
    rid = _acquire(project)
    rc, out, err = _cli("checkpoint", project, "--note", "tick", "--run-id", rid,
                       extra_env={"HERMES_SESSION_ID": "sess-cp"})
    assert rc == 0, err
    cp = sorted((project / ".research" / "checkpoints").glob("cp_*.md"))[-1]
    body = cp.read_text()
    assert "hermes_session_id: sess-cp" in body, body
    assert "campaign_run_id" in body
