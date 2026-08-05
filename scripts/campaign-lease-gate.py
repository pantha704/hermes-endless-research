#!/usr/bin/env python3
"""
Campaign worker-lease manager for hermes-endless-research.

Two modes:

  CHECK  (default; used as the cron PRERUN script gate)
    Emits on stdout's last line:
        {"wakeAgent": false}   -> Hermes skips the agent run entirely (no model call)
        {"run_id": "..."}      -> wake the agent (default), and this run_id owns a lease
    Skips when:
        - another LIVE worker holds a non-expired lease (recent heartbeat + live PID), OR
        - the campaign state is DORMANT / SUCCESS / EXHAUSTED
    Otherwise acquires a lease (run_id, pid, heartbeat, expires_at) under
    <project>/.research/.worker-lease.json and lets the agent run.
    An EXPIRED lease with a dead PID is treated as stale and recovered (takeover).

  RELEASE
    Called by the research agent at the end of its tick (or by a human/operator) to
    clear the lease for a given run_id, so a later cron fire can immediately re-acquire.
        python3 campaign-lease-gate.py RELEASE <project-dir> [--run-id <id>]
        (if --run-id omitted, only clears if this process is the lease owner's pid)

Lease TTL / heartbeat:
    HEALTHY: 20m expires_at, 10m heartbeat window. A normal cron run (seconds to
    minutes) holds the lease far below TTL. If the process crashes, the lease expires
    and the next fire recovers.

This is the one-worker-per-campaign guard. It complements (not replaces) Hermes'
scheduler in-flight guard (`_running_job_ids`), which only blocks the SAME cron job;
the lease also blocks manual runs, a second cron job, another profile, or a separately
launched script from touching the same campaign concurrently.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

LEASE_TTL_SECONDS = 60 * 20
HEARTBEAT_TTL_SECONDS = 60 * 10


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _project(research: Path) -> None:
    research.mkdir(parents=True, exist_ok=True)


def check(proj: Path) -> int:
    research = proj / ".research"
    lease_file = research / ".worker-lease.json"

    # 1. State gate: no research if dormant/finished OR state file missing (conservative).
    try:
        state = json.loads((research / "state.json").read_text())
    except Exception:
        # No readable state -> do not spin a research session against a broken project.
        print('{"wakeAgent": false}', flush=True)
        return 0
    else:
        st = (state.get("current_state") or "CONTINUE").upper()
        if st in ("DORMANT", "SUCCESS", "EXHAUSTED"):
            print('{"wakeAgent": false}', flush=True)
            return 0

    # 2. Live-worker gate: one worker per campaign.
    now = time.time()
    try:
        lease = json.loads(lease_file.read_text())
        if lease.get("status") == "running":
            pid = lease.get("pid", 0)
            heartbeat_at = lease.get("heartbeat_at", 0)
            expires_at = lease.get("expires_at", 0)
            lease_live = (now < expires_at) and \
                ((now - heartbeat_at) < HEARTBEAT_TTL_SECONDS or pid == os.getpid())
            pid_live = _pid_alive(pid)
            if lease_live and pid_live:
                print('{"wakeAgent": false}', flush=True)
                return 0
            # Stale/expired -> allowed to take over below.
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass

    # 3. Acquire the lease for this run.
    run_id = f"RUN-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    lease = {
        "run_id": run_id,
        "status": "running",
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "heartbeat_at": now,
        "expires_at": now + LEASE_TTL_SECONDS,
    }
    _project(research)
    lease_file.write_text(json.dumps(lease, indent=2))
    print(json.dumps({"run_id": run_id}), flush=True)
    return 0


def release(proj: Path, run_id: str | None) -> int:
    research = proj / ".research"
    lease_file = research / ".worker-lease.json"
    try:
        lease = json.loads(lease_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        print("no active lease to release", flush=True)
        return 0
    owner_tok = lease.get("pid") == os.getpid() or run_id is None or \
        run_id == lease.get("run_id")
    lease_file.unlink(missing_ok=True)
    if owner_tok:
        print("lease released", flush=True)
    else:
        print("lease exists and not owned by this run; left in place", flush=True)
    return 0


def main(argv) -> int:
    argv = argv[1:]
    cmd = "CHECK"
    if argv and argv[0].upper() in ("CHECK", "RELEASE"):
        cmd = argv[0].upper()
        argv = argv[1:]
    if not argv:
        # No project dir given: use the working directory (cron runs scripts in the
        # job's workdir, which is the campaign project dir). If still nothing usable,
        # skip the agent (safe default) rather than spinning a session against a
        # non-existent project.
        try:
            cwd_proj = Path.cwd().resolve()
            if (cwd_proj / ".research" / "state.json").exists():
                proj = cwd_proj
            else:
                print('{"wakeAgent": false}', flush=True)
                return 0
        except OSError:
            print('{"wakeAgent": false}', flush=True)
            return 0
    else:
        proj = Path(argv[0]).expanduser().resolve()
    run_id = None
    if "--run-id" in argv:
        i = argv.index("--run-id")
        if i + 1 < len(argv):
            run_id = argv[i + 1]
    if cmd == "RELEASE":
        return release(proj, run_id)
    return check(proj)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
