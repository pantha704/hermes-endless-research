"""Objective clarifier / compiler tests for hermes-endless-research (v0.2.0)."""
import research_project as rp


def test_ambiguous_goal_asks_questions(project, cli):
    code, out, err = cli("clarify", project, "https://site.com/x",
                         "--goal", "", "--mode", "auto")
    assert code == 0
    assert "questions" in out.lower()
    assert "What exactly do you want to understand" in out


def test_technical_goal_compiles_understand_aspects(project, cli):
    code, out, err = cli("clarify", project, "https://site.com/x",
                         "--goal", "understand how it technically works",
                         "--mode", "clear")
    assert code == 0
    assert "technical" in out.lower() or "How it technically" in out
    assert "compiled objective" in out


def test_credibility_goal_adds_verification(project, cli):
    code, out, err = cli("clarify", project, "https://site.com/x",
                         "--goal", "are its claims credible", "--mode", "clear")
    assert code == 0
    assert "credible" in out.lower()


def test_clarify_writes_objective_md(project, cli):
    cli("clarify", project, "https://site.com/x",
        "--goal", "who created it", "--mode", "clear")
    obj = (project / ".research" / "objective.md").read_text()
    assert "https://site.com/x" in obj
    assert "Seed URL" in obj


def test_no_write_respects_flag(project, cli):
    before = (project / ".research" / "objective.md").read_text()
    cli("clarify", project, "https://site.com/x",
        "--goal", "how does it work", "--mode", "clear", "--no-write")
    after = (project / ".research" / "objective.md").read_text()
    assert before == after  # unchanged


def test_default_broad_aspects_when_goal_has_no_signal(project, cli):
    code, out, err = cli("clarify", project, "https://site.com/x",
                         "--goal", "learn everything important", "--mode", "clear")
    assert code == 0
    # Should include the broad understand+credibility+origin set.
    assert "Understand" in out
    assert "credible" in out.lower()
    assert "created" in out.lower()
