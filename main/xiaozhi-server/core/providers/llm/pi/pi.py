"""Pi is the Hub's single API model engine. Private pipes, no additional listening port."""
import atexit
import functools
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from core.providers.llm.base import LLMProviderBase
from core.providers.llm.multi_api.protocols import api_root
from config.logger import setup_logging

RUNTIME = Path(__file__).parent / 'runtime'
logger=setup_logging()
SECRET=re.compile(r'(bearer\s+)[^\s"\',]+|([?&](?:key|api[_-]?key|access_token|token)=)[^&\s"\']+|\b(sk-|AIza)[A-Za-z0-9_-]{6,}',re.I)


def redact(text):
    return SECRET.sub(lambda m:(m.group(1) or m.group(2) or m.group(3))+'***',str(text))[:500]


@functools.cache
def node_binary():
    """The runtime's own Node (an optional npm package per Linux arch), else a system Node 22.19+."""
    arch={'x86_64':'x64','amd64':'x64','aarch64':'arm64','arm64':'arm64'}.get(platform.machine().lower())
    bundled=RUNTIME/'node_modules'/f'node-linux-{arch}'/'bin'/'node'
    if sys.platform=='linux' and arch and bundled.is_file():return str(bundled)
    system=shutil.which('node')
    if system:
        version=subprocess.run([system,'--version'],capture_output=True,text=True,timeout=10).stdout.strip()
        if tuple(int(n) for n in version.lstrip('v').split('.')[:2])>=(22,19):return system
    raise RuntimeError(f'Pi 引擎找不到 Node 22.19+：在 {RUNTIME} 运行 npm ci，或安装系统 Node')


def choose_protocol(model, configured='auto', listed=()):
    if configured in ('chat','responses','messages','gemini'): return configured
    name=model.lower()
    preferred='messages' if 'claude' in name else 'gemini' if 'gemini' in name else 'responses' if any(s in name for s in ('gpt','codex')) else 'chat'
    return preferred if not listed or preferred in listed or preferred=='responses' and 'chat' in listed else next(iter(listed),'chat')


class Bridge:
    def __init__(self):
        self.lock=threading.RLock();self.write_lock=threading.Lock();self.requests={};self.process=None

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:return self.process
            # SYSTEMROOT: Node on Windows can't resolve hosts without it.
            env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL','TZ','HTTP_PROXY','HTTPS_PROXY','NO_PROXY','NODE_EXTRA_CA_CERTS','SYSTEMROOT')}
            self.process=subprocess.Popen([node_binary(),str(RUNTIME/'bridge.mjs')],cwd=RUNTIME,env=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1)
            process=self.process
            threading.Thread(target=self._read,args=(process,),daemon=True,name='pi-events').start()
            threading.Thread(target=self._log_stderr,args=(process,),daemon=True,name='pi-stderr').start()
            return process

    def _read(self,process):
        try:
            for line in process.stdout:
                try:event=json.loads(line) if len(line)<=16*1024*1024 else None
                except ValueError:event=None
                if not isinstance(event,dict):
                    logger.warning(f'Pi 引擎输出了无法解析的一行，已忽略: {redact(line.strip())[:200]}')
                    continue
                if event.get('type')=='ready':
                    logger.info(f'Pi runtime ready: {event.get("version")}')
                with self.lock:entry=self.requests.get(event.get('id'))
                if entry and entry[0] is process:entry[1].put(event)
        except OSError as exc:
            logger.warning(f'Pi 引擎输出读取失败: {exc}')
        finally:
            with self.lock:
                for _,(owner,events) in list(self.requests.items()):
                    if owner is process:events.put({'type':'error','message':'Pi 引擎进程已退出'});events.put({'type':'done'})
                if self.process is process:self.process=None
            if process.poll() is None:process.terminate()
            try:code=process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();code='killed'
            logger.warning(f'Pi 引擎进程已退出: code={code}')

    def _log_stderr(self,process):
        try:
            for line in process.stderr:
                if line.strip():logger.warning(f'Pi 引擎: {redact(line.rstrip())}')
        except (OSError,ValueError):pass

    def send(self,process,message):
        raw=json.dumps(message,ensure_ascii=False)+'\n'
        if len(raw.encode())>16*1024*1024:raise ValueError('Pi 请求过大')
        with self.write_lock:
            if process.poll() is not None:raise RuntimeError('Pi 引擎不可用')
            process.stdin.write(raw);process.stdin.flush()

    def request(self,payload,bind=None,stale=lambda:False,tool=None):
        process=self.start();rid=uuid.uuid4().hex;events=queue.Queue()
        with self.lock:self.requests[rid]=(process,events)
        if bind:bind(rid,process)
        deadline=time.monotonic()+190
        try:
            self.send(process,{'type':'run','id':rid,**payload})
            while True:
                if stale():self.abort(rid,process);return
                if time.monotonic()>deadline:raise TimeoutError('Pi 本轮响应超时')
                try:event=events.get(timeout=.2)
                except queue.Empty:continue
                kind=event.get('type')
                if kind=='done':return
                if kind=='tool_request':
                    result=tool(event) if tool else {'text':'此请求未开放工具','error':True,'terminate':True}
                    if stale():self.abort(rid,process);return
                    self.send(process,{'type':'tool_result','id':rid,'call_id':event['call_id'],**result})
                else:yield event
        finally:
            self.abort(rid,process)
            with self.lock:self.requests.pop(rid,None)
            if bind:bind(None,None)

    def abort(self,rid,process):
        try:self.send(process,{'type':'abort','id':rid})
        except (OSError,RuntimeError,ValueError):pass

    def close(self):
        with self.lock:process=self.process
        if process and process.poll() is None:process.terminate()


BRIDGE=Bridge()
atexit.register(BRIDGE.close)


class LLMProvider(LLMProviderBase):
    def __init__(self,config):
        self.config=dict(config);listed=[]
        if self.config.get('service_id'):
            from core.admin.config_store import load_merged
            service=(load_merged().get('model_services') or {}).get(self.config['service_id']) or {}
            if not service:raise ValueError('模型服务不存在')
            self.config.update(base_url=service['base_url'],api_key=service.get('api_key',''))
            row=next((m for m in service.get('models',[]) if m['id']==config['model_name']),{})
            listed=row.get('listed_by',[])
        self.model_name=self.config['model_name']
        self.protocol=choose_protocol(self.model_name,self.config.get('protocol','auto'),listed)
        self.max_tokens=int(self.config.get('max_tokens') or 800)
        self.active={};self.lock=threading.Lock()
        BRIDGE.start()

    def payload(self,dialogue,tools=None):
        api={'chat':'openai-completions','responses':'openai-responses','messages':'anthropic-messages','gemini':'google-generative-ai'}[self.protocol]
        base=api_root(self.config.get('base_url') or self.config.get('url'),self.protocol)
        # Anthropic SDK itself appends /v1/messages; OpenAI SDK expects the /v1 base.
        if self.protocol=='messages' and base.endswith('/v1'):base=base[:-3]
        model={'id':self.model_name,'name':self.model_name,'api':api,'provider':'hub-'+self.protocol,
               'baseUrl':base,'reasoning':bool(self.config.get('reasoning')),'input':['text'],
               'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},'contextWindow':128000,'maxTokens':16384}
        if self.protocol=='chat':model['compat']={'supportsStore':False,'supportsDeveloperRole':False,
            'supportsUsageInStreaming':False,'supportsStrictMode':False,'maxTokensField':'max_tokens',
            'requiresReasoningContentOnAssistantMessages': 'deepseek' in self.model_name.lower()}
        if self.config.get('compat'):model.setdefault('compat',{}).update(self.config['compat'])
        return {'model':model,'api_key':self.config.get('api_key',''),'dialogue':dialogue,'tools':tools or [],
                'max_tokens':self.max_tokens,'reasoning':self.config.get('reasoning','off'),
                'extra_body':self.config.get('extra_body') or {},'timeout_ms':180000}

    def events(self,session_id,dialogue,tools=None,tool=None,stale=lambda:False):
        run_key=uuid.uuid4().hex
        def bind(rid,process):
            with self.lock:
                if rid:self.active[run_key]=(session_id,rid,process)
                else:self.active.pop(run_key,None)
        yield from BRIDGE.request(self.payload(dialogue,tools),bind,stale,tool)

    def response(self,session_id,dialogue,**kwargs):
        payload=self.payload(dialogue)
        if kwargs.get('max_tokens'):payload['max_tokens']=int(kwargs['max_tokens'])
        for event in BRIDGE.request(payload):
            if event['type']=='text':yield event['text']
            elif event['type']=='error':raise RuntimeError(event['message'])

    def abort(self,conn):
        with self.lock:requests=[r for r in self.active.values() if r[0]==conn.session_id]
        for _,rid,process in requests:BRIDGE.abort(rid,process)

    def on_conn_close(self,conn):self.abort(conn)
