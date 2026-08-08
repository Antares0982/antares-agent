"""SSE ↔ AMQP pipe. Runs next to antares-agent; carries no business logic.

The Pi is behind NAT and the bot host is not, so neither can dial the other --
both dial the broker instead (D10). This process is the Pi-side end of that:
it turns `agent.cmd` messages into HTTP calls and the resulting event stream
into `agent.event` messages.

What it deliberately does *not* do is translate. The bus carries
`02-sse-api.md`'s events verbatim (D11), so nothing here knows what a Telegram
message is, and adding a new event type to the agent needs no change here.

Two rules earn their keep and both are load-bearing:

* **Follow only while a thread is busy** (D13). `GET /events` revives the
  thread as a side effect of subscribing, and a live thread is ~123MB (V4).
  Holding a stream per chat would pin every chat's runner and defeat the LRU.
  Idle threads emit nothing by definition, so nothing is missed.
* **Never lose an approval.** A dropped `approval.required` strands the thread
  in `awaiting_approval` with nobody able to answer, so the queue is durable
  and the cursor is the client's, not ours.

Standalone: depends only on `httpx` and `aio_pika`. Configure by environment.

Env:
  ANTARES_API_SOCKET   unix socket of the agent (preferred)
  ANTARES_API_URL      http base URL instead; default http://127.0.0.1:60001
  AGENT_EXCHANGE       topic exchange, default `agent`
  AGENT_CMD_QUEUE      durable queue for commands, default `agent.cmd`
  QUEUE_TTL_MS         message TTL on that queue, default 3600000
  QUEUE_MAX_LEN        max depth, default 5000
  RMQ_HOST RMQ_PORT RMQ_USER RMQ_PASS RMQ_VHOST
  RMQ_CAFILE RMQ_CERTFILE RMQ_KEYFILE   (mTLS; TLS is used when all three are set)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from typing import Any

import aio_pika
import httpx
from aio_pika import ExchangeType

log = logging.getLogger("antares_agent.relay")

EXCHANGE = os.environ.get("AGENT_EXCHANGE", "agent")
CMD_QUEUE = os.environ.get("AGENT_CMD_QUEUE", "agent.cmd")
CMD_KEY = "agent.cmd"
EVENT_KEY = "agent.event"

#: Bounds on how much the broker holds while the bot is down. Past either one
#: the bot recovers with `{"op": "resume", "after": N}` instead, so these only
#: decide when the cheap path stops working -- not whether events are lost.
QUEUE_TTL_MS = int(os.environ.get("QUEUE_TTL_MS", 3_600_000))
QUEUE_MAX_LEN = int(os.environ.get("QUEUE_MAX_LEN", 5000))

#: Relay-originated envelopes. They share `agent.event` with real agent events
#: so the bot has one dispatch, and they are namespaced so it can tell them
#: apart from anything the agent itself will ever emit.
#: One envelope for both `new_thread` and `switch`: the bot does the same
#: thing either way -- point the chat at a thread -- and a second name would
#: only mean a second code path that has to stay in step with the first.
THREAD_BOUND = "relay.thread_bound"
THREAD_LIST = "relay.thread_list"
CMD_FAILED = "relay.cmd_failed"

_TIMEOUT = httpx.Timeout(30.0, read=None)


class Relay:
    #: Pause before re-opening a dropped stream. Reconnecting immediately would
    #: spin against whatever caused the drop.
    _retry_delay = 3.0

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        #: One follower task per thread. The key is what keeps a burst of
        #: messages to a busy thread from opening a second stream.
        self._followers: dict[str, asyncio.Task[None]] = {}

    # -- bus -------------------------------------------------------------

    async def run(self) -> None:
        connection = await aio_pika.connect_robust(**_amqp_kwargs())
        async with connection:
            channel = await connection.channel()
            # One in flight at a time: commands for a thread are ordered, and
            # dispatching two `message` ops concurrently would race the queue
            # position the user is told about.
            await channel.set_qos(prefetch_count=1)
            self._exchange = await channel.declare_exchange(
                EXCHANGE, ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(
                CMD_QUEUE,
                durable=True,
                arguments={"x-message-ttl": QUEUE_TTL_MS, "x-max-length": QUEUE_MAX_LEN},
            )
            await queue.bind(self._exchange, routing_key=CMD_KEY)
            log.info(
                "relay up: exchange=%s queue=%s api=%s", EXCHANGE, CMD_QUEUE, self._http.base_url
            )
            await queue.consume(self._on_command)
            await asyncio.Future()

    async def _publish(self, type_: str, payload: dict[str, Any]) -> None:
        if self._exchange is None:
            return
        body = json.dumps({"type": type_, "payload": payload}, ensure_ascii=False).encode()
        await self._exchange.publish(
            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=EVENT_KEY,
        )

    async def _on_command(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        # `requeue=False`: a command that raised will raise again, and an
        # infinite redelivery loop is worse than a dropped command the user can
        # simply retype.
        async with message.process(requeue=False):
            try:
                cmd = json.loads(message.body)
            except json.JSONDecodeError:
                log.warning("undecodable command: %.100s", message.body)
                return
            try:
                await self._dispatch(cmd)
            except Exception as exc:
                log.exception("command failed: %s", cmd.get("op"))
                await self._publish(
                    CMD_FAILED,
                    {
                        "op": cmd.get("op"),
                        "chat_id": cmd.get("chat_id"),
                        "thread_id": cmd.get("thread_id"),
                        "detail": str(exc),
                    },
                )

    # -- commands --------------------------------------------------------

    async def _dispatch(self, cmd: dict[str, Any]) -> None:
        op = cmd.get("op")
        thread_id = cmd.get("thread_id")

        if op == "new_thread":
            body = {"profile": cmd.get("profile")} if cmd.get("profile") else {}
            await self._bind(await self._post("/v1/threads", body), cmd.get("chat_id"))
            return

        if op == "list":
            reply = await self._get("/v1/threads")
            await self._publish(
                THREAD_LIST, {"chat_id": cmd.get("chat_id"), "threads": reply["threads"]}
            )
            return

        if not isinstance(thread_id, str):
            raise ValueError(f"{op} needs a thread_id")

        match op:
            case "message":
                await self._post(f"/v1/threads/{thread_id}/messages", {"text": cmd["text"]})
                self._follow(thread_id, cmd.get("after"))
            case "approve":
                await self._post(
                    f"/v1/threads/{thread_id}/approve",
                    {
                        "approval_id": cmd["approval_id"],
                        "decision": cmd["decision"],
                        "message": cmd.get("message", ""),
                    },
                )
            case "interrupt":
                await self._post(f"/v1/threads/{thread_id}/interrupt", {})
            case "mode":
                await self._post(f"/v1/threads/{thread_id}/mode", {"mode": cmd["mode"]})
            case "resume":
                await self._resume(thread_id, cmd.get("after"))
            case "switch":
                # Reads the thread rather than trusting the id: a typo would
                # otherwise bind the chat to something that does not exist, and
                # every later command would fail one at a time instead of once.
                # This endpoint does not wake the thread (manager.status only
                # reads the live map).
                await self._bind(await self._get(f"/v1/threads/{thread_id}"), cmd.get("chat_id"))
            case _:
                raise ValueError(f"unknown op {op!r}")

    async def _bind(self, thread: dict[str, Any], chat_id: Any) -> None:
        await self._publish(
            THREAD_BOUND,
            {
                "chat_id": chat_id,
                "thread_id": thread["thread_id"],
                "profile": thread.get("profile"),
                "summary": thread.get("summary"),
                "status": thread.get("status"),
                "last_event_id": thread.get("last_event_id"),
            },
        )

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self._http.get(path, params=params or None)
        if response.status_code >= 400:
            raise RelayError(response.status_code, _detail(response))
        return response.json()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(path, json=body)
        if response.status_code >= 400:
            # 409 in particular is not a bug: the thread was evicted and the
            # approval went with it. The bot needs the code to say so.
            raise RelayError(response.status_code, _detail(response))
        return response.json() if response.content else {}

    async def _resume(self, thread_id: str, after: int | None) -> None:
        """Fill the gap from sqlite, then follow only if there is a turn running.

        This uses the replay endpoint rather than the stream precisely so that
        catching up after a bot restart does not spin up a CLI process for
        every thread the bot happened to have open.
        """
        data = await self._get(f"/v1/threads/{thread_id}/events/replay", after=after or 0)
        for event in data["events"]:
            await self._publish(event["type"], event["payload"])
        if data["status"] != "idle":
            self._follow(thread_id, data["last_event_id"])

    # -- following -------------------------------------------------------

    def _follow(self, thread_id: str, after: int | None) -> None:
        existing = self._followers.get(thread_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._follow_until_idle(thread_id, after))
        self._followers[thread_id] = task
        task.add_done_callback(lambda t: self._followers.pop(thread_id, None))

    async def _follow_until_idle(self, thread_id: str, after: int | None) -> None:
        # `_dispatch` publishes `busy` before `POST /messages` returns, so the
        # busy for *this* turn is always in the replayed gap. Requiring it
        # before honouring an idle keeps a stale idle -- one the bot had not
        # yet consumed when it went down -- from ending the follow immediately.
        seen_busy = await self._is_busy(thread_id)
        cursor = after
        while True:
            try:
                async for type_, payload in self._stream(thread_id, cursor):
                    # Advanced only once the broker has confirmed the publish
                    # (aio_pika confirms by default), never before. Bumping it
                    # first would make the retry below resume *past* the event
                    # that failed to publish, quietly dropping it -- and the
                    # confirm exists precisely so that cannot happen.
                    await self._publish(type_, payload)
                    cursor = _event_id(payload) or cursor
                    if type_ != "thread.status":
                        continue
                    if payload.get("status") == "idle" and seen_busy:
                        return
                    if payload.get("status") != "idle":
                        seen_busy = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The cursor is the whole recovery story: reconnecting with it
                # replays the gap, so a dropped connection costs latency and
                # nothing else.
                log.warning("stream for %s dropped at %s (%s); retrying", thread_id, cursor, exc)
                await asyncio.sleep(self._retry_delay)
                continue
            # A clean end of stream without idle means the server closed it
            # (shutdown, eviction). Reconnecting would revive the thread, so
            # stop and let the next command decide.
            log.info("stream for %s ended without idle at %s", thread_id, cursor)
            return

    async def _is_busy(self, thread_id: str) -> bool:
        response = await self._http.get(f"/v1/threads/{thread_id}")
        return response.status_code < 400 and response.json().get("status") != "idle"

    async def _stream(self, thread_id: str, after: int | None):
        params = {"after": after} if after else {}
        async with self._http.stream(
            "GET", f"/v1/threads/{thread_id}/events", params=params, timeout=_TIMEOUT
        ) as response:
            response.raise_for_status()
            type_ = ""
            data = ""
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        with contextlib.suppress(json.JSONDecodeError):
                            yield type_ or "message", json.loads(data)
                    type_, data = "", ""
                elif line.startswith("event:"):
                    type_ = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
                # `id:` is ignored on purpose -- the payload carries `id` too,
                # and one source of truth for the cursor is enough. `:` comment
                # lines (sse-starlette's keepalive pings) fall through here.


class RelayError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    with contextlib.suppress(Exception):
        return str(response.json().get("detail", response.text))
    return response.text


def _event_id(payload: dict[str, Any]) -> int | None:
    raw = str(payload.get("id", "")).removeprefix("evt_")
    return int(raw) if raw.isdigit() else None


def _amqp_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": os.environ.get("RMQ_HOST", "127.0.0.1"),
        "port": int(os.environ.get("RMQ_PORT", 5671)),
    }
    vhost = os.environ.get("RMQ_VHOST", "/")
    if vhost and vhost != "/":
        kwargs["virtualhost"] = vhost
    if user := os.environ.get("RMQ_USER"):
        kwargs["login"] = user
        kwargs["password"] = os.environ.get("RMQ_PASS", "")

    cafile = os.environ.get("RMQ_CAFILE")
    certfile = os.environ.get("RMQ_CERTFILE")
    keyfile = os.environ.get("RMQ_KEYFILE")
    if cafile and certfile and keyfile:
        context = ssl.create_default_context(cafile=cafile)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        # Matches the existing clients against this broker; its certificate
        # predates the stricter X509 checks Python now applies by default.
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl"] = True
        kwargs["ssl_context"] = context
    return kwargs


def _http_client() -> httpx.AsyncClient:
    socket = os.environ.get("ANTARES_API_SOCKET")
    if socket:
        # base_url still has to be a well-formed URL; the host part is unused
        # once the transport is bound to the socket.
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket),
            base_url="http://agent",
            timeout=_TIMEOUT,
        )
    return httpx.AsyncClient(
        base_url=os.environ.get("ANTARES_API_URL", "http://127.0.0.1:60001"),
        timeout=_TIMEOUT,
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    async def run() -> None:
        async with _http_client() as http:
            await Relay(http).run()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
