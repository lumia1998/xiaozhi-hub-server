"""One credential set per service; paginated model discovery and real protocol probes."""
import asyncio
import hashlib
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import LiteralScalarString

from core.admin import config_store
from config.config_loader import merge_configs
from core.providers.llm.multi_api.protocols import PROTOCOLS,LABELS,base_url,api_root,endpoint,headers,payload,valid_result

MAX_MODELS=10000


def entry_name(service,model):return 'Service_'+service+'_'+hashlib.sha256(model.encode()).hexdigest()[:16]


def model_entry(service,sid,model):
    return (service.get('entry_overrides') or {}).get(model) or entry_name(sid,model)


def normalized_service(body,merged):
    sid=body.get('id') or uuid.uuid4().hex[:12]
    if not isinstance(sid,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,60}',sid):raise ValueError('服务标识不正确')
    old=(merged.get('model_services') or {}).get(sid) or {}
    url=base_url(body.get('base_url') or old.get('base_url',''))
    raw_key=body.get('api_key','')
    if not isinstance(raw_key,str) or len(raw_key)>8192 or any(c.isspace() for c in raw_key):raise ValueError('API Key 格式不正确')
    if old.get('api_key') and url!=old.get('base_url') and not raw_key:raise ValueError('修改接口地址后请重新填写 API Key')
    key=raw_key or old.get('api_key','')
    name=body.get('name') or old.get('name') or '模型服务'
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('服务名称需要 1 到 80 个字')
    return sid,{'name':name.strip(),'base_url':url,'api_key':key},old


async def discover(service):
    """Read every published model page. Cross-origin next URLs are never followed."""
    async def listing(protocol):
        results=[];cursor=None;seen=set();pages=0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12,connect=4),follow_redirects=False) as client:
                while True:
                    params={}
                    if protocol=='gemini':params={'pageSize':1000,**({'pageToken':cursor} if cursor else {})}
                    elif protocol=='messages':params={'limit':1000,**({'after_id':cursor} if cursor else {})}
                    elif cursor:params={'after':cursor,'limit':1000}
                    r=await client.get(api_root(service['base_url'],protocol)+'/models',headers=headers(protocol,service['api_key']),params=params)
                    if r.status_code!=200:return {'protocol':protocol,'ok':False,'error':f'HTTP {r.status_code}','models':[]}
                    if len(r.content)>8*1024*1024:raise ValueError()
                    data=r.json();rows=data.get('models') if protocol=='gemini' else data.get('data')
                    if not isinstance(rows,list):raise ValueError()
                    for row in rows:
                        if not isinstance(row,dict):continue
                        model=row.get('name' if protocol=='gemini' else 'id')
                        if not isinstance(model,str) or not model or len(model)>300:continue
                        if protocol=='gemini':model=model.removeprefix('models/')
                        results.append({'id':model,'label':row.get('displayName') or row.get('display_name') or model,
                                        'methods':row.get('supportedGenerationMethods',[])})
                    pages+=1
                    cursor=data.get('nextPageToken') if protocol=='gemini' else data.get('last_id') if data.get('has_more') else None
                    if protocol=='chat' and not cursor:cursor=data.get('next_cursor')
                    if not cursor:break
                    if not isinstance(cursor,str) or cursor in seen or pages>=100 or len(results)>MAX_MODELS:
                        return {'protocol':protocol,'ok':True,'models':results[:MAX_MODELS],'partial':True,'pages':pages}
                    seen.add(cursor)
            return {'protocol':protocol,'ok':True,'models':results,'pages':pages,'partial':False}
        except (httpx.HTTPError,ValueError,AttributeError,TypeError):
            return {'protocol':protocol,'ok':False,'error':'连接失败或模型列表格式不正确','models':[]}
    reports=await asyncio.gather(*(listing(p) for p in ('chat','gemini','messages')))
    models={}
    for report in reports:
        for row in report['models']:
            m=models.setdefault(row['id'],{**row,'listed_by':[]})
            m['listed_by'].append(report['protocol'])
    return {'models':sorted(models.values(),key=lambda m:m['id'].lower()),
            'discovery':[ {k:v for k,v in r.items() if k!='models'} for r in reports],
            'partial':any(r.get('partial') for r in reports)}


def probe_sync(base,key,model):
    """Four tiny real requests in parallel. Only responses matching the protocol count."""
    def one(protocol):
        start=time.monotonic()
        try:
            request=payload(protocol,model,'Reply only OK.',128)
            with httpx.Client(timeout=httpx.Timeout(22,connect=4),follow_redirects=False) as client:
                r=client.post(endpoint(base,protocol,model),headers=headers(protocol,key),json=request)
                if r.status_code!=200:return {'protocol':protocol,'ok':False,'error':f'HTTP {r.status_code}'}
                if len(r.content)>2*1024*1024:raise ValueError()
                good=valid_result(protocol,r.json())
                return {'protocol':protocol,'ok':good,'milliseconds':round((time.monotonic()-start)*1000),
                        **({} if good else {'error':'响应格式不匹配'})}
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            return {'protocol':protocol,'ok':False,'error':'请求超时或返回格式无效'}
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='protocol-probe') as pool:results=list(pool.map(one,PROTOCOLS))
    available={r['protocol'] for r in results if r['ok']}
    low=model.lower()
    order=['messages','chat','responses','gemini'] if 'claude' in low else ['gemini','chat','responses','messages'] if 'gemini' in low else ['responses','chat','messages','gemini'] if re.search(r'gpt|(^|/)o[134]|codex',low) else ['chat','responses','messages','gemini']
    return {'selected':next((p for p in order if p in available),None),'results':results,'checked_at':int(time.time())}


def save_discovered(sid,service,report,merged,path=None):
    if not report['models']:raise ValueError('没有获取到模型，请检查地址、密钥及模型列表权限')
    with config_store.edit_custom(path) as custom:
        latest=merge_configs(merged,custom)
        previous=(latest.get('model_services') or {}).get(sid) or {}
        if previous != ((merged.get('model_services') or {}).get(sid) or {}):
            raise ValueError('服务配置已变化，请重新获取模型')
        same=previous.get('base_url')==service['base_url'] and previous.get('api_key')==service['api_key']
        detected={m['id']:m for m in previous.get('models',[])} if same else {}
        models=[]
        for model in report['models']:
            m=dict(model);old=detected.get(m['id'],{})
            if old.get('probe'):m['probe']=old['probe']
            models.append(m)
        stored={**service,'models':models,'discovery':report['discovery'],'partial':report['partial'],'updated':int(time.time()),
                'entry_overrides':previous.get('entry_overrides',{})}
        custom.setdefault('model_services',CommentedMap())[sid]=CommentedMap(stored)
        llms=custom.setdefault('LLM',CommentedMap())
        for model in models:
            entry=model_entry(stored,sid,model['id']);old=(latest.get('LLM') or {}).get(entry) or {}
            llms[entry]=CommentedMap({**old,'type':'pi','service_id':sid,'model_name':model['id'],
                                     'protocol':(model.get('probe') or {}).get('selected') or 'auto','max_tokens':old.get('max_tokens',800)})
        # Models absent from refreshed catalog are unavailable, but history is not deleted.
        wanted={model_entry(stored,sid,m['id']) for m in models}
        for name,e in list(llms.items()):
            if isinstance(e,dict) and e.get('service_id')==sid and name not in wanted:e['disabled']=True
        for name in wanted:llms[name].pop('disabled',None)
        # A removed catalog item may still be the running/default model. Keep it usable
        # until the user explicitly chooses another, while no longer listing it as new.
        active=(latest.get('selected_module') or {}).get('LLM')
        if active in llms and llms[active].get('service_id')==sid:llms[active].pop('disabled',None)
    return sid


def save_probe(sid,model,result,merged,path=None):
    with config_store.edit_custom(path) as custom:
        service=(custom.get('model_services') or {}).get(sid)
        if not service:raise ValueError('请先获取并保存模型列表')
        source=(merged.get('model_services') or {}).get(sid) or {}
        if any(service.get(k)!=source.get(k) for k in ('base_url','api_key')):
            raise ValueError('检测期间服务配置已变化，请重试')
        row=next((m for m in service.get('models',[]) if m['id']==model),None)
        if row is None:raise ValueError('模型不在此服务的列表中')
        row['probe']=result
        if result.get('selected'):
            canonical=model_entry(service,sid,model)
            custom['LLM'][canonical]['protocol']=result['selected']
            # The catalog can represent the active legacy entry without renaming its
            # history key. A probe must also reach that exact active equivalent.
            latest=merge_configs(merged,custom)
            selected=(latest.get('selected_module') or {}).get('LLM')
            entry=(latest.get('LLM') or {}).get(selected) or {}
            route=(latest.get('model_services') or {}).get(entry.get('service_id')) if entry.get('service_id') else entry
            route=route or {}
            identity=_model_identity(service.get('base_url'),service.get('api_key'),model)
            active_identity=_model_identity(route.get('base_url') or route.get('url'),route.get('api_key'),entry.get('model_name'))
            if selected and selected!=canonical and identity is not None and active_identity==identity:
                custom['LLM'].setdefault(selected,CommentedMap(entry))['protocol']=result['selected']


def _model_identity(base, key, model):
    """Compare routes privately; credentials must never become response fields."""
    try:
        return api_root(base, 'chat'), key or '', model
    except (ValueError, TypeError):
        return None


def describe(merged):
    from core.agent import llm_router

    services=merged.get('model_services') or {}
    llms=merged.get('LLM') or {}
    selected=(merged.get('selected_module') or {}).get('LLM')
    selected_entry=llms.get(selected) or {}
    ready={name:entry for name,entry in llms.items() if llm_router.model_ready(merged,name)}
    identities={}
    for name,entry in ready.items():
        service=services.get(entry.get('service_id')) if entry.get('service_id') else entry
        service=service or {}
        identities[name]=_model_identity(service.get('base_url') or service.get('url'),
                                         service.get('api_key'),entry.get('model_name'))

    result=[]
    represented=set()
    for sid,service in services.items():
        models=[]
        for model in service.get('models',[]):
            identity=_model_identity(service.get('base_url'),service.get('api_key'),model['id'])
            aliases=[name for name in ready if identity is not None and identities[name]==identity]
            preferred=model_entry(service,sid,model['id'])
            # Preserve the exact active history key. Other equivalent entries stay on disk.
            entry=selected if selected in aliases else preferred if preferred in aliases else next(iter(aliases),preferred)
            represented.update(aliases)
            models.append({**model,'entry':entry,'available':entry in ready,
                           'is_default':entry==selected})
        result.append({'id':sid,'name':service['name'],'base_url':service['base_url'],
                       'key_set':bool(service.get('api_key')),'models':models,
                       'discovery':service.get('discovery',[]),'partial':service.get('partial',False),
                       'updated':service.get('updated')})

    existing=[]
    seen=set()
    # The active entry wins when several standalone entries describe the same route.
    for name in sorted(ready,key=lambda name:name!=selected):
        if name in represented:continue
        identity=identities[name] or ('entry',name)
        if identity in seen:continue
        seen.add(identity)
        entry=ready[name]
        existing.append({'entry':name,'model':entry['model_name'],'label':entry['model_name'],
                         'kind':entry.get('type'),'protocol':entry.get('protocol','auto'),
                         'is_default':name==selected})
    return {'services':result,'selected':selected,
            'current_model':selected_entry.get('model_name') or selected_entry.get('model') or selected,
            'persona':merged.get('prompt',''),'existing':existing,'protocol_labels':LABELS,'engine':'Pi Agent'}


def save_persona(text,expected,merged,path=None):
    if not isinstance(text,str) or not 1<=len(text.strip())<=12000:raise ValueError('人设需要 1 到 12000 个字')
    with config_store.edit_custom(path) as custom:
        current=custom.get('prompt',merged.get('prompt',''))
        if expected is not None and current!=expected:raise ValueError('人设已在其他页面修改，请刷新后再保存')
        custom['prompt']=LiteralScalarString(text.strip()+'\n')
    return text.strip()+'\n'
