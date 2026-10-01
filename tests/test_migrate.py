import pytest

from antares_agent.migrate import migrate


def test_workspace_migration(tmp_path):
    old = tmp_path / "CLAUDE.md"
    old.write_text("instructions")
    skill = tmp_path / ".claude/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill")
    target = tmp_path / "AGENTS.md"
    moves, backup = migrate(tmp_path)
    assert len(moves) == 2 and backup is None and old.exists()
    target.write_text("existing")
    with pytest.raises(ValueError, match="目标已存在"):
        migrate(tmp_path, True)
    assert skill.exists() and old.exists()
    target.unlink()
    _, backup = migrate(tmp_path, True)
    assert target.read_text() == "instructions"
    assert (backup / "CLAUDE.md").read_text() == "instructions"
    assert (tmp_path / ".agents/skills/example/SKILL.md").read_text() == "skill"
    assert not old.exists() and not skill.exists()
    assert migrate(tmp_path, True) == ([], None)


def test_migration_escape(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "CLAUDE.md").write_text("instructions")
    (work / "AGENTS.md").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        migrate(work, True)
    assert not (tmp_path / "outside").exists()
