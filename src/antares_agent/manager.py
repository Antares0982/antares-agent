"""Owns the set of live threads.

A `ClaudeSDKClient` is not free: V4 measured ~218MB PSS for the first one and
~123MB for each additional, so they are pooled rather than kept one-per-thread
forever. Evicting a thread does not lose it -- the CLI keeps its own session
file, and the next message revives it through `resume`.

Only idle threads are evicted. Tearing down a client mid-turn would abandon
work and strand any approval waiting on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import OrderedDict

from . import index
from . import manifest as manifest_mod
from . import profiles as profiles_mod
from .config import Settings
from .eventlog import EventLog
from .events import Event, EventType, ThreadStatus
from .profiles import Profile
from .runner import ThreadRunner
from .store import Store, ThreadRow

log = logging.getLogger(__name__)

#: Left by the CLI when a pending approval dies with the process (V1).
ABORT_MARKER = "Tool permission stream closed"


class UnknownThread(KeyError):
    pass


class UnknownProfile(KeyError):
    pass


class ThreadManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.profiles: dict[str, Profile] = profiles_mod.load(settings)
        self._live: OrderedDict[str, ThreadRunner] = OrderedDict()
        self._logs: dict[str, EventLog] = {}
        self._lock = asyncio.Lock()

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

        # Refreshed here rather than on first message, so the index the agent
        # reads matches the workspace as it was when the thread began.
        # Existing threads keep theirs (01-workspace-manifest.md).
        #
        # A duplicate skill name is raised, not logged: F2 showed the loser is
        # shadowed silently with no syntax to disambiguate, so a warning would
        # just mean the agent quietly runs the wrong skill.
        index.write(manifest)

        thread_id = "thr_" + secrets.token_hex(6)
        return self.store.create_thread(thread_id, name)

    async def runner(self, thread_id: str) -> ThreadRunner:
        """The live runner for a thread, starting or reviving it as needed."""
        async with self._lock:
            existing = self._live.get(thread_id)
            if existing is not None:
                self._live.move_to_end(thread_id)
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

            runner = ThreadRunner(
                thread_id=thread_id,
                settings=self.settings,
                manifest=self.reload_manifest(),
                profile=profile,
                event_log=event_log,
            )
            runner.state.session_id = row.session_id
            runner.state.summary = row.summary
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
            runner = self._live.pop(victim)
            self.store.touch(victim, session_id=runner.state.session_id)
            await runner.close()

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

    # -- input -----------------------------------------------------------

    async def send(self, thread_id: str, text: str) -> int | None:
        runner = await self.runner(thread_id)
        position = await runner.send(text)
        self.store.touch(
            thread_id,
            session_id=runner.state.session_id,
            summary=runner.state.summary,
        )
        return position

    def event_log(self, thread_id: str) -> EventLog | None:
        return self._logs.get(thread_id)

    def status(self, thread_id: str) -> ThreadStatus:
        runner = self._live.get(thread_id)
        return runner.state.status if runner else ThreadStatus.IDLE

    def is_live(self, thread_id: str) -> bool:
        return thread_id in self._live

    def report_approval_lost(self, thread_id: str) -> None:
        """Tell the client a restart ate a pending approval (V1).

        `resume` does not re-issue the request, so without this the thread
        would just sit there looking finished while the user waits for a
        prompt that will never arrive.
        """
        event_log = self._logs.get(thread_id)
        if event_log is None:
            return
        event_log.publish(
            Event(
                type=EventType.ERROR,
                thread_id=thread_id,
                data={
                    "code": "approval_lost",
                    "message": "上次的审批请求因进程重启而失效，可以重发消息让它重试。",
                },
            )
        )
