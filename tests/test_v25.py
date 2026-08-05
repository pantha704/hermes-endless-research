"""v0.2.5 tests: cron-wrapper, strict lease ownership on mutations, graph to_id audit."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"
GATE = Path(__file__).resolve().parent.parent / "scripts" / "campaign-lease-gate.py"


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _gate(*args):
    r = subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _lease(project):
    p = project / ".research" / ".worker-lease.json"
    return json.loads(p.read_text()) if p.exists() else None


def _acquire(project):
    r, out, err = _gate(str(project))
    assert r == 0 and '"run_id":' in out
    return json.loads(out)["run_id"]


# --- cron-wrapper ---

def _wrap_cli(project, *args, scripts_dir):
    env = dict(os.environ)
    env["ENDLESS_SCRIPTS_DIR"] = str(scripts_dir)
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stdout, r.stderr


def test_cron_wrapper_generates_absolute_path_wrapper(project, tmp_path):
    rc, out, err = _wrap_cli(project, "cron-wrapper", project, "--name", "cfg",
                             scripts_dir=tmp_path)
    assert rc == 0, err
    wrapper = tmp_path / "cfg-lease-gate.sh"
    assert wrapper.exists()
    text = wrapper.read_text()
    assert str(project) in text        # absolute project path baked in
    assert "CHECK" in text
    assert "campaign-lease-gate.py" in text


def test_cron_wrapper_works_from_arbitrary_cwd(project, tmp_path):
    # Simulate Hermes running the gate from the scripts dir (not the workdir).
    rc, out, err = _wrap_cli(project, "cron-wrapper", project, "--name", "cwdtest",
                             scripts_dir=tmp_path)
    assert rc == 0
    wrapper = tmp_path / "cwdtest-lease-gate.sh"
    assert wrapper.exists()
    # run it from a different cwd; it must still find the project and wake the agent.
    r = subprocess.run(["bash", str(wrapper)], cwd="/tmp", capture_output=True, text=True)
    assert r.returncode == 0
    assert '"run_id":' in r.stdout      # found the campaign despite cwd


# --- strict lease ownership on mutations ---

def test_mutation_without_run_id_refused_when_lease_live(project):
    _acquire(project)
    rc, out, err = _cli("source", "add", project, "--url", "https://x.test", "--title", "no")
    assert rc == 3, err
    assert "REFUSED" in (out + err)
    assert "run_id" in (out + err)


def test_mutation_wrong_run_id_refused(project):
    run_id = _acquire(project)
    rc, out, err = _cli("source", "add", project, "--url", "https://x.test",
                        "--title", "w", "--run-id", "WRONG")
    assert rc == 3
    assert "REFUSED" in (out + err)


def test_mutation_correct_run_id_allowed(project):
    run_id = _acquire(project)
    rc, out, err = _cli("source", "add", project, "--url", "https://x.test",
                        "--title", "ok", "--run-id", run_id)
    assert rc == 0, err
    srcs = rp._load_jsonl(project / ".research" / "sources.jsonl")
    assert len(srcs) == 1


def test_mutation_operator_override_allowed(project):
    _acquire(project)
    rc, out, err = _cli("source", "add", project, "--url", "https://y.test",
                        "--title", "override", "--operator-override")
    assert rc == 0, err


def test_mutation_allowed_when_no_lease(project):
    # No lease -> manual mutation permitted.
    rc, out, err = _cli("source", "add", project, "--url", "https://z.test", "--title", "manual")
    assert rc == 0, err


def test_edge_writable_under_live_lease_with_run_id(project):
    # Regression for the defect the real-cron run caught: `edge` must accept --run-id
    # and be writable under a live lease (its parser previously lacked --run-id).
    _cli("source", "add", project, "--url", "https://a.test", "--id", "SRC-A1")
    _cli("source", "add", project, "--url", "https://b.test", "--id", "SRC-B1")
    run_id = _acquire(project)
    # without run-id -> refused (edge enforces the lease contract)
    rc, out, err = _cli("edge", project, "SRC-A1", "cites", "SRC-B1")
    assert rc == 3, err
    # with run-id -> allowed
    rc, out, err = _cli("edge", project, "SRC-A1", "cites", "SRC-B1", "--run-id", run_id)
    assert rc == 0, err
    edges = rp._load_edges(project / ".research")
    assert len(edges) == 1
    assert edges[0]["from_id"] == "SRC-A1" and edges[0]["to_id"] == "SRC-B1"


def test_resignal_accepts_run_id(project, cli):
    run_id = _acquire(project)
    rc, out, err = _cli("resignal", project, "DORMANT", "--run-id", run_id, "--note", "n")
    assert rc == 0, err  # resignal's parser must accept --run-id too


# --- graph to_id audit ---

def test_graph_flags_dangling_to_id(project, cli):
    # Add a valid edge then delete its to_id source to simulate legacy/import damage.
    rc, out, err = _cli("source", "add", project, "--url", "https://a.test", "--title", "a",
                        "--id", "SRC-A1")
    assert rc == 0
    rc, out, err = _cli("source", "add", project, "--url", "https://b.test", "--title", "b",
                        "--id", "SRC-B1")
    assert rc == 0
    rc, out, err = _cli("edge", project, "SRC-A1", "cites", "SRC-B1")
    assert rc == 0, err
    # Remove the to_id node directly (simulating a bad import).
    f = project / ".research" / "sources.jsonl"
    rows = rp._load_jsonl(f)
    keep = [r for r in rows if r.get("id") != "SRC-B1"]
    f.write_text("".join(json.dumps(r) + "\n" for r in keep))
    # graph should now warn about the dangling to_id.
    rc, out, err = _cli("graph", project)
    assert "WARN" in out
    assert "SRC-B1" in out  # the dangling to_id is named


def test_graph_clean_reports_all_endpoints_resolve(project, cli):
    _cli("source", "add", project, "--url", "https://a.test", "--title", "a", "--id", "SRC-A9")
    _cli("edge", project, "SRC-A9", "cites", "SRC-A9")
    rc, out, err = _cli("graph", project)
    assert "all edge endpoints resolve" in out
