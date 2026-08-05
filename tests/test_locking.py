"""Atomic project-lock tests for hermes-endless-research.

The lock uses flock (Unix) / msvcrt (Windows). These tests prove two ticking
processes cannot mutate state simultaneously: while one holds the lock a second
tick exits with code 2 (EXIT_ALREADY_LOCKED) and rounds are unchanged.
"""
import subprocess
import sys
from pathlib import Path

import pytest

import research_project as rp
from .conftest import read_state

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "scripts" / "research_project.py"


def test_lock_is_created(project):
    # tick creates the lock file
    rp.cmd_tick(rp.argparse.Namespace(
        dir=str(project), cmd=None, note="lock", lock_timeout=5))
    assert (project / ".research" / ".lock").exists()


def test_second_tick_skipped_while_locked(project):
    # Hold the lock in-process, then try a tick via subprocess with a short timeout.
    with rp._project_lock(project, timeout=0):
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "tick", str(project),
             "--cmd", "echo should-not-run", "--lock-timeout", "1"],
            capture_output=True, text=True,
        )
        assert res.returncode == rp.EXIT_ALREADY_LOCKED == 2
        assert "locked" in (res.stderr + res.stdout).lower()
        # No state mutation while locked.
        assert read_state(project)["rounds_completed"] == 0


def test_tick_succeeds_after_release(project):
    lock = rp._project_lock(project, timeout=0)
    lock.__enter__()
    lock.__exit__(None, None, None)
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "tick", str(project),
         "--cmd", "echo now-runs"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert read_state(project)["rounds_completed"] == 1
