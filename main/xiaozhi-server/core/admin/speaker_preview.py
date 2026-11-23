"""Authenticated gateway view of a paired speaker. Only trusted, installed UI assets are served."""
import asyncio
import json
import os
from pathlib import Path
import re
import uuid

from aiohttp import web, WSMsgType


READ = {'history', 'network', 'health', 'panels/agenda', 'panels/devices', 'panels/weather'}
WRITE = {'text', 'model', 'choice', 'abort', 'end', 'sleep', 'volume', 'brightness', 'brightness-schedule',
         'screen-off', 'panels/agenda', 'panels/agenda-voice', 'ptt/start', 'ptt/stop', 'panels/devices', 'media/playback'}


def allowed(method, path):
    return (method == 'GET' and path in READ or method == 'POST' and path in WRITE or
            method == 'DELETE' and re.fullmatch(r'panels/agenda/[0-9]{1,18}', path) is not None)


class Link:
    def __init__(self, device, ws):
        self.device, self.ws = device, ws
        self.pending, self.viewers = {}, set()

    async def call(self, **data):
        if len(self.pending) >= 12: raise web.HTTPTooManyRequests()
        key = uuid.uuid4().hex
        future = self.pending[key] = asyncio.get_running_loop().create_future()
        try:
            await self.ws.send_json({'id':key, **data})
            return await asyncio.wait_for(future, 25)
        except (ConnectionError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text='音箱未响应，请刷新核对状态') from None
        finally: self.pending.pop(key, None)


class SpeakerPreview:
    def __init__(self, admin, data_dir):
        self.admin = admin
        self.links = {}
        self.assets = Path(os.environ.get('THINKSMART_PREVIEW_WEB') or data_dir / 'speaker-web').resolve()

    def register(self, app):
        async def shutdown(_app):
            await asyncio.gather(*(link.ws.close() for link in tuple(self.links.values())),return_exceptions=True)
        app.on_shutdown.append(shutdown)
        app.router.add_get('/display/console', self.console)
        app.router.add_get('/admin/speaker/{device}/', self.index)
        app.router.add_get('/admin/speaker/{device}/static/{file:.*}', self.asset)
        app.router.add_get('/admin/speaker/{device}/events', self.events)
        app.router.add_route('*','/admin/speaker/{device}/api/{path:.*}', self.api)

    async def console(self, request):
        device = request.headers.get('Device-Id','')
        store = self.admin._pairing_store()
        token = request.headers.get('X-Device-Pairing-Token','')
        if not store or not re.fullmatch(r'[\w:.-]{1,128}',device) or not await asyncio.to_thread(store.verify,device,token):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4*1024*1024)
        await ws.prepare(request)
        link = Link(device,ws); previous = self.links.get(device); self.links[device] = link
        if previous: await previous.ws.close()
        async def recheck():
            while True:
                try: await asyncio.wait_for(asyncio.Event().wait(),30)
                except asyncio.TimeoutError: pass
                if not await asyncio.to_thread(store.verify,device,token):
                    await ws.close(); return
        check = asyncio.create_task(recheck())
        try:
            async for frame in ws:
                if frame.type != WSMsgType.TEXT: break
                data = frame.json()
                if not isinstance(data,dict): break
                future = link.pending.get(data.get('id'))
                if future and not future.done(): future.set_result(data)
                elif data.get('event') in ('state','thread','model','message','toast','media_pause','volume','level','bluetooth','snapshot','layout_changed'):
                    for queue in tuple(link.viewers):
                        if not queue.full(): queue.put_nowait((data['event'],data.get('data')))
        finally:
            if self.links.get(device) is link: self.links.pop(device,None)
            for future in list(link.pending.values()):
                if not future.done(): future.set_exception(ConnectionError())
            check.cancel(); await asyncio.gather(check,return_exceptions=True)
        return ws

    def target(self,request):
        self.admin._require_login(request)
        link = self.links.get(request.match_info['device'])
        if not link or link.ws.closed: raise web.HTTPServiceUnavailable(text='音箱未连接预览')
        return link

    async def api(self,request):
        link=self.target(request); path=request.match_info['path']
        if request.method != 'GET' and (request.headers.get('X-Speaker-Console')!='1' or
                request.headers.get('Origin') not in (None, f'{request.scheme}://{request.host}')):
            raise web.HTTPForbidden()
        if not allowed(request.method,path): return self.admin._error('远程预览不支持此操作',403)
        body = await self.admin._read_json(request) if request.method=='POST' and request.can_read_body else {}
        result=await link.call(method=request.method,path=path,month=request.query.get('month',''),body=body)
        return self.admin._json(result.get('body',{}),int(result.get('status',502)))

    async def events(self,request):
        link=self.target(request)
        if len(link.viewers)>=6: raise web.HTTPTooManyRequests()
        queue=asyncio.Queue(100); link.viewers.add(queue)
        try:
            snap=await link.call(op='subscribe')
            resp=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-store'})
            await resp.prepare(request)
            async def send(kind,value):
                await resp.write(f'event: {kind}\ndata: {json.dumps(value)}\n\n'.encode())
            await send('snapshot',snap['body'])
            while self.admin._logged_in(request) and not link.ws.closed:
                try: await send(*(await asyncio.wait_for(queue.get(),5)))
                except asyncio.TimeoutError: await resp.write(b': ping\n\n')
            return resp
        finally:
            link.viewers.discard(queue)
            if not link.viewers and not link.ws.closed: await link.ws.send_json({'op':'unsubscribe'})

    async def index(self,request):
        self.target(request)
        file=self.assets/'v2/index.html'
        if not file.is_file(): raise web.HTTPServiceUnavailable(text='网关未安装客户端 web 资源')
        prefix=request.path.rstrip('/')
        html=file.read_text('utf-8').replace('/static/',prefix+'/static/')
        html=html.replace('<head>', '<head><script src="'+prefix+'/static/console-base.js"></script>')
        return web.Response(text=html,content_type='text/html',headers={'Cache-Control':'no-store','X-Frame-Options':'SAMEORIGIN',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' http: https: data: blob:; media-src 'self' http: https: blob:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"})

    async def asset(self,request):
        self.admin._require_login(request)
        name=request.match_info['file']
        if name=='console-base.js':
            return web.FileResponse(Path(__file__).parent/'static'/'console-base.js',headers={'Cache-Control':'no-cache'})
        file=(self.assets/name).resolve()
        if not file.is_relative_to(self.assets) or not file.is_file(): raise web.HTTPNotFound()
        if file.suffix in ('.js','.css'):
            prefix=request.path.split('/static/',1)[0]
            return web.Response(text=file.read_text('utf-8').replace('/static/',prefix+'/static/'),
                content_type='text/javascript' if file.suffix=='.js' else 'text/css',headers={'Cache-Control':'no-cache'})
        return web.FileResponse(file,headers={'Cache-Control':'no-cache'})
