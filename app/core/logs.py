"""Логи одной строкой JSON — и без персональных данных.

Serverless Containers разбирает строку JSON сам: `message` становится
текстом записи, `level` — уровнем, остальные поля — структурой, по которой
в Cloud Logging можно фильтровать (`json_payload.service = "cdek"`).
Уровни у Yandex свои: WARN вместо WARNING, FATAL вместо CRITICAL.

Маскировка — страховка, а не основной способ: код не должен класть в лог
ФИО, телефоны, адреса, почту и текст переписки вовсе. Но тело ответа
перевозчика или трассировка могут их принести, и тогда здесь они
заменяются заглушкой. Имена и адреса регуляркой не поймать — их не пишем.

`LOG_FORMAT=text` возвращает обычный текст — удобно при запуске руками.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

_LEVELS = {"WARNING": "WARN", "CRITICAL": "FATAL"}
# Поля, которые обёртки передают через extra=… и которые попадают в JSON.
_FIELDS = ("service", "operation", "order_id", "http_status", "error_code", "duration_ms")
_MESSAGE_LIMIT = 4000
_STACK_LIMIT = 8000

_PATTERNS = (
    # Токен Telegram-бота: «123456789:AA…».
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "<токен>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <токен>"),
    # Пароль в адресе подключения: postgresql://user:пароль@host.
    (re.compile(r"(://[^:/\s@]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)\b(client_secret|secret|password|token|api_key)=([^&\s\"']+)"), r"\1=***"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<почта>"),
    # Российский мобильный в любом виде: +7 (900) 123-45-67, 89001234567.
    (re.compile(r"(?<![\w-])(?:\+7|8|7)[\s(-]*9\d{2}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)"), "<телефон>"),
)


def mask(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": _LEVELS.get(record.levelname, record.levelname),
            "logger": record.name,
            "message": mask(record.getMessage())[:_MESSAGE_LIMIT],
        }
        for field in _FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                data[field] = value
        if record.exc_info:
            # Хвост трассировки ценнее начала: там место, где упало.
            data["stack"] = mask(self.formatException(record.exc_info))[-_STACK_LIMIT:]
        return json.dumps(data, ensure_ascii=False, default=str)


class _MaskingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def setup(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("LOG_FORMAT", "json").lower() == "text":
        handler.setFormatter(_MaskingFormatter("%(levelname)s:%(name)s:%(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
