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
    store.create_thread("thr_a", "quick")
    store.touch("thr_a", session_id="codex-1", permission_mode="plan")
    store.append_event(Event(EventType.THREAD_STATUS, "thr_a", {"status": "busy"}).with_id(1))
    assert manager.recover() == ["thr_a"]
    assert manager.recover() == []
    assert store.last_status("thr_a") == "idle"
    assert store.get_thread("thr_a").permission_mode == "plan"
    assert store.events_since("thr_a", 1)[0]["payload"]["code"] == "interrupted"
    manager.artifacts.close()
    store.close()
