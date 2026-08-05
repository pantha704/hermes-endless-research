"""Campaign worker-lease gate tests.

The lease gate is the one-worker-per-campaign guard that a cron PRE-RUN script uses to
emit {"wakeAgent": false} and skip the agent run (no model tokens) when another live
worker is active or the campaign is dormant/finished — and to acquire a recoverable lease
otherwise.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _gate(project, mode="CHECK", extra=None):
    argv = []
    if mode.upper() == "RELEASE":
        argv.append("RELEASE")
    argv.append(str(project))
    if extra:
        argv.extend(extra)
    res = subprocess.run([sys.executable, str(SCRIPT), *argv],
                         capture_output=True, text=True)
    return res.returncode, res.stdout, res.stderr


def _lease(project):
    p = project / ".research" / ".worker-lease.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_active_campaign_acquires_lease(project):
    rc, out, err = _gate(project)
    assert rc == 0
    assert "wakeAgent" not in out  # no skip -> agent should run
    l = _lease(project)
    assert l and l["status"] == "running"
    assert l["pid"] > 0


def test_dormant_skips_agent(project, cli):
    cli("resignal", project, "DORMANT")
    rc, out, err = _gate(project)
    assert rc == 0
    assert '"wakeAgent": false' in out


def test_success_skips_agent(project, cli):
    cli("resignal", project, "SUCCESS", "--note", "done", "--force")
    rc, out, err = _gate(project)
    assert '"wakeAgent": false' in out


def test_live_worker_blocks_concurrent(project):
    # Write a lease owned by a live pid with a recent heartbeat -> gate must skip.
    now = time.time()
    lease = {"run_id": "RUN-live", "status": "running", "pid": 1,
             "started_at": "x", "heartbeat_at": now - 2, "expires_at": now + 600}
    (project / ".research" / ".worker-lease.json").write_text(json.dumps(lease))
    rc, out, err = _gate(project)
    assert '"wakeAgent": false' in out


def test_expired_lease_allows_takeover(project):
    now = time.time()
    lease = {"run_id": "RUN-dead", "status": "running", "pid": 1,
             "started_at": "x", "heartbeat_at": now - 3600, "expires_at": now - 10}
    (project / ".research" / ".worker-lease.json").write_text(json.dumps(lease))
    rc, out, err = _gate(project)
    # expired -> the gate should NOT skip; it should acquire a new lease
    assert "wakeAgent" not in out
    l = _lease(project)
    assert l and l["run_id"] != "RUN-dead"


def test_release_clears_lease(project):
    _gate(project)
    assert _lease(project) is not None
    run_id = _lease(project)["run_id"]
    rc, out, err = _gate(project, mode="RELEASE", extra=["--run-id", run_id])
    assert rc == 0
    assert _lease(project) is None


def test_no_state_json_skips(project):
    # Removing state.json makes the gate conservative (skip, don't spin a session).
    (project / ".research" / "state.json").unlink()
    rc, out, err = _gate(project)
    assert '"wakeAgent": false' in out
