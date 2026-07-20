from __future__ import annotations

import json
import logging
from pathlib import Path

from app.modules.catalog import service as catalog_service
from app.modules.dialog import claude_client
from app.modules.dialog.claude_client import _BASE_SYSTEM_PROMPT
from app.modules.orders import state
from app.modules.orders.state import OrderDraft
from app.modules.payment import service as payment_service

logger = logging.getLogger(__name__)

_ORDER_FLOW_PROMPT_PATH = Path(__file__).parent.parent / "dialog" / "prompts" / "order_flow_prompt.md"
_ORDER_FLOW_PROMPT = _ORDER_FLOW_PROMPT_PATH.read_text(encoding="utf-8")

_TARIFFS_PATH = Path(__file__).parent / "delivery_tariffs.json"

TOOLS = [
    {
        "name": "propose_order",
        "description": (
            "Зафиксировать список товаров, которые клиент хочет заказать, "
            "когда он явно выразил намерение купить и назвал товары."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Название товара, максимально близкое к названию в ассортименте",
                            },
                            "quantity": {"type": "integer"},
                        },
                        "required": ["name", "quantity"],
                    },
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "set_delivery_method",
        "description": (
            "Зафиксировать выбранный клиентом способ доставки, когда есть "
            "активный черновик заказа, ожидающий выбора доставки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["ozon_pvz", "russian_post", "cdek"],
                }
            },
            "required": ["method"],
        },
    },
    {
        "name": "confirm_order",
        "description": (
            "Зафиксировать согласие клиента оформить заказ, когда есть "
            "черновик, ожидающий подтверждения, и клиент явно согласился."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _find_catalog_item(catalog: list[dict], wanted_name: str) -> dict | None:
    wanted_lower = wanted_name.strip().lower()
    for item in catalog:
        item_lower = item["name"].strip().lower()
        if wanted_lower in item_lower or item_lower in wanted_lower:
            return item
    return None


def _load_tariffs() -> dict:
    with _TARIFFS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _describe_draft(draft: OrderDraft | None) -> str:
    if draft is None:
        return "Активного черновика заказа у клиента нет."

    lines = [f"Черновик заказа на этапе «{draft.stage}»:"]
    for item in draft.items:
        lines.append(f"- {item['name']} x{item['quantity']} = {item['price'] * item['quantity']} руб.")
    lines.append(f"Сумма товаров: {draft.items_total} руб.")
    if draft.delivery_label:
        lines.append(f"Способ доставки: {draft.delivery_label}, стоимость {draft.delivery_cost} руб.")
    return "\n".join(lines)


async def _execute_propose_order(peer_id: int, tool_input: dict) -> str:
    catalog = catalog_service.load_items()
    resolved, unresolved = [], []

    for wanted in tool_input.get("items", []):
        match = _find_catalog_item(catalog, wanted["name"])
        if match and match.get("in_stock", True):
            resolved.append({"name": match["name"], "quantity": wanted["quantity"], "price": match["price"]})
        else:
            unresolved.append(wanted["name"])

    if not resolved:
        return f"Не нашли в ассортименте товар(ы): {', '.join(unresolved)}. Уточни у клиента точное название."

    items_total = sum(i["price"] * i["quantity"] for i in resolved)
    draft = OrderDraft(items=resolved, items_total=items_total, stage="awaiting_delivery")
    state.set_draft(peer_id, draft)

    lines = [f"{i['name']} x{i['quantity']} = {i['price'] * i['quantity']} руб." for i in resolved]
    result = "Черновик заказа создан:\n" + "\n".join(lines) + f"\nСумма товаров: {items_total} руб."
    if unresolved:
        result += f"\nНе нашли в ассортименте: {', '.join(unresolved)} — уточни у клиента точное название."
    result += "\nТеперь предложи клиенту выбрать способ доставки: Ozon ПВЗ, Почта России или СДЭК."
    return result


async def _execute_set_delivery_method(peer_id: int, tool_input: dict) -> str:
    draft = state.get_draft(peer_id)
    if draft is None or draft.stage != "awaiting_delivery":
        return "Нет черновика заказа, ожидающего выбора доставки. Уточни у клиента, что он хочет заказать."

    method = tool_input.get("method")
    tariffs = _load_tariffs()
    tariff = tariffs.get(method)
    if tariff is None:
        return f"Неизвестный способ доставки: {method}. Предложи клиенту выбрать из трёх вариантов ещё раз."

    draft.delivery_method = method
    draft.delivery_label = tariff["label"]
    draft.delivery_cost = tariff["price"]
    draft.stage = "awaiting_confirmation"
    state.set_draft(peer_id, draft)

    total = draft.items_total + draft.delivery_cost
    return (
        f"Способ доставки зафиксирован: {draft.delivery_label}, {draft.delivery_cost} руб.\n"
        f"Сумма товаров: {draft.items_total} руб. Итого с доставкой: {total} руб.\n"
        "Сообщи клиенту эти суммы и спроси, готов ли он оформить заказ."
    )


async def _execute_confirm_order(peer_id: int) -> str:
    draft = state.get_draft(peer_id)
    if draft is None or draft.stage != "awaiting_confirmation":
        return "Нет черновика заказа, ожидающего подтверждения. Уточни у клиента, что он хочет заказать."

    draft.stage = "confirmed"
    state.set_draft(peer_id, draft)

    payment_message = await payment_service.generate_payment_link(draft)
    return f"Заказ подтверждён. {payment_message}"


async def _execute_tool(peer_id: int, name: str, tool_input: dict) -> str:
    if name == "propose_order":
        return await _execute_propose_order(peer_id, tool_input)
    if name == "set_delivery_method":
        return await _execute_set_delivery_method(peer_id, tool_input)
    if name == "confirm_order":
        return await _execute_confirm_order(peer_id)
    return f"Неизвестный инструмент: {name}"


async def handle_turn(peer_id: int, user_text: str) -> str:
    catalog_context = await catalog_service.build_catalog_context()
    draft = state.get_draft(peer_id)

    system_prompt = _BASE_SYSTEM_PROMPT
    if catalog_context:
        system_prompt += f"\n\nТекущий ассортимент:\n{catalog_context}"
    system_prompt += f"\n\n{_ORDER_FLOW_PROMPT}"
    system_prompt += f"\n\n{_describe_draft(draft)}"

    messages: list[dict] = [{"role": "user", "content": user_text}]

    response = await claude_client.converse(messages, system_prompt, TOOLS)

    if response.stop_reason != "tool_use":
        return claude_client.extract_text(response)

    tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
    messages.append({"role": "assistant", "content": response.content})

    tool_results = []
    for block in tool_use_blocks:
        result_text = await _execute_tool(peer_id, block.name, block.input)
        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
    messages.append({"role": "user", "content": tool_results})

    follow_up = await claude_client.converse(messages, system_prompt, TOOLS)
    return claude_client.extract_text(follow_up)
