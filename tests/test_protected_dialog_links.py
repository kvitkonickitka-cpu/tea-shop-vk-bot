import json
import re
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.api import dialog_links as api
from app.core.config import settings
from app.modules.dialog import telegram_client
from app.privacy import crypto, dialog_links as links

PEER = 123456789
RAW = f'https://vk.com/gim240363526?sel={PEER}'

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'dialog_links_enabled', True)
    monkeypatch.setattr(settings, 'dialog_links_origin', 'https://stats.teapotwice.ru')
    monkeypatch.setattr(settings, 'dialog_links_metabase_url', 'http://metabase:3000')


def test_encrypted_link_randomized_and_authenticated():
    first, second = links.make_link(PEER), links.make_link(PEER)
    assert first != second and str(PEER) not in first
    assert links.destination(first.rsplit('/', 1)[1]) == RAW
    token = first.rsplit('/', 1)[1]
    with pytest.raises(ValueError):
        links.destination(('A' if token[0] != 'A' else 'B') + token[1:])


def test_link_bound_to_store_and_key(monkeypatch):
    token = links.make_link(PEER).rsplit('/', 1)[1]
    monkeypatch.setattr(settings, 'vk_group_id', 'club111')
    with pytest.raises(ValueError):
        links.destination(token)
    monkeypatch.setattr(settings, 'vk_group_id', 'club240363526')
    monkeypatch.setattr(settings, 'pii_encryption_key', '')
    with pytest.raises(ValueError):
        links.destination(token)
    with pytest.raises(crypto.KeyMissing):
        links.protect_text(RAW)


def test_legacy_text_and_button_have_no_vk_identifier():
    payload = {'chat_id': str(PEER), 'text': f'диалог {PEER}', 'reply_markup': {'inline_keyboard': [[
        {'text': 'Диалог', 'url': RAW}, {'text': 'Опубликовать', 'callback_data': 'review:47'}]]}}
    protected = links.protect_payload(payload)
    assert str(PEER) not in protected['text']
    assert str(PEER) not in json.dumps(protected['reply_markup'])
    assert protected['chat_id'] == str(PEER)
    assert protected['reply_markup']['inline_keyboard'][0][1]['callback_data'] == 'review:47'
    assert payload['reply_markup']['inline_keyboard'][0][0]['url'] == RAW
    legacy = links.protect_text(f'Удалить ПДн покупателя {PEER}: clients/{PEER}/anonymize\n{RAW}')
    assert str(PEER) not in legacy and 'vk.com' not in legacy


@pytest.fixture
def page(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    state = {'user_status': 200, 'user': {'is_superuser': True, 'is_active': True}, 'requests': []}
    def metabase(request):
        state['requests'].append(request)
        assert request.url.host == 'metabase'
        if state.get('unavailable'):
            raise httpx.ConnectError('unavailable', request=request)
        if request.url.path == '/api/user/current':
            assert 'metabase.SESSION=session-test' in request.headers.get('cookie', '')
            assert 'X-Metabase-Session' not in request.headers
            assert 'unrelated' not in request.headers.get('cookie', '')
            return httpx.Response(state['user_status'], json=state['user'])
        assert request.url.path == '/api/session/properties'
        assert not request.headers.get('cookie')
        return httpx.Response(200, json={'google-auth-client-id': 'synthetic.apps.googleusercontent.com'})
    monkeypatch.setattr(api, '_metabase_client', lambda: httpx.AsyncClient(
        base_url='http://metabase:3000', transport=httpx.MockTransport(metabase)))
    state['app'] = app
    state['path'] = urlsplit(links.make_link(PEER)).path
    return state


async def get(page, *, authenticated=False, path=None, headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=page['app']), base_url='https://stats.teapotwice.ru',
                                 follow_redirects=False) as client:
        if authenticated:
            client.cookies.set('metabase.SESSION', 'session-test')
            client.cookies.set('unrelated', 'must-not-forward')
        return await client.get(path or page['path'], headers=headers)


async def test_anonymous_preview_never_decrypts(page, monkeypatch):
    monkeypatch.setattr(links, 'destination', lambda _: pytest.fail('Decrypted before auth'))
    response = await get(page)
    assert response.status_code == 200 and 'Войдите через Google' in response.text
    assert str(PEER) not in response.text and 'vk.com' not in response.text
    assert 'location.reload()' in response.text and '/api/session/google_auth' in response.text
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'frame-ancestors' in response.headers['content-security-policy']
    assert not response.headers.get('location')


async def test_admin_redirects_only_to_fixed_vk_dialog(page):
    response = await get(page, authenticated=True, path=page['path']+'?next=https://evil.example')
    assert response.status_code == 303 and response.headers['location'] == RAW
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert 'no-store' in response.headers['cache-control']


@pytest.mark.parametrize('user', [
    {'is_active': True, 'is_superuser': False}, {'is_active': False, 'is_superuser': True},
    {'is_active': True, 'is_superuser': 'true'}, {},
])
async def test_unauthorized_account_cannot_resolve(page, user, monkeypatch):
    page['user'] = user
    monkeypatch.setattr(links, 'destination', lambda _: pytest.fail('Decrypted without permission'))
    response = await get(page, authenticated=True)
    assert response.status_code == 403 and not response.headers.get('location')
    assert str(PEER) not in response.text


async def test_expired_session_gets_login_without_destination(page):
    page['user_status'] = 401
    response = await get(page, authenticated=True)
    assert response.status_code == 200 and 'Войдите через Google' in response.text
    assert 'vk.com' not in response.text and str(PEER) not in response.text


async def test_permission_is_rechecked_on_every_click(page):
    assert (await get(page, authenticated=True)).status_code == 303
    page['user']['is_superuser'] = False
    assert (await get(page, authenticated=True)).status_code == 403


async def test_outage_fails_closed(page):
    page['unavailable'] = True
    response = await get(page, authenticated=True)
    assert response.status_code == 503 and not response.headers.get('location')
    assert str(PEER) not in response.text


async def test_tampered_token_and_missing_auth_header(page):
    bad = page['path'][:-12] + 'AAAAAAAAAAAA'
    assert (await get(page, authenticated=True, path=bad)).status_code == 404
    # API keys, spoofed user headers or query arguments never authenticate this route.
    response = await get(page, headers={'X-Metabase-Session': 'session-test', 'X-User-Role': 'admin'})
    assert response.status_code == 200 and not response.headers.get('location')


@pytest.mark.parametrize('method', ['send_message', 'reply', 'call'])
async def test_telegram_http_payload_is_protected(monkeypatch, method):
    requests = []
    def send(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(PEER) not in json.dumps(body) and 'vk.com' not in json.dumps(body)
        assert '/secure-dialog/' in body['text']
        return httpx.Response(200, json={'ok': True, 'result': {}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(telegram_client.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(send), **kw))
    if method == 'send_message':
        await telegram_client.send_message(RAW)
    elif method == 'reply':
        await telegram_client.reply(RAW, chat_id='manager', reply_to=42)
    else:
        await telegram_client.call('sendMessage', {'text': RAW, 'chat_id': 'manager'})
    assert len(requests) == 1
