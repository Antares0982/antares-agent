import pytest

from antares_agent.preflight import check_requirements


def test_managed_approvals(tmp_path):
    path = tmp_path / "requirements.toml"
    rules = """allowed_approval_policies = ["on-request"]
allowed_approvals_reviewers = ["auto_review"]
allowed_sandbox_modes = ["read-only", "workspace-write"]
"""
    path.write_text(rules)
    check_requirements(path)
    for old, new in [
        ("auto_review", "user"),
        ("workspace-write", "danger-full-access"),
        ("on-request", "never"),
    ]:
        path.write_text(rules.replace(old, new))
        with pytest.raises(ValueError):
            check_requirements(path)
