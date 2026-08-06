"""HTTP + SSE surface.

Every IM adapter is meant to be a thin client: grouping, folding and replay
are decided here, so an adapter never has to model an Agent SDK concept.

See docs/design/02-sse-api.md.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import index, preflight
from . import profiles as profiles_mod
from .approvals import UnknownApproval
from .config import Settings
from .manager import ThreadManager, UnknownProfile, UnknownThread
from .store import Store

log = logging.getLogger(__name__)


class NewThread(BaseModel):
    profile: str | None = None


class NewMessage(BaseModel):
    text: str = Field(min_length=1)


class Approval(BaseModel):
    approval_id: str
    decision: Literal["allow", "deny"]
    message: str = ""


class ModeChange(BaseModel):
    mode: Literal["default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # F23: the sandbox fails open when bwrap or socat is missing, so this
        # has to run before anything can accept a request.
        preflight.run(settings)
        profiles_mod.materialise(settings)
        store = Store(settings.db_path)
        app.state.settings = settings
        app.state.store = store
        app.state.manager = ThreadManager(settings, store)
        try:
            yield
        finally:
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
            # F2: shadowed skills cannot be disambiguated, so refuse to open a
            # thread that would silently run the wrong one.
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

    @app.post("/v1/threads/{thread_id}/messages", status_code=202)
    async def post_message(thread_id: str, body: NewMessage, request: Request) -> dict[str, Any]:
        # Returns immediately rather than awaiting the turn, so that queueing
        # (D1) has a natural expression: the client learns its position from
        # the event stream instead of a blocked request.
        try:
            position = await manager(request).send(thread_id, body.text)
        except UnknownThread as exc:
            raise HTTPException(404, "unknown thread") from exc
        return {"status": "queued" if position else "accepted", "position": position}

    @app.post("/v1/threads/{thread_id}/approve")
    async def approve(thread_id: str, body: Approval, request: Request) -> dict[str, str]:
        mgr = manager(request)
        if not mgr.is_live(thread_id):
            raise HTTPException(409, "thread is not running; the approval no longer exists")
        runner = await mgr.runner(thread_id)
        try:
            runner.resolve_approval(
                body.approval_id, allow=body.decision == "allow", message=body.message
            )
        except UnknownApproval as exc:
            raise HTTPException(404, "unknown or already-resolved approval") from exc
        return {"status": "accepted"}

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
        # Hot, unlike the profile: permission mode never enters a model
        # request, so switching costs no cache (D7).
        try:
            runner = await manager(request).runner(thread_id)
        except UnknownThread as exc:
            raise HTTPException(404, "unknown thread") from exc
        await runner.set_permission_mode(body.mode)
        return {"status": "ok", "mode": body.mode}

    # -- events ----------------------------------------------------------

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
        "pending_approvals": len(runner.approvals.pending) if runner else 0,
        "last_event_id": mgr.store.last_event_id(row.thread_id),
    }
