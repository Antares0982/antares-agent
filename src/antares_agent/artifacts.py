from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from collections.abc import AsyncIterable
from pathlib import Path

CHUNK = 256 * 1024
MAX_BYTES = 2_000_000_000
RETENTION = 7 * 86400
RETRY_WINDOW = 86400


def valid_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("文件标识无效")
    return value


def descriptor(data: dict) -> dict:
    valid_id(data["artifact_id"])
    if type(data["size"]) is not int or not 0 <= data["size"] <= MAX_BYTES:
        raise ValueError("文件大小超限")
    if not re.fullmatch(r"[0-9a-f]{64}", data["sha256"]):
        raise ValueError("文件摘要无效")
    if not isinstance(data["name"], str) or len(data["name"]) > 256:
        raise ValueError("文件名无效")
    if not isinstance(data["mime"], str) or len(data["mime"]) > 128:
        raise ValueError("文件类型无效")
    return {key: data[key] for key in ("artifact_id", "name", "mime", "size", "sha256")}


def signature(path: Path) -> str:
    info = path.lstat()
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def snapshot(source: Path, target: Path, limit: int = MAX_BYTES) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("只支持普通文件")
        if info.st_size > limit:
            raise ValueError("文件大小超限")
        if shutil.disk_usage(target.parent).free < info.st_size + CHUNK:
            raise OSError("暂存磁盘空间不足")
        with os.fdopen(fd, "rb", closefd=False) as src, target.open("xb") as dst:
            while chunk := src.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ValueError("文件大小超限")
                dst.write(chunk)
                digest.update(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("文件仍在写入，请稍后重试")
        return size, digest.hexdigest()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


class Artifacts:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "index.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, thread TEXT NOT NULL, "
            "direction TEXT NOT NULL, metadata TEXT NOT NULL, source TEXT, signature TEXT, "
            "created REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
            "published INTEGER NOT NULL DEFAULT 0, UNIQUE(thread, source, signature))"
        )
        self.db.commit()
        self.upload_lock = asyncio.Lock()
        self.active: set[str] = set()

    def close(self) -> None:
        self.db.close()

    def path(self, artifact_id: str) -> Path:
        return self.root / valid_id(artifact_id)

    def get(self, artifact_id: str) -> dict | None:
        valid_id(artifact_id)
        row = self.db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def list(self, direction: str, state: str = "pending") -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM artifacts WHERE direction=? AND state=? ORDER BY created",
                (direction, state),
            )
        ]

    def mark(self, artifact_id: str, state: str) -> None:
        self.db.execute("UPDATE artifacts SET state=? WHERE id=?", (state, artifact_id))
        self.db.commit()

    def published(self, artifact_id: str) -> None:
        self.db.execute("UPDATE artifacts SET published=1 WHERE id=?", (artifact_id,))
        self.db.commit()

    async def collect(self, thread: str, source: Path, limit: int = MAX_BYTES) -> dict:
        stamp = signature(source)
        row = self.db.execute(
            "SELECT * FROM artifacts WHERE thread=? AND source=? AND signature=?",
            (thread, str(source), stamp),
        ).fetchone()
        if row and row["state"] not in ("expired", "failed"):
            return dict(row)
        if row:
            self.db.execute(
                "UPDATE artifacts SET source=NULL,signature=NULL WHERE id=?", (row["id"],)
            )
            self.db.commit()
        artifact_id = secrets.token_hex(16)
        target = self.path(artifact_id)
        temporary = target.with_suffix(".part")
        size, sha256 = await asyncio.to_thread(snapshot, source, temporary, limit)
        data = descriptor(
            {
                "artifact_id": artifact_id,
                "name": source.name,
                "mime": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                "size": size,
                "sha256": sha256,
            }
        )
        temporary.replace(target)
        self.db.execute(
            "INSERT INTO artifacts(id,thread,direction,metadata,source,signature,created) "
            "VALUES(?,?,'outgoing',?,?,?,?)",
            (artifact_id, thread, json.dumps(data), str(source), stamp, time.time()),
        )
        self.db.commit()
        result = self.get(artifact_id)
        assert result is not None
        return result

    async def receive(self, thread: str, data: dict, chunks: AsyncIterable[bytes]) -> None:
        data = descriptor(data)
        async with self.upload_lock:
            existing = self.get(data["artifact_id"])
            if existing and existing["state"] == "expired":
                raise ValueError("附件已过期，请重新发送")
            if existing and (
                existing["thread"] != thread
                or existing["direction"] != "incoming"
                or json.loads(existing["metadata"]) != data
            ):
                raise ValueError("文件标识冲突")
            if shutil.disk_usage(self.root).free < data["size"] + CHUNK:
                raise OSError("暂存磁盘空间不足")
            target = self.path(data["artifact_id"])
            temporary = target.with_suffix(".part")
            size = 0
            digest = hashlib.sha256()
            try:
                with temporary.open("wb") as output:
                    async for chunk in chunks:
                        size += len(chunk)
                        if size > data["size"]:
                            raise ValueError("文件长度不符")
                        await asyncio.to_thread(output.write, chunk)
                        digest.update(chunk)
                    await asyncio.to_thread(output.flush)
                    await asyncio.to_thread(os.fsync, output.fileno())
                if size != data["size"] or digest.hexdigest() != data["sha256"]:
                    raise ValueError("文件长度或摘要不符")
                temporary.replace(target)
                self.db.execute(
                    "INSERT OR IGNORE INTO artifacts(id,thread,direction,metadata,created) "
                    "VALUES(?,?,'incoming',?,?)",
                    (data["artifact_id"], thread, json.dumps(data), time.time()),
                )
                self.db.commit()
            finally:
                temporary.unlink(missing_ok=True)

    def resolve(self, thread: str, data: dict) -> Path:
        data = descriptor(data)
        row = self.get(data["artifact_id"])
        if not row or row["thread"] != thread or row["direction"] != "incoming":
            raise ValueError("附件不存在或不属于当前会话")
        if json.loads(row["metadata"]) != data or not self.path(row["id"]).is_file():
            raise ValueError("附件元数据不符或文件已过期")
        return self.path(row["id"])

    def cleanup(self) -> None:
        if self.upload_lock.locked():
            return
        cutoff = time.time() - RETENTION
        for row in self.db.execute("SELECT id FROM artifacts WHERE created<?", (cutoff,)):
            if row["id"] not in self.active:
                self.path(row["id"]).unlink(missing_ok=True)
                self.db.execute("UPDATE artifacts SET state='expired' WHERE id=?", (row["id"],))
        self.db.commit()
        known = {r[0] for r in self.db.execute("SELECT id FROM artifacts")}
        for path in self.root.iterdir():
            if (path.suffix == ".part" or re.fullmatch(r"[0-9a-f]{32}", path.name)) and (
                path.name not in known and path.stat().st_mtime < cutoff
            ):
                path.unlink(missing_ok=True)
