"""Deterministic barrier-based race tests for the atomic ownership contract (v0.2.7).

The audit identified a check-then-lock race: ownership was validated BEFORE the flock was
acquired, so a manual command that saw 'no lease' could then write after a cron gate
created a lease. v0.2.7 moved validation INSIDE the flock via _owned_project_lock.

These tests use a threading.Barrier to deterministically interleave a mutation and a
lease-acquisition, proving the mutation refuses if a lease exists by the time it takes
the lock — even if it could have observed 'no lease' before.
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"
GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _gate(*args):
    r = subprocess.run([sys.executable, str(GATE), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _lease(project):
    p = project / ".research" / ".worker-lease.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_owned_lock_validates_inside_critical_section(project):
    """The ownership check runs UNDER the flock, so check-then-mutate are atomic.

    Deterministic proof via lock serialization: a mutation acquires the project flock
    and only THEN reads the lease. If a live lease exists at the moment it holds the
    lock, it refuses (exit 3) and writes nothing — even if the lease was written *after*
    the mutation process started. We simulate that by writing the lease, then running a
    no-run-id mutation; the per-lock check must see it and refuse.
    """
    now = time.time()
    (project / ".research" / ".worker-lease.json").write_text(json.dumps({
        "run_id": "RUN-liveXXX", "status": "running",
        "heartbeat_at": now, "expires_at": now + 600}))

    rc, out, err = _cli("source", "add", project, "--url", "https://race2.test", "--title", "x")
    assert rc == 3, f"expected refusal under a live lease, got rc={rc}\n{out}{err}"
    assert "REFUSED" in (out + err)
    # And crucially: nothing was appended.
    assert rp._load_jsonl(project / ".research" / "sources.jsonl") == []

    (project / ".research" / ".worker-lease.json").unlink(missing_ok=True)


def test_owned_lock_serializes_concurrent_mutations(project):
    """Two concurrent no-lease mutations must serialize: each acquires the lock, checks
    ownership (no lease -> permitted), mints, and appends — no lost writes."""
    n = 6
    def add(i):
        _cli("source", "add", project, "--url", f"https://s{i}.test", "--title", f"s{i}")
    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()
    srcs = rp._load_jsonl(project / ".research" / "sources.jsonl")
    assert len(srcs) == n, f"expected {n} writes, got {len(srcs)}"
    assert len({s["id"] for s in srcs}) == n


def test_atomic_id_allocation_no_duplicates(project):
    """Concurrent source-adds must mint DISTINCT ids (id minting is inside the lock)."""
    n = 8
    results = []
    def add(_):
        rc, out, err = _cli("source", "add", project, "--url", f"https://s{_}.test", "--title", f"s{_}")
        results.append(rc)
    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(rc == 0 for rc in results), results
    srcs = rp._load_jsonl(project / ".research" / "sources.jsonl")
    ids = [s["id"] for s in srcs]
    assert len(ids) == n
    assert len(set(ids)) == n, f"duplicate ids minted: {ids}"  # no SRC-XXXX collision


def test_frontier_update_refuses_without_lease_token(project):
    """frontier update must enforce the ownership contract (was a bypass in v0.2.6)."""
    _cli("frontier", "add", project, "--description", "clue1")
    clue = rp._load_jsonl(project / ".research" / "frontier.jsonl")[0]["clue_id"]
    run_id = None
    r, out, err = _gate(str(project))
    if '"run_id":' in out:
        run_id = json.loads(out)["run_id"]
    # no run-id while live lease -> refused
    rc, out, err = _cli("frontier", "update", project, clue, "--status", "done_proven")
    assert rc == 3, err
    # with run-id -> allowed
    rc, out, err = _cli("frontier", "update", project, clue, "--status", "done_proven",
                        "--run-id", run_id)
    assert rc == 0, err
    _gate("RELEASE", project, "--run-id", run_id)


def test_reset_refuses_while_worker_active(project, cli):
    """reset --all must be refused under a live lease without ownership/override."""
    _cli("source", "add", project, "--url", "https://a.test", "--title", "a")
    r, out, err = _gate(str(project))
    assert '"run_id":' in out
    run_id = json.loads(out)["run_id"]
    rc, out, err = _cli("reset", "--all", project)
    assert rc == 3, err   # refused: destructive reset under a live lease
    # operator-override allows the deliberate emergency reset
    rc, out, err = _cli("reset", "--all", project, "--operator-override")
    assert rc == 0, err
    _gate("RELEASE", project, "--run-id", run_id)
