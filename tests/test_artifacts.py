import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from antares_agent.artifacts import CHUNK, RETENTION, Artifacts, snapshot
from antares_agent.transfers import Jobs, Transfers, checked_stream


async def test_snapshot_recovery(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    store = Artifacts(tmp_path / "files")
    row = await store.collect("thread", source)
    item = json.loads(row["metadata"])
    assert item["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert store.path(row["id"]).read_bytes() == b"payload"
    assert (await store.collect("thread", source))["id"] == row["id"]
    store.close()
    store = Artifacts(tmp_path / "files")
    assert store.list("outgoing")[0]["id"] == row["id"]
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        await store.collect("thread", link)
    with pytest.raises(ValueError):
        snapshot(source, tmp_path / "small", 2)
    assert source.exists()
    assert not (tmp_path / "small").exists()
    store.close()


async def test_atomic_uploads(tmp_path: Path) -> None:
    store = Artifacts(tmp_path)
    data = b"x" * (CHUNK + 10)
    item = {
        "artifact_id": "d" * 32,
        "name": "a",
        "mime": "application/octet-stream",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }

    async def chunks(broken=False):
        yield data[:CHUNK]
        assert not store.path(item["artifact_id"]).exists()
        if broken:
            raise ConnectionError("interrupted")
        yield data[CHUNK:]

    with pytest.raises(ConnectionError):
        await store.receive("t", item, chunks(True))
    assert not store.path(item["artifact_id"]).exists()
    assert not list(tmp_path.glob("*.part"))
    await store.receive("t", item, chunks())
    assert store.resolve("t", item).read_bytes() == data
    with pytest.raises(ValueError):
        store.resolve("other", item)
    with pytest.raises(ValueError):
        store.path("../escape")
    store.db.execute("UPDATE artifacts SET created=?", (time.time() - RETENTION - 1,))
    store.db.commit()
    store.cleanup()
    assert not store.path(item["artifact_id"]).exists()
    store.close()


async def test_stream_order(tmp_path: Path) -> None:
    data = b"x" * (CHUNK * 3 + 1)
    item = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    response = httpx.Response(200, content=data)
    sizes = [len(chunk) async for chunk in checked_stream(response, item)]
    assert max(sizes) <= CHUNK and sum(sizes) == len(data)
    with pytest.raises(ValueError):
        async for _ in checked_stream(response, {**item, "sha256": "0" * 64}):
            pass
    jobs = Jobs(tmp_path / "jobs.sqlite")
    jobs.add("one", "incoming", {"thread_id": "a"})
    jobs.add("two", "incoming", {"thread_id": "a"})
    jobs.add("three", "incoming", {"thread_id": "b"})
    jobs.retry(jobs.due("incoming"))
    assert jobs.due("incoming")["id"] == "three"
    jobs.close()
    jobs = Jobs(tmp_path / "jobs.sqlite")
    jobs.finish("three")
    assert jobs.due("incoming") is None
    jobs.finish("one")
    assert jobs.due("incoming")["id"] == "two"
    jobs.close()


async def test_export_import(tmp_path: Path) -> None:
    data = b"example"
    item = {
        "artifact_id": "e" * 32,
        "name": "sample",
        "mime": "text/plain",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    calls = []

    async def local(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, content=data)
        assert await request.aread() == data
        return httpx.Response(200, json=item)

    async def remote(request):
        if request.method == "PUT":
            assert await request.aread() == data
            assert request.headers["content-length"] == str(len(data))
            return httpx.Response(201)
        return httpx.Response(200, content=data)

    async def post(path, body):
        calls.append(("complete", path))

    async def dispatch(cmd):
        calls.append(("message", cmd["thread_id"]))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(local), base_url="http://agent") as api,
        httpx.AsyncClient(transport=httpx.MockTransport(remote), base_url="https://files") as files,
    ):
        relay = SimpleNamespace(_http=api, _post=post, _dispatch=dispatch)
        transfers = Transfers(relay, files, tmp_path / "jobs.sqlite")
        await transfers.export(item)
        await transfers.deliver({"thread_id": "thread", "attachments": [item]})
        assert calls[-1] == ("message", "thread")
        assert any(c[0] == "complete" for c in calls)
        transfers.jobs.close()


async def test_event_recovery(tmp_path: Path) -> None:
    from antares_agent.config import Settings
    from antares_agent.manager import ThreadManager
    from antares_agent.store import Store

    settings = Settings(
        workspace=tmp_path, db_path=tmp_path / "db.sqlite", profiles_dir=tmp_path / "profiles"
    )
    store = Store(settings.db_path)
    store.create_thread("thread", "quick")
    manager = ThreadManager(settings, store)
    source = tmp_path / "file"
    source.write_bytes(b"hello")
    await manager.artifacts.collect("thread", source)
    manager.recover()
    assert len(store.events_since("thread", 0)) == 1
    manager.artifacts.db.execute("UPDATE artifacts SET published=0")
    manager.artifacts.db.commit()
    manager.recover()
    assert len(store.events_since("thread", 0)) == 1
    request_id = "f" * 32
    store.begin_request(request_id, "thread", "fingerprint")
    store.finish_request(request_id, {"status": "queued", "position": 1})
    manager.recover()
    assert store.request(request_id)["state"] == "uncertain"
    assert store.events_since("thread", 1)[0]["payload"]["code"] == "message_uncertain"
    await manager.shutdown()
    store.close()


async def test_failed_queue_requeues() -> None:
    import sqlite3
    from contextlib import asynccontextmanager
    from unittest.mock import Mock

    from antares_agent.relay import Relay

    class Incoming:
        body = json.dumps({"op": "message", "request_id": "a" * 32, "thread_id": "t"}).encode()

        @asynccontextmanager
        async def process(self, requeue):
            assert requeue
            yield

    async with httpx.AsyncClient() as http:
        relay = Relay(http)
        relay.transfers = SimpleNamespace(
            enqueue=Mock(side_effect=sqlite3.OperationalError("full"))
        )
        with pytest.raises(sqlite3.OperationalError):
            await relay._on_command(Incoming())


async def test_invalid_command() -> None:
    from contextlib import asynccontextmanager

    from antares_agent.relay import Relay

    class Incoming:
        body = b"[]"

        @asynccontextmanager
        async def process(self, requeue):
            yield

    async with httpx.AsyncClient() as http:
        await Relay(http)._on_command(Incoming())
