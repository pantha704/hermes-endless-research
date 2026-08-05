"""Locked mutation-primitive tests (Design 2: lock every shared-state write).

These prove that adding a source/claim/frontier via the CLI holds the project lock
for the append, so a concurrent writer cannot interleave a partial record — every
state-changing operation is a single flock-guarded critical section.
"""
import threading
import subprocess
import sys
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def test_source_add_locked_and_recorded(project, cli):
    code, out, err = cli("source", "add", project,
                         "--url", "https://example.com/x", "--title", "X",
                         "--type", "primary_paper")
    assert code == 0, out + err
    srcs = rp._load_jsonl(project / ".research" / "sources.jsonl")
    assert len(srcs) == 1
    assert srcs[0]["url"] == "https://example.com/x"
    assert srcs[0]["id"].startswith("SRC-")
    # canonical url computed conservatively
    assert srcs[0]["canonical_url"] == "https://example.com/x"


def test_claim_add_locked(project, cli):
    code, out, err = cli("claim", "add", project, "--claim", "c", "--sources", "SRC-1,SRC-2")
    assert code == 0, out + err
    claims = rp._load_jsonl(project / ".research" / "claims.jsonl")
    assert len(claims) == 1
    assert claims[0]["sources"] == ["SRC-1", "SRC-2"]
    assert claims[0]["id"].startswith("CLM-")


def test_frontier_add_and_update_locked(project, cli):
    code, out, err = cli("frontier", "add", project, "--description", "d", "--parent", "SRC-1")
    assert code == 0, out + err
    front = rp._load_jsonl(project / ".research" / "frontier.jsonl")
    assert len(front) == 1
    cid = front[0]["clue_id"]
    # update via locked CLI
    code, out, err = cli("frontier", "update", project, cid, "--status", "done_proven", "--attempt")
    assert code == 0, out + err
    front = rp._load_jsonl(project / ".research" / "frontier.jsonl")
    assert front[0]["status"] == "done_proven"
    assert front[0]["attempts"] == 1


def test_locked_writes_serialize(project):
    # Two concurrent source-add subprocesses must each produce a complete record
    # (no interleaving) thanks to the project lock.
    def add(url):
        subprocess.run([sys.executable, str(SCRIPT), "source", "add", str(project),
                        "--url", url, "--title", url], capture_output=True, text=True)
    t1 = threading.Thread(target=add, args=("https://a.test/1",))
    t2 = threading.Thread(target=add, args=("https://b.test/2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    rows = rp._load_jsonl(project / ".research" / "sources.jsonl")
    urls = [r["url"] for r in rows]
    assert sorted(urls) == sorted(["https://a.test/1", "https://b.test/2"])
    assert len(rows) == 2  # both complete records, no partial/corrupt line


def test_locked_write_when_project_locked_returns_2(project):
    lock = rp._project_lock(project, timeout=0)
    lock.__enter__()
    try:
        res = subprocess.run([sys.executable, str(SCRIPT), "source", "add", str(project),
                              "--url", "https://x.test/1", "--lock-timeout", "1"],
                             capture_output=True, text=True)
        assert res.returncode == 2
        assert "locked" in (res.stderr + res.stdout).lower()
    finally:
        lock.__exit__(None, None, None)
