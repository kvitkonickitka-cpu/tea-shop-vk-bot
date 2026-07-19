import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "catalog.json"


def _load_catalog() -> list[dict]:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


async def build_catalog_context() -> str:
    try:
        items = _load_catalog()
    except Exception:
        logger.exception("Failed to load catalog.json")
        return ""

    lines = []
    for item in items:
        if not item.get("in_stock", True):
            continue

        line = f"- {item['name']} ({item['price']} руб."
        quantity = item.get("quantity")
        if quantity is not None:
            line += f", в наличии {quantity} шт."
        line += f"): {item.get('description', '')}"
        lines.append(line)

    return "\n".join(lines)
