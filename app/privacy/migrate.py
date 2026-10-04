"""Перенос накопленной истории на метки (Б6).

История диалогов, записанная до меток, хранит ФИО, телефоны и почты как
есть — и уходит в Claude на каждом ходу этих клиентов. Здесь она проходит
через тот же `tokenize`, что и новые сообщения. Остальное, что подаётся
модели (описание черновика, получатель прошлого заказа, вопросы менеджеру,
отзывы), собирается на каждом ходу заново и меняется на метки уже там —
переписывать это в базе не нужно. Значения из заказов и черновиков
регистрируются заранее: тогда «Получатель: Иванов Иван» в старой сводке
узнаётся как известное значение, а не угадывается по словарю.

По умолчанию — пробный прогон: метки раздаются в памяти, в базе не меняется
ничего, в отчёте — сколько и каких меток будет и примеры замен с
замаскированными значениями. Настоящих значений в отчёте нет: его читают
люди и, через Claude Code в терминале, — Anthropic.

С `apply` перед записью делается копия таблицы истории. Повторный запуск
ничего не меняет: уже заменённое — метки, а метки не распознаются.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import text

from app import privacy
from app.core.client_key import client_key
from app.core.database import get_session_factory
from app.modules.dialog import history as dialog_history
from app.privacy import vault

logger = logging.getLogger(__name__)


def mask(kind: str, value: str) -> str:
    """Вид значения без самого значения: для отчёта."""
    if kind == "NAME":
        return " ".join(word[:1] + "*" * (len(word) - 1) for word in value.split())
    if kind == "PHONE":
        return "+7 *** ***-**-" + value[-2:]
    if kind == "EMAIL":
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"‹адрес, {len(value)} симв.›"


async def _count_labels(session) -> Counter:
    rows = (await session.execute(text("select kind, count(*) from pii_vault group by kind"))).all()
    return Counter({kind: int(count) for kind, count in rows})


async def run(*, apply: bool = False, examples: int = 12) -> dict:
    if not privacy.is_enabled():
        return {"ошибка": "метки выключены: нет PII_ENCRYPTION_KEY или CLIENT_KEY_SECRET, или PII_TOKENS_ENABLED=false"}
    session_factory = get_session_factory()
    async with session_factory() as session:
        before = await _count_labels(session)
        orders = (await session.execute(text("select peer_id, details, delivery_method from orders"))).all()
        drafts = (await session.execute(text("select peer_id, details, delivery_method from order_drafts"))).all()
        messages = (await session.execute(text(
            "select id, peer_id, role, author, content from conversation_messages order by peer_id, id"
        ))).all()

    report: dict = {"режим": "применение" if apply else "пробный прогон (ничего не меняется)"}
    backup = None
    if apply and messages:
        backup = "pii_backup_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_conversation_messages"
        async with session_factory() as session:
            await session.execute(text(f"create table {backup} as table conversation_messages"))
            await session.commit()
        report["резервная копия"] = backup

    changed: list[tuple[int, str]] = []
    kinds: Counter = Counter()
    by_role: Counter = Counter()
    shown: list[dict] = []
    simulated = contextlib.nullcontext() if apply else vault.simulate()
    with simulated:
        for peer_id, details, method in [*orders, *drafts]:
            await privacy.remember_details(peer_id, details or {}, method)

        last_bot: dict[int, str] = {}
        for row_id, peer_id, role, author, content in messages:
            found: list = []
            names = role == "user" or author == dialog_history.AUTHOR_MANAGER
            stage = privacy.stages(None, last_bot.get(peer_id, "")) if role == "user" else frozenset()
            new = await privacy.tokenize(peer_id, content or "", names=names, stage=stage, collect=found)
            if role == "assistant":
                last_bot[peer_id] = content or ""
            if new == content:
                continue
            changed.append((row_id, new))
            by_role[f"{role}/{author}" if author else role] += 1
            for _, kind, _ in found:
                kinds[kind] += 1
            if len(shown) < examples:
                shown.append({
                    "сообщение": row_id, "роль": role if not author else f"{role} ({author})",
                    "замены": [f"{mask(kind, value)} → [{label}]" for label, kind, value in reversed(found)],
                })

        if apply:
            async with session_factory() as session:
                for row_id, new in changed:
                    await session.execute(text("update conversation_messages set content = :c where id = :i"),
                                          {"c": new, "i": row_id})
                await session.commit()
            async with session_factory() as session:
                after = await _count_labels(session)
        else:
            after = Counter()
            for book in (vault._simulated or {}).values():
                for kind, _ in book.values.values():
                    after[kind] += 1

    created = {kind: after[kind] - before[kind] for kind in vault.KINDS if after[kind] - before[kind]}
    report.update({
        "сообщений в истории": len(messages),
        "сообщений с заменами": len(changed),
        "по ролям": dict(by_role),
        "замен по видам": dict(kinds),
        ("меток создано" if apply else "меток будет создано"): created,
        "клиентов": len({client_key(peer) for _, peer, *_ in messages}),
        "примеры": shown,
    })
    if apply:
        vault.forget_cache()
        logger.info("Перенос истории на метки: заменено в %d сообщениях, копия %s", len(changed), backup)
    return report
