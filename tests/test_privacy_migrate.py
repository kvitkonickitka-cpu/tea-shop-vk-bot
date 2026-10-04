"""Часть Б, Б6: перенос накопленной истории на метки."""

from __future__ import annotations

import json

from sqlalchemy import select, text

from app.modules.dialog import history as dialog_history
from app.modules.dialog.models import Conversation, ConversationMessage
from app.modules.orders.models import Order
from app.privacy import migrate
from app.privacy.models import PiiEntry

PEER = 9700
OLD = [
    ("assistant", None, "Выберите пункт и пришлите ФИО, телефон и почту."),
    ("user", None, "2, иванов иван, 8 900 123-45-67, ivanov@mail.ru"),
    ("assistant", None, "Заказ №1 — проверьте:\nПолучатель: Иванов Иван, +79001234567, ivanov@mail.ru"),
    ("assistant", dialog_history.AUTHOR_MANAGER, "Мария Петровна, добрый день! Отправили."),
    ("user", None, "Спасибо! Да Хун Пао отличный"),
]
VALUES = ["иванов", "Иванов", "123-45-67", "9001234567", "ivanov@mail.ru", "Мария Петровна", "Петровн"]


async def seed(db):
    async with db() as session:
        session.add(Conversation(peer_id=PEER))
        await session.flush()
        # Мимо append_message: так история лежала до меток.
        session.add_all(ConversationMessage(peer_id=PEER, role=r, author=a, content=c) for r, a, c in OLD)
        session.add(Order(peer_id=PEER, items=[], items_total=0, total=0, delivery_method="ozon_pvz",
                          details={"recipient_name": "Иванов Иван", "recipient_phone": "+79001234567",
                                   "recipient_email": "ivanov@mail.ru"}))
        await session.commit()


async def contents(db) -> list[str]:
    async with db() as session:
        return (await session.execute(select(ConversationMessage.content).order_by(ConversationMessage.id))).scalars().all()


async def test_dry_run_changes_nothing_and_shows_no_values(clean):
    await seed(clean)
    report = await migrate.run()
    assert report["сообщений с заменами"] == 3
    assert report["меток будет создано"] == {"NAME": 2, "PHONE": 1, "EMAIL": 1}
    assert report["примеры"][0]["замены"] == ["И***** И*** → [NAME_1]", "+7 *** ***-**-67 → [PHONE_1]",
                                              "i***@mail.ru → [EMAIL_1]"]
    dumped = json.dumps(report, ensure_ascii=False)
    assert not [value for value in VALUES if value in dumped]
    assert await contents(clean) == [c for _, _, c in OLD]
    async with clean() as session:
        assert (await session.execute(select(PiiEntry))).first() is None


async def test_apply_backs_up_and_is_idempotent(clean):
    await seed(clean)
    report = await migrate.run(apply=True)
    assert await contents(clean) == [
        OLD[0][2],
        "2, [NAME_1], [PHONE_1], [EMAIL_1]",
        "Заказ №1 — проверьте:\nПолучатель: [NAME_1], [PHONE_1], [EMAIL_1]",
        "[NAME_2], добрый день! Отправили.",
        OLD[4][2],
    ]
    async with clean() as session:
        backup = (await session.execute(text(f"select content from {report['резервная копия']} order by id"))).scalars().all()
        await session.execute(text(f"drop table {report['резервная копия']}"))
        await session.commit()
    assert backup == [c for _, _, c in OLD]
    again = await migrate.run(apply=True)
    assert again["сообщений с заменами"] == 0 and again["меток создано"] == {}
    async with clean() as session:
        await session.execute(text(f"drop table {again['резервная копия']}"))
        await session.commit()
    assert [m["content"] for m in await dialog_history.get_history(PEER)][1] == "2, [NAME_1], [PHONE_1], [EMAIL_1]"
