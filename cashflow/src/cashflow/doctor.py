"""Самопроверка окружения: сертификаты, IP, токен, база, свежесть данных."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg

from .config import Config
from .tbank import TBankClient, TBankError

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"

# Корневой сертификат Минцифры. Без него ВМ не доверяет сертификату банка.
RUSSIAN_CA_MARKERS = ("Russian Trusted Root CA", "Russian Trusted Sub CA")
YC_METADATA_IP = (
    "http://169.254.169.254/computeMetadata/v1/instance/"
    "network-interfaces/0/access-configs/0/external-ip"
)


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_certificates(cfg: Config) -> Check:
    bundle = Path(cfg.ca_bundle) if cfg.ca_bundle else Path("/etc/ssl/certs/ca-certificates.crt")
    if not bundle.exists():
        return Check("Сертификаты Минцифры", FAIL, f"Файл {bundle} не найден")
    try:
        text = bundle.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return Check("Сертификаты Минцифры", FAIL, f"Не прочитать {bundle}: {exc}")

    found = [marker for marker in RUSSIAN_CA_MARKERS if marker in text]
    if len(found) == len(RUSSIAN_CA_MARKERS):
        return Check("Сертификаты Минцифры", OK, f"Root и Sub CA есть в {bundle}")
    if found:
        return Check(
            "Сертификаты Минцифры", WARN,
            f"В {bundle} найден только {found[0]} — добавьте недостающий",
        )
    # В bundle может не быть текстовых меток, тогда проверяем фактическим соединением.
    try:
        context = ssl.create_default_context(cafile=str(bundle))
        with httpx.Client(verify=context, timeout=15) as client:
            client.get("https://business.tbank.ru/openapi", follow_redirects=False)
    except Exception as exc:  # noqa: BLE001 — важен сам факт неудачи проверки TLS
        return Check(
            "Сертификаты Минцифры", FAIL,
            f"TLS до business.tbank.ru не проверяется: {exc}. "
            "Установите Russian Trusted Root CA и Sub CA.",
        )
    return Check("Сертификаты Минцифры", OK, "TLS до банка проверяется корректно")


def check_external_ip() -> Check:
    try:
        with httpx.Client(timeout=5) as client:
            response = client.get(YC_METADATA_IP, headers={"Metadata-Flavor": "Google"})
        if response.status_code == 200:
            ip = response.text.strip()
            return Check(
                "Внешний IP", WARN,
                f"IP машины {ip}. Убедитесь в консоли Yandex Cloud (VPC → IP-адреса), "
                "что он зарезервирован как статический и совпадает с IP в настройках токена.",
            )
    except httpx.HTTPError:
        pass
    return Check(
        "Внешний IP", WARN,
        "Метаданные Yandex Cloud недоступны — проверьте статический IP вручную в консоли.",
    )


def check_database(cfg: Config) -> list[Check]:
    checks: list[Check] = []
    try:
        conn = psycopg.connect(cfg.dsn, connect_timeout=10)
    except psycopg.Error as exc:
        return [Check("Подключение к PostgreSQL", FAIL, str(exc).strip()[:200])]

    with conn:
        checks.append(Check("Подключение к PostgreSQL", OK, "Соединение установлено"))
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            checks.append(Check("Версия PostgreSQL", OK, (row[0] if row else "")[:60]))

            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'finance'"
            )
            row = cur.fetchone()
            tables = row[0] if row else 0
            checks.append(
                Check("Схема finance", OK if tables else FAIL,
                      f"объектов: {tables}" if tables else "схема пуста, примените миграции")
            )

            if tables:
                cur.execute("SELECT count(*) FROM finance.articles")
                row = cur.fetchone()
                articles = row[0] if row else 0
                checks.append(
                    Check("Справочник статей", OK if articles else FAIL,
                          f"{articles} статей" if articles else "пуст, выполните import-rules")
                )

                cur.execute(
                    "SELECT account_number, synced_through, last_success_at, last_status "
                    "FROM finance.sync_state ORDER BY account_number"
                )
                rows = cur.fetchall()
                if not rows:
                    checks.append(
                        Check("Свежесть синхронизации", WARN, "синхронизация ещё ни разу не проходила")
                    )
                for account, through, success_at, status in rows:
                    stale = (
                        success_at is None
                        or datetime.now(timezone.utc) - success_at > timedelta(days=2)
                    )
                    checks.append(
                        Check(
                            f"Синхронизация счёта …{account[-4:]}",
                            FAIL if status != "ok" else (WARN if stale else OK),
                            f"статус {status}, данные по {through}, последний успех {success_at}",
                        )
                    )

                cur.execute(
                    "SELECT count(*) FROM finance.v_cash_summary "
                    "WHERE period_type = 'M' AND check_diff <> 0"
                )
                row = cur.fetchone()
                bad = row[0] if row else 0
                checks.append(
                    Check("Сходимость остатков", OK if bad == 0 else FAIL,
                          "все месяцы сходятся" if bad == 0
                          else f"месяцев с расхождением: {bad} — смотрите v_unclassified")
                )
    return checks


def check_token(cfg: Config) -> Check:
    """Пробный запрос выписки за вчера. Токен живёт 90 дней с последнего использования."""
    yesterday = date.today() - timedelta(days=1)
    try:
        with TBankClient(cfg.tbank_token, cfg.tbank_api_base, cfg.ca_bundle, timeout=30) as client:
            pages = client.statement(cfg.accounts[0], yesterday, yesterday)
            operations, _ = next(pages, ([], []))
    except TBankError as exc:
        return Check("Токен T-API", FAIL, str(exc)[:250])
    except Exception as exc:  # noqa: BLE001
        return Check("Токен T-API", FAIL, f"{type(exc).__name__}: {exc}"[:250])

    where = "песочница" if cfg.is_sandbox else "прод"
    return Check(
        "Токен T-API", OK,
        f"{where}, токен {cfg.masked_token()}, за вчера операций: {len(operations)}",
    )


def check_data_files(cfg: Config) -> Check:
    missing = [
        name
        for name in ("articles.csv", "rules.csv", "manual_overrides.csv", "loan_schedule.csv")
        if not (cfg.data_dir / name).exists()
    ]
    if missing:
        return Check("Файлы справочников", FAIL, f"нет файлов: {', '.join(missing)}")
    return Check("Файлы справочников", OK, f"все на месте в {cfg.data_dir}")


def run_doctor(cfg: Config, skip_bank: bool = False) -> list[Check]:
    checks = [check_certificates(cfg), check_external_ip(), check_data_files(cfg)]
    checks.extend(check_database(cfg))
    if not skip_bank:
        checks.append(check_token(cfg))
    return checks
