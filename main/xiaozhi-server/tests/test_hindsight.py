import hashlib
import json
import sqlite3

import httpx
import pytest

from core.agent import hindsight as hs


@pytest.fixture(autouse=True)
def reset_cache():
    hs._CACHE.clear()


@pytest.fixture
def config(tmp_path):
    return {'hindsight': {'enabled': True, 'base_url': 'http://192.0.2.60:40100',
                         'bank_id': 'xiaozhi', 'receipt_path': str(tmp_path / 'receipts.db')}}


def mock_http(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(hs.httpx, 'Client', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def tag(owner):
    return 'owner:' + hashlib.sha256(owner.encode()).hexdigest()


def fact(owner='alice', text='用户偏好简短回答', doc='doc-1'):
    return {'id': 'fact-1', 'text': text, 'document_id': doc, 'type': 'world', 'tags': [tag(owner)]}


def accepted(request):
    body = json.loads(request.content)
    return httpx.Response(200, json={'success': True, 'bank_id': 'xiaozhi', 'items_count': 1,
                                   'async': True, 'operation_id': body['operation_id']})


@pytest.mark.parametrize('bank', ['hermes', 'hermes::conv::abc', 'HERMES', 'Hermes::abc'])
def test_credentials_bank_is_never_opened(config, bank):
    config['hindsight']['bank_id'] = bank
    with pytest.raises(hs.HindsightError, match='Hermes'):
        hs.HindsightClient(config)


@pytest.mark.parametrize('content', ['PVE 的 root 密码是什么', 'Authorization: Bearer abcdef',
                                   'api_key=abcdef', '我的密钥是abc', 'https://user:password@host/'])
def test_credential_input_rejected_before_network(config, content):
    client = hs.HindsightClient(config)
    with pytest.raises(hs.HindsightError) as exc:
        client.recall('alice', content)
    assert exc.value.code == 'sensitive_input'
    with pytest.raises(hs.HindsightError):
        client.retain('alice', content, 'request-1')


def test_recall_contract_owner_filter_and_cache(monkeypatch, config):
    calls = []
    def request(req):
        calls.append(req)
        body = json.loads(req.content)
        assert req.url.path.endswith('/banks/xiaozhi/memories/recall')
        assert 'authorization' not in req.headers
        assert body == {'query': '回答偏好', 'max_tokens': 300, 'budget': 'low',
                        'types': ['world', 'experience'], 'tags': [tag('alice')], 'tags_match': 'all_strict'}
        mixed = fact(); mixed['tags'].append(tag('bob'))
        return httpx.Response(200, json={'results': [fact(), fact('bob'), mixed,
            fact(text='密码是 abcdef'), {**fact(), 'type': 'observation'}, {**fact(), 'tags': []}]})
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    result = client.recall('alice', '回答偏好')
    assert result == {'ok': True, 'cached': False,
                      'facts': [{'id': 'fact-1', 'text': '用户偏好简短回答', 'document_id': 'doc-1'}]}
    result['facts'][0]['text'] = 'mutated'
    assert client.recall('alice', '回答偏好')['facts'][0]['text'] == '用户偏好简短回答'
    assert len(calls) == 1


def test_cache_isolated_by_owner_and_endpoint(monkeypatch, config):
    calls = []
    def request(req):
        calls.append(req.url.host)
        owner = 'alice' if json.loads(req.content)['tags'] == [tag('alice')] else 'bob'
        return httpx.Response(200, json={'results': [fact(owner)]})
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    client.recall('alice', '偏好')
    client.recall('bob', '偏好')
    other = {'hindsight': {**config['hindsight'], 'base_url': 'http://192.0.2.61:40100'}}
    hs.HindsightClient(other).recall('alice', '偏好')
    assert len(calls) == 3


def test_retain_stable_identity_private_receipt_and_conflict(monkeypatch, config):
    calls = []
    def request(req):
        calls.append(json.loads(req.content))
        assert 'authorization' not in req.headers
        return accepted(req)
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    first = client.retain('alice', '喜欢简短回答', 'same-request')
    second = client.retain('alice', '喜欢简短回答', 'same-request')
    assert first == second and first['status'] == 'pending' and not first['completed']
    body = calls[0]
    assert body['async'] is True and body['operation_id'] == first['operation_id']
    assert body['items'][0]['tags'] == [tag('alice'), 'source:xiaozhi-hub']
    assert body['items'][0]['observation_scopes'] == 'combined'
    assert body['items'][0]['document_id'] == first['document_id']
    assert calls[1] == calls[0]
    with pytest.raises(hs.HindsightError) as exc:
        client.retain('alice', '喜欢详细回答', 'same-request')
    assert exc.value.code == 'idempotency_conflict' and len(calls) == 2
    with sqlite3.connect(client.path) as db:
        data = list(db.execute('SELECT * FROM hindsight_operations'))
    assert '喜欢简短回答' not in str(data) and 'alice' not in str(data)


def test_lost_retain_response_can_retry_same_operation(monkeypatch, config):
    bodies = []
    def request(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            raise httpx.ReadTimeout('upstream body may contain secrets')
        return accepted(req)
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    with pytest.raises(hs.HindsightError) as exc:
        client.retain('alice', '喜欢简短回答', 'lost-response')
    assert 'secrets' not in str(exc.value)
    assert exc.value.receipt['status'] == 'unknown' and not exc.value.receipt['accepted']
    assert exc.value.receipt['operation_id'] == bodies[0]['operation_id']
    result = client.retain('alice', '喜欢简短回答', 'lost-response')
    assert result['accepted'] and bodies[0] == bodies[1]


def test_status_requires_local_owner_receipt_and_omits_error(monkeypatch, config):
    calls = []
    def request(req):
        calls.append(req)
        if req.method == 'POST': return accepted(req)
        assert req.url.params['include_payload'] == 'false'
        return httpx.Response(200, json={'status': 'failed', 'error_message': 'private original source'})
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    receipt = client.retain('alice', '喜欢简短回答', 'a')
    with pytest.raises(hs.HindsightError) as exc:
        client.status('bob', receipt['operation_id'])
    assert exc.value.code == 'not_owned' and len(calls) == 1
    result = client.status('alice', receipt['operation_id'])
    assert result['status'] == 'failed' and 'private' not in str(result)


def test_forget_waits_for_completed_retain_and_checks_document_owner(monkeypatch, config):
    state = {'status': 'pending', 'owned': True, 'deletes': 0}
    doc = {}
    def request(req):
        if req.method == 'POST':
            doc['id'] = json.loads(req.content)['items'][0]['document_id']
            return accepted(req)
        if '/operations/' in req.url.path:
            return httpx.Response(200, json={'status': state['status']})
        if req.method == 'GET':
            return httpx.Response(200, json={'id': doc['id'], 'bank_id': 'xiaozhi',
                'tags': [tag('alice' if state['owned'] else 'bob'), 'source:xiaozhi-hub'],
                'original_text': 'must not be returned'})
        state['deletes'] += 1
        return httpx.Response(200, json={'success': True, 'document_id': doc['id'], 'memory_units_deleted': 2})
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    receipt = client.retain('alice', '喜欢简短回答', 'a')
    with pytest.raises(hs.HindsightError) as exc: client.forget('alice', receipt['document_id'])
    assert exc.value.code == 'retain_incomplete' and state['deletes'] == 0
    state.update(status='completed', owned=False)
    with pytest.raises(hs.HindsightError) as exc: client.forget('alice', receipt['document_id'])
    assert exc.value.code == 'not_owned' and state['deletes'] == 0
    state['owned'] = True
    result = client.forget('alice', receipt['document_id'])
    assert result['deleted'] and result['memory_units_deleted'] == 2
    assert client.forget('alice', receipt['document_id'])['memory_units_deleted'] == 0
    with pytest.raises(hs.HindsightError):client.retain('alice', '喜欢简短回答', 'a')
    assert state['deletes'] == 1


def test_recall_filters_deleted_documents_even_from_cache(monkeypatch, config):
    mock_http(monkeypatch, lambda req: accepted(req) if req.url.path.endswith('/memories') else httpx.Response(200, json={'results': [fact(doc=doc)]}))
    client = hs.HindsightClient(config)
    receipt = client.retain('alice', '喜欢简短回答', 'a')
    doc = receipt['document_id']
    assert len(client.recall('alice', '偏好')['facts']) == 1
    client._set_status(receipt['operation_id'], 'deleted', deleting=True)
    assert client.recall('alice', '偏好') == {'ok': True, 'facts': [], 'cached': True}


def test_mutation_invalidates_cached_recall(monkeypatch, config):
    seen = []
    def request(req):
        seen.append(req.url.path)
        return accepted(req) if req.url.path.endswith('/memories') else httpx.Response(200, json={'results': []})
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    assert not client.recall('alice', '偏好')['cached']
    assert client.recall('alice', '偏好')['cached']
    client.retain('alice', '喜欢简短回答', 'a')
    assert not client.recall('alice', '偏好')['cached']
    assert len(seen) == 3


@pytest.mark.parametrize('status', [401, 429, 500])
def test_http_failure_not_cached_or_exposed(monkeypatch, config, status):
    seen = []
    def request(req):
        seen.append(req)
        return httpx.Response(status, text='credential from remote error body')
    mock_http(monkeypatch, request)
    client = hs.HindsightClient(config)
    for _ in range(2):
        with pytest.raises(hs.HindsightError) as exc:client.recall('alice', '偏好')
        assert str(status) in str(exc.value) and 'credential' not in str(exc.value)
    assert len(seen) == 2
