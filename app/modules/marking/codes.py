"""Разбор кода маркировки «Честного знака».

Код в DataMatrix — строка элементов GS1: идентификатор применения (AI) и
значение. Нам нужны три:

- `01` — GTIN, 14 цифр, фиксированная длина;
- `21` — серийный номер, до 20 символов, **переменная длина**;
- криптохвост: `93` (4 символа, короткий код проверки) или пара `91` + `92`
  (ключ и подпись 44 или 88 символов).

Поля переменной длины заканчиваются невидимым разделителем GS (0x1D). Он —
часть кода: без него `21ABC93xyz` не отличить от серийника `ABC93xyz`, и
касса код не примет. Поэтому код храним и передаём **целиком, с GS**, а
сканировать надо камерой на странице сборки: копирование текста из
стороннего сканера разделители теряет.

Если разделителей нет вовсе (ручной ввод, копия из текстового файла),
пробуем восстановить: криптохвост стоит последним и имеет фиксированную
длину, так что серийный номер находится однозначно. Если однозначно не
выходит — отказываем, а не угадываем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GS = "\x1d"

# Идентификатор символики, который добавляют сканеры: `]d2` — GS1 DataMatrix.
_SYMBOLOGY_PREFIXES = ("]d2", "]d1", "]C1", "]Q3", "]e0")
# Как GS выглядит там, где его нельзя напечатать: FNC1 как символ 232 и
# текстовые подстановки, которыми разделитель вписывают руками.
_GS_SPELLINGS = ("\xe8", "<GS>", "{GS}", "\\x1d", "\\u001d", "\\u001D", "\\x1D", "␝")

# Длины элементов GS1, которые встречаются в кодах «Честного знака».
_FIXED = {"01": 14, "11": 6, "13": 6, "15": 6, "17": 6, "3103": 6, "8005": 6, "7003": 10}
_VARIABLE = {"21": 20, "10": 20, "240": 30, "91": 90, "92": 90, "93": 35}

_CRYPTO_93 = re.compile(r"^(?P<serial>.{1,20})93(?P<check>.{4})$", re.S)
_CRYPTO_91_92 = re.compile(r"^(?P<serial>.{1,20})91(?P<key>.{4})92(?P<sig>.{44}|.{88})$", re.S)


class CodeError(ValueError):
    """Код не годится. Текст — для сборщика, по-человечески."""


@dataclass(frozen=True)
class MarkingCode:
    code: str        # целиком, с разделителями GS
    gtin: str
    serial: str
    crypto: str      # «93» или «91+92»
    restored: bool = False  # разделители восстановлены — код пришёл без них


def gtin_is_valid(gtin: str) -> bool:
    """Контрольная цифра GTIN по GS1: веса 3 и 1 справа налево."""
    if not re.fullmatch(r"\d{14}", gtin or ""):
        return False
    digits = [int(ch) for ch in gtin]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def normalize_gtin(raw: str) -> str:
    """GTIN из справочника: цифры, дополненные нулями слева до 14.

    Владелец может вписать 13-значный EAN с упаковки — в коде маркировки он
    всё равно будет 14-значным, с ведущим нулём.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits.zfill(14) if 8 <= len(digits) <= 14 else digits


def _clean(raw: str) -> str:
    text = raw or ""
    # Края чистим от пробелов и переводов строки, но не от GS: он может
    # стоять и в конце, и в начале (FNC1).
    text = text.strip(" \t\r\n")
    for prefix in _SYMBOLOGY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for spelling in _GS_SPELLINGS:
        text = text.replace(spelling, GS)
    return text.lstrip(GS)


def _from_brackets(text: str) -> str | None:
    """Код в человекочитаемом виде: `(01)046…(21)…(93)…`."""
    if not text.startswith("(01)"):
        return None
    parts = re.findall(r"\((\d{2,4})\)([^()]*)", text)
    if not parts:
        return None
    out = ""
    for ai, value in parts:
        out += ai + value
        if ai in _VARIABLE:
            out += GS
    return out.rstrip(GS)


def _elements(text: str) -> dict[str, str] | None:
    """Разобрать строку с разделителями. None — строка не разбирается."""
    elements: dict[str, str] = {}
    i = 0
    while i < len(text):
        if text[i] == GS:
            i += 1
            continue
        ai = next(
            (a for a in sorted({**_FIXED, **_VARIABLE}, key=len, reverse=True)
             if text.startswith(a, i)),
            None,
        )
        if ai is None:
            return None
        i += len(ai)
        if ai in _FIXED:
            value = text[i:i + _FIXED[ai]]
            if len(value) != _FIXED[ai]:
                return None
            i += len(value)
        else:
            end = text.find(GS, i)
            end = len(text) if end == -1 else end
            value = text[i:end]
            if not value or len(value) > _VARIABLE[ai]:
                return None
            i = end
        elements[ai] = value
    return elements


def _restore(text: str) -> MarkingCode | None:
    """Код без разделителей: найти серийный номер по хвосту фиксированной длины."""
    if not text.startswith("01") or GS in text or text[16:18] != "21":
        return None
    gtin, rest = text[2:16], text[18:]
    found = []
    match = _CRYPTO_93.match(rest)
    if match:
        found.append(MarkingCode(
            code=f"01{gtin}21{match['serial']}{GS}93{match['check']}",
            gtin=gtin, serial=match["serial"], crypto="93", restored=True,
        ))
    match = _CRYPTO_91_92.match(rest)
    if match:
        found.append(MarkingCode(
            code=f"01{gtin}21{match['serial']}{GS}91{match['key']}{GS}92{match['sig']}",
            gtin=gtin, serial=match["serial"], crypto="91+92", restored=True,
        ))
    return found[0] if len(found) == 1 else None


def parse(raw: str) -> MarkingCode:
    """Разобрать код или объяснить сборщику, что с ним не так."""
    text = _clean(raw)
    if not text:
        raise CodeError("Пустой код — отсканируйте ещё раз.")

    bracketed = _from_brackets(text)
    if bracketed is not None:
        text = bracketed

    if not text.startswith("01") or not text[2:16].isdigit():
        raise CodeError(
            "Это не код маркировки: он должен начинаться с GTIN (01 и 14 цифр). "
            "Возможно, отсканирован штрихкод EAN вместо квадратного DataMatrix."
        )

    elements = _elements(text) if GS in text else None
    if elements is None or "21" not in elements:
        restored = _restore(text.replace(GS, "")) if GS not in text else None
        if restored is None and GS not in text:
            raise CodeError(
                "В коде нет разделителей, и серийный номер не отделить от "
                "криптохвоста однозначно. Отсканируйте камерой на этой странице — "
                "при копировании текста разделители теряются."
            )
        if restored is None:
            raise CodeError("Код не разбирается — отсканируйте ещё раз.")
        parsed = restored
    else:
        gtin = elements.get("01", "")
        serial = elements["21"]
        if "93" in elements:
            crypto = "93"
        elif "91" in elements and "92" in elements:
            crypto = "91+92"
        else:
            raise CodeError(
                "В коде нет криптохвоста (93 или 91+92) — такой код касса не "
                "примет. Возможно, отсканирована только часть или это текст с "
                "этикетки, а не сам DataMatrix."
            )
        parsed = MarkingCode(code=text.rstrip(GS), gtin=gtin, serial=serial, crypto=crypto)

    if not gtin_is_valid(parsed.gtin):
        raise CodeError(
            "Контрольная цифра GTIN не сходится — код прочитан с ошибкой, "
            "отсканируйте ещё раз."
        )
    return parsed


def printable(code: str) -> str:
    """Код для показа человеку: GS заменён видимым значком."""
    return (code or "").replace(GS, "␝")
