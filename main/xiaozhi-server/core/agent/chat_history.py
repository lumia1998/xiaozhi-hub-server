"""聊天记录落盘：按设备和模型存进 data/chat.db（SQLite），网关重启、连接断开后都还在。

原来对话只在内存里（每个模型一份，见 llm_router），连接一断就没了；发给模型时整份全发，不截断。现在：

- 每轮结束、回复被打断改写（reply_progress）、连接关闭时，把对话里新增和改过的消息同步进库（sync）。
  系统提示不存；
- 新连接（或切到这个模型）时从库里载入这台设备这个模型最近 max_messages 条（load），
  每次发给模型时再按"最近 max_messages 条 + 约 max_tokens 个 token"截一个窗口（select_window）。
  窗口只从用户的话开始切，工具调用、工具结果和后面的回复跟着它那句话走，不会拆开；
- 被窗口截掉的部分在后台增量总结成一段记忆（summarize），放进系统提示的 <memory>。记忆按设备一份，
  所有模型共用（记的是关于用户的事）；管理后台能查看、手改、清空（set_memory，马上生效）。
- 清除（clear）：删库里这台设备的记录、清空记忆、清在线连接里的对话。

配置（config.yaml 的 chat_history）：enabled、max_messages、max_tokens、summary、summary_min、summary_chars、
summary_llm（总结用哪个 LLM 条目，空则用当前模型）、display_limit。
"""

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.agent import memory_policy

TAG = __name__
logger = setup_logging()

DEFAULTS = {
    "enabled": True,
    "max_messages": 30,     # 每次最多带最近这么多条（用户、助手、工具消息都算）
    "max_tokens": 8000,     # 再加一道 token 上限（估算值）
    "summary": True,        # 截掉的部分总结成记忆
    "summary_min": 6,       # 截掉、还没总结的攒到这么多条才总结一次，免得每轮都调一次模型
    "summary_chars": 500,   # 记忆最多多少字
    "summary_llm": "",      # 总结用哪个 LLM 条目；空 = 当前这个模型
    "display_limit": 100,   # 给屏幕显示时每个模型最多返回多少条
    "use_memory": True,
}
CUT_MARK = "……"  # reply_progress 给被打断的回复加的结尾
SUMMARY_BATCH = 200  # 一次最多总结这么多条（第一次上线时库里可能已经有很长的记录）
SUMMARY_PROMPT = """你负责维护语音助手的长期记忆，内容按设备内的用户对话整理。
只保留用户明确表达的稳定个人信息、长期偏好、固定习惯、正在推进的项目、未完成的约定和待办、明确要求记住的事项。
不要保存天气、温度、开关设备的操作和状态、日期查询、一次性新闻或比赛查询、寒暄、助手的自我介绍和推测。一次操作不代表习惯。
【已有自动记忆】中的有效信息应保留，只有用户明确纠正或说明已完成时才更新；不要把助手或工具说的话当作用户事实。
【手动保留】由用户维护，不要改写，也不要重复加入自动记忆。记录中带时间，过期的临时信息不要保留。
用简洁中文要点，每条一行，最多 {chars} 字。只输出更新后的自动记忆；没有新增长期信息就原样保留已有自动记忆，两者都没有则输出“无”。"""

_db = None
_db_lock = threading.Lock()
_sync_lock = threading.RLock()
_summarizing = set()  # 正在总结的设备
_summarizing_lock = threading.Lock()


# ---------- 配置 ----------

def settings(config: Optional[dict]) -> dict:
    merged = dict(DEFAULTS)
    merged.update((config or {}).get("chat_history") or {})
    return merged


def _conn_config(conn) -> dict:
    server = getattr(conn, "server", None)
    return getattr(server, "config", None) or getattr(conn, "config", None) or {}


def device_of(conn) -> Optional[str]:
    """只给带 device-id 的连接存：没有的话只有一次性的 session_id，存了也没人能再取回来。"""
    return getattr(conn, "device_id", None) or None


# ---------- 数据库 ----------

class ChatDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 聊天记录和记忆只给自己看（同目录的 .config.yaml 也是 600）。先建好库文件、设好权限，
        # SQLite 新建 -wal、-shm 时照库文件的权限来；以前按默认 umask 建出来的也一并改掉
        self.path.touch(mode=0o600, exist_ok=True)
        self._private()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,
                model TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_device_model ON messages (device, model, id);
            CREATE TABLE IF NOT EXISTS device_memory (
                device TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_progress (
                device TEXT NOT NULL,
                model TEXT NOT NULL,
                upto INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device, model)
            );
            CREATE TABLE IF NOT EXISTS owners (
                owner_id TEXT PRIMARY KEY,
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS device_owners (
                device TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                epoch INTEGER NOT NULL DEFAULT 1,
                updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'explicit',
                explicit INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(owner_id, topic)
            );
            CREATE INDEX IF NOT EXISTS preferences_owner ON preferences(owner_id, updated);
            CREATE TABLE IF NOT EXISTS preference_tombstones (
                owner_id TEXT NOT NULL,
                topic_key TEXT NOT NULL,
                deleted REAL NOT NULL,
                PRIMARY KEY(owner_id, topic_key)
            );
            CREATE TABLE IF NOT EXISTS operation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                device TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                result TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'assistant',
                confirmed INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS operation_events_owner ON operation_events(owner_id, id);
            CREATE TABLE IF NOT EXISTS memory_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                summary TEXT NOT NULL,
                pinned TEXT NOT NULL,
                version INTEGER NOT NULL,
                epoch INTEGER NOT NULL,
                source TEXT NOT NULL,
                archived REAL NOT NULL
            );
            """
        )
        self._migrate()
        self._retention_schema()
        self._private()

    def _retention_schema(self):
        columns = {r[1] for r in self._conn.execute('PRAGMA table_info(messages)')}
        if 'retention' not in columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN retention TEXT NOT NULL DEFAULT ''")
        if 'expires_at' not in columns:
            self._conn.execute('ALTER TABLE messages ADD COLUMN expires_at REAL')
        if 'pi_message' not in columns:
            self._conn.execute('ALTER TABLE messages ADD COLUMN pi_message TEXT')
        self._conn.execute('CREATE INDEX IF NOT EXISTS messages_expiry ON messages(expires_at)')
        columns = {r[1] for r in self._conn.execute('PRAGMA table_info(device_memory)')}
        if 'pinned' not in columns:
            self._conn.execute("ALTER TABLE device_memory ADD COLUMN pinned TEXT NOT NULL DEFAULT ''")
            self._conn.execute("UPDATE device_memory SET pinned=summary, summary='' WHERE source='manual'")
            self._conn.execute("INSERT OR REPLACE INTO memory_progress(device,model,upto) "
                               "SELECT m.device,m.model,MAX(m.id) FROM messages m JOIN device_memory d ON d.device=m.device "
                               "WHERE d.source='cleared' GROUP BY m.device,m.model")
        if 'owner_id' not in columns:
            self._conn.execute("ALTER TABLE device_memory ADD COLUMN owner_id TEXT")
        if 'owner_epoch' not in columns:
            self._conn.execute("ALTER TABLE device_memory ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0")
        if 'summary_epoch' not in columns:
            self._conn.execute("ALTER TABLE device_memory ADD COLUMN summary_epoch INTEGER NOT NULL DEFAULT 0")
        message_columns = {r[1] for r in self._conn.execute('PRAGMA table_info(messages)')}
        if 'owner_epoch' not in message_columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0")
        self._conn.execute('CREATE TABLE IF NOT EXISTS memory_cleanup (id INTEGER PRIMARY KEY CHECK(id=1), updated REAL, deleted INTEGER)')

    def classify_turns(self, device, model, config):
        """Classify only new rows plus their full current turn. No LLM call is involved."""
        epoch = self.owner_epoch(device)
        first = self._exec("SELECT MIN(id) FROM messages WHERE device=? AND model=? AND owner_epoch=? AND retention=''", (device, model, epoch)).fetchone()[0]
        if first is None: return
        user = self._exec("SELECT MAX(id) FROM messages WHERE device=? AND model=? AND owner_epoch=? AND role='user' AND id<=?", (device, model, epoch, first)).fetchone()[0]
        rows = self.after(device, model, (user or first) - 1)
        with self._lock:
            for group in memory_policy.group_turns(rows):
                kind, expiry = memory_policy.turn_policy(group, config)
                self._conn.executemany('UPDATE messages SET retention=?,expires_at=? WHERE id=?',
                                       [(kind, expiry, r['id']) for r in group])

    def cleanup_expired(self, config):
        with _sync_lock:
            pairs = self._exec("SELECT DISTINCT device,model FROM messages WHERE retention='' ").fetchall()
            for device, model in pairs:
                self.classify_turns(device, model, config)
            with self._lock:
                count = self._conn.execute('DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at<=?', (time.time(),)).rowcount
                if count:
                    self._conn.execute('INSERT OR REPLACE INTO memory_cleanup VALUES(1,?,?)', (time.time(), count))
            return count

    def metadata(self, ids):
        result = {}
        ids = list(ids)
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = self._exec(f"SELECT id,retention,expires_at FROM messages WHERE id IN ({','.join('?' for _ in chunk)})", chunk).fetchall()
            result.update({r[0]: (r[1], r[2]) for r in rows})
        return result

    def _private(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except FileNotFoundError:
                pass

    def _migrate(self) -> None:
        """早期按"设备 + 模型"各存一份记忆（memories 表）：合并成每台设备一份，进度按模型留着。"""
        if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone():
            return
        rows = self._conn.execute("SELECT device, model, summary, upto, updated FROM memories ORDER BY device, updated").fetchall()
        merged = {}
        self._conn.execute("BEGIN")
        for device, model, summary, upto, updated in rows:
            parts, last = merged.get(device, ([], 0))
            if summary:
                parts.append(summary)
            merged[device] = (parts, max(last, updated))
            self._conn.execute("INSERT OR REPLACE INTO memory_progress (device, model, upto) VALUES (?,?,?)",
                               (device, model, upto))
        for device, (parts, updated) in merged.items():
            self._conn.execute("INSERT OR REPLACE INTO device_memory (device, summary, version, source, updated) "
                               "VALUES (?,?,1,'summary',?)", (device, "\n".join(parts), updated))
        self._conn.execute("DROP TABLE memories")
        self._conn.execute("COMMIT")
        logger.bind(tag=TAG).info(f"记忆合并成每台设备一份：{len(merged)} 台设备")

    def _exec(self, sql: str, args=()):
        with self._lock:
            return self._conn.execute(sql, args)

    def insert(self, device, model, role, content, tool_calls=None, tool_call_id=None, created=None, pi_message=None,
               owner_epoch=None) -> int:
        epoch = self.owner_epoch(device) if owner_epoch is None else int(owner_epoch)
        cur = self._exec(
            "INSERT INTO messages (device, model, role, content, tool_calls, tool_call_id, created, pi_message, owner_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (device, model, role, content, json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
             tool_call_id, created if created is not None else time.time(), json.dumps(pi_message,ensure_ascii=False) if pi_message else None,
             epoch))
        return cur.lastrowid

    def update_content(self, msg_id: int, content) -> None:
        self._exec("UPDATE messages SET content=?,pi_message=NULL WHERE id=?", (content, msg_id))

    def delete_ids(self, ids) -> None:
        ids = list(ids)
        if ids:
            self._exec(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)

    def recent(self, device, model, limit: int) -> List[dict]:
        epoch = self.owner_epoch(device)
        rows = self._exec(
            "SELECT id, role, content, tool_calls, tool_call_id, created, retention, expires_at, pi_message FROM messages "
            "WHERE device=? AND model=? AND owner_epoch=? AND (expires_at IS NULL OR expires_at>?) ORDER BY id DESC LIMIT ?",
            (device, model, epoch, time.time(), int(limit))).fetchall()
        return [_row(r) for r in reversed(rows)]

    def after(self, device, model, after_id: int, limit: int = None) -> List[dict]:
        epoch = self.owner_epoch(device)
        sql = ("SELECT id, role, content, tool_calls, tool_call_id, created, retention, expires_at, pi_message FROM messages "
               "WHERE device=? AND model=? AND owner_epoch=? AND id>? ORDER BY id")
        args = [device, model, epoch, int(after_id)]
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_row(r) for r in self._exec(sql, args).fetchall()]

    def exists(self, msg_id: int) -> bool:
        return self._exec("SELECT 1 FROM messages WHERE id=?", (int(msg_id),)).fetchone() is not None

    def models(self, device) -> List[str]:
        return [r[0] for r in self._exec("SELECT DISTINCT model FROM messages WHERE device=? AND owner_epoch=?", (device, self.owner_epoch(device))).fetchall()]

    def owner_epoch(self, device: str) -> int:
        row = self._exec("SELECT epoch FROM device_owners WHERE device=?", (device,)).fetchone()
        return int(row[0]) if row else 0

    # ---------- owners, explicit preferences and operation facts ----------
    def owner_for_device(self, device: str) -> str:
        """Resolve a device without writing. Unmapped devices keep device-scoped compatibility."""
        row = self._exec("SELECT owner_id FROM device_owners WHERE device=?", (device,)).fetchone()
        return row[0] if row else device

    def owner_mappings(self) -> List[dict]:
        rows = self._exec("SELECT device,owner_id,version,epoch,updated FROM device_owners ORDER BY device").fetchall()
        return [{"device": r[0], "owner": r[1], "version": r[2], "epoch": r[3], "updated": r[4]} for r in rows]

    def set_device_owner(self, device: str, owner_id: str, expected_version: int = None):
        """Set an explicit owner mapping. Old device history is retained but not moved into the new owner."""
        device, owner_id = str(device or "").strip(), str(owner_id or "").strip()
        if not device or not owner_id or len(device) > 300 or len(owner_id) > 300:
            raise ValueError("设备或所有者不能为空")
        now = time.time()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT owner_id,version,epoch,updated FROM device_owners WHERE device=?", (device,)).fetchone()
                current = {"device": device, "owner": row[0], "version": row[1], "epoch": row[2], "updated": row[3]} if row else \
                    {"device": device, "owner": device, "version": 0, "epoch": 0, "updated": None}
                if expected_version is not None and int(expected_version) != current["version"]:
                    c.execute("ROLLBACK")
                    return False, current
                if current["owner"] == owner_id:
                    c.execute("COMMIT")
                    return True, current
                old_owner = row[0] if row else device
                new_epoch = (row[2] if row else 0) + 1
                c.execute("INSERT OR IGNORE INTO owners(owner_id,created,updated) VALUES(?,?,?)", (owner_id, now, now))
                c.execute("INSERT OR IGNORE INTO owners(owner_id,created,updated) VALUES(?,?,?)", (old_owner, now, now))
                c.execute("INSERT INTO device_owners(device,owner_id,version,epoch,updated) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(device) DO UPDATE SET owner_id=excluded.owner_id,version=excluded.version,epoch=excluded.epoch,updated=excluded.updated",
                          (device, owner_id, current["version"] + 1, new_epoch, now))
                # Archive old visible memory, then start a clean namespace for the new owner.
                old = c.execute("SELECT summary,pinned,version,summary_epoch FROM device_memory WHERE device=?", (device,)).fetchone()
                if old and (old[0] or old[1]):
                    c.execute("INSERT INTO memory_archives(device,owner_id,summary,pinned,version,epoch,source,archived) VALUES(?,?,?,?,?,?,?,?)",
                              (device, old_owner, old[0], old[1], old[2], old[3], "owner_change", now))
                c.execute("INSERT OR IGNORE INTO device_memory(device,summary,version,source,updated,pinned,owner_id,owner_epoch) VALUES(?,'',0,'',?,'',?,?)",
                          (device, now, owner_id, new_epoch))
                c.execute("UPDATE device_memory SET summary='',pinned='',version=version+1,source='owner_change',updated=?,owner_id=?,owner_epoch=?,summary_epoch=summary_epoch+1 WHERE device=?",
                          (now, owner_id, new_epoch, device))
                # Keep old chat history but never let the new owner summarize it.
                c.execute("INSERT OR REPLACE INTO memory_progress(device,model,upto) "
                          "SELECT device,model,MAX(id) FROM messages WHERE device=? GROUP BY model", (device,))
                c.execute("COMMIT")
                return True, {"device": device, "owner": owner_id, "version": current["version"] + 1,
                              "epoch": new_epoch, "updated": now}
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def list_preferences(self, owner_id: str) -> List[dict]:
        rows = self._exec("SELECT id,topic,content,version,updated,source,explicit FROM preferences WHERE owner_id=? ORDER BY topic,id", (owner_id,)).fetchall()
        return [{"id": r[0], "topic": r[1], "content": r[2], "version": r[3], "updated": r[4],
                 "source": r[5], "explicit": bool(r[6])} for r in rows]

    @staticmethod
    def _topic_key(topic: str) -> str:
        return re.sub(r"\s+", " ", str(topic or "").strip().casefold())

    def forgotten_topics(self, owner_id: str) -> List[str]:
        return [r[0] for r in self._exec("SELECT topic_key FROM preference_tombstones WHERE owner_id=? ORDER BY deleted DESC", (owner_id,)).fetchall()]

    def _reset_owner_summaries_locked(self, c, owner_id: str, reason: str, now: float):
        """Archive opaque auto summaries and CAS-invalidate every in-flight summary for this owner."""
        devices = c.execute("SELECT device FROM device_memory UNION SELECT device FROM messages UNION SELECT device FROM device_owners").fetchall()
        for (device,) in devices:
            mapped = c.execute("SELECT owner_id,epoch FROM device_owners WHERE device=?", (device,)).fetchone()
            current_owner, epoch = mapped if mapped else (device, 0)
            if current_owner != owner_id:
                continue
            old = c.execute("SELECT summary,pinned,version,summary_epoch FROM device_memory WHERE device=?", (device,)).fetchone()
            if old and (old[0] or old[1]):
                c.execute("INSERT INTO memory_archives(device,owner_id,summary,pinned,version,epoch,source,archived) VALUES(?,?,?,?,?,?,?,?)",
                          (device, owner_id, old[0], old[1], old[2], old[3], reason, now))
            c.execute("INSERT OR IGNORE INTO device_memory(device,summary,version,source,updated,pinned,owner_id,owner_epoch,summary_epoch) "
                      "VALUES(?,'',0,'',?,'',?,?,0)", (device, now, owner_id, epoch))
            c.execute("UPDATE device_memory SET summary='',version=version+1,source=?,updated=?,owner_id=?,owner_epoch=?,summary_epoch=summary_epoch+1 WHERE device=?",
                      (reason, now, owner_id, epoch, device))
            c.execute("INSERT INTO memory_progress(device,model,upto) "
                      "SELECT device,model,MAX(id) FROM messages WHERE device=? AND owner_epoch=? GROUP BY model "
                      "ON CONFLICT(device,model) DO UPDATE SET upto=MAX(memory_progress.upto,excluded.upto)", (device, epoch))

    def upsert_preference(self, owner_id: str, topic: str, content: str, preference_id: int = None,
                          expected_version: int = None, source: str = "explicit", explicit: bool = True):
        owner_id, topic, content = str(owner_id or "").strip(), str(topic or "").strip(), str(content or "").strip()
        if not owner_id or not topic or not content or len(topic) > 200 or len(content) > 5000:
            raise ValueError("偏好主题和内容不能为空")
        topic_key, now = self._topic_key(topic), time.time()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                where, args = (("id=? AND owner_id=?", (int(preference_id), owner_id)) if preference_id is not None else
                               ("owner_id=? AND topic=?", (owner_id, topic)))
                old = c.execute(f"SELECT id,topic,content,version,updated,source,explicit FROM preferences WHERE {where}", args).fetchone()
                if old and expected_version is not None and int(expected_version) != old[3]:
                    c.execute("ROLLBACK")
                    return False, self._preference_row(old)
                if old:
                    changed = old[1] != topic or old[2] != content
                    version = old[3] + 1
                    c.execute("UPDATE preferences SET topic=?,content=?,source=?,explicit=?,version=?,updated=? WHERE id=?",
                              (topic, content, source, int(bool(explicit)), version, now, old[0]))
                    ident = old[0]
                else:
                    if expected_version not in (None, 0):
                        c.execute("ROLLBACK")
                        return False, None
                    cur = c.execute("INSERT INTO preferences(owner_id,topic,content,source,explicit,version,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                                     (owner_id, topic, content, source, int(bool(explicit)), 1, now, now))
                    ident, version = cur.lastrowid, 1
                    changed = False
                c.execute("DELETE FROM preference_tombstones WHERE owner_id=? AND topic_key=?", (owner_id, topic_key))
                if changed:
                    self._reset_owner_summaries_locked(c, owner_id, "preference_corrected", now)
                c.execute("COMMIT")
                return True, {"id": ident, "topic": topic, "content": content, "version": version, "updated": now,
                              "source": source, "explicit": bool(explicit)}
            except sqlite3.IntegrityError:
                c.execute("ROLLBACK")
                raise ValueError("该所有者已有相同主题的偏好")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _preference_row(row):
        return {"id": row[0], "topic": row[1], "content": row[2], "version": row[3], "updated": row[4],
                "source": row[5], "explicit": bool(row[6])}

    def delete_preference(self, owner_id: str, preference_id: int, expected_version: int = None):
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                old = c.execute("SELECT id,topic,content,version,updated,source,explicit FROM preferences WHERE id=? AND owner_id=?",
                                (int(preference_id), owner_id)).fetchone()
                if old is None:
                    c.execute("COMMIT")
                    return False, None
                if expected_version is not None and int(expected_version) != old[3]:
                    c.execute("ROLLBACK")
                    return False, self._preference_row(old)
                c.execute("DELETE FROM preferences WHERE id=?", (old[0],))
                now = time.time()
                topic_key = self._topic_key(old[1])
                c.execute("INSERT OR REPLACE INTO preference_tombstones(owner_id,topic_key,deleted) VALUES(?,?,?)",
                          (owner_id, topic_key, now))
                self._reset_owner_summaries_locked(c, owner_id, "preference_forgotten", now)
                c.execute("COMMIT")
                return True, {"id": old[0], "version": old[3] + 1}
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def append_operation_event(self, owner_id: str, device: str, action: str, target: str, result: str,
                               actor: str = "assistant", confirmed: bool = False):
        if not confirmed:
            return None
        cur = self._exec("INSERT INTO operation_events(owner_id,device,action,target,result,actor,confirmed,created) VALUES(?,?,?,?,?,?,1,?)",
                         (owner_id, device, str(action)[:200], str(target)[:500], str(result)[:5000], str(actor)[:100], time.time()))
        return cur.lastrowid

    def recent_operation_events(self, owner_id: str, limit: int = 5) -> List[dict]:
        rows = self._exec("SELECT id,action,target,result,actor,created FROM operation_events WHERE owner_id=? AND confirmed=1 ORDER BY id DESC LIMIT ?",
                          (owner_id, max(1, min(int(limit), 50)))).fetchall()
        return [{"id": r[0], "action": r[1], "target": r[2], "result": r[3], "actor": r[4], "created": r[5]} for r in reversed(rows)]

    def memory(self, device) -> dict:
        """当前设备命名空间的记忆；旧库仍按设备读取，owner 变更后 active 内容已隔离。"""
        r = self._exec("SELECT summary, version, source, updated, pinned,owner_id,owner_epoch,summary_epoch FROM device_memory WHERE device=?", (device,)).fetchone()
        return {"summary": r[0], "version": r[1], "source": r[2], "updated": r[3], "pinned": r[4],
                "owner": r[5] or self.owner_for_device(device), "owner_epoch": r[6], "summary_epoch": r[7]} if r else \
            {"summary": "", "version": 0, "source": "", "updated": None, "pinned": "",
             "owner": self.owner_for_device(device), "owner_epoch": 0, "summary_epoch": 0}

    def progress(self, device) -> Dict[str, int]:
        """每个模型的消息总结到了哪条（id）。"""
        rows = self._exec("SELECT model, upto FROM memory_progress WHERE device=?", (device,)).fetchall()
        return {m: u for m, u in rows}

    def set_memory(self, device, summary: str, source: str, expect_version: int = None, progress: dict = None,
                   field: str = 'summary', forget_before: bool = False, expect_epoch: int = None) -> bool:
        """写记忆（版本加 1）和总结进度。expect_version 不为空时，记忆期间被改过（版本变了）就不写，返回 False。"""
        with self._lock:
            if field not in ('summary', 'pinned', 'both'): raise ValueError('不支持的记忆类型')
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                r = c.execute("SELECT version,summary_epoch FROM device_memory WHERE device=?", (device,)).fetchone()
                version, epoch = (r if r else (0, 0))
                if expect_version is not None and version != expect_version:
                    c.execute("ROLLBACK")
                    return False
                if expect_epoch is not None and epoch != expect_epoch:
                    c.execute("ROLLBACK")
                    return False
                c.execute("INSERT OR IGNORE INTO device_memory(device,summary,version,source,updated,pinned) VALUES(?,'',0,'',?,'')", (device, time.time()))
                if forget_before:
                    old = c.execute("SELECT summary,pinned,owner_id,summary_epoch FROM device_memory WHERE device=?", (device,)).fetchone()
                    if old and (old[0] or old[1]):
                        owner = old[2] or c.execute("SELECT owner_id FROM device_owners WHERE device=?", (device,)).fetchone()
                        owner = owner[0] if isinstance(owner, tuple) else owner or device
                        c.execute("INSERT INTO memory_archives(device,owner_id,summary,pinned,version,epoch,source,archived) VALUES(?,?,?,?,?,?,?,?)",
                                  (device, owner, old[0], old[1], version, old[3], source, time.time()))
                    epoch += 1
                if field == 'both':
                    c.execute('UPDATE device_memory SET summary=?,pinned=?,version=?,source=?,updated=? WHERE device=?',
                              (summary, summary, version + 1, source, time.time(), device))
                else:
                    c.execute(f'UPDATE device_memory SET {field}=?,version=?,source=?,updated=? WHERE device=?',
                              (summary, version + 1, source, time.time(), device))
                c.execute("UPDATE device_memory SET summary_epoch=? WHERE device=?", (epoch, device))
                if forget_before:
                    # Edited/deleted automatic facts must not be extracted again from old messages.
                    epoch_row = c.execute('SELECT epoch FROM device_owners WHERE device=?', (device,)).fetchone()
                    current_epoch = int(epoch_row[0]) if epoch_row else 0
                    progress = dict(c.execute('SELECT model,MAX(id) FROM messages WHERE device=? AND owner_epoch=? GROUP BY model',
                                              (device, current_epoch)).fetchall())
                for model, upto in (progress or {}).items():
                    c.execute("INSERT INTO memory_progress (device, model, upto) VALUES (?,?,?) "
                              "ON CONFLICT (device, model) DO UPDATE SET upto=excluded.upto", (device, model, int(upto)))
                c.execute("COMMIT")
                return True
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def memory_devices(self) -> List[str]:
        rows = self._exec("SELECT device FROM device_memory UNION SELECT DISTINCT device FROM messages UNION SELECT device FROM device_owners").fetchall()
        return sorted(r[0] for r in rows)

    def memory_archives(self, device: str) -> List[dict]:
        rows = self._exec("SELECT id,owner_id,summary,pinned,version,epoch,source,archived FROM memory_archives WHERE device=? ORDER BY id DESC", (device,)).fetchall()
        return [{"id": r[0], "owner": r[1], "summary": r[2], "pinned": r[3], "version": r[4],
                 "epoch": r[5], "source": r[6], "archived": r[7]} for r in rows]

    def restore_memory_archive(self, device: str, archive_id: int, expected_version: int = None):
        now = time.time()
        with self._lock:
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                owner_row = c.execute("SELECT owner_id FROM device_owners WHERE device=?", (device,)).fetchone()
                owner = owner_row[0] if owner_row else device
                archived = c.execute("SELECT owner_id,summary,pinned FROM memory_archives WHERE id=? AND device=?", (int(archive_id), device)).fetchone()
                current = c.execute("SELECT version FROM device_memory WHERE device=?", (device,)).fetchone()
                version = current[0] if current else 0
                if not archived or archived[0] != owner or (expected_version is not None and int(expected_version) != version):
                    c.execute("ROLLBACK")
                    return False, self.memory(device)
                c.execute("INSERT OR IGNORE INTO device_memory(device,summary,version,source,updated,pinned,owner_id,owner_epoch,summary_epoch) "
                          "VALUES(?,'',0,'',?,'',?, ?,0)", (device, now, owner, self.owner_epoch(device)))
                c.execute("UPDATE device_memory SET summary=?,pinned=?,version=version+1,summary_epoch=summary_epoch+1,source='archive_restored',updated=?,owner_id=? WHERE device=?",
                          (archived[1], archived[2], now, owner, device))
                epoch = self.owner_epoch(device)
                c.execute("INSERT INTO memory_progress(device,model,upto) "
                          "SELECT device,model,MAX(id) FROM messages WHERE device=? AND owner_epoch=? GROUP BY model "
                          "ON CONFLICT(device,model) DO UPDATE SET upto=MAX(memory_progress.upto,excluded.upto)", (device, epoch))
                c.execute("COMMIT")
                return True, self.memory(device)
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def clear(self, device) -> int:
        """删这台设备的记录、总结进度，记忆清空（留着那一行并把版本加 1：正在进行的总结写不回来）。"""
        n = self._exec("DELETE FROM messages WHERE device=?", (device,)).rowcount
        self._exec("DELETE FROM memory_progress WHERE device=?", (device,))
        if self.memory(device)["version"]:
            self.set_memory(device, "", "cleared", field='both')
        return n

    def clear_memory(self, device) -> None:
        """清空两类长期记忆，并跳过清空前的全部聊天记录。"""
        self.set_memory(device, "", "cleared", field='both', forget_before=True)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(r) -> dict:
    return {"id": r[0], "role": r[1], "content": r[2], "tool_calls": json.loads(r[3]) if r[3] else None,
            "tool_call_id": r[4], "created": r[5], "retention": r[6] if len(r) > 6 else '', "expires_at": r[7] if len(r) > 7 else None,
            "pi_message": json.loads(r[8]) if len(r)>8 and r[8] else None}


def db() -> ChatDB:
    global _db
    with _db_lock:
        if _db is None:
            _db = ChatDB(Path(get_project_dir()) / "data" / "chat.db")
        return _db


def set_db(instance: Optional[ChatDB]) -> None:
    """测试用：换成临时库。"""
    global _db
    with _db_lock:
        _db = instance


# ---------- 截断窗口 ----------

def estimate_tokens(text) -> int:
    """粗估 token：中文等非 ASCII 字符按 1 个算，ASCII 按 4 个字符 1 个算。不同模型的分词不一样，只求量级对。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    wide = sum(1 for c in text if ord(c) > 127)
    return wide + (len(text) - wide + 3) // 4


def _msg_tokens(m) -> int:
    get = m.get if isinstance(m, dict) else lambda k: getattr(m, k, None)
    return estimate_tokens(get("content")) + estimate_tokens(get("tool_calls")) + 4


def select_window(messages: list, max_messages: int, max_tokens: int) -> int:
    """返回窗口从第几条开始：按"一句用户的话 + 它后面的回复、工具调用和结果"成组，从新往旧装，
    超过条数或 token 上限就停。最新的一组总是带上（哪怕它自己就超了）。messages 是 Message 或 dict。"""
    if not messages:
        return 0
    role = (lambda m: m.get("role")) if isinstance(messages[0], dict) else (lambda m: m.role)
    starts = [i for i, m in enumerate(messages) if role(m) == "user"]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    start, count, tokens = len(messages), 0, 0
    for s in reversed(starts):
        group = messages[s:start]
        n, t = len(group), sum(_msg_tokens(m) for m in group)
        if start < len(messages) and ((max_messages > 0 and count + n > max_messages)
                                      or (max_tokens > 0 and tokens + t > max_tokens)):
            break
        start, count, tokens = s, count + n, tokens + t
    # 窗口开头不能是助手或工具消息（截出来的残组）：往后挪到第一句用户的话
    while start < len(messages) and role(messages[start]) != "user" and start != starts[-1]:
        nxt = next((s for s in starts if s > start), None)
        if nxt is None:
            break
        start = nxt
    return start


# ---------- 载入、同步 ----------

def _storable(m) -> bool:
    return m.role != "system"


def load(conn, dialogue, name: str) -> None:
    """把这台设备这个模型的记录接到对话上（新连接、第一次切到这个模型时调用）。"""
    from core.utils.dialogue import Message

    config = _conn_config(conn)
    cfg = settings(config)
    device = device_of(conn)
    if not cfg["enabled"] or not device or not name:
        return
    store = db()
    epoch = store.owner_epoch(device)
    if getattr(dialogue, "chat_key", None) == (device, name) and getattr(dialogue, "chat_epoch", 0) == epoch:
        return
    dialogue.chat_key = (device, name)
    dialogue.chat_epoch = epoch
    dialogue.chat_ids = set()
    dialogue.window = (int(cfg["max_messages"]), int(cfg["max_tokens"]))
    try:
        rows = store.recent(device, name, max(int(cfg["max_messages"]), 1))
    except Exception as e:
        logger.bind(tag=TAG).error(f"读取聊天记录失败: {e}")
        return
    loaded = []
    for r in rows:
        m = Message(role=r["role"], content=r["content"], tool_calls=r["tool_calls"], tool_call_id=r["tool_call_id"])
        m.db_id, m.db_content = r["id"], r["content"]
        m.retention, m.expires_at = r['retention'], r['expires_at']
        m.pi_message = r.get('pi_message')
        dialogue.chat_ids.add(r["id"])
        loaded.append(m)
    # 接在系统提示后面（顺序其实无所谓：发给模型时系统提示和对话分开取）
    dialogue.dialogue[:] = [m for m in dialogue.dialogue if not _storable(m)] + loaded + \
        [m for m in dialogue.dialogue if _storable(m)]
    if loaded:
        logger.bind(tag=TAG).info(f"设备 {device} 模型 {name}：载入 {len(loaded)} 条聊天记录")


def sync(conn, dialogue=None) -> None:
    """把对话里新增、改过、删掉的消息同步进库。"""
    dialogue = dialogue or getattr(conn, "dialogue", None)
    key = getattr(dialogue, "chat_key", None)
    if not key:
        return
    device, name = key
    with _sync_lock:
        try:
            store = db()
            epoch = getattr(dialogue, "chat_epoch", 0)
            if epoch != store.owner_epoch(device):
                # An owner switch invalidates live conversation caches. Never persist old context into the new epoch.
                return
            # A retention pass may have removed rows while this connection stayed open.
            existing_ids = {m.db_id for m in dialogue.dialogue if getattr(m, 'db_id', None) is not None}
            live = store.metadata(existing_ids | dialogue.chat_ids)
            dialogue.dialogue[:] = [m for m in dialogue.dialogue
                                    if getattr(m, 'db_id', None) is None or m.db_id in live]
            dialogue.chat_ids.intersection_update(live)
            present = set()
            for m in list(dialogue.dialogue):
                if not _storable(m):
                    continue
                msg_id = getattr(m, "db_id", None)
                if msg_id is None:
                    m.db_id = store.insert(device, name, m.role, m.content, m.tool_calls, m.tool_call_id,
                                           pi_message=getattr(m,'pi_message',None), owner_epoch=epoch)
                    m.db_content = m.content
                    dialogue.chat_ids.add(m.db_id)
                elif m.content != getattr(m, "db_content", None):
                    store.update_content(msg_id, m.content)
                    m.db_content = m.content
                    m.pi_message = None
                present.add(m.db_id)
            gone = dialogue.chat_ids - present
            if gone:
                store.delete_ids(gone)
                dialogue.chat_ids -= gone
            store.classify_turns(device, name, _conn_config(conn))
            metadata = store.metadata(present)
            for message in dialogue.dialogue:
                if getattr(message, 'db_id', None) in metadata:
                    message.retention, message.expires_at = metadata[message.db_id]
        except Exception as e:
            logger.bind(tag=TAG).error(f"保存聊天记录失败: {e}")


def sync_all(conn) -> None:
    """连接关闭时：这个连接里每个模型的对话都同步一遍。"""
    st = getattr(conn, "hub_route", None) or {}
    seen = set()
    for d in [getattr(conn, "dialogue", None)] + list((st.get("dialogues") or {}).values()):
        if d is not None and id(d) not in seen:
            seen.add(id(d))
            sync(conn, d)


# ---------- 记忆 ----------
# 记的是"关于用户"的事，同一台设备的所有模型共用一份（切模型后也知道）。
# 记录本身仍按设备 + 模型分开存、分开截窗口；总结进度按模型记（memory_progress），一次总结把各模型截掉的部分一起合进去。
# 同一台设备同一时间只跑一个总结（_summarizing 按设备）；写回时比对版本，期间被后台手改或清除过就丢弃这次结果。

def memory_for(conn) -> str:
    key = getattr(getattr(conn, "dialogue", None), "chat_key", None)
    config = _conn_config(conn)
    if not key or not settings(config)["use_memory"]:
        return ""
    try:
        context = memory_context_for(conn)
        parts = []
        if context['pinned']: parts.append('用户手动保留：\n' + context['pinned'])
        if context['preferences']:
            parts.append('用户明确偏好：\n' + '\n'.join(f"- {p['topic']}：{p['content']}" for p in context['preferences']))
        if context['summary']: parts.append('从对话整理的长期信息：\n' + context['summary'])
        return '\n\n'.join(parts)
    except Exception as e:
        logger.bind(tag=TAG).error(f"读取记忆失败: {e}")
        return ""


def _transcript(rows: List[dict]) -> str:
    lines, model = [], None
    for r in rows:
        if r.get("model") != model:
            model = r.get("model")
            lines.append(f"（和 {model} 的对话）")
        content = _display_text(r["content"]) if r["role"] in ("user", "assistant") else (r["content"] or "")
        if r["role"] == "user":
            when = time.strftime('%Y-%m-%d', time.gmtime(r['created'] + 8 * 3600))
            lines.append(f"用户（{when}）：{content}")
        elif r["role"] == "assistant" and r["tool_calls"]:
            names = "、".join(((c.get("function") or {}).get("name") or "") for c in r["tool_calls"] if isinstance(c, dict))
            lines.append(f"助手调用了工具：{names}")
        elif r["role"] == "assistant":
            lines.append(f"助手：{content}")
        elif r["role"] == "tool":
            lines.append(f"工具结果：{content[:300]}")
    return "\n".join(lines)


def pending_summary(device: str, cfg: dict):
    """各模型被窗口截掉、还没总结的消息：(rows（带 model，按模型再按 id 排）, 已有记忆)。"""
    store = db()
    mem = store.memory(device)
    progress = store.progress(device)
    batch = []
    for name in sorted(store.models(device)):
        rows = store.after(device, name, progress.get(name, 0))
        start = select_window(rows, int(cfg["max_messages"]), int(cfg["max_tokens"]))
        batch += [{**r, "model": name} for r in rows[:start]]
    return batch[:SUMMARY_BATCH], mem


def summarize_now(llm, device: str, cfg: dict) -> bool:
    """同步执行一次总结。返回有没有更新记忆。"""
    rows, mem = pending_summary(device, cfg)
    store = db()
    if not rows:
        return False
    relevant = [r for r in rows if r.get('retention') not in memory_policy.TEMPORARY]
    text = mem['summary']
    if relevant:
        forgotten = store.forgotten_topics(store.owner_for_device(device))
        forgotten_context = '、'.join(forgotten) if forgotten else '无'
        prompt = (f"【已明确遗忘的主题】\n{forgotten_context}\n不要从新对话中自动恢复这些主题，除非用户再次明确要求记住。\n\n"
                  f"【已有自动记忆】\n{mem['summary'] or '无'}\n\n【手动保留】\n{mem['pinned'] or '无'}\n\n【较早的对话】\n{_transcript(relevant)}")
        text = (llm.response_no_stream(SUMMARY_PROMPT.format(chars=cfg["summary_chars"]), prompt) or '').strip()
        if not text: return False
        if text in ("无", "无。"):
            text = mem["summary"]
    limit = int(cfg["summary_chars"]) * 2
    progress = {}
    for r in rows:
        progress[r["model"]] = max(progress.get(r["model"], 0), r["id"])
    if not store.exists(rows[-1]["id"]):
        return False  # 这批记录已被清除
    if not store.set_memory(device, text[:limit], "summary", expect_version=mem["version"],
                            expect_epoch=mem.get("summary_epoch", 0), progress=progress):
        logger.bind(tag=TAG).info(f"设备 {device}：总结期间记忆被修改或清除，这次的结果不要了")
        return False
    logger.bind(tag=TAG).info(f"设备 {device}：总结了 {len(rows)} 条较早的对话（{'、'.join(progress)}），记忆 {len(text)} 字")
    return True


def maybe_summarize(conn) -> None:
    """一轮结束后：截掉的旧对话攒够了，就在后台总结一次。"""
    key = getattr(getattr(conn, "dialogue", None), "chat_key", None)
    cfg = settings(_conn_config(conn))
    if not key or not cfg["summary"]:
        return
    device = key[0]
    with _summarizing_lock:
        if device in _summarizing:
            return
        _summarizing.add(device)

    def run():
        try:
            rows, _ = pending_summary(device, cfg)
            if len(rows) < int(cfg["summary_min"]):
                return
            llm = _summary_llm(conn, key[1], cfg)
            if llm is not None:
                summarize_now(llm, device, cfg)
        except Exception as e:
            logger.bind(tag=TAG).error(f"总结聊天记录失败: {e}")
        finally:
            with _summarizing_lock:
                _summarizing.discard(device)

    threading.Thread(target=run, name="chat-summary", daemon=True).start()


def set_memory(device: str, summary: str, expect_version: int = None, kind: str = 'pinned'):
    """管理后台手改记忆：马上生效（每次请求都从库里读），正在进行的总结写不回来。

    expect_version：打开编辑时的版本；期间自动总结更新过就不保存。返回 (保存了没有, 现在的记忆)。"""
    summary = (summary or "").strip()
    if kind not in ('pinned', 'summary'): raise ValueError('记忆类型不正确')
    ok = db().set_memory(device, summary, "manual" if summary else "cleared", expect_version=expect_version,
                         field=kind, forget_before=(kind == 'summary' or not summary))
    if ok:
        logger.bind(tag=TAG).info(f"设备 {device}：记忆已在管理后台{'修改' if summary else '清空'}（{len(summary)} 字）")
    return ok, db().memory(device)


def owner_for_device(device_id: str) -> str:
    """Read-only owner resolution; unmapped devices retain their own namespace."""
    return db().owner_for_device(str(device_id or ""))


def owner_mappings() -> list:
    return db().owner_mappings()


def set_device_owner(device_id: str, owner_id: str, expected_version: int = None):
    return db().set_device_owner(device_id, owner_id, expected_version)


def list_preferences(owner_id: str) -> list:
    return db().list_preferences(owner_id)


def upsert_preference(owner_id: str, topic: str, content: str, preference_id: int = None,
                      expected_version: int = None, source: str = 'explicit', explicit: bool = True):
    return db().upsert_preference(owner_id, topic, content, preference_id, expected_version, source, explicit)


def delete_preference(owner_id: str, preference_id: int, expected_version: int = None):
    return db().delete_preference(owner_id, preference_id, expected_version)


def memory_archives(device_id: str) -> list:
    return db().memory_archives(device_id)


def restore_memory_archive(device_id: str, archive_id: int, expected_version: int = None):
    return db().restore_memory_archive(device_id, archive_id, expected_version)


def append_operation_event(owner_id: str, device_id: str, action: str, target: str, result: str,
                           actor: str = 'assistant', confirmed: bool = False):
    return db().append_operation_event(owner_id, device_id, action, target, result, actor, confirmed)


def recent_operation_events_for(conn, limit: int = 5) -> list:
    context = memory_context_for(conn)
    return [] if context['stale'] or not context['owner_id'] else db().recent_operation_events(context['owner_id'], limit)


def append_operation_event_for(conn, action: str, target: str, result: str,
                              actor: str = 'assistant', confirmed: bool = False):
    context = memory_context_for(conn)
    if context['stale'] or not context['device_id']:
        return None
    return append_operation_event(context['owner_id'], context['device_id'], action, target, result,
                                  actor=actor, confirmed=confirmed)


def upsert_preference_for(conn, topic: str, content: str, preference_id: int = None,
                          expected_version: int = None, source: str = 'explicit'):
    context = memory_context_for(conn)
    if context['stale'] or not context['owner_id']:
        return False, None
    return upsert_preference(context['owner_id'], topic, content, preference_id,
                             expected_version, source=source, explicit=True)


def delete_preference_for(conn, preference_id: int, expected_version: int = None):
    context = memory_context_for(conn)
    if context['stale'] or not context['owner_id']:
        return False, None
    return delete_preference(context['owner_id'], preference_id, expected_version)


def memory_context_for(conn) -> dict:
    """Trusted context for tools and prompts; owner/device never come from model arguments."""
    key = getattr(getattr(conn, 'dialogue', None), 'chat_key', None)
    if not key:
        return {"owner_id": "", "device_id": "", "owner_epoch": 0, "stale": False,
                "preferences": [], "pinned": "", "summary": "", "events": []}
    device = key[0]
    store = db()
    epoch = store.owner_epoch(device)
    stale = getattr(conn.dialogue, 'chat_epoch', epoch) != epoch
    if stale:
        _rebase_dialogue_owner(conn.dialogue, epoch)
    authenticated_owner = getattr(conn, "authenticated_owner_id", None)
    mapped_owner = store.owner_for_device(device)
    owner = authenticated_owner or mapped_owner
    memory = store.memory(device)
    if authenticated_owner and authenticated_owner != mapped_owner:
        # Device-scoped legacy summary may belong to the old unauthenticated
        # grouping; never expose it to a newly paired principal.
        memory = {**memory, "pinned": "", "summary": ""}
    preferences = [p for p in store.list_preferences(owner) if p['explicit']]
    final_epoch = store.owner_epoch(device)
    if final_epoch != epoch:
        _rebase_dialogue_owner(conn.dialogue, final_epoch)
        current_owner = authenticated_owner or store.owner_for_device(device)
        return {"owner_id": current_owner, "authenticated_owner_id": authenticated_owner,
                "device_id": device, "owner_epoch": final_epoch, "stale": True,
                "preferences": [], "pinned": "", "summary": "", "events": []}
    return {"owner_id": owner, "authenticated_owner_id": authenticated_owner,
            "device_id": device, "owner_epoch": epoch, "stale": stale,
            "preferences": preferences,
            "pinned": memory['pinned'], "summary": memory['summary'],
            "events": store.recent_operation_events(owner, 5)}


def _rebase_dialogue_owner(dialogue, epoch: int) -> None:
    """Drop cached prior-owner turns while retaining the new request's latest user message."""
    with _sync_lock:
        system = [m for m in dialogue.dialogue if getattr(m, 'role', None) == 'system']
        latest_user = next((m for m in reversed(dialogue.dialogue)
                            if getattr(m, 'role', None) == 'user' and getattr(m, 'db_id', None) is None), None)
        if latest_user is not None:
            for attr, value in (("db_id", None), ("db_content", None), ("pi_message", None),
                                ("retention", ""), ("expires_at", None)):
                if hasattr(latest_user, attr):
                    setattr(latest_user, attr, value)
        dialogue.dialogue[:] = system + ([latest_user] if latest_user is not None else [])
        dialogue.chat_ids = set()
        dialogue.chat_epoch = epoch


def describe_memories() -> list:
    """管理后台用：每台存过记录或记忆的设备，[{device, memory, models: [{entry, label, count}]}]。"""
    from core.agent import llm_router

    store = db()
    mappings = {row['device']: row for row in store.owner_mappings()}
    out = []
    for device in store.memory_devices():
        epoch = store.owner_epoch(device)
        owner = store.owner_for_device(device)
        counts = store._exec("SELECT model, COUNT(*) FROM messages WHERE device=? AND owner_epoch=? GROUP BY model", (device, epoch)).fetchall()
        temporary = store._exec('SELECT retention,COUNT(*) FROM messages WHERE device=? AND owner_epoch=? AND expires_at IS NOT NULL GROUP BY retention', (device, epoch)).fetchall()
        expiry = store._exec('SELECT MIN(expires_at) FROM messages WHERE device=? AND owner_epoch=? AND expires_at IS NOT NULL', (device, epoch)).fetchone()[0]
        mapping = mappings.get(device) or {"version": 0}
        out.append({"device": device, "memory": store.memory(device),
                    "owner": owner, "owner_version": mapping['version'], "owner_epoch": epoch,
                    "preferences": store.list_preferences(owner), "archives": store.memory_archives(device),
                    "temporary": sum(n for _, n in temporary), "expires_at": expiry,
                    "models": [{"entry": m, "label": llm_router.model_label(m), "count": n}
                               for m, n in counts]})
    return out


def prune_expired(dialogue):
    """Expired turns must not reach a model even if the connection spans midnight."""
    now = time.time()
    with _sync_lock:
        dialogue.dialogue[:] = [m for m in dialogue.dialogue
                                if not getattr(m, 'expires_at', None) or m.expires_at > now]


async def retention_loop(config):
    """One pass at startup and every minute. No requests to models or external services."""
    import asyncio
    while True:
        try:
            count = await asyncio.to_thread(db().cleanup_expired, config)
            if count: logger.bind(tag=TAG).info(f'按日清理临时对话：{count} 条消息')
        except Exception as exc:
            logger.bind(tag=TAG).error(f'临时对话清理未完成: {type(exc).__name__}')
        await asyncio.sleep(60)


def _summary_llm(conn, name: str, cfg: dict):
    from core.agent import llm_router

    target = cfg.get("summary_llm") or name
    config = _conn_config(conn)
    if not llm_router.model_ready(config, target):
        return None
    return llm_router.get_instance(conn, target)


# ---------- 显示 ----------

def _display_text(content) -> str:
    """用户的话可能是带说话人的 JSON（{"speaker", "content"}），只取正文。"""
    text = content or ""
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "content" in data:
                return str(data["content"])
        except ValueError:
            pass
    return text


def history(device: str, limit: int, models: List[str] = None) -> Dict[str, list]:
    """给屏幕显示的记录：{模型: [{id, role, text, time, interrupted}]}，只有用户的话和助手的回复。"""
    from core.utils.textUtils import check_emoji
    from core.agent import media_output, stickers, choice_cards

    store = db()
    out = {}
    for name in models or store.models(device):
        items = []
        media_calls = set()
        choice_calls = set()
        # 多取一些：工具消息不显示
        for r in store.recent(device, name, limit * 3):
            for call in r.get("tool_calls") or []:
                if (call.get('function') or {}).get('name') == choice_cards.TOOL_NAME:
                    choice_calls.add(call.get('id'))
                if (call.get("function") or {}).get("name") in (media_output.TOOL_NAME, stickers.TOOL_NAME):
                    media_calls.add(call.get("id"))
            if r["role"] == "tool" and r.get("tool_call_id") in media_calls:
                media = media_output.from_tool_result(r["content"])
                if media:
                    items.append({"id": r["id"], "role": "bot", "text": "", "media": media,
                                  "time": r["created"], "interrupted": False})
            if r['role'] == 'tool' and r.get('tool_call_id') in choice_calls:
                card = choice_cards.history_card(r['content'], device, name)
                if card:
                    items.append({'id': r['id'], 'role': 'bot', 'text': '', 'choice': card,
                                  'time': r['created'], 'interrupted': False})
            if r["role"] not in ("user", "assistant") or r["tool_calls"]:
                continue
            text = _display_text(r["content"])
            if not text:
                continue
            interrupted = r["role"] == "assistant" and text.endswith(CUT_MARK)
            if r["role"] == "assistant":
                text = check_emoji(text).strip()
                if interrupted and text != CUT_MARK:
                    text = text[:-len(CUT_MARK)]
            items.append({"id": r["id"], "role": "user" if r["role"] == "user" else "bot", "text": text,
                          "time": r["created"], "interrupted": interrupted})
        if items:
            out[name] = items[-limit:]
    return out


# ---------- 清除 ----------

def _clear_dialogue(dialogue) -> None:
    with _sync_lock:
        dialogue.dialogue[:] = [m for m in dialogue.dialogue if not _storable(m)]
        if getattr(dialogue, "chat_key", None):
            dialogue.chat_ids = set()
            dialogue.chat_epoch = db().owner_epoch(dialogue.chat_key[0])


def clear_dialogues(connections) -> None:
    """清在线连接里各模型的对话（留系统提示）。"""
    for conn in connections:
        st = getattr(conn, "hub_route", None) or {}
        dialogues = [getattr(conn, "dialogue", None)] + list((st.get("dialogues") or {}).values())
        for d in dialogues:
            if d is not None:
                _clear_dialogue(d)
        reply_turns = getattr(conn, "reply_turns", None)
        if reply_turns:
            reply_turns.clear()


def clear(device: str, memory_only: bool = False, connections=None) -> dict:
    """清除这台设备存的聊天记录（所有模型）和记忆。connections：在线连接列表。返回 {"deleted": 条数}。"""
    store = db()
    if memory_only:
        store.clear_memory(device)
        logger.bind(tag=TAG).info(f"设备 {device}：已清除记忆")
        return {"deleted": 0}
    deleted = store.clear(device)
    clear_dialogues(connections or [])
    logger.bind(tag=TAG).info(f"设备 {device}：已清除本机存的 {deleted} 条聊天记录和记忆")
    return {"deleted": deleted}
