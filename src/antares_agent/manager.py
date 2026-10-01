"""Persistent threads with a shared Codex runtime."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import secrets
import shutil
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import index
from . import manifest as manifest_mod
from . import profiles as profiles_mod
from .artifacts import Artifacts
from .config import Settings
from .eventlog import EventLog
from .events import Event, EventType, ThreadStatus
from .profiles import Profile
from .runner import ThreadRunner
from .runtime import Runtime
from .store import Store, ThreadRow

log = logging.getLogger(__name__)


class UnknownThread(KeyError):
    pass


class UnknownProfile(KeyError):
    pass


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    data: bytes = b""
    path: Path | None = None


class ThreadManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.artifacts = Artifacts(settings.artifact_path)
        self.profiles: dict[str, Profile] = profiles_mod.load(settings)
        self._live: OrderedDict[str, ThreadRunner] = OrderedDict()
        self._logs: dict[str, EventLog] = {}
        self._lock = asyncio.Lock()
        self.runtime = Runtime(settings)

    def reload_manifest(self) -> manifest_mod.Manifest:
        """Re-read on every thread creation, so adding a repo needs no restart."""
        try:
            return manifest_mod.load(self.settings.workspace)
        except manifest_mod.ManifestError as exc:
            log.error("workspace.toml is invalid, continuing without it: %s", exc)
            return manifest_mod.empty(self.settings.workspace)

    # -- lifecycle -------------------------------------------------------

    async def create(self, profile_name: str | None = None) -> ThreadRow:
        manifest = self.reload_manifest()
        name = profile_name or manifest.default_profile
        if name not in self.profiles:
            raise UnknownProfile(name)

        index.write(manifest)

        thread_id = "thr_" + secrets.token_hex(6)
        self.store.create_thread(thread_id, name)
        self.store.touch(thread_id, permission_mode=self.profiles[name].permission_mode)
        row = self.store.get_thread(thread_id)
        assert row is not None
        return row

    async def runner(self, thread_id: str) -> ThreadRunner:
        """The live runner for a thread, starting or reviving it as needed."""
        async with self._lock:
            existing = self._live.get(thread_id)
            if existing is not None:
                self._live.move_to_end(thread_id)
                # Being asked for counts as activity. Without this the reaper
                # could close a runner between here and the `send()` the caller
                # is about to make on it.
                existing.last_active = time.monotonic()
                return existing

            row = self.store.get_thread(thread_id)
            if row is None:
                raise UnknownThread(thread_id)

            await self._evict_if_needed()

            profile = self.profiles.get(row.profile) or Profile(name=row.profile)
            event_log = self._logs.get(thread_id)
            if event_log is None:
                event_log = EventLog(
                    thread_id,
                    capacity=self.settings.event_buffer,
                    sink=self.store.append_event,
                )
                # Continue the sequence rather than restarting it, so a client
                # reconnecting with `?after=` across a restart is not sent the
                # whole history again under reused ids.
                event_log._next_id = self.store.last_event_id(thread_id) + 1
                self._logs[thread_id] = event_log
            else:
                # The log outlives eviction, and `close()` left it closed --
                # every later `stream()` would end right after its replay, so
                # the thread would go on working with nobody able to follow it.
                event_log.reopen()

            runner = ThreadRunner(
                thread_id=thread_id,
                settings=self.settings,
                manifest=self.reload_manifest(),
                profile=profile,
                event_log=event_log,
                artifacts=self.artifacts,
                runtime=self.runtime,
                persist=lambda state: self.store.touch(
                    state.thread_id,
                    session_id=state.session_id,
                    summary=state.summary,
                    permission_mode=state.permission_mode,
                ),
            )
            runner.state.session_id = row.session_id
            runner.state.summary = row.summary
            runner.state.permission_mode = row.permission_mode
            await runner.start(resume=row.session_id)
            self._live[thread_id] = runner
            return runner

    async def _evict_if_needed(self) -> None:
        while len(self._live) >= self.settings.max_live_threads:
            victim = next(
                (tid for tid, r in self._live.items() if not r.busy),
                None,
            )
            if victim is None:
                log.warning("all %d live threads are busy; not evicting", len(self._live))
                return
            log.info("evicting idle thread %s", victim)
            await self._retire(victim)

    async def _retire(self, thread_id: str) -> None:
        """Drop a live runner, keeping everything a revive needs.

        The caller holds `_lock`. The session id has to reach the store before
        the client goes, or the next message starts a conversation from
        nothing instead of resuming this one.
        """
        runner = self._live.pop(thread_id)
        self.store.touch(thread_id, session_id=runner.state.session_id)
        await runner.close()

    async def reap_idle(self) -> list[str]:
        """Evict idle runner caches."""
        ttl = self.settings.idle_ttl_s
        if ttl <= 0:
            return []
        now = time.monotonic()
        reaped: list[str] = []
        async with self._lock:
            for thread_id, runner in list(self._live.items()):
                if runner.busy or now - runner.last_active < ttl:
                    continue
                log.info("reaping thread %s after %.0fs idle", thread_id, now - runner.last_active)
                await self._retire(thread_id)
                reaped.append(thread_id)
        return reaped

    async def reap_forever(self) -> None:
        """The reaper's own loop. Cancelled at shutdown."""
        ttl = self.settings.idle_ttl_s
        if ttl <= 0:
            return
        while True:
            # A quarter of the TTL: a thread is closed somewhere between one
            # and one-and-a-quarter TTLs after its last turn, which is close
            # enough for something whose only cost is a revive.
            await asyncio.sleep(max(15.0, ttl / 4))
            try:
                await self.reap_idle()
            except Exception:
                # A failure here must not kill the loop; the next tick retries
                # and the alternative is a core spinning until the next restart.
                log.exception("idle reaper failed")

    async def close_thread(self, thread_id: str) -> None:
        runner = self._live.pop(thread_id, None)
        if runner is not None:
            self.store.touch(thread_id, session_id=runner.state.session_id)
            await runner.close()
        self._logs.pop(thread_id, None)

    async def delete(self, thread_id: str) -> None:
        if self.store.get_thread(thread_id) is None:
            raise UnknownThread(thread_id)
        await self.close_thread(thread_id)
        self.store.delete_thread(thread_id)

    async def shutdown(self) -> None:
        for thread_id in list(self._live):
            with contextlib.suppress(Exception):
                await self.close_thread(thread_id)
        await self.runtime.close()
        self.artifacts.close()

    # -- input -----------------------------------------------------------

    async def send(
        self, thread_id: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> int | None:
        runner = await self.runner(thread_id)
        prompt = await asyncio.to_thread(self._stash, runner, text, attachments)
        position = await runner.send(prompt)
        self.store.touch(
            thread_id,
            session_id=runner.state.session_id,
            summary=runner.state.summary,
        )
        return position

    def _stash(
        self, runner: ThreadRunner, text: str, attachments: Sequence[Attachment]
    ) -> str | list[dict]:
        """Persist attachments and include native image inputs."""
        if not attachments:
            return text

        inbox = self.settings.workspace / runner.manifest.scratch / "inbox" / runner.thread_id
        inbox.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        images = []
        for item in attachments:
            path = inbox / (secrets.token_hex(4) + _suffix(item.mime))
            if item.path is None:
                path.write_bytes(item.data)
            else:
                if shutil.disk_usage(inbox).free < item.path.stat().st_size:
                    raise OSError("工作区磁盘空间不足")
                temporary = path.with_suffix(path.suffix + ".part")
                try:
                    shutil.copyfile(item.path, temporary)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            if item.mime.startswith("image/"):
                images.append({"type": "localImage", "path": str(path)})
            kind = "图片" if item.mime.startswith("image/") else "文件"
            named = f" {item.name}" if item.name else ""
            lines.append(f"[用户发来{kind}{named}]：{path}")
        # Below the caption, not above it: the first line becomes the thread's
        # summary, and a file path is a poor name for a conversation.
        return [{"type": "text", "text": "\n".join([*([text] if text else []), *lines])}, *images]

    def event_log(self, thread_id: str) -> EventLog | None:
        return self._logs.get(thread_id)

    def status(self, thread_id: str) -> ThreadStatus:
        runner = self._live.get(thread_id)
        return runner.state.status if runner else ThreadStatus.IDLE

    def is_live(self, thread_id: str) -> bool:
        return thread_id in self._live

    # -- recovery --------------------------------------------------------

    def recover(self) -> list[str]:
        """Persist interrupted work before serving clients."""
        for row in self.artifacts.list("outgoing"):
            if not row["published"] and self.store.get_thread(row["thread"]):
                if not self.store.has_artifact(row["id"]):
                    self._record(row["thread"], EventType.FILE, json.loads(row["metadata"]))
                self.artifacts.published(row["id"])
        for request in self.store.interrupt_requests():
            self._record(
                request["thread"],
                EventType.ERROR,
                {
                    "code": "message_uncertain",
                    "request_id": request["id"],
                    "message": "进程重启，消息交接结果不确定；请检查会话后重新发送。",
                },
            )
        self.artifacts.cleanup()
        recovered: list[str] = []
        for row in self.store.list_threads(limit=10_000):
            status = self.store.last_status(row.thread_id)
            if status is None or status == str(ThreadStatus.IDLE):
                continue

            self._record(
                row.thread_id,
                EventType.ERROR,
                {"code": "interrupted", "message": "服务重启中断了上次任务，未自动重试。"},
            )
            self._record(
                row.thread_id,
                EventType.THREAD_STATUS,
                {"status": str(ThreadStatus.IDLE), "background_agents": 0},
            )
            recovered.append(row.thread_id)

        return recovered

    def _record(self, thread_id: str, type_: EventType, data: dict) -> None:
        """Append straight to the store, outside any EventLog.

        Nothing is subscribed at this point and nothing should be started to
        make it so; the id continues the thread's sequence, which is where a
        later `EventLog` picks it up from anyway.
        """
        event = Event(type=type_, thread_id=thread_id, data=data)
        self.store.append_event(event.with_id(self.store.last_event_id(thread_id) + 1))


def _suffix(mime: str) -> str:
    """The extension to store an attachment under, from its mime type.

    Not from the sender's filename, because the extension is what decides
    whether the CLI reads the file as an image at all, and that call should
    not be theirs to make. `.bin` when the mime says nothing: read as bytes,
    which is the honest answer for something nobody described.
    """
    return mimetypes.guess_extension(mime) or ".bin"
