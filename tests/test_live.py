"""End-to-end against the real CLI. Skipped unless explicitly enabled.

Everything else in this suite fakes the SDK client, which means it verifies
our logic and nothing about whether the options we pass are the options the
CLI accepts. This test drives an actual turn: real permission arbitration,
real message stream, real diff.

    ANTARES_LIVE=1 \
    ANTARES_LIVE_KEY_FILE=~/configs/deepseek_api_key.txt \
    ANTARES_LIVE_BASE_URL=https://api.deepseek.com/anthropic \
    ANTARES_LIVE_MODEL=deepseek-v4-flash \
    pytest tests/test_live.py -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from antares_agent.api import create_app
from antares_agent.config import Settings
from antares_agent.profiles import Profile

pytestmark = pytest.mark.skipif(
    os.environ.get("ANTARES_LIVE") != "1",
    reason="live test: set ANTARES_LIVE=1 (needs a model endpoint and a working sandbox)",
)

TIMEOUT_S = 240


def _key() -> str:
    raw = os.environ.get("ANTARES_LIVE_KEY_FILE")
    if raw:
        return Path(raw).expanduser().read_text().strip()
    return os.environ.get("ANTARES_LIVE_KEY", "")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "agent_work"
    repo = root / "api"
    repo.mkdir(parents=True)
    (repo / "notes.md").write_text("# Notes\n\nnothing yet\n", encoding="utf-8")
    # gpgsign off: a global signing config would want a tty we do not have.
    identity = [
        "-c", "user.email=t@e.st", "-c", "user.name=t", "-c", "commit.gpgsign=false",
    ]
    for args in (["init", "-q"], [*identity, "add", "."], [*identity, "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    (root / "workspace.toml").write_text(
        '[[repo]]\nname = "api"\npath = "api"\n'
        'description = "笔记仓库。涉及 notes.md 的任务用它。"\n',
        encoding="utf-8",
    )
    return root


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def client(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[httpx.Client]:
    """A real uvicorn server, not TestClient.

    TestClient's ASGI transport buffers a response before handing it back, so
    an SSE stream that never ends simply hangs. Since streaming *is* the thing
    under test here, it has to go over a socket.
    """
    for name in list(os.environ):
        if name.startswith("CLAUDE_CODE") or name in {
            "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
        }:
            monkeypatch.delenv(name, raising=False)

    settings = Settings(
        workspace=workspace,
        db_path=tmp_path / "antares.db",
        profiles_dir=tmp_path / "profiles",
        cli_path=Path(os.environ.get("ANTARES_LIVE_CLI", "/run/current-system/sw/bin/claude")),
        gateway_base_url=os.environ.get("ANTARES_LIVE_BASE_URL") or None,
        gateway_auth_token=_key() or None,
        require_sandbox=True,
    )
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/v1/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.fail("server did not come up")

    model = os.environ.get("ANTARES_LIVE_MODEL")
    app.state.manager.profiles["quick"] = Profile(
        name="quick", permission_mode="acceptEdits", effort="low", model=model
    )

    with httpx.Client(base_url=base, timeout=TIMEOUT_S) as c:
        yield c

    server.should_exit = True
    thread.join(timeout=30)


def drain(client: httpx.Client, thread_id: str, until: str) -> list[dict[str, Any]]:
    """Read the SSE stream until `until` appears, or the timeout expires."""
    frames: list[dict[str, Any]] = []
    event_type: str | None = None
    with client.stream("GET", f"/v1/threads/{thread_id}/events?after=0") as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
                frames.append({"type": event_type, "payload": payload})
                if event_type == "thread.status" and payload.get("status") == until:
                    return frames
    return frames


def test_a_real_turn_edits_a_file_and_reports_a_diff(client: httpx.Client, workspace: Path) -> None:
    thread_id = client.post("/v1/threads", json={"profile": "quick"}).json()["thread_id"]

    # The index helper must have produced its file and the CLAUDE.md pointer.
    assert (workspace / ".agent" / "workspace-index.md").exists()
    assert "workspace-index.md" in (workspace / "CLAUDE.md").read_text(encoding="utf-8")

    client.post(
        f"/v1/threads/{thread_id}/messages",
        json={"text": "把 api/notes.md 里的 'nothing yet' 改成 'hello from antares'。直接改。"},
    )
    frames = drain(client, thread_id, until="idle")
    kinds = [f["type"] for f in frames]

    assert "turn.done" in kinds, kinds
    assert "tool.call" in kinds, kinds
    assert (workspace / "api" / "notes.md").read_text(encoding="utf-8").find(
        "hello from antares"
    ) != -1

    diffs = [f["payload"] for f in frames if f["type"] == "diff"]
    assert diffs and diffs[-1]["repo"] == "api"
    assert "notes.md" in diffs[-1]["patch"]

    done = [f["payload"] for f in frames if f["type"] == "turn.done"][-1]
    # F15: cost is priced against the Anthropic rate card regardless of endpoint.
    assert done["cost_trusted"] is (os.environ.get("ANTARES_LIVE_BASE_URL") is None)
    assert done["usage"]

    # Sandboxed work must not have produced an approval prompt (D3).
    assert "approval.required" not in kinds


def test_sandboxed_reads_are_silent_but_still_reported(client: httpx.Client) -> None:
    thread_id = client.post("/v1/threads", json={"profile": "quick"}).json()["thread_id"]
    client.post(
        f"/v1/threads/{thread_id}/messages",
        json={"text": "用 Bash 跑 `ls api/` 并告诉我结果。"},
    )
    frames = drain(client, thread_id, until="idle")

    bash = [f["payload"] for f in frames
            if f["type"] == "tool.call" and f["payload"]["tool"] == "Bash"]
    assert bash, [f["type"] for f in frames]
    assert bash[0]["sandboxed"] is True
    assert bash[0]["render"] == "summary"
    assert "approval.required" not in [f["type"] for f in frames]
