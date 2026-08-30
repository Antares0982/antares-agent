"""Disk profiles are an overlay on the built-ins, never a replacement."""

from __future__ import annotations

from pathlib import Path

from antares_agent.config import Settings
from antares_agent.profiles import load, materialise


def _settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "agent_work"
    workspace.mkdir()
    return Settings(
        workspace=workspace,
        db_path=tmp_path / "antares.db",
        profiles_dir=tmp_path / "profiles",
        require_sandbox=False,
    )


def test_stale_profile_keeps_the_builtin_model(tmp_path: Path) -> None:
    """A file written before `model` existed must not silently drop the tier.

    `materialise` skips files that already exist, so this is the state every
    deployment older than the key is in -- and a None model reaches the CLI as
    "use your default", which is opus.
    """
    settings = _settings(tmp_path)
    settings.profiles_dir.mkdir()
    (settings.profiles_dir / "quick.toml").write_text(
        'description = "日常小改动。"\npermission_mode = "acceptEdits"\neffort = "low"\n',
        encoding="utf-8",
    )

    quick = load(settings)["quick"]
    assert quick.model == "sonnet"
    assert quick.effort == "low"


def test_disk_values_still_win(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    materialise(settings)
    (settings.profiles_dir / "quick.toml").write_text('model = "haiku"\n', encoding="utf-8")

    quick = load(settings)["quick"]
    assert quick.model == "haiku"
    # Untouched keys survive the overlay.
    assert quick.permission_mode == "acceptEdits"


def test_materialise_writes_the_model(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    materialise(settings)
    assert 'model = "opus"' in (settings.profiles_dir / "deep.toml").read_text(encoding="utf-8")
