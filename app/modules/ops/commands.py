"""Команды администраторов в чате Ops: `/flag`, `/own` и следующие.

Команду разбирает код, модель её не видит: переключение флага или пометка
клиента должны работать одинаково и не тратить запрос к Claude. Команды
принимаются только в чате Ops и только от администраторов
(`OPS_ADMIN_TG_IDS`, пусто — те же, кому можно спрашивать ассистента).

Новая команда — функция `async (args, sender, message) -> str` в `COMMANDS`. Ответ —
HTML Telegram, данные клиентов в него не пишем: только идентификаторы.
"""

from __future__ import annotations

import html
import logging
from typing import Awaitable, Callable

from app.core import flags
from app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[str]]
COMMANDS: dict[str, tuple[Handler, str]] = {}


def command(name: str, usage: str):
    def wrap(fn: Handler) -> Handler:
        COMMANDS[name] = (fn, usage)
        return fn

    return wrap


def parse(text: str, bot_username: str | None) -> tuple[str, list[str]] | None:
    """`/flag@bot name on` → ("flag", ["name", "on"]). Не команда — None."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, *args = text.split()
    name, _, mention = head[1:].partition("@")
    # Команда другому боту в том же чате — не наша.
    if mention and bot_username and mention.casefold() != bot_username.casefold():
        return None
    name = name.casefold()
    if name not in COMMANDS:
        return None
    return name, args


def is_admin(user_id) -> bool:
    raw = (settings.ops_admin_tg_ids or settings.ops_assistant_allowed_tg_ids or "").replace(" ", "")
    if not raw:
        # Список не задан — команды разрешены всем в чате Ops: он закрытый,
        # а флаг без администраторов не переключить было бы хуже.
        return True
    return str(user_id) in set(raw.split(","))


def who(sender: dict) -> str:
    """Кто переключил: Telegram ID и имя аккаунта — для журнала флагов."""
    name = sender.get("username") or sender.get("first_name") or ""
    return f"tg:{sender.get('id')}" + (f" {name}" if name else "")


async def run(name: str, args: list[str], sender: dict, message: dict | None = None) -> str:
    if not is_admin(sender.get("id")):
        logger.info("Ops: команда /%s от tg_id=%s — не администратор", name, sender.get("id"))
        return "Эта команда — только для администраторов."
    handler, usage = COMMANDS[name]
    try:
        return await handler(args, sender, message)
    except ValueError as error:
        return f"{html.escape(str(error))}\nКак: <code>{html.escape(usage)}</code>"
    except Exception:
        logger.exception("Ops: команда /%s упала", name)
        return "Команда не выполнилась — подробности в логе бота."


_MODE_MARK = {flags.OFF: "⚪️", flags.ALLOWLIST: "🧪", flags.ON: "🟢"}


@command("flag", "/flag — список; /flag <имя> off|allowlist|on")
async def flag_command(args: list[str], sender: dict, message: dict | None = None) -> str:
    if not args:
        lines = ["<b>Флаги</b> (off — выключен, allowlist — только тестовые аккаунты, on — всем)"]
        for row in await flags.describe():
            when = f" · {row['updated_at'][:16].replace('T', ' ')} UTC {html.escape(row['updated_by'] or '')}" \
                if row["updated_at"] else ""
            lines.append(f"{_MODE_MARK.get(row['mode'], '')} <code>{row['name']}</code> — {row['mode']}"
                         f" · {html.escape(row['title'])}{when}")
        if len(lines) == 1:
            lines.append("флагов пока нет")
        return "\n".join(lines)
    if len(args) != 2:
        raise ValueError("Нужно имя флага и режим.")
    name, mode = args[0], args[1].casefold()
    previous = await flags.set_mode(name, mode, by=who(sender))
    return f"{_MODE_MARK.get(mode, '')} <code>{html.escape(name)}</code>: {previous} → <b>{mode}</b>"


@command("own", "/own <peer_id> — пометить клиента «свои»; /own <peer_id> off — снять")
async def own_command(args: list[str], sender: dict, message: dict | None = None) -> str:
    from app.modules.dialog import sessions

    if not args or not args[0].lstrip("-").isdigit():
        raise ValueError("Нужен peer_id клиента — число из карточки менеджеру.")
    peer_id = int(args[0])
    off = len(args) > 1 and args[1].casefold() in ("off", "снять", "нет")
    found = await sessions.mark_own(peer_id, by=who(sender), own=not off)
    if not found:
        return f"Клиента peer_id={peer_id} в базе нет — он ещё не писал."
    if off:
        return f"peer_id={peer_id}: метка «свои» снята, источник — как при первом контакте."
    return f"peer_id={peer_id}: помечен «свои» — в аналитике источник own, в CAC не входит."


@command("registry", "/registry — забрать реестр ЮKassa сейчас и сверить выплаты с выпиской")
async def registry_command(args: list[str], sender: dict, message: dict | None = None) -> str:
    from app.modules.payment import registry

    result = await registry.sync(force=True)
    if "skipped" in result:
        return f"Реестр ЮKassa: {html.escape(result['skipped'])}"
    return (
        f"<b>Реестр ЮKassa</b> за {result['дней']} дн.: платежей {result['платежей']}, возвратов {result['возвратов']}\n"
        + "\n".join(f"{html.escape(str(k))}: {html.escape(str(v))}" for k, v in result.items()
                    if k not in ("дней", "платежей", "возвратов"))
    )


# Ответ на команду, которому нужен файл: handle отправит документы следом за текстом.
_attachments: dict[int, list[tuple[str, bytes]]] = {}


@command("kudir", "/kudir — за прошлый месяц; /kudir 2026-10; /kudir 2026; /kudir 2026-10-01 2026-12-31")
async def kudir_command(args: list[str], sender: dict, message: dict | None = None) -> str:
    from datetime import datetime, timezone

    from app.core import worktime
    from app.modules.payment import kudir

    start, end = kudir.parse_period(args, worktime.to_msk(datetime.now(timezone.utc)).date())
    records = await kudir.rows(start, end)
    total = sum((r["income"] for r in records), 0)
    name = f"kudir_{start:%Y%m%d}_{end:%Y%m%d}"
    _attachments[id(sender)] = [(f"{name}.xlsx", kudir.to_xlsx(records, start, end)), (f"{name}.csv", kudir.to_csv(records))]
    return (
        f"<b>КУДиР {start:%d.%m.%Y} – {end:%d.%m.%Y}</b>: записей {len(records)}, доходы {total} ₽.\n"
        f"{html.escape(kudir.RULE_NOTE.format(rule=kudir.RULES[kudir.rule()]))}"
    )


@command("act", "файл CSV с подписью /act: трек;сумма[;доставка|возврат;дата акта;номер акта]")
async def act_command(args: list[str], sender: dict, message: dict | None = None) -> str:
    from app.modules.delivery import acts
    from app.modules.dialog import telegram_client

    document = (message or {}).get("document")
    if not document:
        raise ValueError("Пришлите акт файлом CSV с подписью /act.")
    rows, problems = acts.parse(await telegram_client.download_file(document["file_id"]))
    if not rows:
        raise ValueError("В файле нет строк акта. " + "; ".join(problems[:5]))
    result = await acts.store(rows)
    lines = [f"<b>Акт перевозчика</b>: записано строк {result['записано']} из {len(rows)}."]
    if result["трек не найден"]:
        lines.append("Трек не найден среди заказов: " + ", ".join(html.escape(t) for t in result["трек не найден"][:10]))
    if problems:
        lines.append("Пропущено: " + html.escape("; ".join(problems[:10])))
    return "\n".join(lines)


async def handle(message: dict, me: dict) -> bool:
    """Команда из чата Ops — выполнить и ответить. True — это была наша команда."""
    # Файл с подписью-командой (/act) — команда в caption.
    parsed = parse(message.get("text") or message.get("caption") or "", me.get("username"))
    if parsed is None:
        return False
    name, args = parsed
    sender = message.get("from") or {}
    reply = await run(name, args, sender, message)
    from app.modules.dialog import telegram_client

    chat_id = str(message["chat"]["id"])
    try:
        await telegram_client.reply(reply, chat_id=chat_id, reply_to=message["message_id"])
        for filename, content in _attachments.pop(id(sender), []):
            await telegram_client.send_document(filename, content, chat_id=chat_id, reply_to=message["message_id"])
    except Exception:
        logger.exception("Ops: ответ на команду /%s не ушёл", name)
    finally:
        _attachments.pop(id(sender), None)
    return True
