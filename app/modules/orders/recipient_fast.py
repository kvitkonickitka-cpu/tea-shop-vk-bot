"""Получатель одним сообщением — разбирает код, модель не нужна (C1, флаг recipient_one_message).

Когда пункт выбран и не хватает только получателя, ответ клиента — это ФИО,
телефон и почта. Метки (`[NAME_n]`, `[PHONE_n]`, `[EMAIL_n]`) ставит слой
приватности ещё до модели; код берёт значения меток, дописывает к тому, что
уже прислано раньше, и:

- всё есть — записывает получателя тем же `set_recipient` (проверка телефона
  и почты по DNS) и выставляет счёт, как сделала бы модель;
- чего-то нет — переспрашивает только недостающее одним вопросом.

Сообщение с вопросом или длинным текстом рядом с данными («Иванов, 8900…,
а сколько заваривать?») отдаём модели: на вопрос должен ответить продавец.
Проверка не прошла (опечатка в почте, неполный номер) — тоже модели: она
переспросит по результату инструмента, как и без этого пути.
"""

from __future__ import annotations

import logging
import re

from app import privacy
from app.core import flags
from app.messages import funnel, templates
from app.modules.orders import state
from app.modules.orders.state import OrderDraft

logger = logging.getLogger(__name__)

_LABEL = re.compile(r"\[(NAME|PHONE|EMAIL)_\d+\]")
_FIELD = {"NAME": "name", "PHONE": "phone", "EMAIL": "email"}
_FILLER = re.compile(r"[\s,.;:!«»\"'()\-–—]+")
# Что ещё может стоять рядом с данными, не делая сообщение вопросом.
_SMALL_TALK = {"вот", "фио", "телефон", "тел", "почта", "email", "e-mail", "мейл", "получатель", "и", "мой", "моя",
               "мои", "данные", "пожалуйста", "спасибо", "здравствуйте", "добрый", "день", "вечер"}
PARTIAL = "recipient_partial"


def _ready_but_recipient(draft: OrderDraft) -> bool:
    if draft.delivery_method not in ("ozon_pvz", "cdek_pvz", "cdek_courier") or draft.delivery_cost is None:
        return False
    point = {"ozon_pvz": "ozon_point_id", "cdek_pvz": "delivery_point"}.get(draft.delivery_method)
    if point and not draft.details.get(point):
        return False
    if draft.delivery_method == "cdek_courier" and not draft.details.get("address"):
        return False
    return not draft.details.get("email_suggestion")


def _only_data(spoken: str) -> bool:
    rest = _LABEL.sub(" ", spoken)
    if "?" in rest:
        return False
    words = [w for w in _FILLER.split(rest.casefold()) if w]
    return len([w for w in words if w not in _SMALL_TALK]) <= 2


async def try_handle(peer_id: int, draft: OrderDraft | None, spoken: str) -> str | None:
    """Ответ клиенту без модели или None — пусть отвечает модель. `spoken` — уже с метками."""
    if draft is None or not spoken or not _LABEL.search(spoken):
        return None
    if not _ready_but_recipient(draft) or not _only_data(spoken):
        return None
    if not await flags.enabled_for(flags.RECIPIENT_ONE_MESSAGE, peer_id):
        return None
    from app.modules.payment import service as payment_service

    need_email = payment_service.is_enabled()
    details = draft.details
    have = {"name": details.get("recipient_name"), "phone": details.get("recipient_phone"),
            "email": details.get("recipient_email")}
    if have["name"] and have["phone"] and (have["email"] or not need_email):
        return None  # получатель уже есть — это сообщение про другое
    sent: dict[str, str] = {}
    for match in _LABEL.finditer(spoken):
        field = _FIELD[match.group(1)]
        if field not in sent:
            sent[field] = (await privacy.detokenize(peer_id, match.group(0))).strip()
    collected = {**{k: v for k, v in have.items() if v}, **(details.get(PARTIAL) or {}), **sent}
    wanted = ["name", "phone"] + (["email"] if need_email else [])
    missing = [field for field in wanted if not collected.get(field)]
    if missing:
        details[PARTIAL] = {k: v for k, v in collected.items() if k in wanted and v}
        await state.set_draft(peer_id, draft)
        logger.info("peer_id=%s: получатель не целиком, переспрашиваем %s", peer_id, ",".join(missing))
        return templates.recipient_missing(missing, [field for field in wanted if field in sent])

    from app.modules.orders import conversation

    result = await conversation._execute_set_recipient(peer_id, {
        "name": collected["name"], "phone": collected["phone"], "email": collected.get("email", ""),
    })
    if not result.startswith("Получатель записан"):
        # Телефон не тот или почта с опечаткой — объяснит модель по тому же результату.
        logger.info("peer_id=%s: получатель кодом не записался — отдаём модели", peer_id)
        return None
    fresh = await state.get_draft(peer_id)
    if fresh is not None and fresh.details.pop(PARTIAL, None) is not None:
        await state.set_draft(peer_id, fresh)
    await funnel.record(peer_id, "recipient_one_message", source_=funnel.CODE)
    invoiced = await conversation._auto_invoice(peer_id)
    if invoiced is not None and invoiced.client_reply is not None:
        return invoiced.client_reply
    return None
