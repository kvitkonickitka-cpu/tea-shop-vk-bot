"""Где в тексте персональные данные: телефоны, почты, ФИО, адрес курьера.

Телефоны и почты — регулярными выражениями: у них строгий вид. ФИО —
словарём pymorphy3: он знает, что «Никита» — имя, «Иванова» — фамилия, а
«Сергеевна» — отчество, и работает без сети. Natasha точнее на свободном
тексте, но тянет десятки мегабайт моделей и numpy — на холодном старте
бессерверного контейнера это заметно, а у вебхука ВК восемь секунд.

Имя в свободном тексте — только при уверенности: два-три слова с заглавной,
среди которых словарное имя или отчество, или одно слово, которое словарь
уверенно считает именем. Сорта чая, города и перевозчики в стоп-листе:
«Да Хун Пао» не должен стать [NAME_1]. Когда бот ждёт данные получателя,
правило мягче: два-три слова в отдельном куске сообщения — это ФИО, даже
написанное строчными.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache

from app.modules.orders.contacts import normalize_phone

logger = logging.getLogger(__name__)

LABEL = re.compile(r"\[(NAME|PHONE|EMAIL|ADDR)_(\d+)\]")
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Российские номера в любом привычном виде: +7, 8 или 7 впереди, скобки,
# пробелы, дефисы, точки. Без кода страны — только мобильные, на 9: иначе
# за телефон сойдёт десятизначный номер накладной.
PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+7|8|7)[\s(.-]{0,2}\d{3}|\(?9\d{2})[\s).-]{0,2}\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?![\w])"
)

_CAP_WORD = re.compile(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")
_ANY_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+(?:-[A-Za-zА-Яа-яЁё]+)?")

# Перед этими словами «Ленина», «Пушкина» — улица, а не человек.
_PLACE_MARKERS = {
    "ул", "улица", "улице", "улицу", "пр", "пр-т", "просп", "проспект", "пер", "переулок", "бульвар",
    "б-р", "шоссе", "наб", "набережная", "пл", "площадь", "мкр", "микрорайон", "тц", "трц", "г",
    "город", "городе", "пос", "поселок", "посёлок", "с", "село", "д", "дер", "деревня", "ст",
    "станция", "метро", "м", "им", "имени", "р-н", "район", "область", "обл", "край",
}
# «во Владимир», «из Королёва» — город.
_PLACE_PREPOSITIONS = {"в", "во", "из", "до", "под"}

CARRIERS = {
    "сдэк", "cdek", "ozon", "озон", "почта", "почты", "почтой", "россии", "россия", "яндекс",
    "boxberry", "боксберри", "вк", "вконтакте", "юkassa", "юкасса", "сбер", "сбербанк",
    "тинькофф", "т-банк", "wildberries", "вайлдберриз", "авито",
}
# Крупные города и города-тёзки имён и фамилий («Владимир», «Королёв»):
# остальные города словарь сам помечает как географию.
CITIES = {
    "москва", "санкт-петербург", "петербург", "питер", "новосибирск", "екатеринбург", "казань",
    "нижний", "новгород", "челябинск", "самара", "омск", "ростов-на-дону", "ростов", "уфа",
    "красноярск", "пермь", "воронеж", "волгоград", "краснодар", "саратов", "тюмень", "тольятти",
    "ижевск", "барнаул", "ульяновск", "иркутск", "хабаровск", "ярославль", "владивосток",
    "махачкала", "томск", "оренбург", "кемерово", "новокузнецк", "рязань", "астрахань",
    "пенза", "липецк", "киров", "чебоксары", "тула", "калининград", "курск", "сочи",
    "ставрополь", "владимир", "королёв", "королев", "пушкин", "орёл", "орел", "анапа",
    "армавир", "новороссийск", "геленджик", "севастополь", "симферополь", "ялта", "мурманск",
    "архангельск", "сургут", "белгород", "тверь", "иваново", "брянск", "смоленск", "калуга",
    "вологда", "кострома", "псков", "петрозаводск", "сыктывкар", "якутск", "чита", "улан-удэ",
    "майкоп", "грозный", "нальчик", "владикавказ", "черкесск", "элиста", "абакан", "кызыл",
    "магадан", "тамбов", "саранск", "йошкар-ола", "курган", "мытищи", "химки", "балашиха",
    "подольск", "люберцы", "красногорск", "одинцово", "зеленоград", "лобня", "дмитров",
    # Части городов из двух слов: «Сергиев Посад» — не человек.
    "сергиев", "посад", "великий", "старый", "оскол", "набережные", "челны", "минеральные", "воды",
    "петропавловск-камчатский", "южно-сахалинск", "комсомольск-на-амуре", "ленинск-кузнецкий",
}


@lru_cache(maxsize=1)
def _morph():
    started = time.perf_counter()
    import pymorphy3

    analyzer = pymorphy3.MorphAnalyzer()
    logger.info("Словарь имён pymorphy3 загружен за %.0f мс", (time.perf_counter() - started) * 1000)
    return analyzer


def warm_up() -> float:
    """Загрузить словарь заранее; секунды загрузки (для замера при старте)."""
    started = time.perf_counter()
    _morph()
    return time.perf_counter() - started


@dataclass(frozen=True)
class Word:
    name: float  # уверенность, что это имя
    surname: float
    patronymic: float
    geo: float
    unknown: bool
    # Имя, фамилия или отчество в именительном: «Петров», а не «Ленина» из
    # «улицы Ленина» — улицы чаще всего фамилии в родительном.
    person_nominative: float = 0.0


@lru_cache(maxsize=4096)
def word(text: str) -> Word:
    name = surname = patronymic = geo = nominative = 0.0
    unknown = False
    # Вероятность — сумма по разборам: у «Анне» имя делится между падежами
    # по 0,12, и максимум одного разбора недооценил бы его вчетверо.
    for parse in _morph().parse(text):
        tag = parse.tag
        if "Name" in tag:
            name += parse.score
        if "Surn" in tag:
            surname += parse.score
        if "Patr" in tag:
            patronymic += parse.score
        if "Geox" in tag:
            geo += parse.score
        if "UNKN" in tag:
            unknown = True
        if ("Name" in tag or "Surn" in tag or "Patr" in tag) and "nomn" in tag:
            nominative += parse.score
    return Word(min(name, 1.0), min(surname, 1.0), min(patronymic, 1.0), min(geo, 1.0), unknown,
                min(nominative, 1.0))


def _key(text: str) -> str:
    return text.casefold().replace("ё", "е")


@dataclass
class StopList:
    """Слова, которые никогда не имя: сорта, города, перевозчики."""

    products: set[str]
    cities: set[str]

    def product(self, text: str) -> bool:
        return _key(text) in self.products

    def city(self, text: str) -> bool:
        return _key(text) in self.cities or _key(text) in CARRIERS


def stop_list(catalog_items: list[dict], cities: set[str] | None = None) -> StopList:
    products: set[str] = set(CARRIERS)
    for item in catalog_items:
        for phrase in [item.get("name", ""), *(item.get("synonyms") or [])]:
            for part in _ANY_WORD.findall(phrase or ""):
                products.add(_key(part))
    return StopList(products=products, cities={_key(c) for c in (cities or set()) | CITIES})


@dataclass(frozen=True)
class Found:
    start: int
    end: int
    kind: str
    value: str


def phones(text: str) -> list[Found]:
    found = []
    for match in PHONE.finditer(text):
        normalized = normalize_phone(match.group(0))
        if normalized:
            found.append(Found(match.start(), match.end(), "PHONE", normalized))
    return found


def emails(text: str) -> list[Found]:
    return [Found(m.start(), m.end(), "EMAIL", m.group(0).casefold()) for m in EMAIL.finditer(text)]


def _title(words: list[str]) -> str:
    return " ".join("-".join(part[:1].upper() + part[1:].lower() for part in w.split("-")) for w in words)


def _previous_token(text: str, start: int) -> str:
    before = re.findall(r"[\w-]+", text[:start][-20:])
    return _key(before[-1]) if before else ""


def _next_token(text: str, end: int) -> str:
    after = re.findall(r"[\w-]+", text[end:end + 20])
    return _key(after[0]) if after else ""


def names(text: str, stop: StopList) -> list[Found]:
    """ФИО в свободном тексте: только то, в чём словарь уверен."""
    found: list[Found] = []
    words = list(_CAP_WORD.finditer(text))
    i = 0
    while i < len(words):
        # Цепочка слов с заглавной, разделённых только пробелами.
        run = [words[i]]
        while i + 1 < len(words) and text[run[-1].end():words[i + 1].start()].isspace() \
                and "\n" not in text[run[-1].end():words[i + 1].start()]:
            i += 1
            run.append(words[i])
        i += 1
        if _previous_token(text, run[0].start()) in _PLACE_MARKERS:
            continue
        if _next_token(text, run[-1].end()) in {"улица", "ул", "проспект", "переулок", "шоссе", "бульвар"}:
            continue
        # Сорт чая разрывает цепочку: «Да Хун Пао Иван» — имя только «Иван».
        pieces: list[list] = [[]]
        for match in run:
            if stop.product(match.group(0)):
                pieces.append([])
            else:
                pieces[-1].append(match)
        for piece in pieces:
            found.extend(_names_in_piece(text, piece, stop))
    return found


def _names_in_piece(text: str, piece: list, stop: StopList) -> list[Found]:
    result = []
    features = [word(m.group(0)) for m in piece]

    def anchor(f: Word) -> bool:
        return (f.name >= 0.3 and f.geo < 0.5) or f.patronymic >= 0.3

    def companion(f: Word) -> bool:
        return anchor(f) or f.surname >= 0.1 or f.unknown or f.name >= 0.05

    i = 0
    while i < len(piece):
        if not anchor(features[i]):
            i += 1
            continue
        left = i
        while left > 0 and companion(features[left - 1]) and i - left < 2:
            left -= 1
        right = i
        while right + 1 < len(piece) and companion(features[right + 1]) and right - left < 2:
            right += 1
        group = piece[left:right + 1]
        if len(group) >= 2:
            result.append(Found(group[0].start(), group[-1].end(), "NAME", _title([m.group(0) for m in group])))
        else:
            single = group[0]
            f = features[i]
            if (
                f.name >= 0.6 and f.geo < 0.1 and not stop.city(single.group(0))
                and _previous_token(text, single.start()) not in _PLACE_PREPOSITIONS
            ):
                result.append(Found(single.start(), single.end(), "NAME", _title([single.group(0)])))
        i = right + 1
    return result


_SEGMENT_SPLIT = re.compile(r"[,;\n]|\s[-–—]\s")
_POINT_NUMBER = re.compile(r"^\s*(?:пункт|№|n)?\s*\d{1,2}[.)]?\s*", re.IGNORECASE)


def recipient_names(text: str, stop: StopList) -> list[Found]:
    """Бот ждёт данные получателя: отдельный кусок из 2–3 слов — это ФИО.

    Телефон, почта и метки к этому моменту уже вырезаны (в тексте —
    пробелы на их месте), номер пункта в начале куска отбрасываем.
    """
    found = []
    position = 0
    for segment in _SEGMENT_SPLIT.split(text):
        start = text.index(segment, position)
        position = start + len(segment)
        stripped = _POINT_NUMBER.sub("", segment)
        offset = start + (len(segment) - len(stripped))
        if any(ch.isdigit() for ch in stripped):
            continue
        parts = list(_ANY_WORD.finditer(stripped))
        rest = _ANY_WORD.sub("", stripped)
        if not 2 <= len(parts) <= 3 or re.search(r"[^\s.]", rest):
            continue
        texts = [p.group(0) for p in parts]
        if any(stop.product(t) for t in texts):
            continue
        features = [word(t) for t in texts]
        evidence = any(f.name >= 0.3 or f.surname >= 0.3 or f.patronymic >= 0.3 for f in features)
        places = [stop.city(t) or f.geo >= 0.5 for t, f in zip(texts, features)]
        if any(places):
            # «Пермь Ленина», «Казань Баумана» — город и улица. «Владимир
            # Петров» — человек: рядом с городом-тёзкой фамилия в именительном.
            evidence = any(f.person_nominative >= 0.5 for f, place in zip(features, places) if not place)
            if not evidence:
                continue
        if not (evidence or all(t[:1].isupper() for t in texts)):
            continue
        found.append(Found(offset + parts[0].start(), offset + parts[-1].end(), "NAME", _title(texts)))
    return found


def courier_address(text: str) -> Found | None:
    """Бот спросил адрес для курьера: кусок с цифрой и словами — адрес целиком.

    Куски — между уже поставленными метками: телефон и ФИО к этому моменту
    заменены, адресом остаётся то, что между ними.
    """
    best: tuple[int, int, str] | None = None
    edges = [0]
    for label in LABEL.finditer(text):
        edges += [label.start(), label.end()]
    edges.append(len(text))
    for start, end in zip(edges[::2], edges[1::2]):
        piece = text[start:end]
        stripped = piece.strip(" ,.;:!?\n\t")
        if not stripped or not any(ch.isdigit() for ch in stripped):
            continue
        if len(_ANY_WORD.findall(stripped)) < 2:
            continue
        if best is None or len(stripped) > len(best[2]):
            begin = start + piece.index(stripped)
            best = (begin, begin + len(stripped), stripped)
    if best is None:
        return None
    return Found(best[0], best[1], "ADDR", " ".join(best[2].split()))
