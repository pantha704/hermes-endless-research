"""v0.3.0 Phase 1 acceptance tests: human-review state machine + versioned objectives/criteria.

Covers the locked architectural decisions (#3-#7): single campaign_state enum, RESEARCH_SUCCESS
as an immutable audit event, versioned immutable snapshots, refinements.jsonl, and REFINE as
one lock-protected logical transaction.
"""
import json
import subprocess
import sys
from pathlib import Path

import research_project as rp

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def _cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def _get_state(project):
    return json.loads((project / ".research" / "state.json").read_text())


def _drive_to_awaiting_review(project):
    """Scaffold + add a source + criterion + force SUCCESS (bypass gate for test speed),
    yielding a campaign in AWAITING_REVIEW with a frozen v1."""
    _cli("init", project, "--objective", "Original objective")
    _cli("source", "add", project, "--url", "https://a.test", "--title", "a", "--id", "SRC-1")
    _cli("criterion", "add", project, "--description", "criterion-1", "--id", "C-1")
    rc, _, err = _cli("resignal", project, "SUCCESS", "--force", "--note", "done")
    assert rc == 0, err


def test_success_transitions_to_awaiting_review(project):
    _drive_to_awaiting_review(project)
    st = _get_state(project)
    assert st["campaign_state"] == "AWAITING_REVIEW"
    assert st["current_state"] == "SUCCESS"   # legacy derived field
    assert st["objective_version"] == 1 and st["criteria_version"] == 1
    # RESEARCH_SUCCESS is an immutable audit event tied to v1
    hist = (project / ".research" / "run-history.jsonl").read_text()
    assert '"event": "RESEARCH_SUCCESS"' in hist
    assert '"objective_version": 1' in hist and '"criteria_version": 1' in hist


def test_success_freezes_v1_snapshots(project):
    _drive_to_awaiting_review(project)
    obj = (project / ".research" / "objective.md").read_text()
    crit = (project / ".research" / "criteria.jsonl").read_text()
    assert (project / ".research" / "versions" / "objective" / "v0001.md").read_text() == obj
    assert (project / ".research" / "versions" / "criteria" / "v0001.jsonl").read_text() == crit


def test_accept_from_awaiting_review(project):
    _drive_to_awaiting_review(project)
    rc, out, err = _cli("review", project, "ACCEPT")
    assert rc == 0, err
    assert _get_state(project)["campaign_state"] == "ACCEPTED"


def test_accept_refused_from_non_awaiting_review(project):
    # Fresh campaign in CONTINUE (no SUCCESS) -> ACCEPT must fail closed
    _cli("init", project)
    rc, out, err = _cli("review", project, "ACCEPT")
    assert rc != 0
    assert "only allowed from AWAITING_REVIEW" in (out + err)
    # review falls back to current_state for legacy campaigns (no campaign_state key yet)
    st = _get_state(project)
    assert st.get("campaign_state", st.get("current_state")) == "CONTINUE"


def _refine(project, **kwargs):
    args = ["review", project, "REFINE"]
    for k, v in kwargs.items():
        flag = "--" + k.replace("_", "-")
        if isinstance(v, list):
            for item in v:
                args += [flag, item]
        else:
            args += [flag, v]
    return _cli(*args)


def test_refine_only_from_awaiting_review(project):
    _cli("init", project)   # CONTINUE
    rc, out, err = _refine(project, feedback="x", criteria=["c"])
    assert rc != 0, "REFINE from CONTINUE must be refused"
    assert "only allowed from AWAITING_REVIEW" in (out + err)


def test_refine_creates_v2_preserves_v1_and_returns_to_continue(project):
    _drive_to_awaiting_review(project)
    v1_obj = (project / ".research" / "objective.md").read_text()
    v1_crit = (project / ".research" / "criteria.jsonl").read_bytes()
    rc, out, err = _refine(project, feedback="need defenses",
                           criteria=["criterion-2: defenses"],
                           frontier_add=["investigate defenses"])
    assert rc == 0, err
    st = _get_state(project)
    assert st["campaign_state"] == "CONTINUE"
    assert st["criteria_version"] == 2
    # v1 preserved byte-for-byte (both files)
    assert (project / ".research" / "versions" / "objective" / "v0001.md").read_text() == v1_obj
    assert (project / ".research" / "versions" / "criteria" / "v0001.jsonl").read_bytes() == v1_crit
    # v2 exists and carries the old + new criterion
    v2 = (project / ".research" / "versions" / "criteria" / "v0002.jsonl").read_text()
    assert "criterion-1" in v2 and "criterion-2: defenses" in v2
    # active criteria now carries both
    active_crit = (project / ".research" / "criteria.jsonl").read_text()
    assert "criterion-2: defenses" in active_crit
    # frontier got the new clue
    frontier = (project / ".research" / "frontier.jsonl").read_text()
    assert "investigate defenses" in frontier


def test_refine_logs_refinement_verbatim(project):
    _drive_to_awaiting_review(project)
    _refine(project, feedback="please add defenses",
            criteria=["criterion-2"], frontier_add=["clue-2"])
    lines = (project / ".research" / "refinements.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["feedback"] == "please add defenses"
    assert rec["old_objective_version"] == 1 and rec["old_criteria_version"] == 1
    assert rec["new_objective_version"] == 1 and rec["new_criteria_version"] == 2
    assert rec["frontier_added"] == ["clue-2"]


def test_previous_success_remains_tied_to_v1(project):
    _drive_to_awaiting_review(project)
    _refine(project, feedback="more", criteria=["criterion-2"])
    hist = (project / ".research" / "run-history.jsonl").read_text()
    # The RESEARCH_SUCCESS event still records v1 (it is immutable, never rewritten)
    assert '"objective_version": 1' in hist and '"criteria_version": 1' in hist


def test_evidence_preserved_after_refine(project):
    _drive_to_awaiting_review(project)
    sources_before = (project / ".research" / "sources.jsonl").read_bytes()
    meta_before = (project / ".research" / "claims.jsonl").read_bytes()
    _refine(project, feedback="more")
    assert (project / ".research" / "sources.jsonl").read_bytes() == sources_before
    assert (project / ".research" / "claims.jsonl").read_bytes() == meta_before


def test_active_files_compatible_with_existing_engine(project):
    """After REFINE, the active objective.md/criteria.jsonl must still work with engine verbs."""
    _drive_to_awaiting_review(project)
    _refine(project, feedback="more", criteria=["criterion-2"])
    # status (reads state), criterion add (writes criteria) must succeed against active files
    rc, _, err = _cli("status", project)
    assert rc == 0, err
    # a subsequent research cycle can add another frontier clue and it persists
    rc, _, err = _cli("frontier", "add", project, "--description", "post-refine-clue")
    assert rc == 0, err
