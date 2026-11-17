"""Isolated task/memory acceptance tests.

Run from the repository root with ``python -m pytest -c test/pytest.ini
test/test_task_system.py -q``. No Hub, model, network,
or production data is used.
"""
import threading
import time
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

class _Logger:
    def warning(self, *_args, **_kwargs):
        pass

    info = warning


from core.agent import assistant_tools, chat_history, task_service, task_worker
from core.agent.ha_commands import ExecutionResult
from core.device_pairing import PairingStore
from core.credentials import CredentialManager, InMemorySecretBackend
from core.connectors.mock import MockConnector
from plugins_func.pre_llm import home_assistant


class _Conn:
    def __init__(self, text=""):
        self.device_id = "dev-1"
        self.session_id = "session-1"
        self.dialogue = SimpleNamespace(
            chat_key=(self.device_id, "model"),
            chat_epoch=0,
            dialogue=[SimpleNamespace(role="user", content=text)],
        )
        self.logger = _Logger()


def _config():
    return {
        "selected_module": {"LLM": "Pi"},
        "LLM": {"Pi": {"type": "pi", "model_name": "fixture", "base_url": "http://fixture", "api_key": "fixture-key"}},
    }


class TaskSystemTests(unittest.TestCase):
    def setUp(self):
        logger_patch = patch.object(task_worker, "setup_logging", return_value=_Logger())
        logger_patch.start()
        self.addCleanup(logger_patch.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="hub-task-")
        self.addCleanup(self.directory.cleanup)
        self.db = task_service.TaskService(self.directory.name + "/tasks.db")

    def tearDown(self):
        self.db.close()

    def test_memory_tools_require_explicit_intent_and_reject_credentials(self):
        conn = _Conn("记住我不吃香菜")
        context = {"owner_id": "owner-1", "device_id": "dev-1", "stale": False,
                   "preferences": [], "pinned": "", "summary": "", "events": []}
        row = {"id": 1, "topic": "饮食", "content": "不吃香菜", "version": 1}
        with patch.object(chat_history, "memory_context_for", return_value=context), \
             patch.object(chat_history, "upsert_preference_for", return_value=(True, row)):
            result = assistant_tools.execute(conn, "remember_preference", {"topic": "饮食", "content": "不吃香菜"})
        self.assertIn("已记住", result["text"])
        conn.dialogue.dialogue[-1].content = "我觉得香菜不错"
        with patch.object(chat_history, "memory_context_for", return_value=context), \
             patch.object(chat_history, "upsert_preference_for") as save:
            result = assistant_tools.execute(conn, "remember_preference", {"topic": "饮食", "content": "密码是 hunter2"})
        self.assertTrue(result["error"])
        save.assert_not_called()

    def test_task_tools_start_status_cancel_answer(self):
        conn = _Conn("帮我整理资料")
        context = {"owner_id": "owner-1", "device_id": "dev-1", "stale": False,
                   "preferences": [], "pinned": "", "summary": "", "events": []}
        with patch.object(chat_history, "memory_context_for", return_value=context), \
             patch.object(task_worker, "is_registered", return_value=True), \
             patch.object(task_worker, "wake"):
            with patch.object(task_service, "task_start", return_value={"id": "t1"}) as start:
                result = assistant_tools.execute(conn, "task_start", {"request": "整理资料"})
                start.assert_called_once()
            self.assertIn("t1", result["text"])
            with patch.object(task_service, "task_status", return_value={"id": "t1", "status": "queued"}):
                self.assertIn("queued", assistant_tools.execute(conn, "task_status", {"task_id": "t1"})["text"])
            with patch.object(task_service, "task_cancel", return_value={"id": "t1", "status": "cancelled"}):
                self.assertIn("cancelled", assistant_tools.execute(conn, "task_cancel", {"task_id": "t1"})["text"])
            with patch.object(task_service, "task_answer", return_value={"id": "t1", "status": "queued"}):
                self.assertIn("重新排队", assistant_tools.execute(conn, "task_answer", {"task_id": "t1", "answer": "补充"})["text"])

    def test_worker_availability_tracks_configuration_and_lifecycle(self):
        worker = task_worker.TaskWorker({}, service=self.db, runner=lambda *_: "fixture")
        try:
            self.assertFalse(worker.is_registered())
            worker.start()
            self.assertTrue(worker.poller.is_alive())
            self.assertFalse(worker.is_registered())
            self.assertFalse(task_worker.is_registered())
            worker.update_config(_config())
            self.assertTrue(worker.is_registered())
            self.assertTrue(task_worker.is_registered())
            worker.update_config({})
            self.assertFalse(worker.is_registered())
            self.assertFalse(task_worker.is_registered())
            worker.update_config(_config())
            self.assertTrue(worker.is_registered())
            worker.shutting_down.set()
            self.assertFalse(worker.is_registered())
            self.assertFalse(task_worker.is_registered())
        finally:
            worker.stop()
        self.assertFalse(worker.is_registered())
        self.assertFalse(task_worker.is_registered())

    def test_dead_dispatcher_is_not_available(self):
        worker = task_worker.TaskWorker(_config(), service=self.db, runner=lambda *_: "fixture")
        try:
            with patch.object(worker, "_poll", return_value=None):
                worker.start()
                worker.poller.join(timeout=1)
            self.assertFalse(worker.poller.is_alive())
            self.assertFalse(worker.is_registered())
            self.assertFalse(task_worker.is_registered())
        finally:
            worker.stop()

    def test_worker_success_cancel_reconcile_and_shutdown(self):
        service = self.db
        task = service.task_start("owner-1", "dev-1", "成功", idempotency_key="ok")
        worker = task_worker.TaskWorker(_config(), service=service, runner=lambda *_: "结果")
        with patch.object(chat_history, "owner_for_device", return_value="owner-1"):
            worker.start()
            deadline = time.time() + 4
            while time.time() < deadline and service.task_status("owner-1", task["id"])["status"] != task_service.SUCCEEDED:
                time.sleep(.02)
            worker.stop()
        self.assertEqual(service.task_status("owner-1", task["id"])["status"], task_service.SUCCEEDED)

        queued = service.task_start("owner-1", "dev-1", "租约")
        claimed = service.claim_next(worker_id="fixture")
        service._db.execute("UPDATE task_runs SET lease_until=? WHERE id=?", (time.time() - 1, claimed["run"]["id"]))
        self.assertEqual(service.reconcile_expired(), 1)
        self.assertEqual(service.task_status("owner-1", queued["id"])["status"], task_service.NEEDS_RECONCILIATION)

        blocked = threading.Event()
        def runner(*_):
            blocked.wait(3)
            return "late"
        shutdown_task = service.task_start("owner-1", "dev-1", "关闭")
        worker = task_worker.TaskWorker(_config(), service=service, runner=runner)
        with patch.object(chat_history, "owner_for_device", return_value="owner-1"):
            worker.start()
            time.sleep(.1)
            worker.stop()
        blocked.set()
        time.sleep(.1)
        self.assertIn(service.task_status("owner-1", shutdown_task["id"])["status"],
                      (task_service.NEEDS_RECONCILIATION, task_service.CANCEL_REQUESTED))

    def test_owner_reassignment_blocks_delivery(self):
        service = self.db
        task = service.task_start("owner-1", "dev-1", "结果")
        claimed = service.claim_next(worker_id="fixture")
        with patch.object(chat_history, "owner_for_device", return_value="owner-2"):
            self.assertIsNone(service.enqueue_delivery("owner-1", task["id"], "dev-1", 1))

    def test_memory_owner_epochs_archive_and_filter_old_history(self):
        with tempfile.TemporaryDirectory(prefix="hub-memory-") as directory:
            memory = chat_history.ChatDB(__import__("pathlib").Path(directory) / "chat.db")
            previous = chat_history.db()
            chat_history.set_db(memory)
            try:
                old_id = memory.insert("dev-1", "model-a", "user", "old owner fact")
                memory.set_memory("dev-1", "old summary", "manual", field="both")
                ok, mapping = memory.set_device_owner("dev-1", "owner-a", expected_version=0)
                self.assertTrue(ok)
                self.assertEqual(mapping["owner"], "owner-a")
                self.assertEqual(memory.recent("dev-1", "model-a", 10), [])
                self.assertEqual(memory.memory("dev-1")["summary"], "")
                archive = memory.memory_archives("dev-1")
                self.assertEqual(archive[-1]["owner"], "dev-1")
                current_id = memory.insert("dev-1", "model-a", "user", "new owner fact")
                memory.set_memory("dev-1", "", "cleared", field="summary", forget_before=True)
                self.assertEqual(memory.progress("dev-1"), {"model-a": current_id})
                self.assertTrue(memory.exists(old_id))
            finally:
                chat_history.set_db(previous)
                memory.close()

    def test_memory_context_rechecks_owner_epoch_after_reads(self):
        with tempfile.TemporaryDirectory(prefix="hub-memory-") as directory:
            memory = chat_history.ChatDB(__import__("pathlib").Path(directory) / "chat.db")
            previous = chat_history.db()
            chat_history.set_db(memory)
            try:
                memory.set_device_owner("dev-1", "owner-a", expected_version=0)
                memory.upsert_preference("owner-a", "饮食", "不吃香菜")
                conn = _Conn("这句话不能跨归属")
                conn.dialogue.chat_epoch = 1
                with patch.object(memory, "owner_epoch", side_effect=[1, 2]):
                    context = chat_history.memory_context_for(conn)
                self.assertTrue(context["stale"])
                self.assertEqual(context["preferences"], [])
                self.assertEqual(context["summary"], "")
                self.assertEqual(conn.dialogue.chat_epoch, 2)
            finally:
                chat_history.set_db(previous)
                memory.close()

    def test_delivery_claim_binds_session_and_recovers_expired_claim(self):
        service = self.db
        task = service.task_start("owner-1", "dev-1", "结果")
        claimed = service.claim_next(worker_id="fixture")
        service.finish_run("owner-1", claimed["run"]["id"], task_service.SUCCEEDED, result={"text": "x"})
        with patch.object(chat_history, "owner_for_device", return_value="owner-1"):
            service.enqueue_delivery("owner-1", task["id"], "dev-1", 1, session_id="")
            delivery = service.claim_delivery("owner-1", "dev-1", "session-1")
        self.assertEqual(delivery["session_id"], "session-1")
        service._db.execute("UPDATE task_deliveries SET claimed_until=? WHERE id=?", (time.time() - 1, delivery["id"]))
        self.assertEqual(service.reconcile_expired(), 0)
        recovered = service._db.execute("SELECT status FROM task_deliveries WHERE id=?", (delivery["id"],)).fetchone()[0]
        self.assertEqual(recovered, task_service.DELIVERY_DEFERRED)

    def test_idempotency_contract_rejects_different_device(self):
        service = self.db
        service.task_start("owner-1", "dev-1", "same", model="m", idempotency_key="k")
        with self.assertRaises(task_service.TaskError):
            service.task_start("owner-1", "dev-2", "same", model="m", idempotency_key="k")

    def test_home_assistant_result_has_explicit_confirmation(self):
        conn = _Conn("打开台灯")
        conn.config = {"plugins": {"home_assistant": {"direct_commands": {"enabled": True}, "devices": ["客厅,台灯,light.lamp"]}}}
        command = SimpleNamespace(action="turn_on", device=SimpleNamespace(entity_id="light.lamp"))
        with patch("plugins_func.pre_llm.home_assistant.match", return_value=command), \
             patch("core.agent.ha_commands.execute", return_value=ExecutionResult("台灯已打开。", True)):
            result = __import__("asyncio").run(home_assistant.intercept(conn, "打开台灯"))
        self.assertTrue(result["confirmed"])

    def test_pairing_token_is_bound_and_never_listed(self):
        with tempfile.TemporaryDirectory(prefix="hub-pairing-") as directory:
            store = PairingStore(__import__("pathlib").Path(directory) / "device_auth.db", "server-secret")
            token, row = store.issue("dev-1", "owner-1")
            self.assertTrue(store.verify("dev-1", token))
            self.assertFalse(store.verify("dev-2", token))
            listed = store.list()[0]
            self.assertNotIn("token", listed)
            self.assertNotIn(token, repr(listed))
            self.assertTrue(store.revoke("dev-1"))
            self.assertFalse(store.verify("dev-1", token))
            store.close()

    def test_mock_connector_never_returns_secret(self):
        manager = CredentialManager(InMemorySecretBackend())
        connector = MockConnector(manager)
        public = connector.connect("owner-1", secret="top-secret", scopes=("status",))
        self.assertNotIn("top-secret", repr(public))
        result = connector.execute("owner-1", public["ref_id"], "status", required_scope="status")
        self.assertNotIn("top-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
