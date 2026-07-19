import json
import logging
from pathlib import Path

from app.modules.catalog import vk_market_client

logger = logging.getLogger(__name__)

EXTRAS_PATH = Path(__file__).parent / "extras.json"


def _load_extras() -> dict[str, str]:
    with EXTRAS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


async def build_catalog_context() -> str:
    try:
        items = await vk_market_client.fetch_market_items()
    except Exception:
        logger.exception("Failed to fetch VK Market items")
        return ""

    if not items:
        return ""

    extras = _load_extras()
    lines = []
    for item in items:
        if item.get("availability") != 0:
            continue

        price = item.get("price", {}).get("amount")
        price_str = f"{int(price) / 100:.0f} руб." if price else "цена не указана"

        line = f"- {item['title']} ({price_str}): {item.get('description', '')}"
        extra = extras.get(str(item["id"]))
        if extra:
            line += f" Доп. информация: {extra}"
        lines.append(line)

    return "\n".join(lines)
