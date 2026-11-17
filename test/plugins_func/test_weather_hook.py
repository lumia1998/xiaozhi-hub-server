"""天气直答（不经大模型）：问法解析、播报格式、地名和预报缓存、打断。"""
import asyncio
import contextlib
import json
import os
import pathlib
import queue
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.agent import weather_commands as commands, pre_llm
from core.utils import weather_places, turn_gen
from plugins_func.functions import get_weather as gw
from plugins_func.pre_llm import weather as hook, home_assistant
from core.handle import sendAudioHandle
import httpx

PLACE=gw.Place('大连市（辽宁省）',38.914,121.614)


def forecast():
    today=datetime.utcfromtimestamp(time.time()+28800).date()
    return {'utc_offset_seconds':28800,'current':{'time':today.isoformat()+'T10:00','temperature_2m':12.4,'weather_code':0},
            'daily':{'time':[(today+timedelta(days=i)).isoformat() for i in range(3)],
                     'weather_code':[0,61,71],'temperature_2m_min':[8,-2,-5],'temperature_2m_max':[16,6,0],
                     'precipitation_sum':[0,3,4],'precipitation_probability_max':[10,80,90]}}


def conn():
    return SimpleNamespace(config={'selected_module':{'Intent':'function_call'},'Intent':{'function_call':{'type':'function_call','functions':['get_weather']}},
        'plugins':{'get_weather':{'location':'大连市','default_location':'广州'}}},client_ip='192.0.2.130',
        logger=Mock(),client_abort=False,sentence_id=None,stop_event=threading.Event(),
        dialogue=SimpleNamespace(put=Mock()),tts=SimpleNamespace(store_tts_text=Mock(),tts_one_sentence=Mock(),tts_text_queue=queue.Queue()))


class Parsing(unittest.TestCase):
    def test_common_phrases(self):
        rows=[('今天天气','',0,'weather'),('小葵，今天天气怎么样？','',0,'weather'),('请帮我查一下明天天气','',1,'weather'),
              ('大连明天天气如何','大连',1,'weather'),('今天大连的天气','大连',0,'weather'),('明天北京市天气怎么样','北京市',1,'weather'),
              ('深圳南山区的天气','深圳南山区',0,'weather'),('本地天气','',0,'weather'),('当地明天天气','',1,'weather'),
              ('现在多少度','',0,'temperature'),('明天气温多少','',1,'temperature'),('后天会不会下雨','',2,'rain'),
              ('今天天气会下雨吗','',0,'rain'),('需要带伞吗','',0,'rain'),('明天大连下雪吗','大连',1,'snow')]
        for text,location,day,kind in rows:
            with self.subTest(text=text):
                q=commands.parse(text);self.assertIsNotNone(q);self.assertEqual((q.location,q.day,q.kind),(location,day,kind))
    def test_complex_negative_and_contextual_phrases_not_intercepted(self):
        texts=['如果下雨就关窗','今天天气怎么样然后关闭主灯','北京和上海天气对比','今天和明天天气',
               '今天天气为什么这么冷','那边天气','北京上海天气','我想听天气的故事','不要查天气','我不想知道天气',
               '你觉得今天天气怎么样','今天天气适合出去吗','查明天早上天气','明天下午下雨吗','今晚会不会下雨',
               '今天下雨几点停','未来七天天气','昨天的天气','主灯开','明天天气，台灯关','出差去上海天气怎么样',
               '请解释市天气','你知道区天气','这个市天气','这个区的天气','北京明天上海天气','前天温度','室内多少度']
        for text in texts:
            with self.subTest(text=text):self.assertIsNone(commands.parse(text))
    def test_disabled_weather_and_explicit_optout(self):
        c=conn();self.assertIsNotNone(hook.match(c,'今天天气'))
        c.config['Intent']['function_call']['functions']=[];self.assertIsNone(hook.match(c,'今天天气'))
        c=conn();c.config['plugins']['get_weather']['direct_weather']=False;self.assertIsNone(hook.match(c,'今天天气'))
    def test_current_time(self):self.assertTrue(commands.parse('大连现在多少度').current)


class Formatting(unittest.TestCase):
    def test_today_tomorrow_and_day_after(self):
        self.assertEqual(commands.reply(commands.parse('今天天气'),PLACE,forecast()),'大连市今天晴。气温8度到16度。')
        self.assertIn('明天小雨',commands.reply(commands.parse('明天天气'),PLACE,forecast()))
        self.assertIn('后天小雪',commands.reply(commands.parse('后天天气'),PLACE,forecast()))
    def test_current_and_negative_temperature(self):
        self.assertEqual(commands.reply(commands.parse('现在多少度'),PLACE,forecast()),'大连市现在12度。')
        self.assertIn('零下2度到6度',commands.reply(commands.parse('明天多少度'),PLACE,forecast()))
    def test_precipitation_not_false_certainty(self):
        self.assertIn('没有明显下雨迹象',commands.reply(commands.parse('今天下雨吗'),PLACE,forecast()))
        self.assertIn('预报有小雨',commands.reply(commands.parse('明天要不要带伞'),PLACE,forecast()))
        self.assertIn('80%',commands.reply(commands.parse('明天会不会下雨'),PLACE,forecast()))
        self.assertIn('预报有小雪',commands.reply(commands.parse('后天会不会下雪'),PLACE,forecast()))
        f=forecast();f['daily']['precipitation_probability_max'][0]=60
        self.assertIn('建议带伞',commands.reply(commands.parse('今天下雨吗'),PLACE,f))
    def test_missing_data_does_not_speak_fabricated_values(self):
        for field in ['weather_code','temperature_2m_min','temperature_2m_max','time']:
            f=forecast();f['daily'][field]=[]
            with self.subTest(field=field),self.assertRaises(gw.WeatherError):commands.reply(commands.parse('今天天气'),PLACE,f)
    def test_timezone_relative_date_and_midnight(self):
        f=forecast();f['utc_offset_seconds']=-36000
        today=gw.local_date(f);f['daily']['time']=[(today+timedelta(days=i)).isoformat() for i in range(3)]
        self.assertIn('明天小雨',commands.reply(commands.parse('明天天气'),PLACE,f))


class CacheAndNetwork(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(weather_places,'PATH',pathlib.Path(self.tmp.name)/'places.json'))
        gw._places.clear();gw._forecasts.clear()
    async def asyncTearDown(self):self.stack.close();self.tmp.cleanup()
    async def test_disk_location_survives_memory_reset(self):
        with patch.object(gw,'_nominatim',AsyncMock(return_value=PLACE)) as geo:
            self.assertEqual(await gw.find_place('大连市'),PLACE);gw._places.clear()
            self.assertEqual(await gw.find_place('大连市'),PLACE);geo.assert_awaited_once()
        if os.name != 'nt':self.assertEqual(weather_places.PATH.stat().st_mode&0o777,0o600)  # Windows 没有 POSIX 权限位
    async def test_corrupt_expired_and_bad_coordinates_ignored(self):
        weather_places.PATH.write_text('invalid');self.assertIsNone(weather_places.get('大连市'))
        for row in [{'name':'大连','lat':999,'lon':0,'saved':time.time()}, {'name':'大连','lat':1,'lon':1,'saved':time.time()-weather_places.TTL-1}]:
            weather_places.PATH.write_text(json.dumps({'大连市':row}));self.assertIsNone(weather_places.get('大连市'))
    async def test_location_changes_have_independent_keys(self):
        weather_places.put('大连市',PLACE);other=gw.Place('北京',39.9,116.4);weather_places.put('北京',other)
        self.assertNotEqual(weather_places.get('北京')['lat'],weather_places.get('大连市')['lat'])
    async def test_forecast_cache_and_no_air_request(self):
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(200,json=forecast())
        with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            first=await gw.fetch_forecast(PLACE);second=await gw.fetch_forecast(PLACE)
            self.assertEqual(first,second);self.assertEqual(len(calls),1)
            self.assertEqual(calls[0].url.host,'api.open-meteo.com')
            key=next(iter(gw._forecasts));v=gw._forecasts[key];gw._forecasts[key]=(time.time()-601,v[1],v[2])
            await gw.fetch_forecast(PLACE);self.assertEqual(len(calls),2)
    async def test_new_local_day_invalidates_cache(self):
        key=(round(PLACE.lat,4),round(PLACE.lon,4));f=forecast()
        gw._forecasts[key]=(time.time(),f,gw.local_date(f)-timedelta(days=1))
        with patch.object(gw,'_client',side_effect=lambda:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=f)))) as client:
            await gw.fetch_forecast(PLACE);client.assert_called_once()
    async def test_upstream_errors_and_outdated_forecast_are_not_cached(self):
        old=forecast();old['daily']['time']=['2000-01-01']
        for response in [httpx.Response(502),httpx.Response(200,json={}),httpx.Response(200,json=old)]:
            with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:response))):
                with self.assertRaises(gw.WeatherError):await gw.fetch_forecast(PLACE)
                self.assertFalse(gw._forecasts)
    async def test_connection_failure_retries_same_query_once_then_caches_success(self):
        for error in (httpx.ConnectTimeout, httpx.ConnectError):
            with self.subTest(error=error.__name__):
                gw._forecasts.clear()
                calls=[]
                data=forecast()
                def handler(req):
                    calls.append(req)
                    if len(calls)==1:raise error('temporary connection failure',request=req)
                    return httpx.Response(200,json=data)
                with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
                    self.assertEqual(await gw.fetch_forecast(PLACE),data)
                    self.assertEqual(await gw.fetch_forecast(PLACE),data)
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0].url,calls[1].url)
                self.assertEqual(calls[0].method,'GET')
                self.assertEqual(calls[0].url.host,'api.open-meteo.com')
    async def test_persistent_connection_timeout_stops_after_two_attempts_without_cache(self):
        calls=[]
        def handler(req):
            calls.append(req)
            raise httpx.ConnectTimeout('unreachable',request=req)
        with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            with self.assertRaises(gw.WeatherError):await gw.fetch_forecast(PLACE)
        self.assertEqual(len(calls),2)
        self.assertFalse(gw._forecasts)
    async def test_other_failures_are_not_retried_or_cached(self):
        for failure in ('http_status','read_timeout','invalid_json','missing_data'):
            with self.subTest(failure=failure):
                calls=[]
                def handler(req):
                    calls.append(req)
                    if failure=='read_timeout':raise httpx.ReadTimeout('slow response',request=req)
                    if failure=='http_status':return httpx.Response(503)
                    if failure=='invalid_json':return httpx.Response(200,text='not json')
                    return httpx.Response(200,json={})
                with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
                    with self.assertRaises(gw.WeatherError):await gw.fetch_forecast(PLACE)
                self.assertEqual(len(calls),1)
                self.assertFalse(gw._forecasts)
    async def test_outer_deadline_can_cancel_retry(self):
        calls=[]
        retry_started=asyncio.Event()
        async def handler(req):
            calls.append(req)
            if len(calls)==1:raise httpx.ConnectTimeout('unreachable',request=req)
            retry_started.set()
            await asyncio.Event().wait()
        with patch.object(gw,'_client',lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            task=asyncio.create_task(gw.fetch_forecast(PLACE))
            await asyncio.wait_for(retry_started.wait(),1)
            with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(task,.01)
        self.assertTrue(task.cancelled())
        self.assertEqual(len(calls),2)
        self.assertFalse(gw._forecasts)


class HookFlow(unittest.IsolatedAsyncioTestCase):
    async def test_existing_weather_lookup_is_reused(self):
        with patch.object(gw,'find_place',AsyncMock(return_value=PLACE)) as geo,patch.object(gw,'fetch_forecast',AsyncMock(return_value=forecast())) as fetch:
            result=await hook.intercept(conn(),'今天天气');self.assertTrue(result['handled']);self.assertIn('大连',result['reply'])
            geo.assert_awaited_once_with('大连市');fetch.assert_awaited_once_with(PLACE)
    async def test_requested_city_overrides_config(self):
        with patch.object(gw,'find_place',AsyncMock(return_value=PLACE)) as geo,patch.object(gw,'fetch_forecast',AsyncMock(return_value=forecast())):
            await hook.intercept(conn(),'北京明天天气');geo.assert_awaited_once_with('北京')
    async def test_failure_consumed_and_does_not_fall_back(self):
        for error in [gw.WeatherError('天气服务暂时连不上'),asyncio.TimeoutError(),ValueError('test')]:
            with patch.object(commands,'execute',AsyncMock(side_effect=error)):
                r=await hook.intercept(conn(),'今天天气');self.assertTrue(r['handled']);self.assertIn('没查到',r['reply'])
    async def test_cancellation_before_and_after_lookup(self):
        c=conn()
        with patch.object(gw,'find_place',AsyncMock(return_value=PLACE)) as geo:
            r=await hook.intercept(c,'今天天气',lambda:True);self.assertTrue(r['cancelled']);geo.assert_not_awaited()
        stale=False
        async def slow(place):
            nonlocal stale;stale=True;return forecast()
        with patch.object(gw,'find_place',AsyncMock(return_value=PLACE)),patch.object(gw,'fetch_forecast',slow):
            r=await hook.intercept(c,'今天天气',lambda:stale);self.assertTrue(r['cancelled']);self.assertIsNone(r['reply'])
    async def test_dispatch_to_tts_no_model_path_and_existing_ha(self):
        c=conn();c.chat=Mock(side_effect=AssertionError('LLM must not run'))
        self.assertIs(pre_llm.PLUGINS[0],home_assistant)
        with patch.object(commands,'execute',AsyncMock(return_value='大连今天晴。')),patch.object(sendAudioHandle,'send_stt_message',AsyncMock()),patch('core.agent.chat_history.sync'):
            self.assertTrue(await pre_llm.handle(c,'今天天气',turn_gen.current(c)))
        c.chat.assert_not_called();c.tts.tts_one_sentence.assert_called_once()
        self.assertEqual(c.tts.tts_one_sentence.call_args.kwargs['content_detail'],'大连今天晴。')
        self.assertEqual(c.tts.tts_text_queue.qsize(),2)
        self.assertTrue(any('llm_calls=0' in str(call) for call in c.logger.bind().info.call_args_list))
    async def test_unmatched_question_continues_normal_routing(self):
        self.assertFalse(await pre_llm.handle(conn(),'如果下雨就关窗',0))
    async def test_json_asr_content_is_supported(self):
        c=conn()
        with patch.object(commands,'execute',AsyncMock(return_value='大连今天晴。')),patch.object(sendAudioHandle,'send_stt_message',AsyncMock()),patch('core.agent.chat_history.sync'):
            self.assertTrue(await pre_llm.handle(c,json.dumps({'content':'今天天气'},ensure_ascii=False),0))
    async def test_query_completed_after_abort_is_not_spoken(self):
        c=conn()
        async def abort(*args):turn_gen.bump(c);return '不应播放'
        with patch.object(commands,'execute',side_effect=abort),patch.object(sendAudioHandle,'send_stt_message',AsyncMock()):
            self.assertTrue(await pre_llm.handle(c,'今天天气',0))
        c.tts.tts_one_sentence.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
