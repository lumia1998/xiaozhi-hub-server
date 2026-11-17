"""agent_sessions.json 的读写：同一个文件的几个命名空间各有各的实例，要共用一把锁。"""

import threading
import time

from core.agent.session_store import SessionStore


def test_instances_of_same_file_share_lock(tmp_path):
    a = SessionStore(tmp_path / "agent_sessions.json", "a")
    b = SessionStore(tmp_path / "." / "agent_sessions.json", "last_model")
    other = SessionStore(tmp_path / "other.json", "a")
    assert a._lock is b._lock and a._lock is not other._lock


def test_concurrent_writers_in_different_namespaces_lose_nothing(tmp_path, monkeypatch):
    """同一个文件的几个命名空间同时写：每边的记录都要留下。"""
    write = SessionStore._write_all

    def slow_write(self, data):
        time.sleep(0.002)  # 拉长"读 → 写回"之间的空档，锁不共享时必然互相覆盖
        write(self, data)

    monkeypatch.setattr(SessionStore, "_write_all", slow_write)
    path = tmp_path / "agent_sessions.json"
    stores = [SessionStore(path, "a"), SessionStore(path, "last_model"), SessionStore(path, "b")]

    def work(store, n):
        for i in range(10):
            store.put(f"dev-{n}-{i}", {"model": store.namespace})

    threads = [threading.Thread(target=work, args=(s, n)) for n, s in enumerate(stores)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = stores[0]._read_all()
    assert {ns: len(data.get(ns, {})) for ns in ("a", "last_model", "b")} == {"a": 10, "last_model": 10, "b": 10}
