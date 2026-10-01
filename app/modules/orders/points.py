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


def remember(details: dict, method: str, city: str, points: list[dict]) -> list[dict]:
    """Записать показанный список в детали черновика и вернуть его."""
    shown = [{"n": i, **point} for i, point in enumerate(points[:MAX_SHOWN], start=1)]
    details["shown_points"] = shown
    details["shown_for"] = f"{method}:{city.strip().lower()}"
    return shown


def shown_for(details: dict, method: str, city: str) -> list[dict]:
    """Список, показанный для этого способа и города, — или пустой."""
    if details.get("shown_for") != f"{method}:{(city or '').strip().lower()}":
        return []
    return list(details.get("shown_points") or [])


def forget(details: dict) -> None:
    details.pop("shown_points", None)
    details.pop("shown_for", None)


def short(address: str, limit: int = 40) -> str:
    """Адрес для подписи кнопки: без города в начале и не длиннее лимита."""
    text = (address or "").strip()
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) > 1 and not any(ch.isdigit() for ch in parts[0]):
        text = ", ".join(parts[1:])
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,") + "…"


def listing(shown: list[dict]) -> str:
    """Список для модели: «1) адрес — 121 ₽; 2) …»."""
    from app.messages import templates

    return "; ".join(
        f"{point['n']}) {point['address']}"
        + (f" — {templates.amount(point['price'])} ₽" if point.get("price") is not None else "")
        for point in shown
    )
