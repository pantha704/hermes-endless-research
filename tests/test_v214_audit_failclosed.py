"""v0.2.14 tests: audit journal fail-closed + strict metadata + locked audit."""
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


def _acquire(project):
    GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"
    r = subprocess.run([sys.executable, str(GATE), str(project)],
                       capture_output=True, text=True)
    return json.loads(r.stdout).get("run_id")


def _append_raw(project, text):
    with open(project / ".research" / "run-history.jsonl", "a") as fh:
        fh.write(text + "\n")


# ---- lifecycle fails closed on a corrupt journal ----

def test_run_start_fails_closed_on_corrupt_journal(project):
    rid = _acquire(project)
    _append_raw(project, "{ this is corrupt json ")     # put a bad line in before any start
    rc, out, err = _cli("run", "start", project, "--run-id", rid,
                        env_mult={"HERMES_SESSION_ID": "s"})
    assert rc == 8, f"expected corrupt-journal refusal (exit 8), got rc={rc}\n{err}"
    assert "corrupt line" in (out + err).lower() or "incomplete journal" in (out + err).lower()


def test_run_finish_fails_closed_on_corrupt_journal(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "s"})
    _append_raw(project, "garbage not json")            # corrupt after start
    rc, out, err = _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "J",
                        env_mult={"HERMES_SESSION_ID": "s"})
    assert rc == 8, f"expected corrupt-journal refusal (exit 8), got rc={rc}\n{err}"


# ---- strict metadata: empty supplied terminal value = mismatch ----

def test_finish_must_provide_matching_session(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "S-ORIG"})
    # start recorded S-ORIG; finish provides nothing (no env) -> must refuse as mismatch
    rc, out, err = _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "J")
    assert rc == 5, f"expected session mismatch (exit 5), got rc={rc}\n{err}"
    assert "session mismatch" in (out + err).lower()


def test_finish_must_provide_matching_cron(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J-ORIG",
         env_mult={"HERMES_SESSION_ID": "S"})
    # start recorded J-ORIG; finish omits --cron-job-id -> must refuse as cron mismatch
    rc, out, err = _cli("run", "finish", project, "--run-id", rid,
                        env_mult={"HERMES_SESSION_ID": "S"})
    assert rc == 6, f"expected cron mismatch (exit 6), got rc={rc}\n{err}"
    assert "cron job mismatch" in (out + err).lower()


# ---- run audit still works (locked) and reports clean chain ----

def test_audit_locked_clean_chain(project):
    rid = _acquire(project)
    _cli("run", "start", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "S"})
    _cli("run", "finish", project, "--run-id", rid, "--cron-job-id", "J",
         env_mult={"HERMES_SESSION_ID": "S"})
    rc, out, err = _cli("run", "audit", project, "--json")
    d = json.loads(out)
    assert d["journal_integrity"] == "ok"
    assert d["runs_started"] == 1 and d["runs_completed"] == 1
    assert d["duplicate_terminal_events"] == []
