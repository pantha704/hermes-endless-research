"""State machine tests for hermes-endless-research."""
import pytest

from .conftest import read_state
import research_project as rp


def test_all_states_mutate_state(project, cli):
    for state in ["CONTINUE", "CHECKPOINT", "BLOCKED", "DORMANT",
                  "EXHAUSTED", "CONTINUE"]:
        code, out, err = cli("resignal", project, state, "--note", f"to {state}")
        assert code == 0, f"resignal {state} failed: {out}{err}"
        assert read_state(project)["current_state"] == state


def test_invalid_state_rejected(project, cli):
    code, out, err = cli("resignal", project, "NOT_A_STATE")
    # argparse `choices=` rejects an invalid state before cmd_resignal runs,
    # exiting 2; both are non-zero rejections.
    assert code != 0
    assert "NOT_A_STATE" in (out + err)


def test_blocked_records_blocker(project, cli):
    cli("resignal", project, "BLOCKED", "--note", "paywall")
    st = read_state(project)
    assert st["current_state"] == "BLOCKED"
    assert "paywall" in st["blockers"]


def test_rounds_increments_on_tick(project, cli):
    before = read_state(project)["rounds_completed"]
    code, out, err = cli("tick", project, "--cmd", "echo noop")
    assert code == 0, out + err
    assert read_state(project)["rounds_completed"] == before + 1


def test_dormant_is_resumable(project, cli):
    cli("resignal", project, "DORMANT")
    assert read_state(project)["current_state"] == "DORMANT"
    # Re-awaken: this is what the watcher does.
    cli("resignal", project, "CONTINUE")
    assert read_state(project)["current_state"] == "CONTINUE"
