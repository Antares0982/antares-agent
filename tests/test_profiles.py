import pytest

from antares_agent.config import Settings
from antares_agent.profiles import load, materialise


def test_profiles(tmp_path):
    settings = Settings(profiles_dir=tmp_path)
    materialise(settings)
    profiles = load(settings)
    assert profiles["quick"].permission_mode == "auto"
    assert profiles["deep"].permission_mode == "plan"
    assert profiles["quick"].model is None
    (tmp_path / "quick.toml").write_text('model = "fixture-model"\n')
    assert load(settings)["quick"].model == "fixture-model"
    (tmp_path / "quick.toml").write_text('permission_mode = "bypassPermissions"\n')
    with pytest.raises(ValueError):
        load(settings)
