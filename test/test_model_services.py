import asyncio
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient

from core.admin import model_services as ms
from core.admin.admin_handler import AdminHandler, COOKIE_NAME
from core.providers.llm.multi_api.protocols import *

BASE={'selected_module':{'LLM':'CustomOpenAILLM','Intent':'function_call','TTS':'EdgeTTS'},
      'LLM':{'CustomOpenAILLM':{'type':'openai','base_url':'http://example.test/v1','api_key':'old-key','model_name':'old'}},
      'Intent':{'function_call':{'type':'function_call'},'nointent':{'type':'nointent'}},'prompt':'你叫小葵。',
      'plugins':{'home_assistant':{'direct_commands':{'enabled':True}}}}


def reply(p):
    if p=='chat':return {'choices':[{'message':{'role':'assistant','content':'OK'}}]}
    if p=='responses':return {'object':'response','status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'OK'}]}]}
    if p=='messages':return {'type':'message','content':[{'type':'text','text':'OK'}]}
    return {'candidates':[{'content':{'role':'model','parts':[{'text':'OK'}]},'finishReason':'STOP'}]}


class ConversionTests(unittest.TestCase):
    def test_url_and_credential_headers(self):
        for suffix in ['/v1','/v1/chat/completions','/v1/responses','/v1/messages']:
            self.assertEqual(endpoint('https://proxy.test'+suffix,'chat'),'https://proxy.test/v1/chat/completions')
        self.assertEqual(endpoint('https://proxy.test/v1','gemini','models/gemini-test'),'https://proxy.test/v1beta/models/gemini-test:generateContent')
        for bad in ['file:///tmp','https://u:p@host','https://host?key=s','https://host:99999','http://host/a b']:
            with self.assertRaises(ValueError):base_url(bad)
        self.assertEqual(headers('messages','key')['x-api-key'],'key')
        self.assertEqual(headers('gemini','key')['x-goog-api-key'],'key')


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def server(self,handler):
        app=web.Application();app.router.add_route('*','/{path:.*}',handler)
        server=TestServer(app);await server.start_server();self.addAsyncCleanup(server.close)
        return str(server.make_url('')).rstrip('/')

    async def test_discovery_all_three_formats_and_pagination(self):
        async def handler(r):
            if r.path=='/v1beta/models':
                self.assertEqual(r.headers['x-goog-api-key'],'test-key')
                return web.json_response({'models':[{'name':'models/gemini-one','displayName':'Gemini One'}]})
            if r.headers.get('x-api-key'):
                return web.json_response({'data':[{'id':'claude-two'}], 'has_more':False})
            if r.query.get('after'):return web.json_response({'data':[{'id':'other-model'}],'has_more':False})
            return web.json_response({'data':[{'id':'gpt-one'}],'has_more':True,'last_id':'page-1'})
        url=await self.server(handler)
        result=await ms.discover({'base_url':url+'/v1','api_key':'test-key'})
        self.assertEqual({m['id'] for m in result['models']},{'gpt-one','other-model','gemini-one','claude-two'})
        self.assertFalse(result['partial']);self.assertTrue(all(x['ok'] for x in result['discovery']))

    async def test_probe_real_shape_not_model_name(self):
        async def handler(r):
            p='responses' if r.path.endswith('/responses') else 'chat' if r.path.endswith('completions') else 'messages' if r.path.endswith('messages') else 'gemini'
            if p=='messages':return web.json_response({'choices':[]}) # HTTP 200 but wrong protocol
            return web.json_response(reply(p))
        url=await self.server(handler)
        report=await asyncio.to_thread(ms.probe_sync,url,'test-key','claude-model')
        self.assertEqual(report['selected'],'chat')
        self.assertEqual({r['protocol'] for r in report['results'] if r['ok']},{'chat','responses','gemini'})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=pathlib.Path(self.tmp.name)/'.config.yaml'
        self.path.write_text(__import__('yaml').safe_dump(BASE,allow_unicode=True),encoding='utf-8')
    def merged(self):return __import__('yaml').safe_load(self.path.read_text(encoding='utf-8'))
    def test_save_discovery_masks_key_and_persona_conflict(self):
        sid,service,_=ms.normalized_service({'name':'网关','base_url':'http://proxy.test/v1','api_key':'test-key'},BASE)
        report={'models':[{'id':'m1','label':'M1','listed_by':['chat']},{'id':'m2','label':'M2','listed_by':['gemini']}],'discovery':[],'partial':False}
        ms.save_discovered(sid,service,report,BASE,self.path);m=self.merged()
        self.assertEqual(m['selected_module'],BASE['selected_module']);self.assertEqual(m['plugins'],BASE['plugins'])
        self.assertNotIn('test-key',json.dumps(ms.describe(m)))
        self.assertEqual(m['LLM'][ms.entry_name(sid,'m1')]['protocol'],'auto')
        ms.save_probe(sid,'m1',{'selected':'responses','results':[],'checked_at':1},m,self.path)
        self.assertEqual(self.merged()['LLM'][ms.entry_name(sid,'m1')]['protocol'],'responses')
        ms.save_persona('你叫小葵，回答简短。',BASE['prompt'],self.merged(),self.path)
        with self.assertRaises(ValueError):ms.save_persona('new',BASE['prompt'],self.merged(),self.path)
        with self.assertRaises(ValueError):ms.normalized_service({'id':sid,'base_url':'http://evil.test'},self.merged())


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=pathlib.Path(self.tmp.name)/'.config.yaml'
        self.path.write_text(__import__('yaml').safe_dump(BASE,allow_unicode=True),encoding='utf-8');self.handler=AdminHandler(copy.deepcopy(BASE),config_path=self.path)
        app=web.Application();self.handler.register(app);self.client=TestClient(TestServer(app));await self.client.start_server();self.addAsyncCleanup(self.client.close)
        self.cookies={COOKIE_NAME:self.handler.sessions.create()}
    async def test_auth_and_persona_save(self):
        self.assertEqual((await self.client.get('/admin/api/model-services')).status,401)
        response=await self.client.post('/admin/api/persona',json={'persona':'你叫小葵，讲重点。','expected':BASE['prompt']},cookies=self.cookies)
        self.assertEqual(response.status,200);self.assertIn('讲重点',self.handler.config['prompt'])
        response=await self.client.post('/admin/api/persona',json={'persona':'new','expected':BASE['prompt']},cookies=self.cookies)
        self.assertEqual(response.status,400)
    async def test_discover_probe_use_and_failure_does_not_switch(self):
        report={'models':[{'id':'claude-x','label':'Claude X','listed_by':['chat']}],'discovery':[],'partial':False}
        with patch.object(ms,'discover',return_value=report):
            r=await self.client.post('/admin/api/model-services/discover',json={'name':'Test','base_url':'http://proxy.test','api_key':'test-key'},cookies=self.cookies)
            self.assertEqual(r.status,200);data=await r.json();sid=data['id'];self.assertNotIn('test-key',json.dumps(data))
        # 检测：四种协议都不通过时只记下结果，不换模型
        with patch.object(ms,'probe_sync',return_value={'selected':None,'results':[],'checked_at':0}):
            r=await self.client.post('/admin/api/model-services/probe',json={'id':sid,'model':'claude-x'},cookies=self.cookies)
            self.assertEqual(r.status,200);self.assertIsNone((await r.json())['probe']['selected'])
            self.assertEqual(self.handler.config['selected_module']['LLM'],'CustomOpenAILLM')
        with patch.object(ms,'probe_sync',return_value={'selected':'messages','results':[{'protocol':'messages','ok':True}],'checked_at':int(__import__('time').time())}):
            r=await self.client.post('/admin/api/model-services/probe',json={'id':sid,'model':'claude-x'},cookies=self.cookies)
            self.assertEqual(r.status,200);self.assertEqual(self.handler.config['LLM'][ms.entry_name(sid,'claude-x')]['protocol'],'messages')
        # 切换不检测：条目配好了就切
        with patch.object(ms,'probe_sync',side_effect=AssertionError('切换时不该检测')):
            r=await self.client.post('/admin/api/model-services/use',json={'id':sid,'model':'claude-x'},cookies=self.cookies)
            self.assertEqual(r.status,200,await r.text())
            self.assertEqual(self.handler.config['selected_module']['LLM'],ms.entry_name(sid,'claude-x'))


if __name__=='__main__':unittest.main()
