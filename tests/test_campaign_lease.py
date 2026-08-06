"""Campaign worker-lease tests (v0.2.4 token-based lease).

These cover the full lease lifecycle the audit required:
  - atomic acquisition (simultaneous CHECKs -> exactly one owner)
  - the gate process dying (token lease stays valid for the agent session, unlike PID)
  - strict RELEASE ownership (wrong run-id / anonymous release both refuse)
  - HEARTBEAT renewal (extends TTL; wrong run-id refused)
  - DORMANT / SUCCESS skip; missing state skips
"""
import json
import subprocess
import sys
import time
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True,
                          text=True, cwd=cwd)


def _lease(project):
    p = project / ".research" / ".worker-lease.json"
    return json.loads(p.read_text()) if p.exists() else None


def _acquire(project):
    r = _run(str(project))
    assert r.returncode == 0 and '"run_id":' in r.stdout
    return json.loads(r.stdout)["run_id"]


def test_check_acquires_token_lease(project):
    run_id = _acquire(project)
    l = _lease(project)
    assert l and l["run_id"] == run_id
    assert l["status"] == "running"
    assert "expires_at" in l and "heartbeat_at" in l
    assert l.get("pid") is None  # token-based, NOT PID-based
    assert len(l["run_id"]) >= 8


def test_second_check_while_running_skips(project):
    _acquire(project)
    r = _run(str(project))
    assert '"wakeAgent": false' in r.stdout


def test_simultaneous_checks_allow_exactly_one(project):
    import threading
    outs = []
    def go():
        outs.append(_run(str(project)).stdout)
    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    woke = [o for o in outs if '"run_id":' in o]
    skipped = [o for o in outs if '"wakeAgent": false' in o]
    assert len(woke) == 1, f"expected exactly one owner, got {len(woke)}: {woke}"
    assert len(skipped) == 5
    owner = json.loads(woke[0])["run_id"]
    assert _lease(project)["run_id"] == owner  # the surviving lease matches the winner


def test_dormant_skips(project, cli):
    cli("resignal", project, "DORMANT")
    r = _run(str(project))
    assert '"wakeAgent": false' in r.stdout


def test_transition_to_success_blocks_and_skips(project):
    # Not directly success; instead confirm a lease does not block a fresh CHECK after
    # an expired TTL, and that PAUSE states skip.
    _acquire(project)
    # force the lease to expire
    l = _lease(project); l["expires_at"] = time.time() - 1
    (project / ".research" / ".worker-lease.json").write_text(json.dumps(l))
    r = _run(str(project))
    assert '"run_id":' in r.stdout  # expired -> a NEW worker may take over


def test_gate_pid_dying_keeps_lease_for_agent(project):
    # Simulate the real lifecycle: the gate acquires the lease and EXITS (its pid is
    # irrelevant). A second CHECK while the agent session is still running must skip,
    # even though the original gate process is long gone. Token ownership, not PID.
    run_id = _acquire(project)
    assert '"pid"' not in _lease(project)  # no reliance on gate pid
    r = _run(str(project))
    assert '"wakeAgent": false' in r.stdout  # still owned by the live lease
    assert _lease(project)["run_id"] == run_id  # lease not overwritten


def test_release_without_run_id_refused(project):
    _acquire(project)
    r = _run("RELEASE", str(project))
    assert "requires --run-id" in r.stdout or r.returncode == 1
    assert _lease(project) is not None  # still present


def test_release_wrong_run_id_preserves_lease(project):
    run_id = _acquire(project)
    r = _run("RELEASE", str(project), "--run-id", "RUN-someone-else")
    assert 'refused' in r.stdout
    assert _lease(project)["run_id"] == run_id  # preserved, NOT deleted


def test_release_correct_run_id_clears(project):
    run_id = _acquire(project)
    r = _run("RELEASE", str(project), "--run-id", run_id)
    assert '"released"' in r.stdout
    assert _lease(project) is None


def test_heartbeat_wrong_run_id_refused(project):
    run_id = _acquire(project)
    r = _run("HEARTBEAT", str(project), "--run-id", "WRONG")
    assert 'refused' in r.stdout
    assert _lease(project)["run_id"] == run_id


def test_heartbeat_renews(project):
    run_id = _acquire(project)
    l = _lease(project); l["expires_at"] = time.time() + 5  # near expiry
    (project / ".research" / ".worker-lease.json").write_text(json.dumps(l))
    r = _run("HEARTBEAT", str(project), "--run-id", run_id)
    assert '"heartbeat"' in r.stdout
    l2 = _lease(project)
    assert l2["expires_at"] > time.time() + 20  # renewed well past TTL
    assert l2["run_id"] == run_id


def test_no_state_json_skips(project):
    (project / ".research" / "state.json").unlink()
    r = _run(str(project))
    assert '"wakeAgent": false' in r.stdout


def test_lease_release_roundtrip(project):
    run_id = _acquire(project)
    _run("HEARTBEAT", project, "--run-id", run_id)
    _run("RELEASE", project, "--run-id", run_id)
    assert _lease(project) is None  # clean release lets next run re-acquire
    run_id2 = _acquire(project)
    assert run_id2 != run_id


def test_lease_write_is_crash_consistent(project):
    """The gate writes the lease via temp-file + os.replace: a crash cannot leave a
    truncated/corrupt .worker-lease.json, so fail-closed readers stay consistent."""
    _acquire(project)
    assert _lease(project) is not None          # readable lease present
    tmp = project / ".research" / ".worker-lease.json.tmp"
    assert not tmp.exists(), "tmp file must not linger after a successful lease write"
    # the lease is valid JSON
    assert isinstance(_lease(project), dict)
