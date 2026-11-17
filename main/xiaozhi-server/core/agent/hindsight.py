"""Explicit, owner-scoped Hindsight tools. No automatic conversation export or recall."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


class HindsightError(ValueError):
    def __init__(self, code, message, receipt=None):
        super().__init__(message)
        self.code = code
        self.receipt = receipt


_CREDENTIAL = re.compile(
    r"密码|口令|密钥|令牌|凭据|私钥|\b(?:password|passwd|secret|credentials?|bearer|authorization)\b"
    r"|(?:api|access|refresh|auth|session)[_ -]?(?:key|token)\b|\bprivate[_ -]?key\b"
    r"|\b(?:sk|ghp|gho|github_pat)[_-][A-Za-z0-9_-]{12,}|\bAIza[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|-----BEGIN [^-]*PRIVATE KEY-----|https?://[^/\s:@]+:[^/\s@]+@"
    r"|[?&](?:key|token|password)=[^&\s]+|\b(?:token|pwd)\s*[:=]",
    re.I,
)
_REMOTE_STATES = {'pending', 'processing', 'completed', 'failed', 'cancelled', 'not_found'}
_DELETE_STATES = {'deleting', 'delete_unknown', 'deleted'}
_SOURCE_TAG = 'source:xiaozhi-hub'
_CACHE = {}
_CACHE_LOCK = threading.RLock()
_CACHE_EPOCH = 0


def _text(value, field, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise HindsightError('invalid_input', f'{field}不能为空或过长')
    return value.strip()


def _safe_text(value, field, maximum):
    value = _text(value, field, maximum)
    if _CREDENTIAL.search(value):
        raise HindsightError('sensitive_input', '共享记忆不接受密码、密钥或令牌相关内容')
    return value


def _number(config, key, default, minimum, maximum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HindsightError('invalid_config', f'Hindsight {key} 配置无效')
    if not minimum <= value <= maximum:
        raise HindsightError('invalid_config', f'Hindsight {key} 超出范围')
    return value


class HindsightClient:
    def __init__(self, config):
        cfg = config.get('hindsight') or {}
        if cfg.get('enabled') is not True:
            raise HindsightError('disabled', '共享记忆尚未启用')
        base = _text(cfg.get('base_url'), '共享记忆服务地址', 2048)
        parsed = urlsplit(base)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise HindsightError('invalid_config', '共享记忆服务地址无效')
        self.base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))
        self.bank = _text(cfg.get('bank_id', 'xiaozhi'), '记忆库', 200)
        if self.bank.casefold() == 'hermes' or self.bank.casefold().startswith('hermes::'):
            raise HindsightError('restricted_bank', '音箱不能访问 Hermes 凭据记忆库')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', self.bank):
            raise HindsightError('invalid_config', '记忆库标识无效')
        self.timeout = float(_number(cfg, 'timeout_seconds', 15, 1, 60))
        self.max_tokens = int(_number(cfg, 'max_tokens', 300, 1, 2000))
        self.cache_seconds = float(_number(cfg, 'cache_seconds', 60, 0, 300))
        self.scope = hashlib.sha256(f'{self.base}\0{self.bank}'.encode()).hexdigest()
        self.path = Path(cfg.get('receipt_path') or Path(__file__).resolve().parents[2] / 'data' / 'hindsight_operations.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create privately before SQLite opens it; receipts never contain source text.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS hindsight_operations (
                scope TEXT NOT NULL, operation_id TEXT NOT NULL, owner TEXT NOT NULL,
                document_id TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(scope, operation_id), UNIQUE(scope, document_id))''')

    @contextmanager
    def _db(self):
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _owner(self, owner):
        digest = hashlib.sha256(_text(owner, '所有者', 300).encode()).hexdigest()
        return digest, 'owner:' + digest

    def _invalidate(self, digest):
        global _CACHE_EPOCH
        with _CACHE_LOCK:
            _CACHE_EPOCH += 1
            for key in list(_CACHE):
                if key[:2] == (self.scope, digest):
                    _CACHE.pop(key, None)

    def _removed_docs(self, digest):
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT document_id FROM hindsight_operations WHERE scope=? AND owner=? AND status IN ('deleting','delete_unknown','deleted')", (self.scope, digest))}

    def _request(self, method, path, payload=None, allow_missing=False):
        url = self.base + '/v1/default/banks/' + quote(self.bank, safe='') + path
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, json=payload) as response:
                    if allow_missing and response.status_code == 404:
                        return None
                    if not 200 <= response.status_code < 300:
                        raise HindsightError('service_http', f'共享记忆服务返回 HTTP {response.status_code}')
                    raw = bytearray()
                    for part in response.iter_bytes():
                        raw.extend(part)
                        if len(raw) > 2 * 1024 * 1024:
                            raise HindsightError('invalid_response', '共享记忆返回内容过大')
                    data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except HindsightError:
            raise
        except httpx.HTTPError:
            raise HindsightError('service_unavailable', '共享记忆服务暂时无法连接') from None
        except (ValueError, UnicodeError):
            raise HindsightError('invalid_response', '共享记忆返回格式无效') from None

    def _receipt(self, digest, operation_id=None, document_id=None):
        field, value = ('operation_id', operation_id) if operation_id else ('document_id', document_id)
        with self._db() as db:
            row = db.execute(f'SELECT * FROM hindsight_operations WHERE scope=? AND owner=? AND {field}=?',
                             (self.scope, digest, value)).fetchone()
        if row is None:
            raise HindsightError('not_owned', '没有找到属于当前用户的共享记忆操作')
        return dict(row)

    def _set_status(self, operation_id, status, deleting=False):
        with self._db() as db:
            clause = '' if deleting else " AND status NOT IN ('deleting','delete_unknown','deleted')"
            db.execute('UPDATE hindsight_operations SET status=?,updated=? WHERE scope=? AND operation_id=?' + clause,
                       (status, time.time(), self.scope, operation_id))

    @staticmethod
    def _result(row):
        return {'ok': True, 'operation_id': row['operation_id'], 'document_id': row['document_id'],
                'status': row['status'], 'completed': row['status'] == 'completed'}

    def recall(self, owner, query):
        digest, tag = self._owner(owner)
        query = _safe_text(query, '查询', 2000)
        key = (self.scope, digest, query, self.max_tokens)
        with _CACHE_LOCK:
            epoch = _CACHE_EPOCH
            cached = _CACHE.get(key)
            if cached and time.monotonic() - cached[0] < self.cache_seconds:
                removed = self._removed_docs(digest)
                return {'ok': True, 'facts': copy.deepcopy([f for f in cached[1] if f['document_id'] not in removed]), 'cached': True}
        data = self._request('POST', '/memories/recall', {
            'query': query, 'max_tokens': self.max_tokens, 'budget': 'low',
            'types': ['world', 'experience'], 'tags': [tag], 'tags_match': 'all_strict'})
        rows = data.get('results')
        if not isinstance(rows, list):
            raise HindsightError('invalid_response', '共享记忆未返回事实列表')
        removed = self._removed_docs(digest)
        facts = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('tags'), list) or tag not in row['tags']:
                continue
            if any(isinstance(t, str) and t.startswith('owner:') and t != tag for t in row['tags']):
                continue
            text, ident, document = row.get('text'), row.get('id'), row.get('document_id')
            if (row.get('type') not in ('world', 'experience') or not isinstance(text, str)
                    or not text.strip() or len(text) > 12000 or _CREDENTIAL.search(text)
                    or not isinstance(ident, str) or not isinstance(document, str)
                    or len(ident) > 200 or len(document) > 200 or _CREDENTIAL.search(ident + ' ' + document)
                    or document in removed):
                continue
            facts.append({'text': text.strip(), 'id': ident, 'document_id': document})
        with _CACHE_LOCK:
            if epoch == _CACHE_EPOCH:
                if len(_CACHE) >= 256:
                    _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]), None)
                _CACHE[key] = (time.monotonic(), copy.deepcopy(facts))
        return {'ok': True, 'facts': facts, 'cached': False}

    def retain(self, owner, content, request_id):
        digest, tag = self._owner(owner)
        content = _safe_text(content, '记忆内容', 12000)
        request_id = _text(request_id, '请求标识', 200)
        operation = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([owner.strip(), request_id], ensure_ascii=False)))
        document = 'xiaozhi-' + digest[:16] + '-' + uuid.UUID(operation).hex
        fingerprint = hashlib.sha256(content.encode()).hexdigest()
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO hindsight_operations VALUES (?,?,?,?,?,?,?)',
                       (self.scope, operation, digest, document, fingerprint, 'submitting', time.time()))
        receipt = self._receipt(digest, operation_id=operation)
        if receipt['content_hash'] != fingerprint:
            raise HindsightError('idempotency_conflict', '同一请求标识不能用于不同记忆内容')
        if receipt['status'] in _DELETE_STATES:
            raise HindsightError('already_deleted', '该记忆已进入删除流程，不能重新写入')
        if receipt['status'] == 'completed':
            return {**self._result(receipt), 'accepted': True}
        self._invalidate(digest)
        try:
            response = self._request('POST', '/memories', {
                'async': True, 'operation_id': operation,
                'items': [{'content': content, 'context': '用户明确要求保存的音箱共享记忆',
                           'document_id': document, 'tags': [tag, _SOURCE_TAG],
                           'observation_scopes': 'combined'}]})
            if (response.get('success') is not True or response.get('operation_id') != operation
                    or response.get('bank_id') != self.bank or response.get('async') is not True
                    or isinstance(response.get('items_count'), bool) or response.get('items_count') != 1):
                raise HindsightError('invalid_response', '共享记忆写入未确认，请查询操作状态')
        except HindsightError as exc:
            self._set_status(operation, 'unknown')
            exc.receipt = {**self._result(self._receipt(digest, operation_id=operation)),
                           'ok': False, 'accepted': False, 'error': str(exc)}
            raise
        self._set_status(operation, 'pending')
        self._invalidate(digest)
        return {**self._result(self._receipt(digest, operation_id=operation)), 'accepted': True}

    def status(self, owner, operation_id):
        digest, _ = self._owner(owner)
        operation_id = _text(operation_id, '操作标识', 100)
        receipt = self._receipt(digest, operation_id=operation_id)
        if receipt['status'] in _DELETE_STATES:
            return self._result(receipt)
        data = self._request('GET', '/operations/' + quote(operation_id, safe='') + '?include_payload=false', allow_missing=True)
        if data is not None and (data.get('operation_id', operation_id) != operation_id or data.get('bank_id', self.bank) != self.bank):
            raise HindsightError('invalid_response', '共享记忆操作归属无效')
        state = 'not_found' if data is None else data.get('status')
        if state not in _REMOTE_STATES:
            raise HindsightError('invalid_response', '共享记忆操作状态无效')
        self._set_status(operation_id, state)
        self._invalidate(digest)
        return self._result(self._receipt(digest, operation_id=operation_id))

    def forget(self, owner, document_id):
        digest, tag = self._owner(owner)
        document_id = _text(document_id, '文档标识', 100)
        if not re.fullmatch('xiaozhi-' + digest[:16] + '-[a-f0-9]{32}', document_id):
            raise HindsightError('not_owned', '不能删除其他用户或其他应用的共享记忆')
        receipt = self._receipt(digest, document_id=document_id)
        if receipt['status'] == 'deleted':
            return {'ok': True, 'document_id': document_id, 'deleted': True, 'memory_units_deleted': 0}
        operation = self._request('GET', '/operations/' + quote(receipt['operation_id'], safe='') + '?include_payload=false', allow_missing=True)
        if operation is None or operation.get('status') != 'completed':
            raise HindsightError('retain_incomplete', '请等待记忆写入完成后再删除，避免后台写入重新恢复内容')
        path = '/documents/' + quote(document_id, safe='')
        data = self._request('GET', path, allow_missing=True)
        if data is None and receipt['status'] in ('deleting', 'delete_unknown'):
            self._set_status(receipt['operation_id'], 'deleted', deleting=True)
            self._invalidate(digest)
            return {'ok': True, 'document_id': document_id, 'deleted': True, 'memory_units_deleted': 0}
        if (data is None or data.get('id') != document_id or data.get('bank_id') != self.bank
                or not isinstance(data.get('tags'), list) or tag not in data['tags']
                or _SOURCE_TAG not in data['tags']):
            raise HindsightError('not_owned', '共享记忆文档归属无法确认，未执行删除')
        self._set_status(receipt['operation_id'], 'deleting', deleting=True)
        self._invalidate(digest)
        try:
            response = self._request('DELETE', path)
            count = response.get('memory_units_deleted')
            if (response.get('success') is not True or response.get('document_id') != document_id
                    or isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise HindsightError('invalid_response', '共享记忆删除尚未确认，请重试查询')
        except HindsightError:
            self._set_status(receipt['operation_id'], 'delete_unknown', deleting=True)
            raise
        self._set_status(receipt['operation_id'], 'deleted', deleting=True)
        self._invalidate(digest)
        return {'ok': True, 'document_id': document_id, 'deleted': True, 'memory_units_deleted': count}


__all__ = ['HindsightClient', 'HindsightError']
