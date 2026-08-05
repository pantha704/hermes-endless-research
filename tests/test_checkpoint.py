"""Checkpoint tests for hermes-endless-research."""
from .conftest import read_state


def test_checkpoint_writes_file_and_updates_state(project, cli):
    code, out, err = cli("checkpoint", project, "--note", "round one")
    assert code == 0, out + err
    cps = list((project / ".research" / "checkpoints").glob("cp_*.md"))
    assert cps, "no checkpoint file written"
    assert "Checkpoint written" in out  # stdout confirms the write
    assert read_state(project)["last_checkpoint"]


def test_checkpoint_requires_initialized_project(tmp_path, cli):
    code, out, err = cli("checkpoint", tmp_path / "nonexistent")
    assert code == 1  # no state.json


def test_multiple_checkpoints_preserve_history(project, cli):
    for i in range(3):
        cli("checkpoint", project, "--note", f"round {i}")
    cps = list((project / ".research" / "checkpoints").glob("cp_*.md"))
    assert len(cps) == 3
