"""HTTP + SSE surface.

Every IM adapter is meant to be a thin client: grouping, folding and replay
are decided here, so an adapter never has to model an Agent SDK concept.

See docs/design/02-sse-api.md.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from . import index, preflight
from . import profiles as profiles_mod
from .artifacts import MAX_BYTES, descriptor
from .config import Settings
from .events import Event, EventType
from .manager import Attachment, ThreadManager, UnknownProfile, UnknownThread
from .store import Store

log = logging.getLogger(__name__)

MAX_ATTACHMENT_B64 = 28_000_000


class NewThread(BaseModel):
    profile: str | None = None


class AttachmentIn(BaseModel):
    name: str = Field("", max_length=256)
    mime: str = Field("", max_length=128)
    data_b64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_B64)


class ArtifactIn(BaseModel):
    artifact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str = Field(max_length=256)
    mime: str = Field(max_length=128)
    size: int = Field(ge=0, le=MAX_BYTES, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TransferResult(BaseModel):
    state: Literal["transferred", "failed"] = "transferred"
    error: str = Field("", max_length=512)


class NewMessage(BaseModel):
    text: str = ""
    attachments: list[AttachmentIn | ArtifactIn] = Field(default_factory=list, max_length=10)
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def _not_empty(self) -> NewMessage:
        if not self.text.strip() and not self.attachments:
            raise ValueError("需要 text 或 attachments")
        return self


class ModeChange(BaseModel):
    mode: Literal["plan", "auto"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        preflight.run(settings)
        profiles_mod.materialise(settings)
        store = Store(settings.db_path)
        app.state.settings = settings
        app.state.store = store
        app.state.manager = ThreadManager(settings, store)
        # Before the first request, so a client that reconnects immediately
        # reads the correction rather than the crash's last word.
        app.state.manager.recover()
        reaper = asyncio.create_task(app.state.manager.reap_forever(), name="idle-reaper")

        async def clean_files() -> None:
            while True:
                await asyncio.sleep(3600)
                try:
                    app.state.manager.artifacts.cleanup()
                except OSError:
                    log.exception("artifact cleanup failed")

        file_reaper = asyncio.create_task(clean_files(), name="file-reaper")
        try:
            yield
        finally:
            file_reaper.cancel()
            with suppress(asyncio.CancelledError):
                await file_reaper
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            await app.state.manager.shutdown()
            store.close()

    app = FastAPI(title="antares-agent", version="0.1.0", lifespan=lifespan)

    def manager(request: Request) -> ThreadManager:
        return request.app.state.manager

    # -- discovery -------------------------------------------------------

    @app.get("/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        mgr = manager(request)
        return {
            "status": "ok",
            "workspace": str(settings.workspace),
            "profiles": sorted(mgr.profiles),
            "live_threads": len(mgr._live),
            "max_live_threads": settings.max_live_threads,
        }

    @app.get("/v1/profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        return {
            "profiles": [
                {
                    "name": p.name,
                    "description": p.description,
                    "permission_mode": p.permission_mode,
                    "model": p.model,
                    "effort": p.effort,
                }
                for p in manager(request).profiles.values()
            ]
        }

    # -- threads ---------------------------------------------------------

    @app.post("/v1/threads", status_code=201)
    async def create_thread(body: NewThread, request: Request) -> dict[str, Any]:
        try:
            row = await manager(request).create(body.profile)
        except UnknownProfile as exc:
            raise HTTPException(400, f"unknown profile: {exc.args[0]}") from exc
        except index.DuplicateSkillError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _thread_payload(row, manager(request))

    @app.get("/v1/threads")
    async def list_threads(request: Request) -> dict[str, Any]:
        mgr = manager(request)
        return {"threads": [_thread_payload(r, mgr) for r in mgr.store.list_threads()]}

    @app.get("/v1/threads/{thread_id}")
    async def get_thread(thread_id: str, request: Request) -> dict[str, Any]:
        mgr = manager(request)
        row = mgr.store.get_thread(thread_id)
        if row is None:
            raise HTTPException(404, "unknown thread")
        return _thread_payload(row, mgr)

    @app.delete("/v1/threads/{thread_id}")
    async def delete_thread(thread_id: str, request: Request) -> dict[str, str]:
        try:
            await manager(request).delete(thread_id)
        except UnknownThread as exc:
            raise HTTPException(404, "unknown thread") from exc
        return {"status": "deleted"}

    # -- conversation ----------------------------------------------------

    @app.get("/v1/artifacts")
    async def pending_artifacts(request: Request) -> dict:
        artifacts = manager(request).artifacts
        return {
            "artifacts": [
                {
                    **json.loads(row["metadata"]),
                    "created": row["created"],
                    "thread_id": row["thread"],
                }
                for row in artifacts.list("outgoing")
            ]
        }

    @app.get("/v1/artifacts/{artifact_id}")
    async def download_artifact(artifact_id: str, request: Request) -> FileResponse:
        artifacts = manager(request).artifacts
        try:
            row = artifacts.get(artifact_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not row or row["direction"] != "outgoing" or not artifacts.path(artifact_id).is_file():
            raise HTTPException(404, "文件不存在或已过期")
        artifacts.active.add(artifact_id)
        return FileResponse(
            artifacts.path(artifact_id),
            media_type="application/octet-stream",
            background=BackgroundTask(artifacts.active.discard, artifact_id),
        )

    @app.post("/v1/artifacts/{artifact_id}/complete")
    async def complete_artifact(artifact_id: str, body: TransferResult, request: Request) -> dict:
        mgr = manager(request)
        try:
            row = mgr.artifacts.get(artifact_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not row or row["direction"] != "outgoing":
            raise HTTPException(404, "文件不存在")
        if row["state"] == "pending" and body.state == "failed":
            data = {"code": "file_failed", "message": f"文件传输失败：{body.error}"}
            event_log = mgr.event_log(row["thread"])
            if event_log:
                event_log.publish(Event(type=EventType.ERROR, thread_id=row["thread"], data=data))
            else:
                mgr._record(row["thread"], EventType.ERROR, data)
        mgr.artifacts.mark(artifact_id, body.state)
        return {"status": body.state}

    @app.put("/v1/threads/{thread_id}/attachments/{artifact_id}")
    async def upload_attachment(
        thread_id: str,
        artifact_id: str,
        request: Request,
        name: str = Query(max_length=256),
        mime: str = Query(max_length=128),
        size: int = Query(ge=0, le=MAX_BYTES),
        sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> dict:
        mgr = manager(request)
        if mgr.store.get_thread(thread_id) is None:
            raise HTTPException(404, "会话不存在")
        if size > mgr.settings.max_file_bytes:
            raise HTTPException(413, "文件大小超限")
        if request.headers.get("content-length") != str(size):
            raise HTTPException(411, "需要准确的 Content-Length")
        try:
            data = descriptor(
                dict(artifact_id=artifact_id, name=name, mime=mime, size=size, sha256=sha256)
            )
            await mgr.artifacts.receive(thread_id, data, request.stream())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(507, "附件写入失败") from exc
        return data

    @app.post("/v1/threads/{thread_id}/messages", status_code=202)
    async def post_message(thread_id: str, body: NewMessage, request: Request) -> dict[str, Any]:
        mgr = manager(request)
        if mgr.store.get_thread(thread_id) is None:
            raise HTTPException(404, "会话不存在")
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        if body.request_id and (previous := mgr.store.request(body.request_id)):
            if previous["thread"] != thread_id or previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "请求标识冲突")
            if previous["state"] not in ("accepted", "settled"):
                raise HTTPException(409, "消息交接结果不确定，请检查会话后重新发送")
            return json.loads(previous["result"])
        try:
            attachments = [
                Attachment(a.name, a.mime, path=mgr.artifacts.resolve(thread_id, a.model_dump()))
                if isinstance(a, ArtifactIn)
                else Attachment(a.name, a.mime, base64.b64decode(a.data_b64, validate=True))
                for a in body.attachments
            ]
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.request_id:
            mgr.store.begin_request(body.request_id, thread_id, fingerprint)
        position = await mgr.send(thread_id, body.text, attachments)
        result = {"status": "queued" if position else "accepted", "position": position}
        if body.request_id:
            mgr.store.finish_request(body.request_id, result)
        return result

    @app.post("/v1/threads/{thread_id}/interrupt")
    async def interrupt(thread_id: str, request: Request) -> dict[str, str]:
        mgr = manager(request)
        if not mgr.is_live(thread_id):
            return {"status": "idle"}
        runner = await mgr.runner(thread_id)
        await runner.interrupt()
        return {"status": "interrupted"}

    @app.post("/v1/threads/{thread_id}/mode")
    async def set_mode(thread_id: str, body: ModeChange, request: Request) -> dict[str, str]:
        try:
            runner = await manager(request).runner(thread_id)
        except UnknownThread as exc:
            raise HTTPException(404, "unknown thread") from exc
        try:
            await runner.set_permission_mode(body.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "ok", "mode": body.mode}

    # -- events ----------------------------------------------------------

    @app.get("/v1/threads/{thread_id}/events/replay")
    async def replay(
        thread_id: str,
        request: Request,
        after: int | None = Query(default=None),
    ) -> dict[str, Any]:
        """History only, from sqlite, without starting the thread.

        The streaming endpoint below revives the thread as a side effect of
        subscribing (a CLI process is ~123MB, V4), which is the wrong price for
        a client that only wants to catch up on what it missed while it was
        down. This serves that case and leaves the thread cold.
        """
        mgr = manager(request)
        if mgr.store.get_thread(thread_id) is None:
            raise HTTPException(404, "unknown thread")
        return {
            "events": mgr.store.events_since(thread_id, after or 0),
            "last_event_id": mgr.store.last_event_id(thread_id),
            "status": str(mgr.status(thread_id)),
        }

    @app.get("/v1/threads/{thread_id}/events")
    async def events(
        thread_id: str,
        request: Request,
        after: int | None = Query(default=None),
    ) -> EventSourceResponse:
        mgr = manager(request)
        if mgr.store.get_thread(thread_id) is None:
            raise HTTPException(404, "unknown thread")

        runner = await mgr.runner(thread_id)
        event_log = runner.log

        async def stream() -> AsyncIterator[dict[str, str]]:
            # Anything older than the in-memory ring buffer comes from sqlite,
            # so a client that was away across a restart still gets a
            # contiguous sequence rather than a hole.
            cursor = after
            if after is not None:
                buffered = event_log.since(after)
                oldest = buffered[0].id if buffered else None
                if oldest is None or oldest > after + 1:
                    for row in mgr.store.events_since(thread_id, after):
                        payload = row["payload"]
                        cursor = int(str(payload.get("id", "evt_0")).removeprefix("evt_"))
                        yield {
                            "event": row["type"],
                            "id": str(cursor),
                            "data": json.dumps(payload, ensure_ascii=False),
                        }

            async for event in event_log.stream(cursor):
                if await request.is_disconnected():
                    break
                yield event.sse()

        return EventSourceResponse(stream())

    return app


def _thread_payload(row: Any, mgr: ThreadManager) -> dict[str, Any]:
    runner = mgr._live.get(row.thread_id)
    return {
        "thread_id": row.thread_id,
        "profile": row.profile,
        "summary": row.summary,
        "created_at": row.created_at,
        "last_active_at": row.last_active_at,
        "status": str(mgr.status(row.thread_id)),
        "background_agents": len(runner.translator.background_tasks) if runner else 0,
        "permission_mode": runner.state.permission_mode if runner else row.permission_mode,
        "last_event_id": mgr.store.last_event_id(row.thread_id),
    }
