#!/usr/bin/env python3
"""Проверка баланса Aeza: хватит ли денег на ближайшее автопродление.

Живёт на самом VPS Aeza рядом со сторожем и раз в день спрашивает API Aeza.
Нужна потому, что на Aeza держится прокси к Claude и Telegram: сервер,
не продлённый за неуплату, гасит бота так же, как поломка, но предупредить
об этом заранее может только проверка баланса — монитор Timeweb увидит
уже упавший сервер.

Тревога в чат Ops, если до продления какой-то услуги осталось не больше
WARN_DAYS дней, а на счёте меньше, чем стоят все такие продления. Повторяется
каждый день, пока не пополнят: раз в сутки — не шум. Ещё тревога, если
услуга не в статусе active и если сам API не ответил (ключ отозвали —
иначе проверка молча перестала бы работать).

Ставится по deploy/watchdog/README.md. Ключ, токен и чат — в
/etc/teashop-watchdog.env, ключ передаётся только заголовком и не печатается.
Проверить, ничего не отправляя: AEZA_BALANCE_DRY_RUN=1 aeza-balance
(WARN_DAYS=60 — увидеть текст тревоги заранее).
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = "/etc/teashop-watchdog.env"
WARN_DAYS = int(os.environ.get("WARN_DAYS", "14"))
URGENT_DAYS = 3
DRY_RUN = os.environ.get("AEZA_BALANCE_DRY_RUN") == "1"
TOP_UP_URL = "https://my.aeza.ru/finance"


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key] = value.strip().strip('"')
    return env


def aeza(env, path):
    request = urllib.request.Request(
        f"{env['AEZA_API_BASE']}/api/v2/{path}",
        headers={"X-API-Key": env["AEZA_API_KEY"], "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def send(env, text):
    if DRY_RUN:
        print(text)
        return
    data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text}).encode()
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
    urllib.request.urlopen(url, data=data, timeout=20).read()


def rub(kopecks):
    # Aeza отдаёт суммы в копейках: balance 1900 — это 19 ₽.
    return f"{kopecks / 100:,.0f} ₽".replace(",", " ")


def main():
    env = load_env(ENV_FILE)
    if not env.get("AEZA_API_KEY") or not env.get("AEZA_API_BASE"):
        print("Нет AEZA_API_KEY/AEZA_API_BASE в", ENV_FILE, file=sys.stderr)
        return 1

    try:
        account = aeza(env, "accounts/me")
        services = aeza(env, "services").get("items", [])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
        code = getattr(error, "code", None)
        reason = f"HTTP {code}" if code else type(error).__name__
        send(env, f"⚠️ Проверка баланса Aeza не сработала: {reason}. "
                  "Если ключ отозван — новый в my.aeza.ru → Настройки → API ключи, "
                  "записать /root/aeza_set_key.sh на Aeza.")
        return 0

    balance = int(account.get("balance") or 0) + int(account.get("bonusBalance") or 0)
    now = dt.datetime.now(dt.timezone.utc)
    lines, need, soonest = [], 0, None

    for service in services:
        name = service.get("name") or service.get("id")
        status = service.get("status")
        if status != "active":
            lines.append(f"• {name}: статус {status}")
            continue
        if not service.get("autoProlong") or not service.get("expiresAt"):
            continue
        expires = dt.datetime.fromisoformat(service["expiresAt"].replace("Z", "+00:00"))
        days_left = (expires - now).days
        if days_left > WARN_DAYS:
            continue
        price = int(service.get("price") or 0)
        need += price
        soonest = days_left if soonest is None else min(soonest, days_left)
        moscow = expires.astimezone(dt.timezone(dt.timedelta(hours=3)))
        lines.append(f"• {name} ({service.get('productName', '')}): "
                     f"продление {moscow:%d.%m} за {rub(price)}")

    short = need > balance
    if not short and not any("статус" in line for line in lines):
        print(f"ok: на счёте {rub(balance)}, к продлению в {WARN_DAYS} дн. {rub(need)}")
        return 0

    icon = "🔴" if soonest is not None and soonest <= URGENT_DAYS else "💳"
    head = (f"{icon} Aeza: на счёте {rub(balance)}, не хватает {rub(need - balance)} на продление"
            if short else "⚠️ Aeza: услуга не активна")
    tail = (f"Не продлится — ляжет прокси к Claude и Telegram, бот замолчит. Пополнить: {TOP_UP_URL}"
            if short else "Прокси к Claude и Telegram на этом сервере. Кабинет: https://my.aeza.ru")
    send(env, "\n".join([head, *lines, tail]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
