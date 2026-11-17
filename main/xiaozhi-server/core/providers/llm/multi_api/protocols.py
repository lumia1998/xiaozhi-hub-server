"""Wire formats for OpenAI Chat/Responses, Gemini v1beta and Anthropic Messages."""
import urllib.parse

PROTOCOLS = ('chat', 'responses', 'gemini', 'messages')
LABELS = {'chat': 'OpenAI Chat', 'responses': 'OpenAI Responses', 'gemini': 'Gemini v1beta', 'messages': 'Claude Messages'}


def base_url(value):
    if not isinstance(value, str): raise ValueError('请填写接口地址')
    value = value.strip().rstrip('/')
    try:
        p = urllib.parse.urlsplit(value)
        valid = p.scheme in ('http','https') and p.hostname and not p.username and not p.password and not p.query and not p.fragment
        valid = valid and not any(c.isspace() or ord(c)<32 for c in value) and '\\' not in value and (p.port is None or 0<p.port<65536)
    except ValueError: valid = False
    if not valid: raise ValueError('接口地址必须是 HTTP/HTTPS 地址，不含账号、查询参数或片段')
    for tail in ('/chat/completions','/responses','/messages','/models'):
        if value.endswith(tail): value = value[:-len(tail)]
    return value


def api_root(base, protocol):
    base = base_url(base)
    p = urllib.parse.urlsplit(base)
    path = p.path.rstrip('/')
    if protocol == 'gemini':
        if path.endswith('/v1'): path = path[:-3] + '/v1beta'
        elif not path.endswith('/v1beta'): path += '/v1beta'
    else:
        if path.endswith('/v1beta'): path = path[:-7] + '/v1'
        elif not path: path = '/v1'
    return urllib.parse.urlunsplit((p.scheme,p.netloc,path,'',''))


def endpoint(base, protocol, model=''):
    root = api_root(base,protocol)
    if protocol == 'gemini':
        model = model.removeprefix('models/')
        if not model or len(model)>300 or any(c in model for c in '?#') or '..' in model.split('/'):
            raise ValueError('模型名称格式不正确')
        return root + '/models/' + urllib.parse.quote(model,safe='/') + ':generateContent'
    return root + {'chat':'/chat/completions','responses':'/responses','messages':'/messages'}[protocol]


def headers(protocol, key):
    result = {'Content-Type':'application/json'}
    if protocol=='messages': result['anthropic-version']='2023-06-01'
    if key:
        result[{'gemini':'x-goog-api-key','messages':'x-api-key'}.get(protocol,'Authorization')] = key if protocol in ('gemini','messages') else 'Bearer '+key
    return result


def payload(protocol, model, text, max_tokens):
    """只有一条用户消息、不流式的请求体。"""
    if protocol=='chat':return {'model':model,'messages':[{'role':'user','content':text}],'stream':False,'max_tokens':max_tokens}
    if protocol=='responses':return {'model':model,'input':[{'role':'user','content':text}],'stream':False,'max_output_tokens':max_tokens,'store':False}
    if protocol=='messages':return {'model':model,'messages':[{'role':'user','content':[{'type':'text','text':text}]}],'stream':False,'max_tokens':max_tokens}
    if protocol=='gemini':return {'contents':[{'role':'user','parts':[{'text':text}]}],'generationConfig':{'maxOutputTokens':max_tokens}}
    raise ValueError('未知模型协议')


def valid_result(protocol, data):
    if not isinstance(data,dict) or data.get('error'):return False
    if protocol=='chat':return bool(data.get('choices') and isinstance(data['choices'][0].get('message'),dict))
    if protocol=='responses':return data.get('object')=='response' and isinstance(data.get('output'),list) and data.get('status') not in ('failed','cancelled')
    if protocol=='messages':return data.get('type')=='message' and isinstance(data.get('content'),list)
    if protocol=='gemini':return bool(data.get('candidates') and isinstance(data['candidates'][0].get('content'),dict))
    return False
