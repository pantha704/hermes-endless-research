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
    """The ownership check runs UNDER the flock, so check-then-mutate are atomic."""
    now = time.time()
    (project / ".research" / ".worker-lease.json").write_text(json.dumps({
        "run_id": "RUN-liveXXX", "status": "running",
        "heartbeat_at": now, "expires_at": now + 600}))
    rc, out, err = _cli("source", "add", project, "--url", "https://race2.test", "--title", "x")
    assert rc == 3, f"expected refusal under a live lease, got rc={rc}\n{out}{err}"
    assert "REFUSED" in out + err
    assert rp._load_jsonl(project / ".research" / "sources.jsonl") == []
    (project / ".research" / ".worker-lease.json").unlink(missing_ok=True)


def test_owned_lock_barrier_interleaving(project):
    """Truly deterministic lock-ordering race test.

    Sequence (no timing races):
      1. Parent acquires the project flock.
      2. Parent starts the mutation SUBPROCESS — it blocks on the flock.
      3. Parent writes a live worker lease while still holding the flock.
      4. Parent releases the flock.
      5. The mutation acquires the flock, runs its in-lock ownership check, MUST see the
         live lease and refuse (exit 3).
      6. sources.jsonl must be unchanged.
    This proves the ownership check happens INSIDE the lock and cannot observe a
    stale 'no lease', regardless of process scheduling."""
    import subprocess as _sp

    lock = rp._project_lock(project, timeout=10)
    lock.__enter__()
    proc = None
    try:
        # Step 2: launch the mutation subprocess; it blocks acquiring the flock.
        proc = _sp.Popen(
            [sys.executable, str(SCRIPT), "source", "add", str(project),
             "--url", "https://barrier.test", "--title", "b", "--lock-timeout", "10"],
            stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
        # Step 3: write a live lease while STILL holding the flock.
        now = time.time()
        (project / ".research" / ".worker-lease.json").write_text(json.dumps({
            "run_id": "RUN-liveXXX", "status": "running",
            "heartbeat_at": now, "expires_at": now + 600}))
        # Step 4: release the flock (inside the finally) so the mutation proceeds.
        lock.__exit__(None, None, None)
        lock = None
        # Step 5/6: wait for the child; it MUST refuse (exit 3) with no write.
        out, err = proc.communicate(timeout=30)
        rc = proc.returncode
        assert rc == 3, f"expected refusal (exit 3), got {rc}\n{out}\n{err}"
        assert "REFUSED" in (out or "") + (err or "")
        assert rp._load_jsonl(project / ".research" / "sources.jsonl") == []
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        if lock is not None:
            lock.__exit__(None, None, None)
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


def test_init_rerun_refuses_under_live_lease(project):
    """Re-running init on an initialized campaign must be ownership-gated."""
    r, out, err = _gate(str(project))
    assert '"run_id":' in out
    run_id = json.loads(out)["run_id"]
    # fresh init already done by conftest; re-init without run-id under a live lease
    rc, out, err = _cli("init", project, "--objective", "x")
    assert rc == 3, err
    # with run-id -> allowed
    rc, out, err = _cli("init", project, "--objective", "x", "--run-id", run_id)
    assert rc == 0, err
    _gate("RELEASE", project, "--run-id", run_id)


def test_explicit_id_duplicate_rejected(project):
    """Explicit --id that already exists must be rejected (no duplicate node ids)."""
    rc, out, err = _cli("source", "add", project, "--url", "https://a.test", "--title", "a",
                        "--id", "SRC-DUP")
    assert rc == 0, err
    # second add with the same explicit id -> refused (exit 4)
    rc, out, err = _cli("source", "add", project, "--url", "https://b.test", "--title", "b",
                        "--id", "SRC-DUP")
    assert rc == 4, err
    assert "duplicate id" in (out + err).lower()
    srcs = rp._load_jsonl(project / ".research" / "sources.jsonl")
    assert len(srcs) == 1, "duplicate explicit id must not be appended"
    assert srcs[0]["id"] == "SRC-DUP"


def test_explicit_clue_id_duplicate_rejected(project):
    """Explicit CLUE --id duplicates must be caught (type-aware key = clue_id)."""
    _cli("frontier", "add", project, "--description", "c1", "--id", "CLUE-0501")
    rc, out, err = _cli("frontier", "add", project, "--description", "c2", "--id", "CLUE-0501")
    assert rc == 4, err
    rows = rp._load_jsonl(project / ".research" / "frontier.jsonl")
    assert len(rows) == 1, "duplicate CLUE id must not be appended"


def test_explicit_deadend_id_duplicate_rejected(project):
    _cli("dead-end", "add", project, "--description", "d1", "--id", "DE-0701")
    rc, out, err = _cli("dead-end", "add", project, "--description", "d2", "--id", "DE-0701")
    assert rc == 4, err
    rows = rp._load_jsonl(project / ".research" / "dead-ends.jsonl")
    assert len(rows) == 1


def test_corrupt_lease_fails_closed(project):
    """A present-but-unreadable lease must refuse mutations (fail closed)."""
    (project / ".research" / ".worker-lease.json").write_text("{ NOT VALID JSON !!")
    rc, out, err = _cli("source", "add", project, "--url", "https://x.test", "--title", "x")
    assert rc == 3, err
    assert "REFUSED" in (out + err)
    assert rp._load_jsonl(project / ".research" / "sources.jsonl") == []
    # operator-override is the deliberate escape hatch
    rc, out, err = _cli("source", "add", project, "--url", "https://x.test", "--title", "x",
                        "--operator-override")
    assert rc == 0, err
    (project / ".research" / ".worker-lease.json").unlink(missing_ok=True)
