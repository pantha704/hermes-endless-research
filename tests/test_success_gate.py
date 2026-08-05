"""Deterministic SUCCESS gate tests for hermes-endless-research.

Verify that `resignal SUCCESS` is BLOCKED unless `verify_success` passes every
acceptance criterion AND the evidence trail checks out.
"""
import json

import pytest

import research_project as rp
from .conftest import write_jsonl, read_state


def _satisfy(project, criteria, sources=None, critical_unresolved=None):
    """Populate the evidence trail so the gate can pass."""
    srcs = sources or [
        {"id": "SRC-001", "url": "https://e.test/p", "title": "Primary",
         "type": "primary_author_text", "accessed": "2026-01-01"},
        {"id": "SRC-002", "url": "https://e.test/c", "title": "Coro",
         "type": "secondary_etymology", "accessed": "2026-01-01"},
    ]
    write_jsonl(project, "sources.jsonl", srcs)
    write_jsonl(project, "criteria.jsonl", criteria)
    if critical_unresolved is not None:
        write_jsonl(project, "contradictions.jsonl", critical_unresolved)
    (project / ".research" / "final-report.md").write_text(
        "# Final Report\n\nfull evidence trail\n" * 20
    )


def test_gate_blocks_when_no_criteria(project, cli):
    code, out, err = cli("verify_success", project)
    assert code == 1
    assert "BLOCKED" in out


def test_resignal_success_blocked_by_gate(project, cli):
    code, out, err = cli("resignal", project, "SUCCESS", "--note", "premature")
    assert code == 1
    assert "BLOCKED" in out or "SUCCESS gate" in out
    # State must NOT have been set (the gate stopped it before the mutation).
    assert read_state(project)["current_state"] != "SUCCESS"


def test_gate_allows_when_criteria_met(project, cli):
    _satisfy(project, [
        {"id": "C-1", "description": "primary found", "met": True,
         "evidence_source_ids": ["SRC-001"], "primary_hard": True,
         "corroboration_required": False},
        {"id": "C-2", "description": "corroborated", "met": True,
         "evidence_source_ids": ["SRC-001", "SRC-002"], "primary_hard": False,
         "corroboration_required": True},
    ])
    code, out, err = cli("verify_success", project)
    assert code == 0, out + err
    assert "UNBLOCKED" in out


def test_gate_blocks_unknown_source_id(project, cli):
    _satisfy(project, [
        {"id": "C-1", "description": "dangling ref", "met": True,
         "evidence_source_ids": ["SRC-999"], "primary_hard": False,
         "corroboration_required": False},
    ])
    code, out, err = cli("verify_success", project)
    assert code == 1
    assert "SRC-999" in out


def test_gate_blocks_missing_primary(project, cli):
    _satisfy(project, [
        {"id": "C-1", "description": "needs primary", "met": True,
         "evidence_source_ids": ["SRC-001"], "primary_hard": True,
         "corroboration_required": False},
    ], sources=[
        {"id": "SRC-001", "url": "https://e.test/x", "title": "Blog",
         "type": "secondary", "accessed": "2026-01-01"},
    ])
    code, out, err = cli("verify_success", project)
    assert code == 1
    assert "PRIMARY" in out.upper()


def test_gate_blocks_critical_unresolved_contradiction(project, cli):
    _satisfy(project, [
        {"id": "C-1", "description": "ok", "met": True,
         "evidence_source_ids": ["SRC-001"], "primary_hard": False,
         "corroboration_required": False},
    ], critical_unresolved=[
        {"id": "X-1", "critical": True, "resolved": False, "description": "conflict"},
    ])
    code, out, err = cli("verify_success", project)
    assert code == 1
    assert "contradiction" in out.lower()


def test_gate_blocks_stub_final_report(project, cli):
    _satisfy(project, [
        {"id": "C-1", "description": "ok", "met": True,
         "evidence_source_ids": ["SRC-001"], "primary_hard": False,
         "corroboration_required": False},
    ])
    # Shrink the report to the stub length to trigger the stub check.
    (project / ".research" / "final-report.md").write_text(
        "# Final Report\n\n_Completed only on SUCCESS._"
    )
    code, out, err = cli("verify_success", project)
    assert code == 1
    assert "final-report" in out.lower() or "stub" in out.lower()


def test_force_bypasses_gate(project, cli):
    code, out, err = cli("resignal", project, "SUCCESS", "--note", "forced", "--force")
    assert code == 0
    assert read_state(project)["current_state"] == "SUCCESS"
