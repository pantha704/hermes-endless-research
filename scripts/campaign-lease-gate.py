#!/usr/bin/env python3
"""
Campaign worker-lease manager for hermes-endless-research.

ONE WORKER PER CAMPAIGN — a Hermes cron PRE-RUN gate plus agent-side heartbeat/release.

LEASES ARE TOKEN-BASED, NOT PID-BASED, so the short-lived gate process can exit and the
lease still correctly represents the *Hermes research session* that runs afterwards.

Modes
-----
  CHECK   <project>              (default; used as the cron `script=` pre-run gate)
  HEARTBEAT <project> --run-id R  (agent calls periodically during a long session)
  RELEASE <project> --run-id R    (agent calls at the end of a tick; REQUIRES run-id)
  STATUS  <project>               (print the current lease, if any)

How CHECK works (atomic, under the project lock):
  acquire <proj>/.research/.lock        (flock)
    read state.json; if current_state in (DORMANT, SUCCESS, EXHAUSTED) or unreadable
       -> skip: emit {"wakeAgent": false}, release lock, exit
    read .worker-lease.json
    if a NON-EXPIRED lease exists (now < expires_at) and it is "running"
       -> another worker owns the campaign: skip -> {"wakeAgent": false}
    else
       -> generate a fresh token run_id, write the new lease, emit {"run_id": ...}
  release project lock

  Because the read-check-write happens under one flock, two gates starting at nearly the
  same instant cannot both wake an agent: exactly one wins the lease.

HEARTBEAT renews the lease owned by run_id (extends heartbeat_at & expires_at). This is
how a long session keeps ownership past the original TTL.

RELEASE deletes the lease ONLY if run_id matches the stored lease.run_id. There is NO
anonymous release — ownership is always required, so a second/unrelated process cannot
clear a live worker's lease.

Lease record .worker-lease.json: {run_id, status, heartbeat_at, expires_at, started_at}.
(No PID is stored or used for decisions, so a crashed/exited gate or agent is handled by
TTL expiry, not process liveness.)
"""
import json
import os
import secrets
import sys
import time
from pathlib import Path

# A normal cron research burst is seconds -> a few minutes; give generous headroom.
LEASE_TTL_SECONDS = 60 * 30          # total ownership window
HEARTBEAT_TTL_SECONDS = 60 * 10      # heartbeat must be refreshed at least this often
PAUSE_STATES = {"DORMANT", "SUCCESS", "EXHAUSTED"}


def _import_rp():
    """Import research_project so we reuse the SAME flock implementation."""
    import importlib.util
    import os
    paths = [
        os.path.expanduser("~/.hermes/skills/research/endless-research/scripts/research_project.py"),
        str(Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"),
    ]
    for p in paths:
        if os.path.isfile(p):
            spec = importlib.util.spec_from_file_location("rp_gate", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    # fallback: minimal local flock (should not normally happen)
    import contextlib, fcntl
    @contextlib.contextmanager
    def _lock(proj, timeout=5):
        f = (proj / ".research").mkdir(parents=True, exist_ok=True)
        lf = proj / ".research" / ".lock"
        fd = open(lf, "a+b")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
    return type("RP", (), {"_project_lock": _lock})


def _now():
    return time.time()


def _read_state(research: Path):
    try:
        return json.loads((research / "state.json").read_text())
    except Exception:
        return None


def _read_lease(research: Path):
    try:
        return json.loads((research / ".worker-lease.json").read_text())
    except Exception:
        return None


def _write_lease(research: Path, lease: dict):
    """Write the lease CRASH-CONSISTENTLY: temp file + os.replace.

    A concurrent crash mid-write cannot leave a truncated .worker-lease.json, so the
    fail-closed reader never sees a corrupt-but-present lease.
    """
    (research).mkdir(parents=True, exist_ok=True)
    dest = research / ".worker-lease.json"
    tmp = research / ".worker-lease.json.tmp"
    tmp.write_text(json.dumps(lease))
    os.replace(tmp, dest)


def _delete_lease(research: Path):
    (research / ".worker-lease.json").unlink(missing_ok=True)


def _emit(**obj):
    print(json.dumps(obj), flush=True)


def check(proj: Path, rp) -> int:
    research = proj / ".research"
    if not (research / "state.json").exists():
        _emit(wakeAgent=False)
        return 0

    # ATOMIC: do the whole read-decide-write under the project flock.
    with rp._project_lock(proj, timeout=10):
        state = _read_state(research)
        if state is None or (state.get("current_state") or "CONTINUE").upper() in PAUSE_STATES:
            _emit(wakeAgent=False)
            return 0
        lease = _read_lease(research)
        if lease and lease.get("status") == "running" and (_now() < lease.get("expires_at", 0)):
            # A valid, non-expired lease exists -> another worker owns the campaign.
            _emit(wakeAgent=False)
            return 0
        # No valid lease -> acquire a new token lease.
        run_id = f"RUN-{secrets.token_hex(8)}"
        now = _now()
        _write_lease(research, {
            "run_id": run_id,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "heartbeat_at": now,
            "expires_at": now + LEASE_TTL_SECONDS,
        })
        _emit(run_id=run_id)
    return 0


def heartbeat(proj: Path, run_id: str, rp) -> int:
    research = proj / ".research"
    if not run_id:
        _emit(error="heartbeat requires --run-id")
        return 1
    with rp._project_lock(proj, timeout=10):
        lease = _read_lease(research)
        if lease is None:
            _emit(error="no active lease to heartbeat")
            return 1
        if lease.get("run_id") != run_id:
            _emit(error="heartbeat refused: run_id does not own the lease", owned=lease.get("run_id"))
            return 1
        now = _now()
        lease["heartbeat_at"] = now
        lease["expires_at"] = now + LEASE_TTL_SECONDS
        _write_lease(research, lease)
        _emit(status="heartbeat", run_id=run_id, expires_at=lease["expires_at"])
    return 0


def release(proj: Path, run_id: str, rp) -> int:
    research = proj / ".research"
    if not run_id:
        _emit(error="release requires --run-id (no anonymous release)")
        return 1
    with rp._project_lock(proj, timeout=10):
        lease = _read_lease(research)
        if lease is None:
            _emit(status="no active lease")
            return 0
        if lease.get("run_id") != run_id:
            # DO NOT delete — another worker owns it.
            _emit(error="release refused: run_id does not own the lease",
                  owned_run_id=lease.get("run_id"))
            return 1
        _delete_lease(research)
        _emit(status="released", run_id=run_id)
    return 0


def status(proj: Path, rp) -> int:
    lease = _read_lease(proj / ".research")
    if lease is None:
        _emit(status="no-lease")
    else:
        now = _now()
        alive = lease.get("status") == "running" and now < lease.get("expires_at", 0)
        _emit(status=lease.get("status"), run_id=lease.get("run_id"),
              alive=alive, expires_at=lease.get("expires_at"))
    return 0


def main(argv) -> int:
    rp = _import_rp()
    argv = argv[1:]
    cmd = "CHECK"
    if argv and argv[0].upper() in ("CHECK", "HEARTBEAT", "RELEASE", "STATUS"):
        cmd = argv[0].upper()
        argv = argv[1:]
    if not argv:
        # Use cwd (the cron workdir) if it's a valid campaign, else skip.
        try:
            proj = Path.cwd().resolve()
            if not (proj / ".research" / "state.json").exists():
                _emit(wakeAgent=False)
                return 0
        except OSError:
            _emit(wakeAgent=False)
            return 0
    else:
        proj = Path(argv[0]).expanduser().resolve()
    run_id = None
    if "--run-id" in argv:
        i = argv.index("--run-id")
        if i + 1 < len(argv):
            run_id = argv[i + 1]

    if cmd == "CHECK":
        return check(proj, rp)
    if cmd == "HEARTBEAT":
        return heartbeat(proj, run_id, rp)
    if cmd == "RELEASE":
        return release(proj, run_id, rp)
    if cmd == "STATUS":
        return status(proj, rp)
    _emit(wakeAgent=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
