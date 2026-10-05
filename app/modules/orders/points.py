"""Показанные клиенту пункты выдачи и выбор из них.

Правило «пункты — только из свежего поиска» раньше жило в инструкции, и
модель ему следовала не всегда: называла пункт по памяти или подставляла
номер дома из прошлого заказа. Теперь список, который увидел клиент,
хранится в черновике (`details.shown_points`), и выбор — «1», «второй»,
«на Ставропольской», нажатие кнопки — сводится к пункту из этого списка
кодом. Чего нет ни в списке, ни в новом поиске, инструмент не принимает.
"""

from __future__ import annotations

import re

# Сколько пунктов показываем: больше четырёх в сообщении не читают, а
# кнопок в ряд ВК всё равно не поместит.
MAX_SHOWN = 4

_ORDINALS = {
    "перв": 1, "один": 1, "одна": 1,
    "втор": 2, "два": 2, "две": 2,
    "трет": 3, "три": 3,
    "четв": 4, "четыре": 4,
}
_NOISE = {
    "улица", "ул", "дом", "д", "проспект", "пр", "проезд", "переулок", "пер",
    "шоссе", "ш", "бульвар", "строение", "стр", "корпус", "корп", "к", "на",
    "в", "пункт", "пвз", "номер", "вариант", "тот", "который", "давайте",
    "мне", "удобно", "подходит", "этот", "г", "город",
}


def _words(text: str) -> list[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower().replace("ё", "е"))
    return [w for w in cleaned.split() if w and w not in _NOISE]


def _number(text: str) -> int | None:
    raw = (text or "").strip().lower()
    match = re.fullmatch(r"[№#]?\s*(\d{1,2})\s*[).,]?", raw)
    if match:
        return int(match.group(1))
    words = _words(raw)
    if len(words) <= 2:
        for word in words:
            for stem, number in _ORDINALS.items():
                if word.startswith(stem):
                    return number
    return None


def is_number(answer: str) -> bool:
    """Ответ — номер из списка («1», «второй»), а не адрес."""
    return _number(answer) is not None


def choose(answer: str, shown: list[dict]) -> dict | None:
    """Пункт из показанного списка по ответу клиента — или None.

    Номер — только из списка. Адрес — если слова ответа сходятся ровно с
    одним показанным пунктом: «на Ставропольской» при двух пунктах на
    Ставропольской — не выбор, а повод искать заново.
    """
    if not shown:
        return None
    number = _number(answer)
    if number is not None:
        return next((point for point in shown if point["n"] == number), None)
    if not _words(answer):
        return None
    matches = []
    for point in shown:
        # Город не сравниваем ни в адресе, ни в ответе: «на Красной» иначе
        # совпало бы с «Краснодар» в каждом пункте, а «Краснодар,
        # Ставропольская, 230» — ни с одним.
        street = _words(short(point["address"], 500))
        city = set(_words(point["address"])) - set(street)
        wanted = [word for word in _words(answer) if word not in city]
        if wanted and all(any(_same_word(word, part) for part in street) for word in wanted):
            matches.append(point)
    return matches[0] if len(matches) == 1 else None


def _same_word(said: str, written: str) -> bool:
    """Одно ли это слово: «Красной» и «Красная», «ставроп» и «Ставропольская».

    Номер дома — только точно: «15» не должен совпасть с «159».
    """
    if said.isdigit() or written.isdigit():
        return said == written
    if written.startswith(said):
        return True
    # Падежные окончания: общая основа без последних двух букв.
    stem = max(4, len(said) - 2)
    return len(said) >= 4 and said[:stem] == written[:stem]


def remember(details: dict, method: str, city: str, points: list[dict], *,
             query: str | None = None, quote=None) -> list[dict]:
    """Записать показанный список в детали черновика и вернуть его.

    `query` и `quote` — по какому запросу список найден и какая цена названа:
    тот же запрос второй раз отдаёт тот же список (`kept`).
    """
    shown = [{"n": i, **point} for i, point in enumerate(points[:MAX_SHOWN], start=1)]
    details["shown_points"] = shown
    details["shown_for"] = f"{method}:{city.strip().lower()}"
    details.pop("shown_query", None)
    details.pop("shown_quote", None)
    if query is not None and quote is not None:
        details["shown_query"] = query
        details["shown_quote"] = {"total": float(quote.total), "days": quote.days}
    return shown


def query_key(hint: str, weight_grams: int, declared_value) -> str:
    """Запрос пунктов: улица (основами слов), вес и сумма — от них зависят список и цены."""
    from app.modules.delivery import ozon_catalog

    return f"{' '.join(ozon_catalog.stems(hint or ''))}|{weight_grams}|{float(declared_value or 0):.2f}"


def kept(details: dict, method: str, city: str, query: str) -> list[dict]:
    """Прежний список, если запрос тот же самый, — иначе пустой."""
    if details.get("shown_query") != query or not details.get("shown_quote"):
        return []
    return shown_for(details, method, city)


def kept_quote(details: dict):
    """Цена, названная вместе с прежним списком."""
    from app.modules.delivery import ozon_client

    saved = details["shown_quote"]
    # Страховка уже внутри суммы: делить её снова не нужно.
    return ozon_client.Quote(delivery_cost=saved["total"], insurance_cost=0, days=saved["days"])


def shown_for(details: dict, method: str, city: str) -> list[dict]:
    """Список, показанный для этого способа и города, — или пустой."""
    if details.get("shown_for") != f"{method}:{(city or '').strip().lower()}":
        return []
    return list(details.get("shown_points") or [])


def forget(details: dict) -> None:
    for key in ("shown_points", "shown_for", "shown_query", "shown_quote", "shown_more"):
        details.pop(key, None)


# Слова, с которых начинается улица: всё, что перед ними, — страна, край и
# город, а их на кнопке клиент и так знает.
_STREET_WORDS = (
    "улица", "ул.", "ул ", "проспект", "пр-т", "просп", "переулок", "пер.", "бульвар", "б-р", "шоссе",
    "проезд", "набережная", "наб.", "площадь", "пл.", "микрорайон", "мкр", "тупик", "аллея", "линия",
    "тракт", "квартал", "территория", "посёлок", "поселок", "снт",
)
_SHORTER = (("улица ", "ул. "), ("проспект ", "пр-т "), ("переулок ", "пер. "), ("корпус ", "к"),
            ("строение ", "стр. "), ("микрорайон ", "мкр "), (" улица", " ул."))


def short(address: str, limit: int = 40) -> str:
    """Адрес для подписи кнопки: улица и дом, без страны, края и города.

    ВК обрезает подпись по ширине кнопки, и «Россия, Краснодарский Край,
    Краснодар, ул…» не говорит ничего. Город клиент уже назвал. Пункт кнопка
    передаёт номером из показанного списка, поэтому подпись на то, что уходит
    в Ozon, не влияет.
    """
    # Часы работы СДЭК в скобках на кнопку не помещаются: «…(Пн-Пт 08:…».
    address = re.sub(r"\s*\([^()]*\)\s*$", "", address or "")
    parts = [part.strip() for part in address.split(",") if part.strip()]
    start = 0
    for i, part in enumerate(parts):
        lowered = part.casefold() + " "
        if any(word in lowered for word in _STREET_WORDS):
            start = i
            break
    else:
        # Улицы словом нет («Краснодар, Красная, 176»): берём кусок перед домом.
        numbered = next((i for i, part in enumerate(parts) if any(ch.isdigit() for ch in part)), None)
        if numbered is not None:
            start = numbered - 1 if numbered > 0 and not any(ch.isdigit() for ch in parts[numbered - 1]) else numbered
            if start == 0 and len(parts) > 2:
                start = len(parts) - 2
    text = ", ".join(parts[start:]) or (address or "").strip()
    for long, brief in _SHORTER:
        text = text.replace(long, brief)
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,") + "…"


def listing(shown: list[dict], *, prices: bool = True) -> str:
    """Список для модели: «1) адрес — 121 ₽; 2) …»."""
    from app.messages import templates

    return "; ".join(
        f"{point['n']}) {point['address']}"
        + (f" — {templates.amount(point['price'])} ₽" if prices and point.get("price") is not None else "")
        for point in shown
    )


def mentioned(reply: str, shown: list[dict]) -> bool:
    """Называет ли ответ показанные пункты — по улице хотя бы одного из них.

    Кнопки пунктов уместны под ответом, который список и показывает: модель
    могла повторить его словами («какие пункты?»). Под вопросом «пункт или
    курьер?» улиц из списка нет — и кнопок нет.
    """
    said = set(_words(reply))
    for point in shown:
        street = [w for w in _words(short(point.get("address", ""), 500)) if len(w) >= 4 and not w.isdigit()]
        if street and street[0] in said:
            return True
    return False
