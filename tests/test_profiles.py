from antares_agent.profiles import PROFILES


def test_profiles():
    assert set(PROFILES) == {"auto", "plan"}
    for name, profile in PROFILES.items():
        assert profile.permission_mode == name
        assert set(vars(profile)) == {"name", "description", "permission_mode"}
