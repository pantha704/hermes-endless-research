"""v0.2.15 tests: semantic journal-event validation + run abort corrupt-journal."""
import json
import os
import subprocess
import sys
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"

VALID_START = {
    "schema_version": 1, "event": "started", "campaign_run_id": "RUN-x",
    "hermes_session_id": "s", "cron_job_id": "j", "timestamp": "2026-08-06T00:00:00Z",
}


def _cli(*args, env_mult=None):
    env = dict(os.environ)
    if env_mult:
        env.update(env_mult)
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def _acquire(project):
    GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"
    r = subprocess.run([sys.executable, str(GATE), str(project)],
                       capture_output=True, text=True)
    return json.loads(r.stdout).get("run_id")


def _append_raw(project, token):
    with open(project / ".research" / "run-history.jsonl", "a") as fh:
        fh.write(json.dumps(token) + "\n")


# ---- semantic schema validator unit tests ----

def test_history_event_schema_valid():
    assert rp._history_event_schema_ok(VALID_START) is True
    completed = dict(VALID_START); completed["event"] = "completed"
    completed["checkpoint"] = "cp.md"; completed["sources_added"] = 1
    assert rp._history_event_schema_ok(completed) is True


def test_history_event_schema_rejects_malformed():
    cases = [
        None,                          # null
        [],                            # array
        42,                            # scalar
        {"schema_version": 2, "event": "started", "campaign_run_id": "RUN-x",
         "timestamp": "t"},            # wrong schema_version
        {"schema_version": 1, "event": "started", "campaign_run_id": "RUN-x"},  # no timestamp
        {"schema_version": 1, "event": "started", "timestamp": "t"},            # no run id
        {"schema_version": 1, "event": "started", "campaign_run_id": "x",
         "timestamp": "t"},            # not RUN- prefixed
        {"schema_version": 1, "event": "bogus", "campaign_run_id": "RUN-x",
         "timestamp": "t"},            # unknown event
        {"schema_version": 1, "event": "completed", "campaign_run_id": "RUN-x",
         "timestamp": "t", "sources_added": "3"},  # bad type
    ]
    for c in cases:
        assert rp._history_event_schema_ok(c) is False, f"expected schema-invalid: {c!r}"


# ---- semantic-invalid rows are reported as corrupt, not crashes ----

def test_audit_reports_nonobject_rows_as_corrupt(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "S"})
    _append_raw(project, None)                       # null
    _append_raw(project, [])                         # array
    _append_raw(project, {"event": "started"})       # missing required fields
    rc, out, err = _cli("run", "audit", project, "--json")
    d = json.loads(out)
    assert d["journal_integrity"] == "failed", d
    assert len(d["corrupt_history_lines"]) >= 3, d


def test_run_start_fails_closed_on_semantic_corruption(project):
    """A valid-JSON-but-malformed event must block a lifecycle mutation (exit 8)."""
    rid = _acquire(project)
    _append_raw(project, {"schema_version": 1, "event": "started"})   # missing fields
    rc, out, err = _cli("run", "start", project, "--run-id", rid,
                        env_mult={"HERMES_SESSION_ID": "S"})
    assert rc == 8, f"expected corrupt-journal refusal (exit 8), got rc={rc}\n{err}"


# ---- dedicated run abort corrupt-journal test (the v0.2.14 gap) ----

def test_run_abort_fails_closed_on_corrupt_journal(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "S"})
    # Append the JSON literal 'null' (valid JSON but not a dict -> schema-invalid -> corrupt)
    with open(project / ".research" / "run-history.jsonl", "a") as fh:
        fh.write("null\n")
    rc, out, err = _cli("run", "abort", project, "--run-id", rid, "--cron-job-id", "J",
                        env_mult={"HERMES_SESSION_ID": "S"})
    assert rc == 8, f"expected corrupt-journal refusal (exit 8), got rc={rc}\n{err}"
    assert "corrupt" in (out + err).lower() or "incomplete journal" in (out + err).lower()
