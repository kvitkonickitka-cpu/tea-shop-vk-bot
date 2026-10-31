from datetime import datetime

import re

import httpx

from .reports import MSK, Period, render

HELP = ("Два чайника · Аналитик\n"
        "/report [1|7|30] — реклама и продажи\n"
        "/sales [1|7|30] — продажи\n"
        "/ads [1|7|30] — реклама\n"
        "/status — состояние источников и расписания\n"
        "Периоды — полные прошедшие дни по Москве. Без числа — вчера.\n"
        "Для вопроса: @логин_бота вопрос, /ask вопрос или ответ на сообщение бота.\n"
        "/reset — забыть ваш контекст уточнений.")


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    async def call(self, method, payload):
        cfg = self.settings
        headers = {"X-Proxy-Secret": cfg.proxy_secret.get_secret_value()} if cfg.proxy_secret.get_secret_value() else {}
        try:
            response = await self.client.post(
                f"{cfg.telegram_base_url.rstrip('/')}/bot{cfg.token.get_secret_value()}/{method}",
                json=payload, headers=headers, timeout=35)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise TelegramError("Telegram rejected request")
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError):
            raise TelegramError("Telegram request failed") from None

    async def send(self, text, reply_to=None):
        payload = {"chat_id": self.settings.chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        await self.call("sendMessage", payload)


class Analyst:
    def __init__(self, settings, telegram, state, sales, ads, username, bot_id=None, assistant=None):
        self.settings, self.telegram, self.state = settings, telegram, state
        self.sales, self.ads, self.username = sales, ads, username
        self.bot_id, self.assistant = bot_id, assistant

    async def report(self, now, days=1, kind="report"):
        from .reports import Ads, Sales, Status
        period = Period.completed(now, days)
        sales = await self.sales.read(period) if kind != "ads" else Sales(Status.NOT_CONNECTED)
        ads = await self.ads.read(period) if kind != "sales" else Ads()
        return render(period, sales, ads, kind)

    async def handle(self, update, now):
        # Chat is the access boundary; no per-user allowlist. Ignore bot loops/forwards.
        msg = update.get("message", {})
        sender = msg.get("from", {})
        if (msg.get("chat", {}).get("id") != self.settings.chat_id
            or (sender.get("is_bot") and not msg.get("sender_chat")) or msg.get("forward_origin")):
            return
        text = msg.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return
        session = (msg["chat"]["id"], msg.get("message_thread_id"), msg.get("sender_chat", {}).get("id", sender.get("id")))
        reply = msg.get("reply_to_message", {})
        own_reply = self.bot_id is not None and reply.get("from", {}).get("id") == self.bot_id
        mention = re.compile(r"(?<![\w@])@" + re.escape(self.username) + r"(?![\w])", re.I)
        addressed = bool(mention.search(text))
        if not text.startswith("/"):
            if not addressed and not own_reply:
                return
            question = mention.sub("", text).strip()
            answer = await self.assistant.answer(session, question, now, reply.get("text", "") if own_reply else "") if self.assistant else "Диалог с аналитиком пока не настроен."
            await self.telegram.send(answer, reply_to=msg.get("message_id"))
            return
        parts = text.split()
        command, _, target = parts[0].partition("@")
        if target and target.lower() != self.username.lower():
            return
        if command == "/reset":
            if self.assistant:
                self.assistant.memory.pop(session, None)
            answer = "Контекст ваших уточнений очищен."
        elif command == "/ask":
            question = text.partition(" ")[2].strip()
            answer = await self.assistant.answer(session, question, now, reply.get("text", "") if own_reply else "") if question and self.assistant else "Напишите /ask и вопрос."
        elif command == "/status":
            mode = f"ежедневно после {self.settings.daily_hour_msk:02d}:00 МСК" if self.settings.daily_enabled else "выключено"
            sales = "настроен; доступ проверяется при отчёте" if self.settings.sales_database_url.get_secret_value() else "не подключён"
            answer = f"Источник продаж: {sales}.\nРеклама: не подключена.\nРасписание: {mode}.\nOpenAI: {"настроен" if self.settings.openai_api_key.get_secret_value() else "не настроен"}."
        elif command in ("/report", "/sales", "/ads"):
            if len(parts) > 2 or (len(parts) == 2 and parts[1] not in {"1", "7", "30"}):
                answer = "Укажите период 1, 7 или 30. Например: /report 7"
            else:
                answer = await self.report(now, int(parts[1]) if len(parts) == 2 else 1, command[1:])
        elif command in ("/start", "/help"):
            answer = HELP
        else:
            return
        await self.telegram.send(answer, reply_to=msg.get("message_id"))

    async def daily(self, now: datetime):
        local = now.astimezone(MSK)
        if not self.settings.daily_enabled or local.hour < self.settings.daily_hour_msk:
            return
        key = f"daily:{self.settings.chat_id}:{local.date()}"
        if self.state.get(key):
            return
        await self.telegram.send(await self.report(now))
        self.state.set(key, "sent")  # Never mark an unsuccessful send as delivered.
