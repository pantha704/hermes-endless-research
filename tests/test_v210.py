"""v0.2.10 tests: gate fail-closed corrupt-lease + explicit-ID prefix/type validation."""
import json
import subprocess
import sys
import time
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"
GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _run(*args, cwd=None):
    r = subprocess.run([sys.executable, str(GATE), *map(str, args)],
                       capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _lease(project):
    p = project / ".research" / ".worker-lease.json"
    return json.loads(p.read_text()) if p.exists() else None


def _write_live_lease(project, run_id="RUN-liveXXX", ttl=600):
    now = time.time()
    (project / ".research" / ".worker-lease.json").write_text(json.dumps({
        "run_id": run_id, "status": "running",
        "heartbeat_at": now, "expires_at": now + ttl}))


# ---- Gate fail-closed on corrupt lease ----

def test_gate_fails_closed_on_corrupt_lease(project):
    (project / ".research" / ".worker-lease.json").write_text("CORRUPT-SENTINEL{{{")
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("wakeAgent") is False, f"expected wakeAgent:false, got {out}"
    assert "warning" in parsed, "expected a recovery warning"
    # The gate must NOT overwrite the corrupt lease with a new token: the file content
    # should still be the corrupt sentinel (i.e. no new RUN-* lease was written).
    raw = (project / ".research" / ".worker-lease.json").read_text()
    assert json.dumps(parsed).find("run_id") == -1, "gate must not emit a new run_id"
    assert "CORRUPT-SENTINEL" in raw, "gate must not overwrite the corrupt lease with a new one"


def test_gate_acquires_when_lease_absent(project):
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("run_id", "").startswith("RUN-"), out
    assert _lease(project) is not None


def test_gate_skips_when_lease_live(project):
    _write_live_lease(project)
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("wakeAgent") is False, out


def test_gate_reacquires_after_lease_expires(project):
    _write_live_lease(project, ttl=-10)
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("run_id", "").startswith("RUN-"), f"expected fresh run_id, got {out}"


def test_gate_heartbeat_refuses_on_corrupt_lease(project):
    (project / ".research" / ".worker-lease.json").write_text("garbage{{{")
    rc, out, err = _run("HEARTBEAT", str(project), "--run-id", "RUN-anything")
    assert "refused" in out.lower(), out


def test_gate_status_reports_corrupt(project):
    (project / ".research" / ".worker-lease.json").write_text("not-json")
    rc, out, err = _run("STATUS", str(project))
    parsed = json.loads(out)
    assert parsed.get("status") == "corrupt", out


def test_gate_release_corrupt_needs_override(project):
    (project / ".research" / ".worker-lease.json").write_text("CORRUPT{{")
    rc, out, err = _run("RELEASE", str(project), "--run-id", "RUN-x")
    assert "refused" in out.lower(), out
    # corrupt lease must NOT be deleted without override
    raw = (project / ".research" / ".worker-lease.json").read_text()
    assert "CORRUPT" in raw, "release must not delete a corrupt lease without override"
    rc2, out2, err2 = _run("RELEASE", str(project), "--run-id", "RUN-x", "--operator-override")
    assert not (project / ".research" / ".worker-lease.json").exists(), \
        "operator-override should force-clear the corrupt lease"


# ---- Explicit-ID prefix/type validation ----

def _assert_wrong_prefix(project, cmd, args_extra, bad_id):
    rc, out, err = _cli(cmd, *(list(args_extra) + [project, "--id", bad_id]))
    assert rc == 5, f"{cmd} --id {bad_id}: expected rc=5 (wrong prefix), got rc={rc}\n{err}{out}"
    assert "wrong prefix" in (out + err).lower(), (out + err)


def test_prefix_validation_each_command(project):
    # source add must be SRC-*
    _assert_wrong_prefix(project, "source", ["add", "--url", "https://a.test", "--title", "a"], "CLM-0001")
    # claim add must be CLM-*
    _assert_wrong_prefix(project, "claim", ["add", "--claim", "x"], "SRC-0001")
    # frontier add must be CLUE-*
    _assert_wrong_prefix(project, "frontier", ["add", "--description", "c"], "DE-0001")
    # dead-end add must be DE-*
    _assert_wrong_prefix(project, "dead-end", ["add", "--description", "d"], "SRC-0001")
    # criterion add must be C-*
    _assert_wrong_prefix(project, "criterion", ["add", "--description", "cr"], "X-0001")
    # contradiction add must be X-* (requires --description)
    _assert_wrong_prefix(project, "contradiction", ["add", "--description", "x", "--side-a", "a"], "C-0001")


def test_correct_prefix_duplicate_rejected_in_target_file(project):
    """A correct-prefix but duplicate id in the target file is rejected (exit 4)."""
    rc, out, err = _cli("source", "add", project, "--url", "https://c.test", "--title", "c",
                        "--id", "SRC-0100")
    assert rc == 0, err
    rc, out, err = _cli("source", "add", project, "--url", "https://d.test", "--title", "d",
                        "--id", "SRC-0100")
    assert rc == 4, err


def test_typed_duplicate_across_different_files(project):
    """A source with id SRC-9 and a claim with id CLM-9 can coexist (different files):
    the duplicate check is scoped to the target file, not guessed globally."""
    rc, _, err = _cli("source", "add", project, "--url", "https://s.test", "--title", "s",
                      "--id", "SRC-9")
    assert rc == 0, err
    rc, _, err = _cli("claim", "add", project, "--claim", "cl", "--id", "CLM-9")
    assert rc == 0, err   # CLM-9 is a different file/prefix from SRC-9
