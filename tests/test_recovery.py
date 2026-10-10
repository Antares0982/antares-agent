from unittest.mock import AsyncMock

from antares_agent.config import Settings
from antares_agent.events import Event, EventType
from antares_agent.manager import ThreadManager
from antares_agent.store import Store


def test_recovery(tmp_path):
    settings = Settings(
        workspace=tmp_path, db_path=tmp_path / "db", profiles_dir=tmp_path / "profiles"
    )
    store = Store(settings.db_path)
    manager = ThreadManager(settings, store)
    store.create_thread("thr_a", "auto")
    store.touch("thr_a", session_id="codex-1", permission_mode="plan")
    store.append_event(Event(EventType.THREAD_STATUS, "thr_a", {"status": "busy"}).with_id(1))
    assert manager.recover() == ["thr_a"]
    assert manager.recover() == []
    assert store.last_status("thr_a") == "idle"
    assert store.get_thread("thr_a").permission_mode == "plan"
    assert store.events_since("thr_a", 1)[0]["payload"]["code"] == "interrupted"
    manager.artifacts.close()
    store.close()


async def test_model_recovery(tmp_path):
    settings = Settings(
        workspace=tmp_path, db_path=tmp_path / "db", profiles_dir=tmp_path / "profiles"
    )
    store = Store(settings.db_path)
    store.create_thread("legacy", "auto")
    store._db.execute("ALTER TABLE threads DROP COLUMN model")
    store.close()
    store = Store(settings.db_path)
    assert store.get_thread("legacy").model is None
    store.touch("legacy", session_id="codex-existing", model="model-a")
    manager = ThreadManager(settings, store)
    manager.runtime.models = AsyncMock(
        return_value=[
            {
                "id": "model-a",
                "is_default": True,
                "default_effort": "medium",
                "supported_efforts": ["low", "medium", "high"],
            },
            {
                "id": "model-b",
                "is_default": False,
                "default_effort": "high",
                "supported_efforts": ["medium", "high", "ultra"],
            },
        ]
    )
    await manager.set_model("legacy", "model-b")
    assert store.get_thread("legacy").session_id == "codex-existing"
    await manager.shutdown()
    store.close()
    store = Store(settings.db_path)
    manager = ThreadManager(settings, store)
    runner = await manager.runner("legacy")
    assert runner.state.model == "model-b"
    assert runner.state.session_id == "codex-existing"
    await manager.shutdown()
    store.close()


def test_profile_migration(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    for name, mode in (("quick", "plan"), ("deep", "auto"), ("custom", "plan")):
        store.create_thread(name, name, "7")
        store.touch(name, model="chosen", session_id=name, permission_mode=mode)
    store._db.execute("ALTER TABLE threads DROP COLUMN effort")
    store._db.execute("ALTER TABLE threads DROP COLUMN prompt")
    store.close()
    store = Store(path)
    assert store.get_thread("quick").profile == "auto"
    assert store.get_thread("deep").profile == "plan"
    assert store.get_thread("custom").profile == "plan"
    assert store.get_thread("quick").permission_mode == "plan"
    assert store.get_thread("deep").permission_mode == "auto"
    assert store.get_thread("custom").model == "chosen"
    assert store.get_thread("custom").session_id == "custom"
    assert store.get_thread("custom").effort is None
    assert store.get_thread("custom").prompt == ""
    store.close()
