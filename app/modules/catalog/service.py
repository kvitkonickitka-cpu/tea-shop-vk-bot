import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).parent / "catalog.json"


def load_items() -> list[dict]:
    with CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


async def build_catalog_context() -> str:
    try:
        items = load_items()
    except Exception:
        logger.exception("Failed to load catalog.json")
        return ""

    lines = []
    for item in items:
        if not item.get("in_stock", True):
            continue

        line = f"- {item['name']} ({item['price']} руб."
        package_sizes = item.get("package_sizes")
        if package_sizes:
            line += f", упаковки: {', '.join(package_sizes)}"
        line += f"): {item.get('description', '')}"
        link = item.get("link")
        if link:
            line += f" Ссылка на товар (в т.ч. с фото): {link}"
        lines.append(line)

    return "\n".join(lines)
