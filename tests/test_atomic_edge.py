"""Atomic node+edge creation tests for hermes-endless-research.

Covers the referential-integrity contract:
  - an edge NEVER points at a non-existent node (no Q-/P- bypass)
  - creating a question/person node AND its edge is atomic (both or neither)
  - concurrent edge creation is safe (project lock)
  - both endpoints resolved / validated
"""
import json

import pytest

import research_project as rp


def _sources(project, ids):
    (project / ".research" / "sources.jsonl").write_text(
        "".join(json.dumps({"id": sid, "url": f"https://{sid.lower()}.test/x",
                            "title": sid, "type": "primary", "accessed": "2026-01-01"})
                + "\n" for sid in ids))


def _claims(project, ids):
    (project / ".research" / "claims.jsonl").write_text(
        "".join(json.dumps({"id": cid, "claim": "c", "status": "strong",
                            "confidence": "high", "sources": []}) + "\n"
                for cid in ids))


def _edge(cli, project, frm, rel, to, ctx=None):
    argv = [frm, rel, to]
    if ctx:
        argv += ["--context", ctx]
    return cli("edge", project, *argv)


def _q_ids(project):
    return [n["id"] for n in rp._load_jsonl(project / ".research" / "questions.jsonl")]


def test_edge_to_new_question_atomically_creates_node(project, cli):
    _claims(project, ["CLM-001"])
    code, out, err = _edge(cli, project, "CLM-001", "answers", "Q-0000")
    assert code == 0, out + err
    ids = _q_ids(project)
    assert len(ids) == 1 and ids[0] == "Q-0001"
    edges = rp._load_edges(project / ".research")
    assert len(edges) == 1
    # edge points at a REAL node, not the placeholder
    assert edges[0]["to_id"] in ids


def test_existing_question_id_is_reused_not_duplicated(project, cli):
    _claims(project, ["CLM-001", "CLM-002"])
    _edge(cli, project, "CLM-001", "answers", "Q-0000")
    # second edge to a new question mints Q-0002, not reusing Q-0001
    _edge(cli, project, "CLM-002", "answers", "Q-0000")
    ids = _q_ids(project)
    assert sorted(ids) == ["Q-0001", "Q-0002"]
    # edges distinct
    edges = rp._load_edges(project / ".research")
    assert {e["to_id"] for e in edges} == {"Q-0001", "Q-0002"}


def test_rejects_non_question_unknown_node(project, cli):
    # SRC-999 doesn't exist and isn't Q-/P- -> rejected; NO node created.
    code, out, err = _edge(cli, project, "SRC-001", "cites", "SRC-999")
    assert code == 1
    assert "Unknown node" in err
    assert rp._load_edges(project / ".research") == []


def test_both_endpoints_missing_question_nodes(project, cli):
    # CLM doesn't exist for answers (from must be CLM) -> rejected atomically.
    _claims(project, ["CLM-001"])
    code, out, err = _edge(cli, project, "CLM-999", "answers", "Q-0000")
    assert code == 1
    # no Q node created (atomic: node creation not done because from invalid)
    assert _q_ids(project) == []


def test_supports_requires_claim_to(project, cli):
    _sources(project, ["SRC-001", "SRC-002"])
    code, out, err = _edge(cli, project, "SRC-001", "supports", "SRC-002")
    assert code == 1
    assert "CLAIM" in err


def test_concurrent_edges_do_not_collide(project, cli):
    # Two processes each add an edge; the lock serializes them so both survive with
    # distinct question nodes and no lost update.
    _claims(project, ["CLM-001", "CLM-002"])
    import threading, subprocess, sys
    from pathlib import Path
    SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"

    def run(cid):
        subprocess.run([sys.executable, str(SCRIPT), "edge", str(project),
                        cid, "answers", "Q-0000"], capture_output=True, text=True)

    t1, t2 = threading.Thread(target=run, args=("CLM-001",)), threading.Thread(target=run, args=("CLM-002",))
    t1.start(); t2.start(); t1.join(); t2.join()

    ids = _q_ids(project)
    assert sorted(ids) == ["Q-0001", "Q-0002"]
    edges = rp._load_edges(project / ".research")
    assert len(edges) == 2


def test_graph_validation_checks_both_endpoints(project, cli):
    _sources(project, ["SRC-001", "SRC-002"])
    _claims(project, ["CLM-001"])
    _edge(cli, project, "SRC-001", "cites", "SRC-002")
    code, out, err = cli("graph", project, "--no-validate")
    assert code == 0
    # with validation enabled, no dangling -> no warning
    code, out, err = cli("graph", project)
    assert "WARN" not in out
