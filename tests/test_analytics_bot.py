import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.modules.analytics_bot.assistant import Assistant
from app.modules.analytics_bot.config import AnalyticsBotSettings
from app.modules.analytics_bot.reports import Ads, Period, PostgresSales, Sales, Status, UnconnectedAds, render
from app.modules.analytics_bot.service import Analyst, Telegram, TelegramError
from app.modules.analytics_bot.state import State

NOW = datetime(2026, 10, 31, 7, tzinfo=timezone.utc)


def cfg(tmp_path, **kw):
    return AnalyticsBotSettings(token="dedicated-test", chat_id=-123, state_path=tmp_path/'state.sqlite3', **kw)


def update(text, chat=-123, sender=17, **fields):
    return {'update_id': 42, 'message': {'message_id': 5, 'chat': {'id':chat},
            'from': {'id':sender, 'is_bot':False}, 'text':text, **fields}}


def setup(tmp_path, **kw):
    settings=cfg(tmp_path, **kw)
    state=State(settings.state_path)
    tg=NS(send=AsyncMock())
    sales=NS(read=AsyncMock(return_value=Sales(Status.OK)))
    ai=NS(answer=AsyncMock(return_value='Ответ аналитика'), memory={})
    analyst=Analyst(settings,tg,state,sales,UnconnectedAds(),'TeaAnalystBot',99,ai)
    return analyst,tg,state,ai


def test_moscow_completed_period():
    period=Period.completed(datetime(2026,10,30,22,tzinfo=timezone.utc),7)
    assert (period.start,period.end)==(date(2026,10,24),date(2026,10,31))
    with pytest.raises(ValueError): Period.completed(NOW,0)


@pytest.mark.parametrize('status,expected',[(Status.NOT_CONNECTED,'пока не подключён'),(Status.ERROR,'временно недоступен'),(Status.OK,'за период нет')])
def test_missing_error_zero_are_distinct(status,expected):
    text=render(Period.completed(NOW),Sales(status),Ads())
    assert expected in text
    assert 'ROAS не рассчитаны' in text
    if status != Status.OK: assert '0,00 ₽' not in text


def test_average_and_refund_definition():
    text=render(Period.completed(NOW),Sales(Status.OK,5,2,Decimal('3000'),1),Ads())
    assert '1 500,00 ₽' in text and 'частичные возвраты' in text


@pytest.mark.parametrize('text,chat,fields',[
    ('@TeaAnalystBot вопрос',999,{}),
    ('просто обсуждение',-123,{}),
    ('@TeaAnalystBotExtra вопрос',-123,{}),
    ('/report@OtherBot',-123,{}),
    ('@TeaAnalystBot вопрос',-123,{'forward_origin':{'type':'user'}}),
    ('вопрос',-123,{'reply_to_message':{'from':{'id':98}}}),
    ('/some_other_command',-123,{})])
async def test_ignore_unaddressed_or_other_chat(tmp_path,text,chat,fields):
    a,tg,state,ai=setup(tmp_path)
    await a.handle(update(text,chat=chat,**fields),NOW)
    tg.send.assert_not_awaited(); ai.answer.assert_not_awaited();state.close()


@pytest.mark.parametrize('sender',[17,18,98765])
async def test_any_chat_member_can_mention(tmp_path,sender):
    a,tg,state,ai=setup(tmp_path)
    await a.handle(update('@teaanalystbot почему нет продаж?',sender=sender),NOW)
    assert ai.answer.await_args.args[1]=='почему нет продаж?'
    assert ai.answer.await_args.args[0]==(-123,None,sender)
    tg.send.assert_awaited_once_with('Ответ аналитика',reply_to=5);state.close()


async def test_reply_to_report_passes_context(tmp_path):
    a,tg,state,ai=setup(tmp_path)
    await a.handle(update('А почему?',reply_to_message={'from':{'id':99},'text':'Продаж пока нет'}),NOW)
    assert ai.answer.await_args.args[-1]=='Продаж пока нет'
    state.close()


async def test_anonymous_admin_is_also_chat_member(tmp_path):
    a,tg,state,ai=setup(tmp_path)
    msg=update('@TeaAnalystBot вопрос',sender_chat={'id':-123})
    msg['message']['from']['is_bot']=True
    await a.handle(msg,NOW)
    ai.answer.assert_awaited_once();state.close()


async def test_command_period_validation(tmp_path):
    a,tg,state,ai=setup(tmp_path)
    await a.handle(update('/sales 999'),NOW)
    a.sales.read.assert_not_awaited()
    await a.handle(update('/sales 7'),NOW)
    assert a.sales.read.await_args.args[0]==Period(date(2026,10,24),date(2026,10,31))
    ai.answer.assert_not_awaited();state.close()


async def test_daily_persisted_and_failed_send_retried(tmp_path):
    a,tg,state,ai=setup(tmp_path,daily_enabled=True)
    tg.send.side_effect=RuntimeError('offline')
    with pytest.raises(RuntimeError):await a.daily(NOW)
    assert state.get('daily:-123:2026-10-31') is None
    tg.send.side_effect=None
    await a.daily(NOW);state.close()
    a.state=State(a.settings.state_path)
    await a.daily(NOW)
    assert tg.send.await_count==2
    await a.daily(NOW+timedelta(days=1))
    assert tg.send.await_count==3;a.state.close()


async def test_disabled_schedule(tmp_path):
    a,tg,state,ai=setup(tmp_path)
    await a.daily(NOW);tg.send.assert_not_awaited();state.close()


def test_config_rejects_private_chat_and_shared_token(tmp_path,monkeypatch):
    with pytest.raises(ValidationError):AnalyticsBotSettings(token='test',chat_id=123)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN','dedicated-test')
    with pytest.raises(ValidationError):cfg(tmp_path)


async def test_telegram_payload_and_secret_safe_errors(tmp_path):
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(500,text='raw-secret-and-private-data')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        tg=Telegram(cfg(tmp_path),client)
        with pytest.raises(TelegramError) as exc:await tg.send('Сводка',reply_to=5)
    assert 'raw-secret' not in str(exc.value) and 'dedicated-test' not in str(exc.value)
    assert requests[0]['chat_id']==-123
    assert requests[0]['reply_parameters']['message_id']==5


def response(output,status='completed'):
    return dict(id='resp_test',object='response',created_at=1,model='gpt-6.1-sol',status=status,
                output=output,usage=dict(input_tokens=100,output_tokens=20,total_tokens=120))


def message(text):
    return dict(id='msg_test',type='message',role='assistant',status='completed',
                content=[dict(type='output_text',text=text,annotations=[])])


async def test_real_sdk_tool_loop_privacy_and_context(tmp_path):
    requests=[]
    outputs=[
        [dict(id='rs',type='reasoning',summary=[],encrypted_content='opaque'),
         dict(id='fc',type='function_call',call_id='call_1',name='get_summary',arguments='{"days":7,"offset_days":7}')],
        [message('Для сравнения нужны данные продаж.')], [message('Реклама ещё не подключена.')]]
    def handle(request):
        requests.append(json.loads(request.content));return httpx.Response(200,json=response(outputs.pop(0)))
    sdk=AsyncOpenAI(api_key='fake',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    settings=cfg(tmp_path,openai_api_key='fake')
    state=State(settings.state_path)
    sales=NS(read=AsyncMock(return_value=Sales(Status.NOT_CONNECTED)))
    a=Assistant(settings,sales,UnconnectedAds(),state,sdk)
    answer=await a.answer(('chat','user'),'Сравни недели. Почта test@example.com, +7 (999) 123-45-67',NOW)
    assert answer=='Для сравнения нужны данные продаж.'
    wire=json.dumps(requests,ensure_ascii=False)
    assert 'test@example.com' not in wire and '123-45-67' not in wire
    assert requests[0]['store'] is False and requests[0]['max_output_tokens']==2000
    assert requests[1]['input'][-1]['call_id']=='call_1'
    assert requests[1]['input'][-2]['name']=='get_summary'
    assert requests[1]['input'][1]['encrypted_content']=='opaque'
    assert 'not_connected' in requests[1]['input'][-1]['output']
    assert sales.read.await_args.args[0]==Period(date(2026,10,17),date(2026,10,24))
    await a.answer(('chat','user'),'А по рекламе?',NOW)
    assert any(i.get('content')==answer for i in requests[-1]['input'])
    assert state.get('input_tokens:2026-10-31')=='300'
    await a.close();state.close()


async def test_api_failure_and_daily_limit(tmp_path):
    settings=cfg(tmp_path,openai_api_key='fake',daily_question_limit=1)
    state=State(settings.state_path)
    client=NS(responses=NS(create=AsyncMock(side_effect=RuntimeError('SECRET'))))
    a=Assistant(settings,PostgresSales(''),UnconnectedAds(),state,client)
    answer=await a.answer(('a',),'Вопрос',NOW)
    assert 'SECRET' not in answer and 'не удалось' in answer
    assert 'лимит' in await a.answer(('b',),'Другой вопрос',NOW)
    assert client.responses.create.await_count==1;state.close()


async def test_tool_rejects_unbounded_or_invalid_periods(tmp_path):
    settings=cfg(tmp_path);state=State(settings.state_path)
    sales=NS(read=AsyncMock())
    a=Assistant(settings,sales,UnconnectedAds(),state)
    for days,offset in [(0,0),(7,-1),(7,366),(True,0)]:
        assert 'error' in await a.summary(NOW,days,offset)
    sales.read.assert_not_awaited();state.close()


async def test_sales_real_database_msk_tests_refunds(clean):
    # Uses the real application view; the bot reads it through its own read-only connection.
    from app.modules.orders.models import Order
    from app.modules.analytics.models import Client
    from app.core.client_key import client_key
    t=datetime(2026,10,29,21,30,tzinfo=timezone.utc)  # Oct 30 MSK
    async with clean() as session:
        session.add(Client(peer_id=77115,client_key=client_key(77115),is_test=True))
        for peer,paid,status,test in [(77111,True,'confirmed',False),(77112,True,'refunded',False),
                                      (77113,True,'confirmed',True),(77114,False,'confirmed',False),
                                      (77115,True,'confirmed',False)]:
            session.add(Order(peer_id=peer,items=[],items_total=900,delivery_cost=100,total=1000,
                              created_at=t,paid_at=t if paid else None,
                              payment_status='succeeded' if paid else 'pending',status=status,is_test=test))
        # Current MSK day must not enter yesterday's report.
        session.add(Order(peer_id=77116,items=[],items_total=900,delivery_cost=100,total=1000,
                          created_at=t+timedelta(days=1),paid_at=t+timedelta(days=1),
                          payment_status='succeeded',status='confirmed'))
        await session.commit()
    source=PostgresSales(os.environ['DATABASE_URL'])
    result=await source.read(Period.completed(NOW))
    assert result==Sales(Status.OK,3,1,Decimal('1000'),1)


async def test_database_failure_not_zero(monkeypatch):
    import asyncpg
    monkeypatch.setattr(asyncpg,'connect',AsyncMock(side_effect=RuntimeError('dsn-secret')))
    result=await PostgresSales('postgresql://invalid').read(Period.completed(NOW))
    assert result.status==Status.ERROR


async def test_tool_budget_prevents_unbounded_model_loop(tmp_path):
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json=response([dict(id='fc',type='function_call',call_id=f'c{len(requests)}',
            name='get_summary',arguments='{"days":1,"offset_days":0}')]))
    settings=cfg(tmp_path,openai_api_key='fake');state=State(settings.state_path)
    sdk=AsyncOpenAI(api_key='fake',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    a=Assistant(settings,PostgresSales(''),UnconnectedAds(),state,sdk)
    assert 'не удалось' in await a.answer(('a',),'Проверь',NOW)
    assert len(requests)==3 and requests[-1]['tool_choice']=='none'
    await a.close();state.close()


async def test_missing_api_key_makes_no_calls_and_spends_no_budget(tmp_path):
    settings=cfg(tmp_path);state=State(settings.state_path)
    client=NS(responses=NS(create=AsyncMock()))
    a=Assistant(settings,PostgresSales(''),UnconnectedAds(),state,client)
    assert 'нужен серверный ключ' in await a.answer(('a',),'Вопрос',NOW)
    client.responses.create.assert_not_awaited()
    assert state.get('questions:2026-10-31') is None;state.close()


async def test_session_isolation_and_expiration(tmp_path):
    requests=[]
    def handle(request):
        requests.append(json.loads(request.content));return httpx.Response(200,json=response([message('Источника пока нет.')]))
    settings=cfg(tmp_path,openai_api_key='fake');state=State(settings.state_path)
    sdk=AsyncOpenAI(api_key='fake',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    a=Assistant(settings,PostgresSales(''),UnconnectedAds(),state,sdk)
    await a.answer(('chat',1),'Первый вопрос',NOW)
    await a.answer(('chat',2),'Второй вопрос',NOW)
    assert len(requests[-1]['input'])==1
    await a.answer(('chat',1),'После паузы',NOW+timedelta(minutes=31))
    assert len(requests[-1]['input'])==1
    await a.close();state.close()


async def test_other_bots_cannot_trigger_llm(tmp_path):
    a,tg,state,ai=setup(tmp_path)
    msg=update('@TeaAnalystBot вопрос');msg['message']['from']['is_bot']=True
    await a.handle(msg,NOW)
    tg.send.assert_not_awaited();ai.answer.assert_not_awaited();state.close()


async def test_daily_waits_for_moscow_hour(tmp_path):
    a,tg,state,ai=setup(tmp_path,daily_enabled=True)
    await a.daily(datetime(2026,10,31,5,59,tzinfo=timezone.utc))
    tg.send.assert_not_awaited();state.close()
