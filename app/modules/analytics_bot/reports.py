"""Deterministic reports: unavailable is never silently replaced with zero."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


class Status(str, Enum):
    NOT_CONNECTED = "not_connected"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive, no partial current day

    @classmethod
    def completed(cls, now: datetime, days: int = 1):
        if days not in (1, 7, 30):
            raise ValueError("Supported periods: 1, 7, 30 days")
        end = now.astimezone(MSK).date()
        return cls(end - timedelta(days=days), end)

    @property
    def label(self):
        return f"{self.start:%d.%m.%Y} - {self.end - timedelta(days=1):%d.%m.%Y} (МСК)"


@dataclass(frozen=True)
class Sales:
    status: Status
    created: int = 0
    paid: int = 0
    amount: Decimal = Decimal("0")
    refunded: int = 0


@dataclass(frozen=True)
class Ads:
    status: Status = Status.NOT_CONNECTED


# Only aggregates leave PostgreSQL; no customer/order identifiers or text.
# analytics.v_orders excludes test orders and test clients at the view boundary.
SALES_SQL = """
SELECT count(*) FILTER (WHERE created_date_msk >= $1 AND created_date_msk < $2) AS created,
       count(*) FILTER (WHERE paid_date_msk >= $1 AND paid_date_msk < $2
                        AND is_paid AND NOT is_refunded) AS paid,
       coalesce(sum(total) FILTER (WHERE paid_date_msk >= $1 AND paid_date_msk < $2
                        AND is_paid AND NOT is_refunded), 0) AS amount,
       count(*) FILTER (WHERE paid_date_msk >= $1 AND paid_date_msk < $2
                        AND is_refunded) AS refunded
FROM analytics.v_orders
WHERE (created_date_msk >= $1 AND created_date_msk < $2)
   OR (paid_date_msk >= $1 AND paid_date_msk < $2)
"""


class PostgresSales:
    def __init__(self, dsn: str):
        self.dsn = dsn

    async def read(self, period: Period) -> Sales:
        if not self.dsn:
            return Sales(Status.NOT_CONNECTED)
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(self.dsn, timeout=5, command_timeout=10,
                                               server_settings={"application_name": "analytics_bot",
                                                                "default_transaction_read_only": "on"})
            async with connection.transaction(readonly=True):
                row = await connection.fetchrow(SALES_SQL, period.start, period.end)
            return Sales(Status.OK, int(row["created"]), int(row["paid"]),
                         Decimal(str(row["amount"])), int(row["refunded"]))
        except Exception:
            # No raw driver error: it can contain host/user/credentials or data.
            return Sales(Status.ERROR)
        finally:
            if connection is not None:
                await connection.close()


class UnconnectedAds:
    """Extension point for a future ad-platform aggregate adapter, not fake data."""
    async def read(self, period: Period) -> Ads:
        return Ads()


def money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def render(period: Period, sales: Sales, ads: Ads, kind: str = "report") -> str:
    lines = ["Два чайника · Аналитика", period.label]
    if kind in ("report", "sales"):
        lines += ["", "Продажи"]
        if sales.status == Status.NOT_CONNECTED:
            lines += ["Источник продаж пока не подключён. Данных для расчёта нет."]
        elif sales.status == Status.ERROR:
            lines += ["Источник продаж временно недоступен. Показатели не рассчитаны."]
        else:
            lines += [f"Создано заказов: {sales.created}", f"Оплаченных без полного возврата: {sales.paid}",
                      f"Сумма таких заказов: {money(sales.amount)}"]
            if sales.paid:
                lines += [f"Средняя сумма заказа: {money(sales.amount / sales.paid)}"]
            else:
                lines += ["Оплат без полного возврата за период нет. Средний чек не рассчитан."]
            lines += [f"Полностью возвращённых из оплат периода: {sales.refunded}",
                      "По дате оплаты; суммы включают доставку. Это не прибыль и не банковские поступления.",
                      "Полные возвраты исключены по текущему статусу; частичные возвраты отдельно не учтены.",
                      "Тестовые заказы исключены витриной."]
    if kind in ("report", "ads"):
        lines += ["", "Реклама", "Рекламный источник пока не подключён. Расходы, показы и клики неизвестны.",
                  "CTR, CPC, CAC и ROAS не рассчитаны: нужны данные рекламы и связь с заказами."]
    lines += ["", "Автоматическая сводка по данным источников; без прогнозов и выдуманных значений."]
    return "\n".join(lines)
