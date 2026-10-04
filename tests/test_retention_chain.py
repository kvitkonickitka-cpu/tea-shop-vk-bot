"""Хронология одного клиента: вручение → оценка → «Повторить» → второй шанс → реактивация.

Тик расписания — раз в день в 12:00 по Москве, 130 дней подряд. По дороге
клиент пишет, менеджер отвечает — общий фильтр откладывает касания.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

from sqlalchemy import update

from app.core import worktime
from app.messages.models import FunnelEvent
from app.messages import templates
from app.modules.dialog import history as dialog_history
from app.modules.orders import buttons, delivery_events, retention
from tests.test_brewing import CSV
from tests.test_feedback_ask import outbox  # noqa: F401
from tests.test_feedback_repeat_optout import NOW, PEER, make_order
from tests.test_retention_rules import say

D0 = NOW - timedelta(days=200)  # 2026-04-03 12:00 МСК — вручение


def day(n: float):
    return D0 + timedelta(days=n)


async def test_one_client_chain(clean, outbox, monkeypatch):
    from app.modules.catalog import service as catalog_service, sheet

    items = sheet.parse_csv(CSV).items
    for item in items:
        item["recommended"] = ["Да Хун Пао"] if item["name"] == "Те Гуань Инь" else []
        item["description"] = {"Да Хун Пао": "Утёсный улун с жареными нотами."}.get(item["name"], "")
    items[3]["is_new"] = True  # Габа — новинка
    monkeypatch.setattr(catalog_service, "load_items", lambda: items)

    log: list[str] = []

    def note(moment, text):
        log.append(f"{worktime.to_msk(moment):%d.%m %H:%M} — {text}")

    order = await make_order(
        clean, items=[{"name": "Те Гуань Инь", "quantity": 1, "price": 1500}],
        created_at=day(-4), handed_over_at=day(-3), delivered_at=day(0),
    )
    assert await delivery_events.tell_client(order, now=day(0) + timedelta(hours=1))
    note(day(0), "«вручено» с заваркой:\n" + outbox.client[-1])

    events = {
        2.8: ("user", None, "клиент написал в диалог"),
        20.8: ("user", None, "клиент написал в диалог (утро дня «Повторить»)"),
        35.2: ("assistant", dialog_history.AUTHOR_MANAGER, "менеджер ответил клиенту в диалоге"),
    }
    sent = []
    for n in range(1, 131):
        for at, (role, author, text) in events.items():
            if n - 1 < at <= n:
                await say(clean, role, day(at), author)
                note(day(at), text)
        now = day(n)
        result = await retention.check(now)
        for decision in result["decisions"]:
            name = retention.NAMES[decision.kind]
            if decision.outcome == "sent":
                sent.append((decision.kind, n))
                note(now, f"ОТПРАВЛЕНО: {name}\n{outbox.client[-1]}\nКнопки: "
                          + " ".join(f"[{b['action']['label']}]" for row in outbox.boards[-1]["buttons"] for b in row))
            elif decision.outcome == "blocked":
                note(now, f"отложено: {name} — {decision.reason}")
        if (templates.FEEDBACK_ASK, n) in sent:
            board = outbox.boards[-1]
            ok = next(b["action"] for row in board["buttons"] for b in row if b["action"]["label"] == "Нормально")
            press = await buttons.handle(PEER, {"payload": ok["payload"]})
            # Журнал воронки пишет нажатие часами машины — сдвигаем к «сейчас» теста.
            async with clean() as session:
                await session.execute(
                    update(FunnelEvent).where(FunnelEvent.event == "button:rate")
                    .values(created_at=now + timedelta(minutes=5))
                )
                await session.commit()
            note(now + timedelta(minutes=5), f"клиент нажал [Нормально] → {press.reply}")

    path = os.environ.get("RETENTION_CHAIN_LOG")
    if path:
        with open(path, "w") as f:
            f.write("\n\n".join(log))
    assert [kind for kind, _ in sent] == [
        templates.FEEDBACK_ASK, templates.REPEAT_NUDGE, templates.SECOND_TOUCH, templates.REACTIVATION,
    ]
    days = dict(sent)
    assert days[templates.FEEDBACK_ASK] == 4  # 3-й день отложен: клиент писал утром
    assert days[templates.REPEAT_NUDGE] == 22  # 21-й день отложен: клиент писал утром
    assert days[templates.SECOND_TOUCH] == 38  # срок — 36-й день; менеджер писал на 35-й — 48 часов тишины
    assert days[templates.REACTIVATION] == 90

    path = os.environ.get("RETENTION_CHAIN_LOG")
    if path:
        with open(path, "w") as f:
            f.write("\n\n".join(log))
    assert json.loads(json.dumps(log))  # хронология собрана целиком
