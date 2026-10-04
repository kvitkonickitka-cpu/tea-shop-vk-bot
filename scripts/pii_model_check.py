"""Живая проверка Б5: вызывает ли модель set_recipient с метками.

Отправляет в Anthropic несколько типичных ответов клиента с данными
получателя — только метками, без настоящих значений — с тем же системным
промптом и инструментами, что у бота на этапе оформления, и смотрит, что
модель вызвала. Ожидание: set_recipient(name=[NAME_1], phone=[PHONE_1],
email=[EMAIL_1]), а где клиент назвал пункт — ещё и set_delivery_method с
пунктом «1».

    python scripts/pii_model_check.py              по 3 прогона на случай
    python scripts/pii_model_check.py --runs 5

Нужен ANTHROPIC_API_KEY (из .env, как у бота). База не нужна. Каждый прогон —
один платный запрос к модели (без продолжения хода). Перед отправкой запрос
проверяется тем же последним рубежом, что в боте: если в нём нашёлся бы
телефон или почта, скрипт остановится, ничего не отправив.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CATALOG = (
    "- Те Гуань Инь 100 г — 1500 ₽, в наличии. Свежий цветочный улун.\n"
    "- Да Хун Пао 100 г — 1500 ₽, в наличии. Утёсный улун."
)
POINTS = {
    "shown_points": [
        {"n": 1, "id": 11, "address": "Краснодар, Ставропольская улица, 230"},
        {"n": 2, "id": 12, "address": "Краснодар, Красная улица, 176"},
    ]
}
LABELS = {"name": "[NAME_1]", "phone": "[PHONE_1]", "email": "[EMAIL_1]"}

# (название, пункт уже выбран?, реплики до текущей, текущая реплика клиента, нужен ли выбор пункта)
CASES = [
    ("пункт и данные одной строкой", False, [], "1, [NAME_1], [PHONE_1], [EMAIL_1]", True),
    ("данные одной строкой", True, [], "[NAME_1], [PHONE_1], [EMAIL_1]", False),
    ("данные с подписями", True, [], "получатель [NAME_1], тел [PHONE_1], почта [EMAIL_1]", False),
    ("данные столбиком", True, [], "[NAME_1]\n[PHONE_1]\n[EMAIL_1]", False),
    ("данные тремя сообщениями", True,
     [("user", "[NAME_1]"), ("assistant", "Записала. Пришлите телефон и почту."),
      ("user", "[PHONE_1]"), ("assistant", "Спасибо! И почту для чека.")],
     "[EMAIL_1]", False),
]


def build(point_chosen: bool):
    from app.modules.dialog.claude_client import _BASE_SYSTEM_PROMPT
    from app.modules.orders import conversation
    from app.modules.orders.state import OrderDraft

    details = {"address": "Краснодар", **POINTS}
    if point_chosen:
        details.update(ozon_point_id=11, ozon_point_address="Краснодар, Ставропольская улица, 230")
    draft = OrderDraft(
        items=[{"name": "Те Гуань Инь 100 г", "quantity": 1, "price": 1500}], items_total=1500,
        delivery_method="ozon_pvz", delivery_cost=121 if point_chosen else None,
        delivery_label="Ozon, пункт выдачи: Краснодар, Ставропольская улица, 230" if point_chosen else None,
        stage="awaiting_confirmation" if point_chosen else "awaiting_delivery", details=details,
    )
    system = "\n\n".join([
        _BASE_SYSTEM_PROMPT, conversation._PII_PROMPT, f"Текущий ассортимент:\n{CATALOG}",
        conversation.order_flow_prompt(), conversation._describe_draft(draft),
    ])
    tools = conversation._tools_for_stage(draft.stage)
    return system, tools


async def one(case, model: str) -> tuple[bool, list[str]]:
    from anthropic import AsyncAnthropic

    from app import privacy
    from app.core.config import settings

    title, point_chosen, before, said, needs_point = case
    system, tools = build(point_chosen)
    messages = [{"role": role, "content": content} for role, content in before] + [{"role": "user", "content": said}]
    # Тот же поиск, что у последнего рубежа в боте, — независимо от того,
    # включены ли метки в локальном .env.
    if privacy.scrub(system + json.dumps(messages, ensure_ascii=False))[1]:
        raise SystemExit("В запросе нашлись телефон или почта — не отправляю. Проверьте промпт.")
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url or None)
    response = await client.messages.create(model=model, max_tokens=1024, system=system, messages=messages, tools=tools)
    calls = [(b.name, b.input) for b in response.content if b.type == "tool_use"]
    shown = [f"{name}({json.dumps(args, ensure_ascii=False)})" for name, args in calls]
    recipient = [args for name, args in calls if name == "set_recipient"]
    ok = bool(recipient) and all(recipient[0].get(k) == v for k, v in LABELS.items())
    if needs_point:
        ok = ok and any(name == "set_delivery_method" and str(args.get("pickup_point")) == "1" for name, args in calls)
    if not calls:
        text = "".join(b.text for b in response.content if b.type == "text")
        shown = [f"(без инструментов) {text[:160]!r}"]
    return ok, shown


async def main(runs: int) -> int:
    from app.core.config import settings

    if not settings.anthropic_api_key:
        print("Нет ANTHROPIC_API_KEY в .env", file=sys.stderr)
        return 2
    model = settings.anthropic_model
    print(f"Модель: {model}, прогонов на случай: {runs}\n")
    failed = 0
    for case in CASES:
        results = [await one(case, model) for _ in range(runs)]
        passed = sum(ok for ok, _ in results)
        failed += runs - passed
        print(f"{'✅' if passed == runs else '⚠️'} {case[0]}: {passed}/{runs}")
        for ok, shown in results:
            print(f"    {'ok ' if ok else 'НЕТ'} " + "; ".join(shown))
    print(f"\nИтого: {len(CASES) * runs - failed} из {len(CASES) * runs} прогонов как ожидалось")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Вызывает ли модель set_recipient с метками")
    parser.add_argument("--runs", type=int, default=3)
    sys.exit(asyncio.run(main(parser.parse_args().runs)))
