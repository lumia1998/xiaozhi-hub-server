"""Persistent task contract for background work.

This module owns task state only. It does not start workers, call models, execute
tools, or deliver audio. Callers must resolve the trusted owner/device identity
before invoking it; no value from model arguments is used for authorization.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path


QUEUED = "queued"
RUNNING = "running"
WAITING_INPUT = "waiting_input"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
NEEDS_RECONCILIATION = "needs_reconciliation"
STATES = frozenset({QUEUED, RUNNING, WAITING_INPUT, SUCCEEDED, FAILED,
                    CANCEL_REQUESTED, CANCELLED, NEEDS_RECONCILIATION})
DELIVERY_PENDING = "pending"
DELIVERY_CLAIMED = "claimed"
DELIVERY_PLAYED = "played"
DELIVERY_DEFERRED = "deferred"
DELIVERY_CANCELLED = "cancelled"
DELIVERY_STATES = frozenset({DELIVERY_PENDING, DELIVERY_CLAIMED, DELIVERY_PLAYED,
                             DELIVERY_DEFERRED, DELIVERY_CANCELLED})


class TaskError(ValueError):
    """Invalid task request or an invalid state transition."""


class TaskService:
    """SQLite-backed task, run, event, and delivery records."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self._private()

    def _private(self):
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass

    def _create_schema(self):
        with self._lock:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > 2:
                raise RuntimeError(f"Unsupported task database schema version: {version}")
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL,
                request TEXT NOT NULL,
                status TEXT NOT NULL,
                concurrency_key TEXT NOT NULL,
                max_parallel INTEGER NOT NULL DEFAULT 1,
                budget_tokens INTEGER,
                budget_seconds REAL,
                max_tool_calls INTEGER,
                deadline REAL,
                idempotency_key TEXT,
                result TEXT,
                result_version INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                CHECK (status IN ('queued','running','waiting_input','succeeded','failed',
                                  'cancel_requested','cancelled','needs_reconciliation'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS tasks_owner_idempotency
                ON tasks(owner_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';
            CREATE INDEX IF NOT EXISTS tasks_owner_status ON tasks(owner_id, status, updated);
            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                owner_id TEXT NOT NULL,
                role TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                attempt INTEGER NOT NULL DEFAULT 1,
                budget_tokens INTEGER,
                budget_seconds REAL,
                max_tool_calls INTEGER,
                deadline REAL,
                heartbeat REAL,
                lease_until REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                checkpoint TEXT,
                status TEXT NOT NULL,
                started REAL,
                finished REAL,
                created REAL NOT NULL,
                CHECK (status IN ('queued','running','waiting_input','succeeded','failed',
                                  'cancel_requested','cancelled','needs_reconciliation'))
            );
            CREATE INDEX IF NOT EXISTS task_runs_task ON task_runs(task_id, attempt);
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT,
                owner_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                created REAL NOT NULL,
                UNIQUE(task_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS task_events_owner ON task_events(owner_id, task_id, sequence);
            CREATE TABLE IF NOT EXISTS task_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                owner_id TEXT NOT NULL,
                result_version INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','played','deferred','cancelled')),
                claimed_until REAL,
                played_ms INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(task_id, result_version, device_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS task_deliveries_pending
                ON task_deliveries(owner_id, device_id, status, updated);
            """)
            delivery_columns = {row[1] for row in self._db.execute("PRAGMA table_info(task_deliveries)")}
            if "claimed_until" not in delivery_columns:
                self._db.execute("ALTER TABLE task_deliveries ADD COLUMN claimed_until REAL")
            self._db.execute("UPDATE task_deliveries SET status=?,claimed_until=NULL WHERE status=? AND claimed_until IS NULL",
                             (DELIVERY_DEFERRED, DELIVERY_CLAIMED))
            self._db.execute("UPDATE task_deliveries SET status=?,claimed_until=NULL WHERE status=? AND claimed_until<=?",
                             (DELIVERY_DEFERRED, DELIVERY_CLAIMED, time.time()))
            self._db.execute("PRAGMA user_version=2")

    @staticmethod
    def _text(value, field, maximum, required=True):
        value = "" if value is None else str(value).strip()
        if required and not value:
            raise TaskError(f"{field}不能为空")
        if len(value) > maximum:
            raise TaskError(f"{field}过长")
        return value

    @staticmethod
    def _task_id():
        return uuid.uuid4().hex

    def _event(self, task_id, owner_id, event_type, content, run_id=None):
        row = self._db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM task_events WHERE task_id=?", (task_id,)).fetchone()
        sequence = int(row[0])
        self._db.execute(
            "INSERT INTO task_events(task_id,run_id,owner_id,sequence,type,content,created) VALUES(?,?,?,?,?,?,?)",
            (task_id, run_id, owner_id, sequence, event_type, json.dumps(content, ensure_ascii=False), time.time()))

    def _row(self, row):
        if row is None:
            return None
        result = dict(row)
        for key in ("request", "result"):
            if result.get(key):
                try:
                    result[key] = json.loads(result[key])
                except (TypeError, ValueError):
                    pass
        return result

    def task_start(self, owner_id, device_id, request, *, model="", role="research",
                   deadline=None, idempotency_key=None, budget_tokens=None,
                   budget_seconds=None, max_tool_calls=None, concurrency_key=None,
                   max_parallel=1):
        """Create or return an idempotent queued task; never waits for execution."""
        owner_id = self._text(owner_id, "owner_id", 300)
        device_id = self._text(device_id, "device_id", 300)
        role = self._text(role, "role", 80)
        model = self._text(model, "model", 300, required=False)
        request = self._text(request, "request", 20000)
        key = self._text(idempotency_key, "idempotency_key", 300, required=False)
        concurrency_key = self._text(concurrency_key or device_id, "concurrency_key", 300)
        max_parallel = int(max_parallel)
        if max_parallel < 1 or max_parallel > 16:
            raise TaskError("并发上限必须在 1 到 16 之间")
        if deadline is not None and float(deadline) <= time.time():
            raise TaskError("任务期限必须在未来")
        if budget_tokens is not None and int(budget_tokens) <= 0:
            raise TaskError("预算必须为正数")
        if budget_seconds is not None and float(budget_seconds) <= 0:
            raise TaskError("时限预算必须为正数")
        if max_tool_calls is not None and int(max_tool_calls) <= 0:
            raise TaskError("工具调用预算必须为正数")
        now = time.time()
        task_id = self._task_id()
        def same_contract(row):
            return (row["device_id"] == device_id and row["model"] == model and row["role"] == role
                    and row["budget_tokens"] == (int(budget_tokens) if budget_tokens is not None else None)
                    and row["budget_seconds"] == (float(budget_seconds) if budget_seconds is not None else None)
                    and row["max_tool_calls"] == (int(max_tool_calls) if max_tool_calls is not None else None)
                    and row["concurrency_key"] == concurrency_key
                    and row["max_parallel"] == max_parallel)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                if key:
                    existing = self._db.execute(
                        "SELECT * FROM tasks WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
                    if existing is not None:
                        old_request = json.loads(existing["request"] or "{}")
                        if old_request.get("text") != request or not same_contract(existing):
                            raise TaskError("幂等键已用于不同的任务请求")
                        self._db.execute("COMMIT")
                        return self._row(existing)
                self._db.execute(
                    "INSERT INTO tasks(id,owner_id,device_id,model,role,request,status,concurrency_key,max_parallel,"
                    "budget_tokens,budget_seconds,max_tool_calls,deadline,idempotency_key,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, owner_id, device_id, model, role, json.dumps({"text": request}, ensure_ascii=False),
                     QUEUED, concurrency_key, max_parallel,
                     int(budget_tokens) if budget_tokens is not None else None,
                     float(budget_seconds) if budget_seconds is not None else None,
                     int(max_tool_calls) if max_tool_calls is not None else None,
                     float(deadline) if deadline is not None else None, key or None, now, now))
                run_id = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO task_runs(id,task_id,owner_id,role,model,attempt,budget_tokens,budget_seconds,"
                    "max_tool_calls,deadline,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, task_id, owner_id, role, model, 1,
                     int(budget_tokens) if budget_tokens is not None else None,
                     float(budget_seconds) if budget_seconds is not None else None,
                     int(max_tool_calls) if max_tool_calls is not None else None,
                     float(deadline) if deadline is not None else None, QUEUED, now))
                self._event(task_id, owner_id, "queued", {"request": request}, run_id)
                self._db.execute("COMMIT")
                self._private()
            except sqlite3.IntegrityError:
                self._db.execute("ROLLBACK")
                existing = self._db.execute("SELECT * FROM tasks WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
                if existing is None:
                    raise
                old_request = json.loads(existing["request"] or "{}")
                if old_request.get("text") != request or not same_contract(existing):
                    raise TaskError("幂等键已用于不同的任务请求")
                return self._row(existing)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return self._row(self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task_status(self, owner_id, task_id=None, *, limit=50):
        """Return only tasks belonging to the trusted owner."""
        owner_id = self._text(owner_id, "owner_id", 300)
        with self._lock:
            if task_id:
                row = self._db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=?", (str(task_id), owner_id)).fetchone()
                result = self._row(row)
                if result is None:
                    return None
                result["runs"] = [dict(r) for r in self._db.execute(
                    "SELECT id,role,model,attempt,status,heartbeat,lease_until,cancel_requested,created,started,finished "
                    "FROM task_runs WHERE task_id=? AND owner_id=? ORDER BY attempt", (result["id"], owner_id)).fetchall()]
                events = self._db.execute(
                    "SELECT sequence,type,content,created FROM task_events WHERE task_id=? AND owner_id=? ORDER BY sequence DESC LIMIT 30",
                    (result["id"], owner_id)).fetchall()
                result["events"] = [{"sequence": r[0], "type": r[1], "content": json.loads(r[2]), "created": r[3]}
                                     for r in reversed(events)]
                return result
            rows = self._db.execute("SELECT * FROM tasks WHERE owner_id=? ORDER BY updated DESC LIMIT ?",
                                    (owner_id, max(1, min(int(limit), 200)))).fetchall()
            return [self._row(row) for row in rows]

    def admin_device_ids(self):
        """Return task origin devices only; callers must resolve owner separately."""
        with self._lock:
            return [row[0] for row in self._db.execute(
                "SELECT DISTINCT device_id FROM tasks WHERE device_id<>'' ORDER BY device_id"
            ).fetchall()]

    def task_cancel(self, owner_id, task_id):
        """Cancel queued work or request cancellation for an active run."""
        owner_id = self._text(owner_id, "owner_id", 300)
        task_id = self._text(task_id, "task_id", 100)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id)).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                status = row["status"]
                if status in (QUEUED, WAITING_INPUT):
                    new_status = CANCELLED
                    self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (new_status, time.time(), task_id))
                    self._db.execute("UPDATE task_runs SET status=? WHERE task_id=? AND status IN (?,?)", (new_status, task_id, QUEUED, WAITING_INPUT))
                elif status == RUNNING:
                    new_status = CANCEL_REQUESTED
                    self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (new_status, time.time(), task_id))
                    self._db.execute("UPDATE task_runs SET status=?,cancel_requested=1 WHERE task_id=? AND status=?", (new_status, task_id, RUNNING))
                else:
                    new_status = status
                if new_status != status:
                    self._event(task_id, owner_id, "cancel_requested" if new_status == CANCEL_REQUESTED else "cancelled", {}, None)
                self._db.execute("COMMIT")
                self._private()
                return self.task_status(owner_id, task_id)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def cancel_device_tasks(self, owner_id, device_id):
        """Invalidate old-owner work and pending delivery after a device is reassigned."""
        owner_id = self._text(owner_id, "owner_id", 300)
        device_id = self._text(device_id, "device_id", 300)
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT id,status FROM tasks WHERE owner_id=? AND device_id=? AND status IN (?,?,?)",
                    (owner_id, device_id, QUEUED, WAITING_INPUT, RUNNING)).fetchall()
                for row in rows:
                    if row["status"] == RUNNING:
                        self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?",
                                         (CANCEL_REQUESTED, now, row["id"]))
                        self._db.execute("UPDATE task_runs SET status=?,cancel_requested=1 WHERE task_id=? AND status=?",
                                         (CANCEL_REQUESTED, row["id"], RUNNING))
                        event = "cancel_requested"
                    else:
                        self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?",
                                         (CANCELLED, now, row["id"]))
                        self._db.execute("UPDATE task_runs SET status=?,finished=? WHERE task_id=? AND status IN (?,?)",
                                         (CANCELLED, now, row["id"], QUEUED, WAITING_INPUT))
                        event = "cancelled"
                    self._event(row["id"], owner_id, event, {"reason": "device_reassigned"}, None)
                self._db.execute("UPDATE task_deliveries SET status=?,updated=? WHERE owner_id=? AND device_id=? AND status IN (?,?,?)",
                                 (DELIVERY_CANCELLED, now, owner_id, device_id,
                                  DELIVERY_PENDING, DELIVERY_CLAIMED, DELIVERY_DEFERRED))
                self._private()
                self._db.execute("COMMIT")
                return len(rows)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def claim_next(self, *, owner_id=None, worker_id="worker", lease_seconds=60):
        """Atomically lease one queued task while respecting its concurrency group."""
        worker_id = self._text(worker_id, "worker_id", 200)
        lease_seconds = max(5, min(float(lease_seconds), 3600))
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # Expired requests cannot remain queued forever.
                expired = self._db.execute(
                    "SELECT id,owner_id FROM tasks WHERE status=? AND deadline IS NOT NULL AND deadline<=?",
                    (QUEUED, now)).fetchall()
                for row in expired:
                    self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?", (FAILED, now, row["id"]))
                    self._db.execute("UPDATE task_runs SET status=?,finished=? WHERE task_id=? AND status=?",
                                     (FAILED, now, row["id"], QUEUED))
                    self._event(row["id"], row["owner_id"], "expired", {}, None)
                query = (
                    "SELECT t.id task_id,t.owner_id,t.device_id,t.model,t.role,t.request,t.deadline,t.result_version,"
                    "t.concurrency_key,t.max_parallel,t.budget_tokens,t.budget_seconds,t.max_tool_calls,r.id run_id,r.attempt "
                    "FROM tasks t JOIN task_runs r ON r.task_id=t.id "
                    "WHERE t.status=? AND r.status=? AND (t.deadline IS NULL OR t.deadline>?) "
                    "AND (SELECT COUNT(*) FROM tasks active WHERE active.concurrency_key=t.concurrency_key "
                    "AND active.status IN (?,?)) < t.max_parallel"
                )
                args = [QUEUED, QUEUED, now, RUNNING, CANCEL_REQUESTED]
                if owner_id is not None:
                    query += " AND t.owner_id=?"
                    args.append(str(owner_id))
                query += " ORDER BY t.created,t.id LIMIT 1"
                candidate = self._db.execute(query, args).fetchone()
                if candidate is None:
                    self._db.execute("COMMIT")
                    self._private()
                    return None
                task_id, run_id = candidate["task_id"], candidate["run_id"]
                self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=? AND status=?",
                                 (RUNNING, now, task_id, QUEUED))
                self._db.execute(
                    "UPDATE task_runs SET status=?,started=?,heartbeat=?,lease_until=?,cancel_requested=0 "
                    "WHERE id=? AND status=?",
                    (RUNNING, now, now, now + lease_seconds, run_id, QUEUED))
                self._event(task_id, candidate["owner_id"], "started", {"worker": worker_id}, run_id)
                self._db.execute("COMMIT")
                self._private()
                task = self._row(self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
                run = dict(self._db.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone())
                return {"task": task, "run": run}
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def heartbeat(self, owner_id, run_id, *, lease_seconds=60, checkpoint=None):
        owner_id = self._text(owner_id, "owner_id", 300)
        run_id = self._text(run_id, "run_id", 100)
        now = time.time()
        lease_until = now + max(5, min(float(lease_seconds), 3600))
        encoded = json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None
        if encoded is not None and len(encoded) > 20000:
            raise TaskError("检查点过大")
        with self._lock:
            cur = self._db.execute(
                "UPDATE task_runs SET heartbeat=?,lease_until=?,checkpoint=COALESCE(?,checkpoint) "
                "WHERE id=? AND owner_id=? AND status=? AND cancel_requested=0",
                (now, lease_until, encoded, run_id, owner_id, RUNNING))
            return cur.rowcount == 1

    def cancel_requested(self, owner_id, run_id):
        with self._lock:
            row = self._db.execute(
                "SELECT cancel_requested,status FROM task_runs WHERE id=? AND owner_id=?",
                (str(run_id), str(owner_id))).fetchone()
            return row is None or bool(row["cancel_requested"]) or row["status"] in (CANCEL_REQUESTED, CANCELLED)

    def append_event(self, owner_id, task_id, event_type, content, *, run_id=None):
        owner_id = self._text(owner_id, "owner_id", 300)
        task_id = self._text(task_id, "task_id", 100)
        event_type = self._text(event_type, "event_type", 100)
        encoded = json.dumps(content, ensure_ascii=False)
        if len(encoded) > 12000:
            raise TaskError("任务事件过大")
        with self._lock:
            task = self._db.execute("SELECT status FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id)).fetchone()
            if task is None:
                return None
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._event(task_id, owner_id, event_type, content, run_id)
                self._db.execute("COMMIT")
                self._private()
                return self._db.execute("SELECT last_insert_rowid()").fetchone()[0]
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def finish_run(self, owner_id, run_id, status, *, result=None, error=None):
        """Persist worker completion without retrying uncertain external operations."""
        owner_id = self._text(owner_id, "owner_id", 300)
        run_id = self._text(run_id, "run_id", 100)
        if status not in (SUCCEEDED, FAILED, WAITING_INPUT, CANCELLED, NEEDS_RECONCILIATION):
            raise TaskError("无效的运行完成状态")
        payload = {"result": result, "error": error}
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > 100000:
            raise TaskError("任务结果过大")
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                run = self._db.execute("SELECT * FROM task_runs WHERE id=? AND owner_id=?", (run_id, owner_id)).fetchone()
                if run is None:
                    self._db.execute("ROLLBACK")
                    return None
                if run["status"] not in (RUNNING, CANCEL_REQUESTED):
                    task = self._db.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
                    self._db.execute("COMMIT")
                    return self._row(task)
                if run["cancel_requested"] and status in (SUCCEEDED, FAILED):
                    raise TaskError("任务已请求取消，完成前必须报告已取消或需要核实")
                self._db.execute("UPDATE task_runs SET status=?,finished=?,lease_until=NULL WHERE id=?",
                                 (status, now, run_id))
                if status == SUCCEEDED:
                    self._db.execute("UPDATE tasks SET status=?,result=?,result_version=result_version+1,updated=? WHERE id=?",
                                     (status, encoded, now, run["task_id"]))
                else:
                    self._db.execute("UPDATE tasks SET status=?,result=?,updated=? WHERE id=?",
                                     (status, encoded, now, run["task_id"]))
                self._event(run["task_id"], owner_id, status, payload, run_id)
                self._db.execute("COMMIT")
                self._private()
                return self.task_status(owner_id, run["task_id"])
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def reconcile_expired(self):
        """Mark expired task leases uncertain and recover abandoned delivery claims."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT r.id,r.task_id,r.owner_id FROM task_runs r JOIN tasks t ON t.id=r.task_id "
                    "WHERE r.status IN (?,?) AND r.lease_until IS NOT NULL AND r.lease_until<=?",
                    (RUNNING, CANCEL_REQUESTED, now)).fetchall()
                for row in rows:
                    self._db.execute("UPDATE task_runs SET status=?,finished=?,lease_until=NULL WHERE id=?",
                                     (NEEDS_RECONCILIATION, now, row["id"]))
                    self._db.execute("UPDATE tasks SET status=?,updated=? WHERE id=?",
                                     (NEEDS_RECONCILIATION, now, row["task_id"]))
                    self._event(row["task_id"], row["owner_id"], "lease_expired", {}, row["id"])
                self._db.execute("UPDATE task_deliveries SET status=?,claimed_until=NULL,updated=? "
                                 "WHERE status=? AND (claimed_until IS NULL OR claimed_until<=?)",
                                 (DELIVERY_DEFERRED, now, DELIVERY_CLAIMED, now))
                self._db.execute("COMMIT")
                self._private()
                return len(rows)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def enqueue_delivery(self, owner_id, task_id, device_id, result_version, session_id=None, current_owner=None):
        owner_id = self._text(owner_id, "owner_id", 300)
        device_id = self._text(device_id, "device_id", 300)
        session_id = self._text(session_id, "session_id", 200, required=False)
        if current_owner is None:
            from core.agent import chat_history
            current_owner = chat_history.owner_for_device(device_id)
        if current_owner != owner_id:
            return None
        with self._lock:
            task = self._db.execute("SELECT status,result_version FROM tasks WHERE id=? AND owner_id=?",
                                    (str(task_id), owner_id)).fetchone()
            if task is None or task["status"] != SUCCEEDED or int(task["result_version"]) != int(result_version):
                return None
            now = time.time()
            self._db.execute("INSERT OR IGNORE INTO task_deliveries(task_id,owner_id,result_version,device_id,session_id,status,created,updated) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (str(task_id), owner_id, int(result_version), device_id, session_id, DELIVERY_PENDING, now, now))
            self._private()
            row = self._db.execute("SELECT * FROM task_deliveries WHERE task_id=? AND result_version=? AND device_id=? AND session_id=?",
                                   (str(task_id), int(result_version), device_id, session_id)).fetchone()
            return dict(row) if row else None

    def claim_delivery(self, owner_id, device_id, session_id=None, current_owner=None):
        owner_id = self._text(owner_id, "owner_id", 300)
        device_id = self._text(device_id, "device_id", 300)
        session_id = self._text(session_id, "session_id", 200, required=False)
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM task_deliveries WHERE owner_id=? AND device_id=? "
                    "AND status IN (?,?) AND (session_id=? OR session_id='') ORDER BY created,id LIMIT 1",
                    (owner_id, device_id, DELIVERY_PENDING, DELIVERY_DEFERRED, session_id)).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                if current_owner is None:
                    from core.agent import chat_history
                    current_owner = chat_history.owner_for_device(device_id)
                task_owner = self._db.execute("SELECT t.owner_id FROM tasks t WHERE t.id=?",
                                              (row["task_id"],)).fetchone()
                if current_owner != owner_id or task_owner is None or task_owner["owner_id"] != owner_id:
                    self._db.execute("UPDATE task_deliveries SET status=?,updated=? WHERE id=?",
                                     (DELIVERY_CANCELLED, now, row["id"]))
                    self._db.execute("COMMIT")
                    self._private()
                    return None
                self._db.execute("UPDATE task_deliveries SET status=?,claimed_until=?,updated=? WHERE id=?",
                                 (DELIVERY_CLAIMED, now + 120, now, row["id"]))
                if not row["session_id"] and session_id:
                    self._db.execute("UPDATE task_deliveries SET session_id=? WHERE id=?", (session_id, row["id"]))
                self._private()
                self._db.execute("COMMIT")
                result = dict(row); result["status"] = DELIVERY_CLAIMED; result["updated"] = now
                if not result.get("session_id") and session_id:
                    result["session_id"] = session_id
                task = self._db.execute("SELECT result FROM tasks WHERE id=? AND owner_id=?", (row["task_id"], owner_id)).fetchone()
                result["result"] = json.loads(task["result"]) if task and task["result"] else None
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def mark_delivery(self, owner_id, delivery_id, status, *, played_ms=None):
        owner_id = self._text(owner_id, "owner_id", 300)
        if status not in (DELIVERY_PENDING, DELIVERY_PLAYED, DELIVERY_DEFERRED, DELIVERY_CANCELLED):
            raise TaskError("无效的投递状态")
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT * FROM task_deliveries WHERE id=? AND owner_id=?", (int(delivery_id), owner_id)).fetchone()
            if row is None:
                return None
            if row["status"] != DELIVERY_CLAIMED:
                return dict(row)
            played = row["played_ms"] if played_ms is None else max(int(row["played_ms"]), int(played_ms))
            new_session = "" if status == DELIVERY_DEFERRED else row["session_id"]
            self._db.execute("UPDATE task_deliveries SET status=?,claimed_until=NULL,session_id=?,played_ms=?,updated=? WHERE id=? AND owner_id=?",
                             (status, new_session, played, now, int(delivery_id), owner_id))
            self._private()
            return dict(self._db.execute("SELECT * FROM task_deliveries WHERE id=?", (int(delivery_id),)).fetchone())

    def task_answer(self, owner_id, task_id, answer):
        """Append missing input and requeue a task waiting for user input."""
        owner_id = self._text(owner_id, "owner_id", 300)
        task_id = self._text(task_id, "task_id", 100)
        answer = self._text(answer, "answer", 20000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id)).fetchone()
                if row is None:
                    self._db.execute("ROLLBACK")
                    return None
                if row["status"] != WAITING_INPUT:
                    self._db.execute("ROLLBACK")
                    raise TaskError("任务当前不在等待补充信息")
                request = json.loads(row["request"] or "{}")
                request.setdefault("answers", []).append(answer)
                now = time.time()
                self._db.execute("UPDATE tasks SET request=?,status=?,updated=? WHERE id=?",
                                 (json.dumps(request, ensure_ascii=False), QUEUED, now, task_id))
                previous = self._db.execute(
                    "SELECT role,model,attempt,budget_tokens,budget_seconds,max_tool_calls,deadline "
                    "FROM task_runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)).fetchone()
                self._db.execute("UPDATE task_runs SET status=?,finished=? WHERE task_id=? AND status=?",
                                 (WAITING_INPUT, now, task_id, WAITING_INPUT))
                if previous is not None:
                    self._db.execute(
                        "INSERT INTO task_runs(id,task_id,owner_id,role,model,attempt,budget_tokens,budget_seconds,"
                        "max_tool_calls,deadline,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, task_id, owner_id, previous[0], previous[1], int(previous[2]) + 1,
                         previous[3], previous[4], previous[5], previous[6], QUEUED, now))
                self._event(task_id, owner_id, "input_received", {"answer": answer}, None)
                self._db.execute("COMMIT")
                self._private()
                return self.task_status(owner_id, task_id)
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._db.close()


__all__ = ["TaskService", "TaskError", "STATES", "QUEUED", "RUNNING", "WAITING_INPUT",
           "SUCCEEDED", "FAILED", "CANCEL_REQUESTED", "CANCELLED", "NEEDS_RECONCILIATION"]


_service = None
_service_lock = threading.Lock()


def store() -> TaskService:
    """Return the Hub-owned task database, separate from chat history."""
    global _service
    with _service_lock:
        if _service is None:
            from config.config_loader import get_project_dir
            _service = TaskService(Path(get_project_dir()) / "data" / "tasks.db")
        return _service


def task_start(owner_id, device_id, request, **kwargs):
    return store().task_start(owner_id, device_id, request, **kwargs)


def task_status(owner_id, task_id=None, **kwargs):
    return store().task_status(owner_id, task_id, **kwargs)


def admin_device_ids():
    return store().admin_device_ids()


def task_cancel(owner_id, task_id):
    return store().task_cancel(owner_id, task_id)


def task_answer(owner_id, task_id, answer):
    return store().task_answer(owner_id, task_id, answer)


def cancel_device_tasks(owner_id, device_id):
    return store().cancel_device_tasks(owner_id, device_id)
