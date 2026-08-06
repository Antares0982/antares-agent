from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from antares_agent import index, manifest


def _workspace(tmp_path: Path, toml: str, repos: tuple[str, ...] = ("api", "web")) -> Path:
    for r in repos:
        (tmp_path / r).mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace.toml").write_text(textwrap.dedent(toml), encoding="utf-8")
    return tmp_path


GOOD = """
    [workspace]
    scratch = ".agent"
    default_profile = "deep"

    [[repo]]
    name = "api"
    path = "api"
    description = "后端 HTTP 服务。涉及接口定义的任务用它。"

    [[repo]]
    name = "web"
    path = "web"
    description = "前端 SPA。涉及页面与组件的任务用它。"

    [[relation]]
    from = "web"
    to = "api"
    kind = "http"
    contract = "api/openapi.yaml"
    note = "web 通过 /v1 调用 api，改动须两边同步。"
"""


def test_load_valid(tmp_path: Path) -> None:
    m = manifest.load(_workspace(tmp_path, GOOD))
    assert [r.name for r in m.repos] == ["api", "web"]
    assert m.default_profile == "deep"
    assert m.relations[0].src == "web" and m.relations[0].dst == "api"
    assert m.repo("api") is not None and m.repo("nope") is None


def test_missing_manifest_is_empty_not_an_error(tmp_path: Path) -> None:
    m = manifest.load(tmp_path)
    assert m.repos == () and m.scratch == ".agent"


def test_all_problems_reported_together(tmp_path: Path) -> None:
    ws = _workspace(
        tmp_path,
        """
        [[repo]]
        name = "api"
        path = "api"

        [[repo]]
        name = "api"
        path = "web"
        description = "dup name"

        [[relation]]
        from = "web"
        to = "api"
        kind = "carrier-pigeon"
        note = "n"
        """,
    )
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.load(ws)
    joined = "\n".join(exc.value.problems)
    assert "duplicate name" in joined
    assert "missing `description`" in joined
    assert "carrier-pigeon" in joined
    assert "not a declared repo" in joined  # `from = "web"` was never declared


@pytest.mark.parametrize("bad", ["../outside", "/etc", "api/../../elsewhere"])
def test_repo_path_must_stay_under_the_root(tmp_path: Path, bad: str) -> None:
    (tmp_path / "outside").mkdir()
    ws = _workspace(
        tmp_path,
        f"""
        [[repo]]
        name = "x"
        path = "{bad}"
        description = "d"
        """,
    )
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.load(ws)
    assert any("escapes" in p or "must be relative" in p for p in exc.value.problems)


def test_nonexistent_repo_dir_is_an_error(tmp_path: Path) -> None:
    ws = _workspace(
        tmp_path,
        """
        [[repo]]
        name = "ghost"
        path = "ghost"
        description = "d"
        """,
    )
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.load(ws)
    assert any("does not exist" in p for p in exc.value.problems)


def test_repo_for_path_picks_longest_prefix(tmp_path: Path) -> None:
    (tmp_path / "api/nested").mkdir(parents=True)
    ws = _workspace(
        tmp_path,
        """
        [[repo]]
        name = "api"
        path = "api"
        description = "d"

        [[repo]]
        name = "nested"
        path = "api/nested"
        description = "d"
        """,
        repos=("api",),
    )
    m = manifest.load(ws)
    assert m.repo_for_path("api/routes.py").name == "api"  # type: ignore[union-attr]
    assert m.repo_for_path("api/nested/x.py").name == "nested"  # type: ignore[union-attr]
    assert m.repo_for_path(tmp_path / "api/x.py").name == "api"  # type: ignore[union-attr]
    assert m.repo_for_path("/somewhere/else") is None


def _skill(root: Path, repo: str, name: str, desc: str) -> None:
    d = root / repo / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n", encoding="utf-8"
    )


def test_index_render(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, GOOD)
    _skill(ws, "api", "run-migration", "生成并执行数据库迁移")
    m = manifest.load(ws)
    out = index.render(m)

    assert "### api  (`api/`)" in out
    assert "- `run-migration` — 生成并执行数据库迁移" in out
    assert "**web → api** (HTTP 调用)，契约见 `api/openapi.yaml`" in out
    # a repo with no skills gets no empty heading
    assert out.count("可用 skill") == 1


def test_duplicate_skill_names_are_fatal(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, GOOD)
    _skill(ws, "api", "deploy", "a")
    _skill(ws, "web", "deploy", "b")
    with pytest.raises(index.DuplicateSkillError, match="deploy"):
        index.scan_all_skills(manifest.load(ws))


def test_write_creates_index_and_pointer(tmp_path: Path) -> None:
    m = manifest.load(_workspace(tmp_path, GOOD))
    target = index.write(m)
    assert target == tmp_path / ".agent" / "workspace-index.md"
    assert "# 工作区索引" in target.read_text(encoding="utf-8")
    assert index.POINTER in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_pointer_is_idempotent_and_preserves_user_content(tmp_path: Path) -> None:
    _workspace(tmp_path, GOOD)
    (tmp_path / "CLAUDE.md").write_text("# 我的说明\n\n手写内容。\n", encoding="utf-8")

    index.ensure_pointer(tmp_path)
    index.ensure_pointer(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert text.count(index.POINTER) == 1
    assert "手写内容。" in text


def test_frontmatter_block_scalars(tmp_path: Path) -> None:
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text(
        '---\nname: "quoted-name"\ndescription: >\n  first line\n  second line\n---\nbody\n',
        encoding="utf-8",
    )
    meta = index._frontmatter(d / "SKILL.md")
    assert meta["name"] == "quoted-name"
    assert meta["description"] == "first line second line"
