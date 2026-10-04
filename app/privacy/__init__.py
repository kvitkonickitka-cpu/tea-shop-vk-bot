"""Персональные данные не уходят в Claude API: вместо них — метки.

Клиент пишет «1, Иванов Иван, 89001234567, ivanov@mail.ru» — модель видит
«1, [NAME_1], [PHONE_1], [EMAIL_1]». Значения меток хранятся только у нас
(`pii_vault`, зашифрованы), а код подставляет их обратно везде, где текст
уходит человеку или в инструмент. Две точки:

- `tokenize(peer_id, text)` — на входе: сообщение клиента после склейки,
  всё, что пишется в историю (там же шаблоны, которые бот отправил сам, и
  ответы менеджера), динамические части системного промпта и результаты
  инструментов;
- `detokenize(peer_id, text)` — на выходе: ответ клиенту в ВК и аргументы
  инструментов до проверок и записи в заказ. Незнакомая метка — ошибка
  хода: клиент получает «техническую заминку», а не текст с дырой.

Последний рубеж — `scrub` в обёртке над клиентом Anthropic: телефон или
почта, пропущенные выше, заменяются на [REDACTED] прямо перед отправкой.
"""

from __future__ import annotations

import logging
import re
import time

from app.core.client_key import client_key
from app.core.config import settings
from app.privacy import crypto, detect, vault

logger = logging.getLogger(__name__)

RECIPIENT = "recipient"
COURIER_ADDRESS = "courier_address"
REDACTED = "[REDACTED]"


class UnknownLabel(Exception):
    """Метки нет у этого клиента: модель выдумала её или взяла чужую."""

    def __init__(self, label: str):
        super().__init__(f"незнакомая метка [{label}]")
        self.label = label


def is_enabled() -> bool:
    return bool(settings.pii_tokens_enabled and settings.client_key_secret and crypto.available())


def can_restore() -> bool:
    """Подставлять значения можно и при выключенном флаге: метки в истории живут."""
    return bool(settings.client_key_secret and crypto.available())


# --- стоп-лист: сорта из таблицы, города, перевозчики -------------------------

_stop: tuple[float, int, detect.StopList] | None = None


async def stop_list() -> detect.StopList:
    """Стоп-лист пересобирается, когда таблица каталога сменилась."""
    global _stop
    from app.modules.catalog import service as catalog_service

    items = catalog_service.load_items()
    if _stop is not None and _stop[1] == id(items) and time.monotonic() - _stop[0] < 300:
        return _stop[2]
    built = detect.stop_list(items)
    _stop = (time.monotonic(), id(items), built)
    return built


# --- на входе ------------------------------------------------------------------


def stages(draft, last_bot_text: str = "") -> frozenset[str]:
    """Чего бот ждёт от клиента: данные получателя, адрес для курьера."""
    result = set()
    details = (draft.details if draft is not None else {}) or {}
    last = (last_bot_text or "").casefold()
    if draft is not None and (
        (draft.delivery_method and not all(details.get(k) for k in ("recipient_name", "recipient_phone",
                                                                    "recipient_email")))
        or details.get("storefront_recipient")
    ):
        result.add(RECIPIENT)
    if "фио" in last or "получател" in last:
        result.add(RECIPIENT)
    # Адрес курьера — только когда его попросили. «Курьер» и «адрес» рядом
    # бывают и в списке пунктов («…или курьер: напишите», «адрес пункта»):
    # там клиент назовёт улицу для поиска пункта, и её трогать нельзя.
    courier = draft is not None and draft.delivery_method == "cdek_courier"
    if (courier and "адрес" in last) or any(
        phrase in last for phrase in ("адрес доставки", "адрес для курьера", "до двери")
    ):
        result.add(COURIER_ADDRESS)
    return frozenset(result)


def _known_pattern(book: vault.Book) -> re.Pattern | None:
    values = sorted(
        {value for kind, value in book.values.values() if kind in ("NAME", "ADDR", "EMAIL") and len(value) >= 3},
        key=len, reverse=True,
    )
    if not values:
        return None
    alternatives = "|".join(
        "".join("[её]" if ch in "её" else "[ЕЁ]" if ch in "ЕЁ" else re.escape(ch) for ch in v) for v in values
    )
    return re.compile(rf"(?<![\w@.-])(?:{alternatives})(?![\w@-])", re.IGNORECASE)


async def _replace(text: str, found: list[detect.Found], key: str) -> str:
    # С конца, чтобы позиции впереди не съезжали.
    taken: list[tuple[int, int]] = []
    for item in sorted(found, key=lambda f: f.start, reverse=True):
        if any(item.start < end and start < item.end for start, end in taken):
            continue
        label = await vault.label_for(key, item.kind, item.value)
        text = text[:item.start] + f"[{label}]" + text[item.end:]
        taken.append((item.start, item.end))
    return text


async def tokenize(peer_id: int, text: str, *, names: bool = True, stage: frozenset[str] = frozenset()) -> str:
    """Текст с метками вместо персональных данных клиента.

    `names=False` — только известные значения, телефоны и почты: для текстов,
    которые пишет не человек (промпт, результаты инструментов), где поиск
    имён по словарю дал бы ложные срабатывания на ровном месте.
    """
    if not text or not is_enabled():
        return text
    key = client_key(peer_id)

    contacts = detect.emails(text)
    text = await _replace(text, contacts, key)
    phones = detect.phones(text)
    text = await _replace(text, phones, key)

    if names:
        # Распознавание — раньше известных значений: «иванов иван иванович»
        # должно стать одной меткой, а не известным «Иванов Иван» и хвостом.
        stop = await stop_list()
        if RECIPIENT in stage or contacts or phones:
            blanked = detect.LABEL.sub(lambda m: " " * len(m.group(0)), text)
            text = await _replace(text, detect.recipient_names(blanked, stop), key)
        text = await _replace(text, detect.names(text, stop), key)

    # Известные значения этого клиента — где бы они ни встретились: в
    # шаблоне со сводкой, в ответе менеджера, в описании черновика.
    book = await vault.load(key)
    known = _known_pattern(book)
    if known is not None:
        lookup = {crypto.fingerprint(kind, value, client_key=key): label
                  for label, (kind, value) in book.values.items()}

        def known_label(match: re.Match) -> str:
            for kind in ("NAME", "ADDR", "EMAIL"):
                label = lookup.get(crypto.fingerprint(kind, match.group(0), client_key=key))
                if label:
                    return f"[{label}]"
            return match.group(0)

        text = known.sub(known_label, text)
    if COURIER_ADDRESS in stage:
        address = detect.courier_address(text)
        if address is not None:
            text = await _replace(text, [address], key)
    return text


async def remember(peer_id: int, kind: str, value: str | None) -> None:
    """Значение, пришедшее не из переписки (витрина, прошлый заказ), — тоже метке."""
    if not value or not is_enabled():
        return
    value = str(value).strip()
    if kind == "PHONE":
        value = detect.normalize_phone(value) or value
    elif kind == "EMAIL":
        value = value.casefold()
    elif kind in ("NAME", "ADDR"):
        value = " ".join(value.split())
    if len(value) >= 3:
        await vault.label_for(client_key(peer_id), kind, value)


async def remember_recipient(peer_id: int, name=None, phone=None, email=None) -> None:
    await remember(peer_id, "NAME", name)
    await remember(peer_id, "PHONE", phone)
    await remember(peer_id, "EMAIL", email)


async def remember_details(peer_id: int, details: dict | None, delivery_method: str | None = None) -> None:
    """Всё личное, что лежит в черновике или заказе: получатель, адрес курьера, витрина."""
    if not details or not is_enabled():
        return
    await remember_recipient(peer_id, details.get("recipient_name"), details.get("recipient_phone"),
                             details.get("recipient_email"))
    storefront = details.get("storefront_recipient") or {}
    if isinstance(storefront, dict):
        await remember_recipient(peer_id, storefront.get("name"), storefront.get("phone"), storefront.get("email"))
    offer = details.get("offer") or {}
    if isinstance(offer, dict):
        await remember_recipient(peer_id, offer.get("name"), offer.get("phone"), offer.get("email"))
    await remember(peer_id, "ADDR", details.get("vk_order_address"))
    if delivery_method == "cdek_courier":
        await remember(peer_id, "ADDR", details.get("address"))


# --- на выходе -----------------------------------------------------------------


async def detokenize(peer_id: int, text: str) -> str:
    """Метки — обратно в значения. Чужая или выдуманная метка — `UnknownLabel`."""
    if not text or not detect.LABEL.search(text):
        return text
    if not can_restore():
        found = detect.LABEL.search(text).group(0)[1:-1]
        logger.error("Метка [%s] в ответе, а ключа PII_ENCRYPTION_KEY нет — подставить нечем", found)
        raise UnknownLabel(found)
    key = client_key(peer_id)
    values: dict[str, str] = {}
    for match in detect.LABEL.finditer(text):
        label = match.group(0)[1:-1]
        if label in values:
            continue
        value = await vault.value_of(key, label)
        if value is None:
            logger.error("Метка [%s] не принадлежит клиенту peer_id=%s — ответ не отправляем", label, peer_id)
            raise UnknownLabel(label)
        values[label] = value
    return detect.LABEL.sub(lambda m: values[m.group(0)[1:-1]], text)


async def detokenize_data(peer_id: int, data):
    """То же для аргументов инструмента: строки внутри словарей и списков."""
    if isinstance(data, str):
        return await detokenize(peer_id, data)
    if isinstance(data, dict):
        return {key: await detokenize_data(peer_id, value) for key, value in data.items()}
    if isinstance(data, list):
        return [await detokenize_data(peer_id, value) for value in data]
    return data


# --- последний рубеж ----------------------------------------------------------


def scrub(text: str) -> tuple[str, int]:
    """Телефоны и почты — на [REDACTED]. Возвращает текст и сколько нашлось."""
    count = 0

    def hide(match: re.Match) -> str:
        nonlocal count
        if match.re is detect.PHONE and not detect.normalize_phone(match.group(0)):
            return match.group(0)
        count += 1
        return REDACTED

    text = detect.EMAIL.sub(hide, text)
    text = detect.PHONE.sub(hide, text)
    return text, count
