"""Resolve an opaque manager link only for an authenticated Metabase admin."""
from __future__ import annotations

import html
import json
import secrets
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import settings
from app.privacy import dialog_links

router = APIRouter(tags=["manager-links"])
_PRIVATE = {
    "Cache-Control": "no-store, private",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    "Vary": "Cookie",
}


def _page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{html.escape(title)}</title><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></html>',
        status_code=status, headers={**_PRIVATE, "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"},
    )


def _login(client_id: str, *, denied: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    encoded = json.dumps(client_id).replace("<", "\\u003c")
    body = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Открыть диалог — Два чайника</title>
<style>body{margin:0;background:#f5f2eb;color:#272923;font:17px/1.55 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}main{box-sizing:border-box;width:min(92vw,440px);padding:36px;background:white;border-radius:20px;box-shadow:0 8px 32px #26332614}h1{font-size:26px;line-height:1.2}p{color:#5d6258}#google{min-height:44px;margin:24px 0 12px}label{display:block;font-size:14px}#status{font-size:14px;min-height:24px}small{display:block;color:#6b7166;margin-top:24px}</style>
<script nonce="NONCE">
async function signedIn(response) {
  const status = document.getElementById('status');
  status.textContent = 'Проверяем вход…';
  try {
    const result = await fetch('/api/session/google_auth', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: response.credential, remember: document.getElementById('remember').checked})
    });
    if (!result.ok) throw new Error('login');
    location.reload();
  } catch (_) { status.textContent = 'Не удалось войти. Используйте Google-аккаунт администратора магазина и попробуйте ещё раз.'; }
}
function initGoogle() {
  google.accounts.id.initialize({client_id: CLIENT_ID, callback: signedIn, auto_select: false});
  google.accounts.id.renderButton(document.getElementById('google'), {type:'standard', theme:'outline', size:'large', text:'signin_with', locale:'ru'});
}
</script></head><body><main><div>Два чайника · Для команды</div><h1>Открыть диалог</h1>
<p>Войдите через Google. После проверки доступа диалог откроется автоматически.</p>
<div id="google"></div><label><input id="remember" type="checkbox"> Запомнить меня на этом устройстве</label>
<p id="status" role="status" aria-live="polite"></p>
<small>Используйте аккаунт администратора Metabase. Если Google не открывается внутри Telegram, выберите «Открыть в браузере» в меню.</small>
<noscript>Для входа через Google включите JavaScript.</noscript></main>
<script nonce="NONCE" src="https://accounts.google.com/gsi/client"></script>
<script nonce="NONCE">if (window.google) { initGoogle(); } else { document.getElementById("status").textContent = "Google не загрузился. Обновите страницу или откройте ссылку во внешнем браузере."; }</script></body></html>'''
    body = body.replace("NONCE", nonce).replace("CLIENT_ID", encoded)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}' https://accounts.google.com; "
           "connect-src 'self' https://accounts.google.com; frame-src https://accounts.google.com; "
           "style-src 'unsafe-inline' https://accounts.google.com; img-src data: https://accounts.google.com https://*.gstatic.com; "
           "font-src https://fonts.gstatic.com; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    if denied:
        body = body.replace('Войдите через Google. После проверки доступа диалог откроется автоматически.',
                            'У этого аккаунта нет доступа. Войдите через Google под аккаунтом администратора магазина.')
    return HTMLResponse(body, status_code=403 if denied else 200, headers={**_PRIVATE, "Content-Security-Policy": csp})


def _metabase_client():
    return httpx.AsyncClient(base_url=settings.dialog_links_metabase_url, timeout=5.0, follow_redirects=False)


@router.get("/secure-dialog/{token}")
async def open_dialog(token: str, request: Request):
    if not settings.dialog_links_enabled:
        return _page("Ссылка недоступна", "Защищённые ссылки пока не включены.", 404)
    if len(token) > 512 or not dialog_links._TOKEN.fullmatch(token):
        return _page("Ссылка недействительна", "Проверьте, что ссылка скопирована целиком.", 404)
    canonical = dialog_links.origin()
    if request.url.hostname != urlsplit(canonical).hostname:
        return RedirectResponse(canonical + "/secure-dialog/" + token, status_code=303, headers=_PRIVATE)
    cookies = {key: request.cookies[key] for key in ("metabase.SESSION", "metabase.SESSION_ID") if request.cookies.get(key)}
    denied = False
    try:
        async with _metabase_client() as client:
            if cookies:
                response = await client.get("/api/user/current", cookies=cookies)
                if response.status_code == 200:
                    user = response.json()
                    if not isinstance(user, dict) or user.get("is_superuser") is not True or user.get("is_active") is not True:
                        denied = True
                    else:
                        try:
                            destination = dialog_links.destination(token)
                        except ValueError:
                            return _page("Ссылка недействительна", "Ссылка повреждена или ключ доступа был изменён.", 404)
                        return RedirectResponse(destination, status_code=303, headers=_PRIVATE)
                if response.status_code not in (401, 403):
                    response.raise_for_status()
            response = await client.get("/api/session/properties")
            response.raise_for_status()
            properties = response.json()
            client_id = properties.get("google-auth-client-id") if isinstance(properties, dict) else None
            if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
                raise ValueError("Google login is not configured")
            return _login(client_id, denied=denied)
    except (httpx.HTTPError, ValueError, TypeError):
        return _page("Вход временно недоступен", "Не удалось проверить доступ. Обновите страницу через минуту.", 503)
