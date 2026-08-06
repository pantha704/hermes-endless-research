"""v0.2.11 tests: SEMANTIC lease-schema validation (fail-closed on malformed-but-valid-JSON).

A lease object that is syntactically valid JSON but fails the required schema must be
treated as corrupt (fail-closed), NOT as a valid-but-expired lease — otherwise a new worker
could take over an unknown owner's campaign, or a bad type (e.g. `expires_at: "tomorrow"`)
could crash the check.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"
GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _run(*args):
    r = subprocess.run([sys.executable, str(GATE), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _valid_lease():
    now = time.time()
    return {"run_id": "RUN-schema", "status": "running",
            "heartbeat_at": now, "expires_at": now + 600, "started_at": "x"}


# ---- _lease_schema_ok unit tests ----

def test_schema_valid_lease():
    assert rp._lease_schema_ok(_valid_lease()) is True
    # schema tolerates a missing started_at (validated only when present)
    l = _valid_lease(); del l["started_at"]
    assert rp._lease_schema_ok(l) is True


def test_schema_rejects_malformed_objects():
    cases = [
        {},                                      # no fields at all
        {"run_id": "RUN-x", "status": "running"},  # missing expires_at/heartbeat_at
        {"run_id": "", "status": "running", "expires_at": 1, "heartbeat_at": 1},  # empty run_id
        {"run_id": "RUN-x", "status": "paused", "expires_at": 1, "heartbeat_at": 1},  # bad status
        {"run_id": "RUN-x", "status": "running", "expires_at": "tomorrow", "heartbeat_at": 1},  # str time
        {"run_id": "RUN-x", "status": "running", "expires_at": 1, "heartbeat_at": "now"},  # str hb
        {"run_id": 123, "status": "running", "expires_at": 1, "heartbeat_at": 1},  # non-str run_id
        {"run_id": "RUN-x", "status": "running", "expires_at": 1, "heartbeat_at": 1, "started_at": 5},  # bad started_at
        {"run_id": "RUN-x", "status": "running", "expires_at": True, "heartbeat_at": 1},  # bool time
    ]
    for c in cases:
        assert rp._lease_schema_ok(c) is False, f"expected schema-invalid: {c}"


# ---- Gate fail-closed on semantic leases ----


def test_gate_fails_closed_on_empty_lease_object(project):
    """{} must be fail-closed: no takeover, wakeAgent:false + warning."""
    (project / ".research" / ".worker-lease.json").write_text("{}")
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("wakeAgent") is False, out
    assert "warning" in parsed, "expected a recovery warning (schema-invalid lease)"
    # gate must NOT have overwritten the file with a new RUN-* lease
    assert json.dumps(parsed).find("run_id") == -1


def test_gate_fails_closed_on_semantically_bad_lease(project):
    """A parseable-but-schema-invalid lease (str expires_at) must fail closed, not crash."""
    (project / ".research" / ".worker-lease.json").write_text(
        json.dumps({"run_id": "RUN-x", "status": "running",
                    "expires_at": "tomorrow", "heartbeat_at": 5}))
    rc, out, err = _run(str(project))
    parsed = json.loads(out)
    assert parsed.get("wakeAgent") is False, out
    assert "warning" in parsed, out
    # and no new lease file with a token was written over it
    raw = (project / ".research" / ".worker-lease.json").read_text()
    assert "expires_at\": \"tomorrow\"" in raw or "tomorrow" in raw


def test_gate_heartbeat_refuses_on_semantically_bad_lease(project):
    (project / ".research" / ".worker-lease.json").write_text(
        json.dumps({"run_id": "RUN-x", "status": "running", "expires_at": 5}))  # missing heartbeat_at
    rc, out, err = _run("HEARTBEAT", str(project), "--run-id", "RUN-x")
    assert "refused" in out.lower(), out


def test_gate_status_reports_corrupt_on_semantic_lease(project):
    (project / ".research" / ".worker-lease.json").write_text("{}")
    rc, out, err = _run("STATUS", str(project))
    parsed = json.loads(out)
    assert parsed.get("status") == "corrupt", out


# ---- Mutation fail-closed on semantic leases ----

def test_mutation_refuses_on_semantically_bad_lease(project):
    """Once a schema-invalid lease exists, a no-run-id mutation must refuse (exit 3)."""
    (project / ".research" / ".worker-lease.json").write_text(
        json.dumps({"run_id": "RUN-x", "status": "running", "expires_at": 5}))
    rc, out, err = _cli("source", "add", project, "--url", "https://z.test", "--title", "z")
    assert rc == 3, f"expected refusal (exit 3), got rc={rc}\n{err}"
    assert "REFUSED" in (out + err)
    assert rp._load_jsonl(project / ".research" / "sources.jsonl") == []
