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
import mimetypes
import secrets
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

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


class UnknownThread(KeyError):
    pass


class UnknownProfile(KeyError):
    pass


@dataclass(frozen=True)
class Attachment:
    """A file that arrived with a message, already decoded.

    `name` is whatever the sender called it and is never used as a path --
    see `ThreadManager._stash`.
    """

    name: str
    mime: str
    data: bytes


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
        """Close the CLI behind threads nobody has spoken to in a while.

        A CPU measure, not a memory one. F31: once it has run a turn the CLI
        keeps a ~60Hz loop going and costs a full core on the Pi for as long
        as it lives, idle or not -- so a pool that only evicts under pressure
        parks a spinning core per thread until something else needs the slot.
        Eviction is lossless (the CLI keeps its session file), so the price of
        being wrong here is one revive, paid by whoever speaks next.
        """
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

    # -- input -----------------------------------------------------------

    async def send(
        self, thread_id: str, text: str, attachments: Sequence[Attachment] = ()
    ) -> int | None:
        runner = await self.runner(thread_id)
        position = await runner.send(self._stash(runner, text, attachments))
        self.store.touch(
            thread_id,
            session_id=runner.state.session_id,
            summary=runner.state.summary,
        )
        return position

    def _stash(self, runner: ThreadRunner, text: str, attachments: Sequence[Attachment]) -> str:
        """Put attachments on disk and name them in the prompt.

        A path is the whole mechanism. `Read` renders images natively and is
        already sandboxed, so an image only has to exist somewhere the model
        may open -- there is no second content-block channel to build, and no
        event type or SDK shape to keep in step with it. It also outlives the
        turn: a blob inlined into a prompt is gone once the thread is evicted,
        a file is still there when it resumes.

        The sender's filename never becomes a path component. It comes from
        Telegram, so a `../` in it would put the file wherever it liked. The
        name on disk is ours; theirs is repeated as prose, where it can
        mislead the model but not the filesystem.
        """
        if not attachments:
            return text

        inbox = self.settings.workspace / runner.manifest.scratch / "inbox" / runner.thread_id
        inbox.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for item in attachments:
            path = inbox / (secrets.token_hex(4) + _suffix(item.mime))
            path.write_bytes(item.data)
            kind = "图片" if item.mime.startswith("image/") else "文件"
            named = f" {item.name}" if item.name else ""
            lines.append(f"[用户发来{kind}{named}]：{path}")
        # Below the caption, not above it: the first line becomes the thread's
        # summary, and a file path is a poor name for a conversation.
        return "\n".join([*([text] if text else []), *lines])

    def event_log(self, thread_id: str) -> EventLog | None:
        return self._logs.get(thread_id)

    def status(self, thread_id: str) -> ThreadStatus:
        runner = self._live.get(thread_id)
        return runner.state.status if runner else ThreadStatus.IDLE

    def is_live(self, thread_id: str) -> bool:
        return thread_id in self._live

    # -- recovery --------------------------------------------------------

    def recover(self) -> list[str]:
        """Write down what a crash left unfinished, before any client reads it.

        A thread's status lives in its event stream, so a process that dies
        mid-turn leaves every client holding a `busy` that is never followed by
        an `idle`, and an `approval.required` that is never answered. Neither
        corrects itself later: the CLI does fold the pending tool call into a
        failure and keep the session consistent (V1), but it does not re-issue
        the request on `resume`, and the thread stays cold until someone speaks
        to it. So the correction is written here, at startup, while every
        thread is still cold -- reviving one to tell it that it is idle would
        cost a CLI process (~123MB, V4) for a thread nobody asked for.

        A non-idle tail is the whole test. An unanswered approval always
        leaves one behind, because the status only returns to `busy` once the
        last pending approval resolves, so this never misses one.
        """
        recovered: list[str] = []
        for row in self.store.list_threads(limit=10_000):
            status = self.store.last_status(row.thread_id)
            if status is None or status == str(ThreadStatus.IDLE):
                continue

            lost = self.store.unanswered_approvals(row.thread_id)
            if lost:
                self._record(
                    row.thread_id,
                    EventType.ERROR,
                    {
                        "code": "approval_lost",
                        # Named so the client can retire exactly the buttons
                        # that died, instead of guessing or leaving them live.
                        "approval_ids": lost,
                        "message": "上次的审批请求因进程重启而失效，可以重发消息让它重试。",
                    },
                )
            self._record(
                row.thread_id,
                EventType.THREAD_STATUS,
                {"status": str(ThreadStatus.IDLE), "background_agents": 0},
            )
            recovered.append(row.thread_id)
            log.info(
                "recovered thread %s from %s (%d lost approvals)", row.thread_id, status, len(lost)
            )
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
