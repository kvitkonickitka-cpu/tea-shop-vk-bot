"""Очистка текста, который ассистент Ops отправляет модели.

Ассистенту можно видеть идентификаторы — номер заказа, peer_id, номер
платежа, — но не данные клиента. Чистим всё, что пришло текстом от людей:
вопрос из чата и сообщение, на которое ответили (обычно тревога бота, но
ответить можно на что угодно). Результаты инструментов собраны из
разрешённых полей и чистятся тем же кодом ещё раз — на случай, если в
текст ошибки перевозчик вернул чьё-то имя или телефон.

Правило «с запасом»: лишняя метка [ИМЯ] вместо слова стоит дешевле, чем имя
клиента в запросе к модели. Числа не трогаем — это номера заказов, ради
них вопрос и задают.
"""

from __future__ import annotations

import re

from app.privacy import detect

# Метки бота с номерами — в общие: номер связывал бы упоминания между
# сообщениями, а ассистенту он ни к чему.
_LABELS = {"NAME": "[ИМЯ]", "PHONE": "[ТЕЛЕФОН]", "EMAIL": "[ПОЧТА]", "ADDR": "[АДРЕС]", "GEO": "[ГЕО]"}

# Адрес с номером дома: «ул. Ленина 5», «на Ленина 5, кв 3», «пр-т Мира, д. 12».
# Пункты выдачи ассистенту не нужны тоже — они его не касаются.
_STREET = (
    r"(?i:ул(?:\.|ица|ице|ицу)?|пр(?:\.|-т|осп\.?|оспект\w*)|пер(?:\.|еул\w*)|"
    r"б-р|бульв\w*|ш(?:\.|оссе)|наб(?:\.|ережн\w*)|пл(?:\.|ощад\w*)|мкр\w*|проезд\w*)"
)
_TAIL = (
    r",?\s*(?i:д\.?|дом)?\s*\d{1,4}[а-яa-z]?"
    r"(?:\s*[,/]?\s*(?i:к(?:орп)?\.?|стр\.?)\s*\d+)?"
    r"(?:\s*,?\s*(?i:кв\.?|квартира|оф\.?|офис)\s*\d+)?"
)
ADDRESS = re.compile(
    # С явным словом-улицей название может быть любым: «ул. 8 Марта», «пр-т Мира».
    rf"\b{_STREET}\s+[\w.-]+(?:\s+[А-ЯЁA-Z][\w.-]*)?{_TAIL}"
    # «на Ленина 5» — только слово с заглавной и строчными: «на СДЭК 5 сбоев»
    # и «на сайте 5» адресом не считаются.
    rf"|\b(?i:на)\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?{_TAIL}"
)
# Квартира сама по себе — уже адрес.
FLAT = re.compile(r"\b(?i:кв\.?|квартира)\s*\d+")


def clean(text: str, stop: detect.StopList, *, generous: bool = True) -> tuple[str, int]:
    """Текст без данных клиента и сколько замен сделано.

    `generous=False` — для сообщений, которые написали боты (наши тревоги и
    отчёты, сторож, Timeweb): их текст собирает код, имён клиентов там нет,
    а проход «с запасом» принимал служебные слова за фамилии («Аптайм:
    100 %» становилось «[ИМЯ]: 100 %»). Телефоны, почты, адреса и уверенные
    имена снимаются всё равно.
    """
    if not text:
        return "", 0
    count = 0

    def label(match: re.Match) -> str:
        nonlocal count
        count += 1
        return _LABELS[match.group(1)]

    def put(token: str):
        def replace(match: re.Match) -> str:
            nonlocal count
            count += 1
            return token
        return replace

    text = detect.LABEL.sub(label, text)
    text = detect.EMAIL.sub(put("[ПОЧТА]"), text)

    def phone(match: re.Match) -> str:
        nonlocal count
        if not detect.normalize_phone(match.group(0)):
            return match.group(0)
        count += 1
        return "[ТЕЛЕФОН]"

    text = detect.PHONE.sub(phone, text)
    text = detect.COORDINATES.sub(put("[ГЕО]"), text)
    text = ADDRESS.sub(put("[АДРЕС]"), text)
    text = FLAT.sub(put("[АДРЕС]"), text)

    # Имена — последними: к этому моменту улицы уже заменены и не сойдут за
    # фамилии («Ленина»), а словарь видит только слова с заглавной.
    found = detect.names(text, stop)
    for item in sorted(found, key=lambda f: f.start, reverse=True):
        text = text[:item.start] + "[ИМЯ]" + text[item.end:]
        count += 1

    # Второй проход «с запасом»: бот метит одиночное слово, только если
    # словарь почти уверен (у него на кону смысл реплики клиента). Здесь
    # смысл не нужен — любое слово с заглавной, которое словарь хоть немного
    # считает именем, фамилией или отчеством, кроме сортов, городов и
    # перевозчиков. «Надежда заберёт», «спросите Петрову» — тоже [ИМЯ].
    def maybe_person(match: re.Match) -> str:
        nonlocal count
        value = match.group(0)
        if stop.product(value) or stop.city(value):
            return value
        f = detect.word(value)
        if f.geo >= 0.5:
            return value
        if f.name >= 0.1 or f.surname >= 0.1 or f.patronymic >= 0.1:
            count += 1
            return "[ИМЯ]"
        return value

    if generous:
        text = _CAP_WORD.sub(maybe_person, text)
    return text, count


_CAP_WORD = re.compile(r"(?<![\w\[])[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?")


def clean_data(value, stop: detect.StopList, counter: list[int], *, generous: bool = True):
    """То же для результата инструмента: строки внутри словарей и списков.

    Для кода и документации — `generous=False`: там слова с заглавной в
    комментариях («Пульс», «Сторож»), а не клиенты; телефоны, почты, адреса
    и уверенные имена (фикстуры тестов) снимаются всё равно.
    """
    if isinstance(value, str):
        cleaned, found = clean(value, stop, generous=generous)
        counter[0] += found
        return cleaned
    if isinstance(value, list):
        return [clean_data(item, stop, counter, generous=generous) for item in value]
    if isinstance(value, dict):
        return {key: clean_data(item, stop, counter, generous=generous) for key, item in value.items()}
    return value
