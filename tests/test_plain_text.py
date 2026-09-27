"""ВК не показывает markdown — ответ модели уходит обычным текстом."""

from __future__ import annotations

import pytest

from app.modules.orders.conversation import plain_text


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("☕ **Те Гуань Инь** — улун, 100 г", "☕ Те Гуань Инь — улун, 100 г"),
        ("**Да Хун Пао**\n**Шу Пуэр**", "Да Хун Пао\nШу Пуэр"),
        ("## Ассортимент\n* Улун\n- Пуэр", "Ассортимент\n• Улун\n• Пуэр"),
        ("__важно__ и 2*3=6", "важно и 2*3=6"),
        ("Итого: 217 руб.", "Итого: 217 руб."),
        ("", ""),
    ],
)
def test_plain_text(raw, expected):
    assert plain_text(raw) == expected
