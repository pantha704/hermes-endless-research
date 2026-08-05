"""Shared pytest fixtures for hermes-endless-research."""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skill" / "scripts" / "research_project.py"
sys.path.insert(0, str(SCRIPT.parent))
import research_project as rp  # noqa: E402


@pytest.fixture()
def cli():
    """Run research_project.py as a subprocess and return (code, stdout, stderr)."""
    import subprocess

    def _run(*args, **kw):
        res = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            capture_output=True, text=True,
            **kw,
        )
        return res.returncode, res.stdout, res.stderr
    return _run


@pytest.fixture()
def project(tmp_path):
    """A fresh, initialized research project directory."""
    proj = tmp_path / "campaign"
    rp.cmd_init(
        rp.argparse.Namespace(
            dir=str(proj), objective="test objective", success="success",
            failure="failure", now=False,
        )
    )
    return proj


def read_state(project) -> dict:
    return json.loads((project / ".research" / "state.json").read_text())


def write_jsonl(project, name, rows):
    (project / ".research" / name).write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
