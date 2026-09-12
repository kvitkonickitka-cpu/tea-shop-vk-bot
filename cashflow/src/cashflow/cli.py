"""Командная строка: python -m cashflow <команда>."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from . import classify, db, doctor, importer, loans, notify, suggest
from .config import Config, ConfigError, load_config
from .tbank import TBankClient

log = logging.getLogger("cashflow")

STATUS_MARK = {"ok": "[ ok ]", "warn": "[ ?  ]", "fail": "[ !! ]"}


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    checks = doctor.run_doctor(cfg, skip_bank=args.skip_bank)
    print("\nПроверка окружения\n" + "-" * 60)
    for check in checks:
        print(f"{STATUS_MARK.get(check.status, '     ')} {check.name}: {check.detail}")
    failed = [c for c in checks if c.status == "fail"]
    print("-" * 60)
    print("Всё в порядке" if not failed else f"Проблем: {len(failed)}")
    return 1 if failed else 0


def cmd_migrate(cfg: Config, args: argparse.Namespace) -> int:
    with db.connect(cfg.dsn) as conn:
        applied = db.apply_migrations(conn)
    print("Применены миграции: " + (", ".join(applied) if applied else "нет новых"))
    return 0


def cmd_import_rules(cfg: Config, args: argparse.Namespace) -> int:
    with db.connect(cfg.dsn) as conn:
        report = importer.import_all(conn, cfg.data_dir)
    if not report.ok:
        print("Справочники НЕ загружены, в базе прежние значения. Ошибки:")
        for error in report.errors:
            print(f"  - {error}")
        return 1
    print(
        f"Загружено: статей {report.articles}, правил {report.rules}, "
        f"ручных разметок {report.overrides}, периодов кредита {report.loan_periods}"
    )
    return 0


def _sync(cfg: Config, full: bool, period_from: date | None) -> int:
    from .sync import run_sync

    with db.connect(cfg.dsn) as conn, TBankClient(
        cfg.tbank_token, cfg.tbank_api_base, cfg.ca_bundle
    ) as client:
        results = run_sync(conn, client, cfg, full=full, period_from=period_from)

    for result in results:
        print(
            f"Счёт …{result.account_number[-4:]} за {result.period_from}…{result.period_to}: "
            f"новых {result.inserted}, обновлено {result.updated}, остатков {result.balances}"
        )
    return 0


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    return _sync(cfg, full=False, period_from=None)


def cmd_backfill(cfg: Config, args: argparse.Namespace) -> int:
    period_from = date.fromisoformat(args.date_from) if args.date_from else cfg.first_day
    print(f"Полная загрузка с {period_from}. Это может занять несколько минут.")
    return _sync(cfg, full=True, period_from=period_from)


def cmd_classify(cfg: Config, args: argparse.Namespace) -> int:
    since = date.fromisoformat(args.since) if args.since else None
    with db.connect(cfg.dsn) as conn:
        report = classify.run_classify(conn, since)
    print(
        f"Операций {report.operations}, строк в отчётах {report.rows}\n"
        f"  по правилам: {report.by_rule}\n"
        f"  вручную:     {report.manual}\n"
        f"  кредит:      {report.loan_split}\n"
        f"  не разобрано:{report.unclassified} на {report.unclassified_amount} ₽"
    )
    if report.flagged:
        print("Требуют внимания:")
        for operation_id, flags in report.flagged[:20]:
            print(f"  {operation_id}: {', '.join(flags)}")
    return 0


def cmd_suggest(cfg: Config, args: argparse.Namespace) -> int:
    with db.connect(cfg.dsn) as conn:
        count, path = suggest.run_suggest(conn, cfg.data_dir)
    print(f"Предложено правил: {count}. Черновик: {path}")
    print("Проверьте их, впишите статьи, перенесите нужные строки в rules.csv и включите active=true.")
    return 0


def cmd_loan_schedule(cfg: Config | None, args: argparse.Namespace) -> int:
    schedule = loans.annuity_schedule(
        Decimal(str(args.amount)), Decimal(str(args.rate)), args.months,
        date.fromisoformat(args.first_payment),
    )
    path = Path(args.out) if args.out else Path("loan_schedule.csv")
    loans.write_csv(schedule, path, args.loan_id)
    total_interest = sum(p.interest for p in schedule)
    print(f"График на {args.months} мес. записан в {path}")
    print(f"Ежемесячный платёж: {schedule[0].payment_total} ₽, всего процентов: {total_interest} ₽")
    return 0


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> int:
    try:
        for step, func in (
            ("import-rules", cmd_import_rules),
            ("sync", cmd_sync),
            ("classify", cmd_classify),
        ):
            log.info("Шаг %s", step)
            code = func(cfg, args)
            if code != 0:
                raise RuntimeError(f"Шаг {step} завершился с ошибкой")
    except Exception as exc:  # noqa: BLE001 — нужно уведомить владельца о любой поломке
        log.exception("run-all остановлен")
        notify.send_error(cfg, f"Синхронизация Т-Банка упала: {exc}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cashflow",
        description="Выписка Т-Бизнеса → PostgreSQL → витрины для DataLens",
    )
    parser.add_argument("--env-file", help="Путь к .env (по умолчанию /etc/cashflow/.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="проверить окружение: сертификаты, IP, токен, базу")
    p.add_argument("--skip-bank", action="store_true", help="не дёргать банк")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("migrate", help="применить миграции схемы finance")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("import-rules", help="загрузить справочники из CSV в базу")
    p.set_defaults(func=cmd_import_rules)

    p = sub.add_parser("sync", help="догрузить свежие операции (с перекрытием)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("backfill", help="полная загрузка с даты открытия счёта")
    p.add_argument("--from", dest="date_from", help="дата ГГГГ-ММ-ДД")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("classify", help="разложить операции по статьям")
    p.add_argument("--since", help="пересчитать только с этой даты")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("suggest-rules", help="черновик правил по неразобранным операциям")
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("run-all", help="import-rules + sync + classify (для systemd)")
    p.add_argument("--since", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run_all)

    p = sub.add_parser("loan-schedule", help="посчитать аннуитетный график кредита")
    p.add_argument("--amount", required=True, type=float, help="сумма кредита")
    p.add_argument("--rate", required=True, type=float, help="ставка годовых, процентов")
    p.add_argument("--months", required=True, type=int, help="срок в месяцах")
    p.add_argument("--first-payment", required=True, help="дата первого платежа ГГГГ-ММ-ДД")
    p.add_argument("--loan-id", default="main")
    p.add_argument("--out", help="куда записать CSV")
    p.set_defaults(func=cmd_loan_schedule, needs_config=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "needs_config", True):
        setup_logging("INFO")
        return args.func(None, args)

    try:
        cfg = load_config(Path(args.env_file) if args.env_file else None)
    except ConfigError as exc:
        setup_logging("INFO")
        print(f"Ошибка настройки: {exc}")
        return 2

    setup_logging(cfg.log_level)
    if not cfg.is_sandbox:
        log.info("Работаем с ПРОДОМ T-API")
    return args.func(cfg, args)
