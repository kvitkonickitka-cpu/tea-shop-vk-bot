"""Кнопки «Взять <сорт>» под консультацией — по тексту ответа модели.

Консультация раньше заканчивалась тем, что клиент сам писал «беру». Модель
здесь не трогаем: после её ответа код ищет в тексте названия товаров,
которые сейчас в наличии, и ставит до трёх кнопок в порядке упоминания.

Как сравниваем название: без регистра, ё/е, кавычек и пояснения в скобках
(«Те Гуань Инь (тест)» совпадает с «Те Гуань Инь»), целым словом. Фасовки в
таблице — отдельные строки с отдельной ценой; у одного сорта их может быть
несколько, тогда кнопка на каждую («Взять Те Гуань Инь 100 г»), но всего не
больше трёх.

Синонимы. Модель пишет название не всегда как в таблице: «тегуанинь»,
«те-гуань-инь», «ТГИ». Поэтому кроме названия сравниваем его же без
пробелов и дефисов, первые буквы слов (для названий из трёх слов и
длиннее — у двух слов «ШП» слишком легко совпасть случайно) и то, что
менеджер вписал в столбец «Синонимы» таблицы.

Чтобы кнопки не висели под каждой репликой, набор под последним сообщением
бота хранится в `client_preferences.last_offer_buttons`: тот же набор — не
ставим.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.dialects.postgresql import insert

from app.core.database import get_session_factory
from app.messages.models import ClientPreference

logger = logging.getLogger(__name__)

MAX_BUTTONS = 3

_QUOTES = re.compile(r"[«»\"'“”„`]")
_BRACKETS = re.compile(r"\([^)]*\)")
_SIZE = re.compile(r"\s*\d+(?:[.,]\d+)?\s*(?:г|гр|грамм|граммов|кг|шт)\.?$", re.I)
# Ответы, под которыми «Взять» неуместно: отказ в медицинском совете.
_MEDICAL = ("врач", "медицинск", "по здоровью")


def normalize(text: str) -> str:
    text = _QUOTES.sub(" ", (text or "").lower().replace("ё", "е"))
    # «Те-Гуань-Инь» и «Те Гуань Инь» — одно и то же; мягкий и твёрдый
    # знак в транскрипциях китайских названий пишут как попало.
    text = re.sub(r"(?<=\w)[-‐–](?=\w)", " ", text).replace("ь", "").replace("ъ", "")
    return " ".join(text.split())


def display_name(name: str) -> str:
    """Название без пояснения в скобках — для подписи кнопки."""
    return " ".join(_BRACKETS.sub(" ", name or "").split())


def base_name(item: dict) -> str:
    """Сорт без пояснения и фасовки — то, что ищем в тексте ответа."""
    name = display_name(item.get("name", ""))
    for size in item.get("package_sizes") or []:
        if size and name.lower().endswith(size.lower()):
            name = name[: -len(size)]
    return normalize(_SIZE.sub("", name))


_MIN_INITIALS_WORDS = 3


def variants(item: dict) -> set[str]:
    """Как ещё модель может назвать товар: слитно, первыми буквами, синонимы из таблицы."""
    base = base_name(item)
    found = {base} if base else set()
    words = base.split()
    if len(words) > 1:
        found.add("".join(words))
    if len(words) >= _MIN_INITIALS_WORDS:
        found.add("".join(word[0] for word in words))
    for synonym in item.get("synonyms") or []:
        synonym = normalize(synonym)
        if synonym:
            found.add(synonym)
            found.add(synonym.replace(" ", ""))
    return found


_STEM_FROM = 5


def _word_pattern(word: str) -> str:
    # Окончание длинного слова свободно: «железную богиню» — это синоним
    # «железная богиня». Короткие слова («да», «хун», «пао») — точно.
    if len(word) >= _STEM_FROM:
        return re.escape(word[:-2]) + r"\w{0,3}"
    return re.escape(word)


def _position(text: str, base: str) -> int | None:
    if not base:
        return None
    pattern = r"\s+".join(_word_pattern(word) for word in base.split())
    match = re.search(rf"(?<!\w){pattern}(?!\w)", text)
    return match.start() if match else None


def _earliest(text: str, names: set[str]) -> int | None:
    places = [where for where in (_position(text, name) for name in names) if where is not None]
    return min(places) if places else None


def mentioned(reply: str, catalog: list[dict], *, exclude: set[str] = frozenset()) -> list[dict]:
    """Товары в наличии, названные в ответе, — в порядке упоминания, до трёх."""
    text = normalize(reply)
    groups: dict[str, list[dict]] = {}
    for item in catalog:
        if not item.get("in_stock", True) or item.get("name") in exclude:
            continue
        groups.setdefault(base_name(item), []).append(item)
    found = []
    for rows in groups.values():
        names = set().union(*(variants(row) for row in rows))
        where = _earliest(text, names)
        if where is not None:
            found.append((where, rows))
    found.sort(key=lambda pair: pair[0])
    picked: list[dict] = []
    for _, rows in found:
        for row in rows:
            if len(picked) >= MAX_BUTTONS:
                return picked
            picked.append({**row, "_several": len(rows) > 1})
    return picked


def label(prefix: str, item: dict) -> str:
    name = display_name(item["name"])
    sizes = item.get("package_sizes") or []
    # Несколько фасовок одного сорта — подпись с фасовкой, если её нет в названии.
    if item.get("_several") and len(sizes) == 1 and sizes[0].lower() not in name.lower():
        name = f"{name} {sizes[0]}"
    return f"{prefix} {name}"


def unsuitable(reply: str) -> bool:
    lowered = (reply or "").lower()
    return any(word in lowered for word in _MEDICAL)


async def last_set(peer_id: int) -> list[str] | None:
    try:
        async with get_session_factory()() as session:
            row = await session.get(ClientPreference, peer_id)
    except Exception:
        return None
    return list(row.last_offer_buttons) if row is not None and row.last_offer_buttons else None


async def remember_set(peer_id: int, names: list[str] | None) -> None:
    """Какие «Взять»/«Добавить» стоят под последним сообщением бота (None — никаких)."""
    try:
        session_factory = get_session_factory()
    except RuntimeError:
        return
    statement = insert(ClientPreference).values(
        peer_id=peer_id, marketing_opt_out=False, last_offer_buttons=names
    ).on_conflict_do_update(
        index_elements=[ClientPreference.peer_id], set_={"last_offer_buttons": names}
    )
    try:
        async with session_factory() as session:
            await session.execute(statement)
            await session.commit()
    except Exception:
        logger.exception("Не запомнили кнопки консультации для peer_id=%s", peer_id)
