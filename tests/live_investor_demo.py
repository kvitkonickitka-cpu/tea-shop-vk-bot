"""Explicit paid demo evaluation; only OpenAI network, synthetic test DB and mocked commerce."""
import json
import os
from urllib.parse import urlparse
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.modules.catalog import service as catalog
from app.modules.dialog import claude_client, openai_transport
from app.modules.orders import conversation as c, state
from app.modules.orders.models import Order
from tests.test_vk_buttons import world, PEER
from tests.test_privacy_interceptor import SECRETS

REAL_CONVERSE=claude_client.converse
pytestmark=pytest.mark.skipif(os.getenv('RUN_OPENAI_LIVE')!='1',reason='Explicit paid API evaluation')


@pytest.fixture
async def demo(monkeypatch,world):
    original_send=httpx.AsyncClient.send
    allowed=urlparse(settings.openai_base_url or 'https://api.openai.com').hostname
    calls=[];count=0
    async def send(client,request,**kwargs):
        nonlocal count
        assert request.url.host==allowed,'Commerce HTTP blocked'
        count+=1;assert count<=25
        body=json.loads(request.content)
        wire=json.dumps(body,ensure_ascii=False)
        assert body['store'] is False
        assert not any(secret in wire for secret in SECRETS)
        return await original_send(client,request,**kwargs)
    monkeypatch.setattr(httpx.AsyncClient,'send',send)
    monkeypatch.setattr(settings,'investor_demo_enabled',True)
    monkeypatch.setattr(settings,'enabled_payment_methods','bank_card,sbp')
    monkeypatch.setattr(settings,'openai_max_tokens',3000)
    monkeypatch.setattr(catalog,'load_items',lambda:[dict(name='Те Гуань (тест)',sku_id='P-TEST-100',price=550,
        in_stock=True,is_test=True,description='Тестовая позиция',brewing='85 °C',recommended=[],net_weight_g=100)])
    monkeypatch.setattr(claude_client,'converse',REAL_CONVERSE)
    monkeypatch.setattr(openai_transport,'_client',None)
    original=c._execute_tool
    async def track(peer,name,args):
        calls.append(name)
        return await original(peer,name,args)
    monkeypatch.setattr(c,'_execute_tool',track)
    notify=AsyncMock(return_value=True);monkeypatch.setattr(c,'_notify_manager',notify)
    yield world,calls,notify
    print('Real model tools:',calls,'API requests:',count,'manager sends:',notify.await_count)
    if openai_transport._client:await openai_transport._client.close()


@pytest.mark.parametrize('question,expected',[
    ('как заказ доставляете',('Ozon','СДЭК')),
    ('Как оплатить? Можно по СБП?',('ЮKassa',)),
    ('Какой товар есть и сколько стоит?',('550',)),
    ('А есть Да Хун Пао или пуэр? Что можно заказать?',('Те Гуань',)),
])
async def test_real_demo_information(clean,demo,question,expected):
    world,calls,notify=demo
    answer=await c.handle_turn(PEER,question)
    compact=answer.replace('\u00a0',' ').replace('\u202f',' ')
    assert all(word.casefold() in compact.casefold() for word in expected),answer
    assert 'передала' not in compact.casefold(),answer
    notify.assert_not_awaited()
    assert not world['payments']
    print('Synthetic question:',question,'Answer:',answer)


async def test_real_demo_order_to_invoice(clean,demo):
    world,calls,notify=demo
    for question in ('Хочу заказать одну пачку Те Гуань (тест).',
                     'Доставка в пункт Ozon в Краснодаре на Ставропольской.',
                     'Выбираю первый пункт из списка.',
                     'Иванов Иван, 89001234567, ivanov@mail.ru'):
        answer=await c.handle_turn(PEER,question)
        assert answer.strip() and c._NO_TEXT_FALLBACK not in answer
        print('Synthetic flow step complete; tools:',calls)
    assert world['payments'],answer
    async with clean() as s:
        order=(await s.execute(select(Order).where(Order.peer_id==PEER).order_by(Order.id.desc()))).scalars().first()
        assert order and order.is_test and order.items[0]['name']=='Те Гуань (тест)'
    assert 'propose_order' in calls and 'set_delivery_method' in calls
    notify.assert_not_awaited()
