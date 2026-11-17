"""Bounded background task worker using the Hub's Pi provider in headless mode."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from config.logger import setup_logging
from core.agent import chat_history, llm_router, task_service

_registered = False
_instance = None


def is_registered() -> bool:
    return bool(_registered and _instance is not None and _instance.is_registered())


def wake() -> None:
    if _instance is not None:
        _instance.wake()


class TaskWorker:
    def __init__(self, config, service=None, runner=None, poll_seconds=0.5):
        self.config = config
        self.service = service or task_service.store()
        self.runner = runner or self._run_pi
        self.owner_resolver = lambda device: chat_history.owner_for_device(device)
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.logger = setup_logging(config)
        self.stop_event = threading.Event()
        self.shutting_down = threading.Event()
        self.wake_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-task")
        self._executor_closed = False
        self.poller = None
        self._futures = set()
        self._lock = threading.Lock()
        self.enabled = self._pi_configured()

    def _pi_configured(self):
        selected = (self.config.get("selected_module") or {}).get("LLM")
        if not selected or not llm_router.model_ready(self.config, selected):
            return False
        entry = (self.config.get("LLM") or {}).get(selected) or {}
        return entry.get("type", selected) in llm_router.PI_TYPES

    def is_registered(self) -> bool:
        """The dispatcher can accept work only while configured and running."""
        return bool(self.enabled and self.poller and self.poller.is_alive()
                    and not self.stop_event.is_set() and not self.shutting_down.is_set()
                    and not self._executor_closed)

    def start(self):
        global _registered, _instance
        if self.poller and self.poller.is_alive():
            return self.enabled
        self.shutting_down.clear()
        self.stop_event.clear()
        if self._executor_closed:
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-task")
            self._executor_closed = False
        self.service.reconcile_expired()
        _instance = self
        self.poller = threading.Thread(target=self._poll, name="hub-task-dispatcher", daemon=True)
        self.poller.start()
        _registered = self.enabled
        self.logger.info("后台任务 worker 已启动（并发上限 2，每设备 1）" if self.enabled
                         else "后台任务 worker 等待 Pi 模型配置")
        return self.enabled

    def update_config(self, config):
        """Apply a hot-reloaded Hub config for subsequent claims/runs."""
        global _registered
        with self._lock:
            self.config = config
            self.enabled = self._pi_configured()
        if self.poller and self.poller.is_alive():
            _registered = self.enabled
            self.wake()
        elif self.enabled:
            self.start()

    def stop(self):
        global _registered, _instance
        _registered = False
        self.shutting_down.set()
        if _instance is self:
            _instance = None
        self.stop_event.set()
        self.wake_event.set()
        if self.poller:
            self.poller.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=False)
        self._executor_closed = True
        self.poller = None

    def wake(self):
        self.wake_event.set()

    def _poll(self):
        while not self.stop_event.is_set():
            self.service.reconcile_expired()
            if not self.enabled:
                self.wake_event.wait(self.poll_seconds)
                self.wake_event.clear()
                continue
            with self._lock:
                active = sum(1 for future in self._futures if not future.done())
            # Keep the process-wide worker bound at two running tasks. The
            # executor queue must not accumulate claimed work that cannot run.
            if active >= 2:
                self.wake_event.wait(self.poll_seconds)
                self.wake_event.clear()
                continue
            claimed = self.service.claim_next(worker_id="hub-task-dispatcher", lease_seconds=60)
            if claimed is not None:
                future = self.executor.submit(self._execute, claimed)
                with self._lock:
                    self._futures.add(future)
                future.add_done_callback(self._done)
                continue
            self.wake_event.wait(self.poll_seconds)
            self.wake_event.clear()

    def _done(self, future):
        with self._lock:
            self._futures.discard(future)
        try:
            future.result()
        except Exception as exc:
            self.logger.warning(f"后台任务 worker 异常: {type(exc).__name__}")
        self.wake()

    def _execute(self, claimed):
        task, run = claimed["task"], claimed["run"]
        owner, run_id, task_id = task["owner_id"], run["id"], task["id"]
        cancel = lambda: self.shutting_down.is_set() or self.service.cancel_requested(owner, run_id)
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(15):
                if not self.service.heartbeat(owner, run_id, lease_seconds=60):
                    return
        heartbeat_thread = threading.Thread(target=heartbeat, name=f"task-lease-{run_id[:8]}", daemon=True)
        heartbeat_thread.start()
        try:
            result = self.runner(task, run, cancel)
            if self.shutting_down.is_set():
                self.service.finish_run(owner, run_id, task_service.NEEDS_RECONCILIATION,
                                        error="worker_shutdown")
                return
            if cancel():
                self.service.finish_run(owner, run_id, task_service.CANCELLED, error="cancelled")
                return
            current_owner = self.owner_resolver(task["device_id"])
            if current_owner != owner:
                self.service.cancel_device_tasks(owner, task["device_id"])
                self.service.finish_run(owner, run_id, task_service.CANCELLED, error="device_reassigned")
                return
            result_text = str(result or "").strip()
            if not result_text:
                raise RuntimeError("Pi worker 没有生成结果")
            finished = self.service.finish_run(owner, run_id, task_service.SUCCEEDED,
                                               result={"text": result_text})
            if finished and self.owner_resolver(task["device_id"]) == owner:
                self.service.enqueue_delivery(owner, task_id, task["device_id"],
                                              finished["result_version"], session_id="", current_owner=owner)
                try:
                    import asyncio
                    from core.admin import connections
                    from core.agent.task_delivery import deliver_pending
                    for conn in connections.all_connections():
                        if (getattr(conn, "device_id", None) == task["device_id"]
                                and self.owner_resolver(task["device_id"]) == owner
                                and getattr(conn, "loop", None) is not None
                                and not getattr(conn, "client_is_speaking", True)):
                            asyncio.run_coroutine_threadsafe(deliver_pending(conn), conn.loop)
                except Exception as exc:
                    self.logger.warning(f"后台任务在线投递调度失败: {type(exc).__name__}")
        except task_service.TaskError as exc:
            self.logger.info(f"任务 {task_id} 状态已改变: {type(exc).__name__}")
        except Exception as exc:
            if self.shutting_down.is_set():
                self.service.finish_run(owner, run_id, task_service.NEEDS_RECONCILIATION,
                                        error="worker_shutdown")
            elif cancel():
                self.service.finish_run(owner, run_id, task_service.CANCELLED, error="cancelled")
            else:
                self.service.finish_run(owner, run_id, task_service.FAILED,
                                        error=f"worker_{type(exc).__name__}")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)

    def _run_pi(self, task, run, cancelled):
        with self._lock:
            config = self.config
        selected = task.get("model") or (config.get("selected_module") or {}).get("LLM")
        if not selected or not llm_router.model_ready(config, selected):
            raise RuntimeError("后台任务的 Pi 模型未配置")
        entry = dict((config.get("LLM") or {}).get(selected) or {})
        if entry.get("type", selected) not in llm_router.PI_TYPES:
            raise RuntimeError("后台任务只能使用 Pi 模型")
        if run.get("budget_tokens"):
            entry["max_tokens"] = min(int(entry.get("max_tokens") or 800), int(run["budget_tokens"]))
        from core.providers.llm.pi.pi import LLMProvider
        provider = LLMProvider(entry)
        request_data = json.loads(task["request"] or "{}")
        request = request_data.get("text", "")
        if request_data.get("answers"):
            request += "\n\n用户补充信息：\n" + "\n".join(str(a) for a in request_data["answers"])
        dialogue = [
            {"role": "system", "content": "你是后台研究助手。当前运行没有搜索、浏览器或其他外部工具，只能依据已有知识完成任务。不要声称查过实时资料或来源；不确定或需要当前信息时明确说明。不要调用工具，不要派发子任务，不要输出推理过程。"},
            {"role": "user", "content": request},
        ]
        parts = []
        deadline = run.get("deadline")
        if run.get("budget_seconds"):
            budget_deadline = __import__("time").time() + float(run["budget_seconds"])
            deadline = min(deadline, budget_deadline) if deadline else budget_deadline
        def stale():
            return cancelled() or (deadline is not None and __import__("time").time() >= float(deadline))
        for event in provider.events("task:" + task["id"], dialogue, [], None, stale):
            if cancelled():
                return ""
            if event.get("type") == "text":
                parts.append(event.get("text", ""))
            elif event.get("type") == "error":
                raise RuntimeError(event.get("message", "Pi worker 请求失败"))
        if deadline is not None and time.time() >= float(deadline) and not cancelled():
            raise TimeoutError("worker_budget_exceeded")
        return "".join(parts)


__all__ = ["TaskWorker", "is_registered", "wake"]
