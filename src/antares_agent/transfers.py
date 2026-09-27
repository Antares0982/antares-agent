from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from .artifacts import CHUNK, RETRY_WINDOW, descriptor, valid_id

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,lane TEXT NOT NULL,"
            "payload TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',"
            "attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL DEFAULT 0)"
        )
        self.db.commit()

    def add(self, job_id: str, lane: str, payload: dict) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO jobs(id,lane,payload,created) VALUES(?,?,?,?)",
            (job_id, lane, json.dumps(payload), payload.get("created", time.time())),
        )
        self.db.commit()

    def due(self, lane: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs j WHERE state='pending' AND lane=? AND due<=? "
            "AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.state='pending' AND p.lane=j.lane "
            "AND p.rowid<j.rowid AND json_extract(p.payload,'$.thread_id')="
            "json_extract(j.payload,'$.thread_id')) ORDER BY rowid LIMIT 1",
            (lane, time.time()),
        ).fetchone()
        return dict(row) if row else None

    def finish(self, job_id: str, state: str = "done") -> None:
        self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))
        self.db.commit()

    def retry(self, row: dict) -> None:
        delay = min(300, 5 * 2 ** min(row["attempts"], 6))
        self.db.execute(
            "UPDATE jobs SET attempts=attempts+1,due=? WHERE id=?", (time.time() + delay, row["id"])
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


async def checked_stream(response: httpx.Response, data: dict):
    size = 0
    digest = hashlib.sha256()
    async for chunk in response.aiter_bytes(CHUNK):
        size += len(chunk)
        if size > data["size"]:
            raise ValueError("文件长度超限")
        digest.update(chunk)
        yield chunk
    if size != data["size"] or digest.hexdigest() != data["sha256"]:
        raise ValueError("文件长度或摘要不符")


class Transfers:
    def __init__(self, relay: Any, remote: httpx.AsyncClient, state: Path) -> None:
        self.relay = relay
        self.remote = remote
        self.jobs = Jobs(state)

    def enqueue(self, command: dict) -> None:
        command.setdefault("request_id", secrets.token_hex(16))
        valid_id(command["request_id"])
        if not isinstance(command.get("thread_id"), str):
            raise ValueError("会话标识无效")
        for item in command.get("attachments", []):
            if "artifact_id" in item:
                descriptor(item)
        self.jobs.add(command["request_id"], "incoming", command)

    async def export(self, data: dict) -> None:
        item = descriptor(data)
        async with self.relay._http.stream("GET", f"/v1/artifacts/{item['artifact_id']}") as src:
            src.raise_for_status()
            reply = await self.remote.put(
                f"/outgoing/{item['artifact_id']}",
                content=checked_stream(src, item),
                headers={"Content-Length": str(item["size"])},
            )
            reply.raise_for_status()
        await self.relay._post(f"/v1/artifacts/{item['artifact_id']}/complete", {})

    async def deliver(self, command: dict) -> None:
        for raw in command.get("attachments", []):
            if "artifact_id" not in raw:
                continue
            item = descriptor(raw)
            async with self.remote.stream("GET", f"/incoming/{item['artifact_id']}") as src:
                src.raise_for_status()
                reply = await self.relay._http.put(
                    f"/v1/threads/{command['thread_id']}/attachments/{item['artifact_id']}",
                    params={k: v for k, v in item.items() if k != "artifact_id"},
                    headers={"Content-Length": str(item["size"])},
                    content=checked_stream(src, item),
                    timeout=httpx.Timeout(120),
                )
                reply.raise_for_status()
        await self.relay._dispatch(command)

    async def tick(self, lane: str) -> None:
        if lane == "outgoing":
            data = await self.relay._get("/v1/artifacts")
            for item in data["artifacts"]:
                self.jobs.add(item["artifact_id"], lane, item)
        row = self.jobs.due(lane)
        if row is None:
            return
        data = json.loads(row["payload"])
        started = time.monotonic()
        try:
            if time.time() - row["created"] > RETRY_WINDOW:
                raise ValueError("文件传输重试已超过 24 小时，请重新发送")
            async with asyncio.timeout(max(1, row["created"] + RETRY_WINDOW - time.time())):
                if lane == "outgoing":
                    await self.export(data)
                else:
                    await self.deliver(data)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if isinstance(exc, httpx.HTTPStatusError):
                status = exc.response.status_code
            terminal = isinstance(exc, ValueError) or status in (400, 401, 403, 409, 413, 422)
            if terminal:
                if lane == "outgoing":
                    await self.relay._post(
                        f"/v1/artifacts/{row['id']}/complete",
                        {
                            "state": "failed",
                            "error": str(exc)[:512],
                        },
                    )
                else:
                    await self.relay._publish(
                        "relay.cmd_failed",
                        {
                            "op": "message",
                            "thread_id": data["thread_id"],
                            "detail": f"消息未完成交接：{exc}",
                        },
                    )
                self.jobs.finish(row["id"], "failed")
            else:
                self.jobs.retry(row)
                log.warning("transfer %s retry: %s", row["id"], type(exc).__name__)
        else:
            self.jobs.finish(row["id"])
            log.info(
                "transfer %s %s complete bytes=%s elapsed=%.1f attempts=%d",
                row["id"],
                lane,
                data.get("size", 0),
                time.monotonic() - started,
                row["attempts"] + 1,
            )

    async def worker(self, lane: str) -> None:
        while True:
            try:
                await self.tick(lane)
            except Exception:
                log.exception("transfer worker %s", lane)
            await asyncio.sleep(1)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.worker(lane)) for lane in ("incoming", "outgoing")]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
