"""CLUE/DE identifier-schema tests (v0.2.8).

Frontier (CLUE-*) and dead-end (DE-*) records store their id under "clue_id", not "id".
These tests verify _mint_id does not mint duplicates and _node_exists resolves them, so
the evidence graph can reference CLUE/DE endpoints correctly.
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_frontier_mint_uses_clue_id(project):
    # 2 sequential frontier adds must produce CLUE-0001, CLUE-0002 (not duplicate CLUE-0001)
    for i in (1, 2):
        rc, out, err = _cli("frontier", "add", project, "--description", f"clue {i}")
        assert rc == 0, err
    rows = rp._load_jsonl(project / ".research" / "frontier.jsonl")
    ids = [r["clue_id"] for r in rows]
    assert "clue_id" in rows[0], f"expected clue_id field, got {rows[0]}"
    assert ids == ["CLUE-0001", "CLUE-0002"], f"clue ids must increment under clue_id: {ids}"


def test_frontier_concurrent_mint_no_duplicates(project):
    n = 6
    def add(i):
        _cli("frontier", "add", project, "--description", f"clue {i}")
    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()
    rows = rp._load_jsonl(project / ".research" / "frontier.jsonl")
    ids = [r["clue_id"] for r in rows]
    assert len(ids) == n
    assert len(set(ids)) == n, f"duplicate CLUE ids minted: {ids}"
    assert all(i.startswith("CLUE-") for i in ids)


def test_deadend_mint_uses_clue_id(project):
    for i in (1, 2):
        rc, out, err = _cli("dead-end", "add", project, "--description", f"dead {i}")
        assert rc == 0, err
    rows = rp._load_jsonl(project / ".research" / "dead-ends.jsonl")
    ids = [r["clue_id"] for r in rows]
    assert ids == ["DE-0001", "DE-0002"], f"dead-end ids must increment under clue_id: {ids}"


def test_deadend_concurrent_mint_no_duplicates(project):
    n = 4
    def add(i):
        _cli("dead-end", "add", project, "--description", f"dead {i}")
    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()
    rows = rp._load_jsonl(project / ".research" / "dead-ends.jsonl")
    ids = [r["clue_id"] for r in rows]
    assert len(ids) == n
    assert len(set(ids)) == n, f"duplicate DE ids minted: {ids}"


def test_node_exists_resolves_clue(project):
    _cli("frontier", "add", project, "--description", "clueX")
    assert rp._node_exists(project / ".research", "CLUE-0001") is True
    assert rp._node_exists(project / ".research", "CLUE-9999") is False


def test_node_exists_resolves_deadend(project):
    _cli("dead-end", "add", project, "--description", "deadX")
    assert rp._node_exists(project / ".research", "DE-0001") is True
    assert rp._node_exists(project / ".research", "DE-9999") is False


def test_graph_edge_with_clue_supports_source(project):
    """A CLUE-* endpoint in an edge must resolve (graph integrity)."""
    _cli("frontier", "add", project, "--description", "the clue")
    _cli("source", "add", project, "--url", "https://a.test", "--title", "a", "--id", "SRC-A7")
    # edge: SRC-A7 derived_from CLUE-0001 (both must resolve)
    rc, out, err = _cli("edge", project, "CLUE-0001", "derived_from", "SRC-A7")
    assert rc == 0, err
    rc, out, err = _cli("graph", project)
    assert "all edge endpoints resolve" in out  # both CLUE (clue_id) and SRC resolve


def test_graph_edge_with_deadend_endpoint(project):
    _cli("dead-end", "add", project, "--description", "deadZ")
    _cli("source", "add", project, "--url", "https://b.test", "--title", "b", "--id", "SRC-B7")
    rc, out, err = _cli("edge", project, "SRC-B7", "derived_from", "DE-0001")
    assert rc == 0, err
    rc, out, err = _cli("graph", project)
    assert "all edge endpoints resolve" in out  # DE-0001 (clue_id) resolves
