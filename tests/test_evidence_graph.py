"""Explicit evidence-graph tests for hermes-endless-research (v0.2.0)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _write_sources(project, ids):
    srcs = [{"id": sid, "url": f"https://{sid.lower()}.test/x", "title": sid,
             "type": "primary", "accessed": "2026-01-01"} for sid in ids]
    (project / ".research" / "sources.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in srcs))


def _add_edge(cli, project, from_id, rel, to_id, ctx=None):
    args = [from_id, rel, to_id]
    if ctx:
        args += ["--context", ctx]
    code, out, err = cli("edge", project, *args)
    return code, out, err


def test_valid_edge_added(project, cli):
    _write_sources(project, ["SRC-001", "SRC-002"])
    code, out, err = _add_edge(cli, project, "SRC-001", "cites", "SRC-002")
    assert code == 0, out + err
    assert "cites" in out
    edges = rp._load_edges(project / ".research")
    assert edges and edges[0]["from_id"] == "SRC-001"
    assert edges[0]["relationship"] == "cites"


def test_rejects_invalid_relationship(project, cli):
    _write_sources(project, ["SRC-001", "SRC-002"])
    # argparse `choices=` rejects it before cmd_edge runs -> non-zero exit
    code, out, err = cli("edge", project, "SRC-001", "hugs", "SRC-002")
    assert code != 0


def test_rejects_unknown_node(project, cli):
    # no sources yet -> SRC-999 unknown
    code, out, err = _add_edge(cli, project, "SRC-001", "cites", "SRC-999")
    assert code == 1
    assert "Unknown node" in err


def test_supports_requires_claim_as_to(project, cli):
    _write_sources(project, ["SRC-001", "SRC-002"])
    code, out, err = _add_edge(cli, project, "SRC-001", "supports", "SRC-002")
    assert code == 1
    assert "CLAIM" in err


def test_graph_summary_counts(project, cli):
    _write_sources(project, ["SRC-001", "SRC-002"])
    # add a claim so supports/contradicts validate
    (project / ".research" / "claims.jsonl").write_text(
        json.dumps({"id": "CLM-001", "claim": "c",
                    "status": "strong", "confidence": "high", "sources": ["SRC-001"]}) + "\n")
    _add_edge(cli, project, "SRC-001", "cites", "SRC-002")
    _add_edge(cli, project, "SRC-001", "supports", "CLM-001")
    code, out, err = cli("graph", project)
    assert code == 0
    assert "SRC" in out and "CLM" in out
    assert "cites" in out and "supports" in out


def test_edges_jsonl_is_created_on_init(project):
    assert (project / ".research" / "edges.jsonl").exists()
    assert (project / ".research" / "scope.json").exists()
