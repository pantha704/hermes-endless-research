"""Locked-write tests for the v0.2.4 mutations (search-log / dead-end / criterion /
contradiction / report). Every shared-state write must go through a locked CLI command
and be serialized — no raw .research/ file edits."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_searchlog_add(project):
    rc, out, err = _cli("search-log", "add", project, "--query", "q1", "--outcome", "ok")
    assert rc == 0, err
    rows = rp._load_jsonl(project / ".research" / "search-log.jsonl")
    assert rows[0]["query"] == "q1"
    assert "ts" in rows[0]


def test_deadend_add(project):
    rc, out, err = _cli("dead-end", "add", project, "--description", "gone",
                        "--why-failed", "404", "--may-reopen")
    assert rc == 0, err
    rows = rp._load_jsonl(project / ".research" / "dead-ends.jsonl")
    assert rows[0]["clue_id"].startswith("DE-")
    assert rows[0]["may_reopen"] is True


def test_criterion_add_and_update(project):
    rc, out, err = _cli("criterion", "add", project, "--description", "primary",
                        "--primary-hard")
    assert rc == 0, err
    cid = rp._load_jsonl(project / ".research" / "criteria.jsonl")[0]["id"]
    rc, out, err = _cli("criterion", "update", project, cid, "--met", "1", "--evidence", "SRC-1")
    assert rc == 0, err
    rows = rp._load_jsonl(project / ".research" / "criteria.jsonl")
    assert rows[0]["met"] is True
    assert rows[0]["evidence_source_ids"] == ["SRC-1"]


def test_contradiction_add_and_resolve(project):
    rc, out, err = _cli("contradiction", "add", project, "--description", "A vs B", "--critical")
    assert rc == 0, err
    xid = rp._load_jsonl(project / ".research" / "contradictions.jsonl")[0]["id"]
    rc, out, err = _cli("contradiction", "resolve", project, xid, "--note", "resolved")
    assert rc == 0, err
    row = rp._load_jsonl(project / ".research" / "contradictions.jsonl")[0]
    assert row["resolved"] is True


def test_report_write(project):
    rc, out, err = _cli("report", "write", project, "--content", "# R\n\nafter a long body text that is longer than the stub threshold of three hundred characters so that it passes the success gate check")
    assert rc == 0, err
    body = (project / ".research" / "final-report.md").read_text()
    assert body.startswith("# R")


def test_locked_mutations_serialize(project):
    # Concurrent dead-end adds must each be complete (no interleaving).
    def add(i):
        subprocess.run([sys.executable, str(SCRIPT), "dead-end", "add", str(project),
                        "--description", f"d{i}"], capture_output=True, text=True)
    threads = [threading.Thread(target=add, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    rows = rp._load_jsonl(project / ".research" / "dead-ends.jsonl")
    assert len(rows) == 4
    assert sorted(r["description"] for r in rows) == ["d0", "d1", "d2", "d3"]
