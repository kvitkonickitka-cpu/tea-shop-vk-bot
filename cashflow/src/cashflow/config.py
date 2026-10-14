"""Настройки из /etc/cashflow/.env. Секреты никогда не логируются."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_ENV_PATH = Path("/etc/cashflow/.env")


def load_env_file(path: Path | None = None) -> None:
    """Читает .env в os.environ. Уже заданные переменные окружения не перетирает."""
    env_path = path or Path(os.environ.get("CASHFLOW_ENV_FILE", DEFAULT_ENV_PATH))
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    tbank_token: str
    tbank_api_base: str
    accounts: list[str]
    first_day: date
    dsn: str
    sync_overlap_days: int = 10
    ca_bundle: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    log_level: str = "INFO"
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / "data")

    @property
    def is_sandbox(self) -> bool:
        return "sandbox" in self.tbank_api_base

    def masked_token(self) -> str:
        if not self.tbank_token:
            return "(пусто)"
        return f"{self.tbank_token[:4]}…{self.tbank_token[-2:]} ({len(self.tbank_token)} симв.)"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Не задана переменная {name}. Проверьте файл /etc/cashflow/.env "
            f"(шаблон — cashflow/.env.example)."
        )
    return value


def load_config(env_path: Path | None = None) -> Config:
    load_env_file(env_path)

    accounts = [a.strip() for a in os.environ.get("TBANK_ACCOUNTS", "").split(",") if a.strip()]
    if not accounts:
        raise ConfigError(
            "Не заданы номера счетов TBANK_ACCOUNTS. Перечислите через запятую: "
            "основной, копилка, депозит."
        )

    try:
        first_day = date.fromisoformat(os.environ.get("TBANK_FIRST_DAY", "").strip())
    except ValueError as exc:
        raise ConfigError(
            "TBANK_FIRST_DAY должна быть датой открытия счёта в формате ГГГГ-ММ-ДД."
        ) from exc

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        dsn = (
            f"host={_require('PGHOST')} port={os.environ.get('PGPORT', '5432')} "
            f"dbname={_require('PGDATABASE')} user={_require('PGUSER')} "
            f"password={_require('PGPASSWORD')}"
        )

    return Config(
        tbank_token=_require("TBANK_TOKEN"),
        tbank_api_base=os.environ.get("TBANK_API_BASE", "https://business.tbank.ru/openapi/sandbox").rstrip("/"),
        accounts=accounts,
        first_day=first_day,
        dsn=dsn,
        sync_overlap_days=int(os.environ.get("SYNC_OVERLAP_DAYS", "10")),
        ca_bundle=os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE"),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
