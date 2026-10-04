"""Перенос накопленной истории диалогов на метки персональных данных (Б6).

Удобнее всего — внутри контейнера, где уже есть база и ключи:

    scripts/api.sh pii/migrate              пробный прогон: отчёт, в базе ничего не меняется
    scripts/api.sh 'pii/migrate?apply=1'    применить (перед записью — копия таблицы истории)

Этот скрипт делает то же самое отсюда, если до базы есть прямой доступ
(например, через SSH-туннель). Нужны те же DATABASE_URL, CLIENT_KEY_SECRET и
PII_ENCRYPTION_KEY, что у бота: с другим ключом метки не расшифруются.
Переменные — из окружения или файла .env; в аргументы их не передавать.

    python scripts/pii_migrate.py           пробный прогон (по умолчанию)
    python scripts/pii_migrate.py --apply   применить

Таблицы схемы не создаёт: сначала деплой ветки с метками, потом перенос.
В отчёте нет настоящих значений — только виды, счётчики и маски.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(apply: bool) -> int:
    from app.core import database
    from app.privacy import migrate

    if not database.is_available():
        print("Нет DATABASE_URL — некуда подключаться.", file=sys.stderr)
        return 2
    report = await migrate.run(apply=apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if "ошибка" in report else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Перенос истории диалогов на метки персональных данных")
    parser.add_argument("--apply", action="store_true", help="применить; без флага — пробный прогон")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
