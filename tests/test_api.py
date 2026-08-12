from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from antares_agent import api as api_mod
from antares_agent import preflight
from antares_agent.config import Settings
from antares_agent.events import Event, EventType
from antares_agent.store import Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "agent_work"
    workspace.mkdir()
    return Settings(
        workspace=workspace,
        db_path=tmp_path / "antares.db",
        profiles_dir=tmp_path / "profiles",
        # The real check needs bwrap; the API surface under test does not.
        require_sandbox=False,
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    started: list[Any] = []

    class FakeRunner:
        """Enough of ThreadRunner for the HTTP layer."""

        def __init__(self, thread_id: str, event_log: Any) -> None:
            self.state = type("S", (), {"session_id": "sess", "summary": "", "status": "idle"})()
            self.log = event_log
            self.thread_id = thread_id
            self.manifest = type("M", (), {"scratch": ".agent"})()
            self.sent: list[str] = []
            self.busy = False
            self.mode: str | None = None
            self.interrupts = 0
            self.resolved: list[tuple[str, bool]] = []
            self.translator = type("T", (), {"background_tasks": set()})()
            self.approvals = type("A", (), {"pending": []})()

        async def send(self, text: str) -> int | None:
            self.sent.append(text)
            return None

        async def interrupt(self) -> None:
            self.interrupts += 1

        async def set_permission_mode(self, mode: str) -> None:
            self.mode = mode

        def resolve_approval(self, approval_id: str, allow: bool, message: str = "") -> None:
            self.resolved.append((approval_id, allow))

        async def close(self) -> None: ...

    async def fake_runner(self: Any, thread_id: str) -> Any:
        existing = self._live.get(thread_id)
        if existing is not None:
            return existing
        if self.store.get_thread(thread_id) is None:
            from antares_agent.manager import UnknownThread

            raise UnknownThread(thread_id)
        from antares_agent.eventlog import EventLog

        event_log = self._logs.get(thread_id) or EventLog(
            thread_id, capacity=64, sink=self.store.append_event
        )
        event_log._next_id = self.store.last_event_id(thread_id) + 1
        self._logs[thread_id] = event_log
        runner = FakeRunner(thread_id, event_log)
        self._live[thread_id] = runner
        started.append(runner)
        return runner

    monkeypatch.setattr("antares_agent.manager.ThreadManager.runner", fake_runner)
    monkeypatch.setattr(preflight, "run", lambda s: preflight.PreflightReport(ok=True))
    monkeypatch.setattr(api_mod.preflight, "run", lambda s: preflight.PreflightReport(ok=True))

    with TestClient(api_mod.create_app(settings)) as c:
        c.started = started  # type: ignore[attr-defined]
        yield c


def new_thread(client: TestClient, profile: str | None = None) -> str:
    response = client.post("/v1/threads", json={"profile": profile})
    assert response.status_code == 201, response.text
    return response.json()["thread_id"]


# --- discovery -----------------------------------------------------------


def test_health_lists_profiles(client: TestClient) -> None:
    body = client.get("/v1/health").json()
    assert body["status"] == "ok"
    assert "quick" in body["profiles"] and "deep" in body["profiles"]


def test_builtin_profiles_are_written_to_disk(client: TestClient, settings: Settings) -> None:
    # They exist as editable files, not string constants: the orchestration
    # prompt is the asset that gets tuned most (and per provider, see F26).
    assert (settings.profiles_dir / "deep.toml").exists()
    assert "编排者" in (settings.profiles_dir / "deep.md").read_text(encoding="utf-8")


def test_unknown_profile_is_rejected(client: TestClient) -> None:
    assert client.post("/v1/threads", json={"profile": "nope"}).status_code == 400


# --- threads -------------------------------------------------------------


def test_thread_lifecycle(client: TestClient) -> None:
    thread_id = new_thread(client)

    assert client.get(f"/v1/threads/{thread_id}").status_code == 200
    assert any(t["thread_id"] == thread_id for t in client.get("/v1/threads").json()["threads"])

    assert client.delete(f"/v1/threads/{thread_id}").status_code == 200
    assert client.get(f"/v1/threads/{thread_id}").status_code == 404


def test_deleted_threads_stay_deleted(client: TestClient, settings: Settings) -> None:
    # Soft delete: the row survives so a stale id keeps 404-ing after restart.
    thread_id = new_thread(client)
    client.delete(f"/v1/threads/{thread_id}")

    store = Store(settings.db_path)
    try:
        assert store.get_thread(thread_id) is None
    finally:
        store.close()


def test_unknown_thread_is_404_everywhere(client: TestClient) -> None:
    for call in (
        lambda: client.get("/v1/threads/thr_missing"),
        lambda: client.post("/v1/threads/thr_missing/messages", json={"text": "hi"}),
        lambda: client.delete("/v1/threads/thr_missing"),
        lambda: client.get("/v1/threads/thr_missing/events"),
    ):
        assert call().status_code == 404


# --- messages and control ------------------------------------------------


def test_post_message_returns_immediately(client: TestClient) -> None:
    thread_id = new_thread(client)
    response = client.post(f"/v1/threads/{thread_id}/messages", json={"text": "改一下登录页"})

    assert response.status_code == 202
    assert client.started[-1].sent == ["改一下登录页"]  # type: ignore[attr-defined]


def test_empty_message_is_rejected(client: TestClient) -> None:
    thread_id = new_thread(client)
    assert client.post(f"/v1/threads/{thread_id}/messages", json={"text": ""}).status_code == 422


# --- attachments ---------------------------------------------------------

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n rest of a png").decode()


def test_an_attachment_lands_in_the_workspace_and_is_named_in_the_prompt(
    client: TestClient, settings: Settings
) -> None:
    thread_id = new_thread(client)
    response = client.post(
        f"/v1/threads/{thread_id}/messages",
        json={
            "text": "这张图哪里不对",
            "attachments": [{"name": "screen.png", "mime": "image/png", "data_b64": PNG}],
        },
    )
    assert response.status_code == 202

    inbox = settings.workspace / ".agent" / "inbox" / thread_id
    written = list(inbox.iterdir())
    assert len(written) == 1
    assert written[0].suffix == ".png"  # from the mime type, not the sender's name
    assert written[0].read_bytes() == base64.b64decode(PNG)

    prompt = client.started[-1].sent[0]  # type: ignore[attr-defined]
    # Caption first: it is what becomes the thread's summary.
    assert prompt.startswith("这张图哪里不对\n")
    assert str(written[0]) in prompt
    assert "screen.png" in prompt


def test_a_caption_is_optional(client: TestClient) -> None:
    thread_id = new_thread(client)
    response = client.post(
        f"/v1/threads/{thread_id}/messages",
        json={"attachments": [{"name": "a.png", "mime": "image/png", "data_b64": PNG}]},
    )
    assert response.status_code == 202
    assert client.started[-1].sent[0].startswith("[用户发来图片")  # type: ignore[attr-defined]


def test_the_senders_name_cannot_choose_the_path(client: TestClient, settings: Settings) -> None:
    thread_id = new_thread(client)
    client.post(
        f"/v1/threads/{thread_id}/messages",
        json={
            "text": "hi",
            "attachments": [
                {"name": "../../../../etc/evil.png", "mime": "image/png", "data_b64": PNG}
            ],
        },
    )
    inbox = settings.workspace / ".agent" / "inbox" / thread_id
    assert [p.parent for p in inbox.iterdir()] == [inbox]
    assert not (settings.workspace.parent / "evil.png").exists()


def test_undecodable_base64_is_a_422_not_a_500(client: TestClient) -> None:
    thread_id = new_thread(client)
    response = client.post(
        f"/v1/threads/{thread_id}/messages",
        json={"text": "hi", "attachments": [{"mime": "image/png", "data_b64": "not base64!!"}]},
    )
    assert response.status_code == 422


def test_mode_switch(client: TestClient) -> None:
    thread_id = new_thread(client)
    assert client.post(f"/v1/threads/{thread_id}/mode", json={"mode": "auto"}).status_code == 200
    assert client.started[-1].mode == "auto"  # type: ignore[attr-defined]
    assert client.post(f"/v1/threads/{thread_id}/mode", json={"mode": "bogus"}).status_code == 422


def test_approving_a_dead_thread_is_a_conflict_not_a_500(client: TestClient) -> None:
    thread_id = new_thread(client)
    response = client.post(
        f"/v1/threads/{thread_id}/approve",
        json={"approval_id": "apr_x", "decision": "allow"},
    )
    assert response.status_code == 409


# --- SSE -----------------------------------------------------------------


def parse_sse(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    event_type: str | None = None
    for line in body.splitlines():
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            frames.append(
                {"type": event_type, "payload": json.loads(line.split(":", 1)[1].strip())}
            )
    return frames


def seed(client: TestClient, count: int = 3) -> tuple[str, Any]:
    """A thread with `count` text events, and a log closed so the stream ends.

    Live follow is covered in test_eventlog.py; here the point is what the
    endpoint hands back for a given `?after=`, and an open stream would just
    hang the test waiting for a next event that never comes.
    """
    thread_id = new_thread(client)
    client.post(f"/v1/threads/{thread_id}/messages", json={"text": "hi"})
    runner = client.started[-1]  # type: ignore[attr-defined]
    for i in range(count):
        runner.log.publish(
            Event(type=EventType.TEXT, thread_id=thread_id, data={"content": f"m{i}"})
        )
    return thread_id, runner


def test_events_replay_from_after(client: TestClient) -> None:
    thread_id, runner = seed(client)
    runner.log.close()

    frames = parse_sse(client.get(f"/v1/threads/{thread_id}/events?after=1").text)

    assert [f["payload"]["content"] for f in frames] == ["m1", "m2"]
    assert frames[0]["type"] == "text"
    assert frames[0]["payload"]["id"] == "evt_000002"
    assert frames[0]["payload"]["agent"]["id"] == "root"


def test_replay_falls_back_to_sqlite_when_the_buffer_has_rolled(client: TestClient) -> None:
    # The ring buffer is bounded; anything older has to come from disk, or a
    # client silently loses events it explicitly asked for -- which is how an
    # approval.required goes missing and a thread waits forever.
    thread_id, runner = seed(client)
    runner.log._buffer.clear()
    runner.log.close()

    frames = parse_sse(client.get(f"/v1/threads/{thread_id}/events?after=0").text)

    assert [f["payload"]["content"] for f in frames] == ["m0", "m1", "m2"]


def test_no_after_starts_from_now(client: TestClient) -> None:
    thread_id, runner = seed(client)
    runner.log.close()

    frames = parse_sse(client.get(f"/v1/threads/{thread_id}/events").text)

    assert frames == []


def test_replay_endpoint_does_not_wake_the_thread(client: TestClient) -> None:
    # D13: the streaming endpoint revives the thread by subscribing, at ~123MB
    # a piece (V4). Catching up on missed history must not pay that -- the
    # relay does exactly this for every thread the bot had open when it
    # restarted.
    thread_id, _ = seed(client)
    client.app.state.manager._live.clear()  # type: ignore[attr-defined]
    before = len(client.started)  # type: ignore[attr-defined]

    body = client.get(f"/v1/threads/{thread_id}/events/replay?after=1").json()

    assert [e["payload"]["content"] for e in body["events"]] == ["m1", "m2"]
    assert body["last_event_id"] == 3
    assert body["status"] == "idle"
    assert len(client.started) == before  # type: ignore[attr-defined]


def test_startup_corrects_a_thread_a_crash_left_awaiting_approval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End to end for the recovery pass: the correction has to be readable
    # through `/events/replay`, which is the only endpoint the bot uses after a
    # restart -- writing it anywhere the client does not look would be no fix.
    store = Store(settings.db_path)
    store.create_thread("thr_x", "quick")
    for n, (type_, data) in enumerate(
        [
            (EventType.APPROVAL_REQUIRED, {"approval_id": "apr_abc", "tool": "Bash"}),
            (EventType.THREAD_STATUS, {"status": "awaiting_approval"}),
        ],
        start=1,
    ):
        store.append_event(Event(type=type_, thread_id="thr_x", data=data).with_id(n))
    store.close()

    monkeypatch.setattr(api_mod.preflight, "run", lambda s: preflight.PreflightReport(ok=True))
    with TestClient(api_mod.create_app(settings)) as fresh:
        body = fresh.get("/v1/threads/thr_x/events/replay?after=2").json()

    assert [e["type"] for e in body["events"]] == ["error", "thread.status"]
    assert body["events"][0]["payload"]["approval_ids"] == ["apr_abc"]
    assert body["events"][1]["payload"]["status"] == "idle"


def test_event_ids_continue_across_a_restart(settings: Settings) -> None:
    store = Store(settings.db_path)
    try:
        store.create_thread("thr_x", "quick")
        store.append_event(
            Event(type=EventType.TEXT, thread_id="thr_x", data={"content": "a"}).with_id(7)
        )
        assert store.last_event_id("thr_x") == 7
    finally:
        store.close()
