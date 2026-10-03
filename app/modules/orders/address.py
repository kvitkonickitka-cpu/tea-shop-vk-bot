"""Город и улица из адреса заказа витрины — для поиска пунктов рядом.

ВК отдаёт адрес строкой, как его ввёл покупатель: «Россия, Краснодарский
край, Краснодар, улица Красная, 176, кв. 5» или «Москва, Тверская ул., 1».
Нужны две вещи: город — пункты ищутся в нём, и улица — пункты на ней
показываются первыми. Не вышло уверенно выделить город — None, и бот
спрашивает город, как раньше: лучше вопрос, чем пункты не в том городе.
"""

from __future__ import annotations

import re

_REGION = re.compile(
    r"\b(край|области|область|обл|республика|респ|округ|автономный|ао|район|р-н)\b\.?", re.I
)
_STREET = re.compile(
    r"\b(улица|ул|проспект|пр-кт|пр-т|пр|переулок|пер|шоссе|ш|бульвар|б-р|набережная|наб|"
    r"площадь|пл|проезд|мкр|микрорайон|тупик|аллея|тракт|линия)\b\.?", re.I
)
_CITY_PREFIX = re.compile(r"^(г|гор|город|пгт|пос|посёлок|поселок|с|село|ст-ца|станица)\.?\s+", re.I)
_FLAT = re.compile(r"^(кв|квартира|оф|офис|под|подъезд|эт|этаж|д|дом|корп|к|стр|строение)\b\.?", re.I)
_COUNTRY = {"россия", "рф", "российская федерация"}


def city_and_street(address: str) -> tuple[str, str] | None:
    """(город, улица) — улица может быть пустой; None — город не выделить."""
    parts = [part.strip() for part in (address or "").split(",") if part.strip()]
    city, street = "", ""
    for part in parts:
        lowered = part.lower()
        if lowered in _COUNTRY or re.fullmatch(r"\d{6}", part) or _REGION.search(part):
            continue
        if _STREET.search(part):
            if not street:
                street = _STREET.sub(" ", part)
            continue
        if _FLAT.match(part) or not re.search(r"[А-Яа-яЁё]{3,}", part):
            continue  # номер дома, квартира, подъезд
        if not city:
            city = _CITY_PREFIX.sub("", part).strip()
        elif not street:
            # «Москва, Тверская, 1» — без «ул.», но вторая часть со словом — улица.
            street = part
    street = " ".join(street.split())
    return (city, street) if city else None
