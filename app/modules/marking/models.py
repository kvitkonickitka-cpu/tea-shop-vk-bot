from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Статус кода в пуле. С 16.10.2026 пул — только список выпущенных кодов:
# где пачка сейчас, знает `order_marking_codes`. Прежние «assigned», «sold»
# и «returned» в старых строках пула остались как были, их никто не читает.
IN_STOCK = "in_stock"

# Статус кода в заказе. Переходы: отсканирован при сборке → продан
# (закрывающий чек зарегистрирован) → возвращён (чек возврата с этим кодом).
# Посылку не вручили или вернули деньги до вручения — строка удаляется:
# пачка не продана и снова лежит на полке.
SCANNED = "scanned"
SOLD = "sold"
RETURNED = "returned"

# Что за единица в позиции: пачка (100 г, 35 г, блин 357 г) или сет / набор
# в коробке. У набора свой GTIN и свой код — код идентификации набора (КИН)
# на коробке; пачки внутри при сборке не сканируются, в чеке набор — одна
# позиция с КИН (ответ «Честного знака», обращение SR8968218).
PACK = "pack"
SET = "set"


class OrderMarkingCode(Base):
    """Код маркировки, закреплённый за позицией заказа, — один на единицу.

    Один код нельзя отдать двум заказам и нельзя посчитать дважды в одном:
    пара GTIN + серийный номер уникальна среди всех строк, кроме
    возвращённых. Возвращённый по чеку возврата код снова в обороте, и ту же
    пачку можно продать ещё раз — новой строкой, а старая остаётся историей
    прошлого заказа (по ней видно, что ушло в его чек возврата).

    Пара, а не код целиком: один и тот же код, отсканированный с
    разделителями и восстановленный без них, даёт одну пару.
    """

    __tablename__ = "order_marking_codes"
    __table_args__ = (
        Index(
            "order_marking_codes_active", "gtin", "serial", unique=True,
            postgresql_where=text("status <> 'returned'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    # Номер позиции в `orders.items` — отдельной таблицы позиций у заказа нет,
    # состав хранится списком, и номер в нём и есть идентификатор позиции.
    item_index: Mapped[int] = mapped_column(Integer)
    # Код целиком, с криптохвостом и разделителями GS (восстановленными, если
    # сканер их потерял). Text: длинные коды с подписью 92 — за сотню символов.
    raw_code: Mapped[str] = mapped_column(Text)
    gtin: Mapped[str] = mapped_column(String(14))
    serial: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String, default=PACK)
    status: Mapped[str] = mapped_column(String, default=SCANNED)
    # Код есть в загруженной выгрузке СУЗ.
    from_pool: Mapped[bool] = mapped_column(Boolean, default=False)
    # Набран руками или разделители восстановлены нами — сверить с пачкой.
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    scanned_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Возврат ЮKassa, в чек которого ушёл код.
    refund_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class MarkingCodeRow(Base):
    """Пул: код маркировки из выгрузки СУЗ «Честного знака».

    Уникален дважды: по коду целиком и по паре GTIN + серийный номер — тот
    же код с разделителями и без них не заведётся второй строкой.

    До 16.10.2026 таблица хранила и привязку к заказу (`order_id`,
    `item_index`, статусы): коды, отсканированные не из пула, тоже ложились
    сюда. Это переехало в `order_marking_codes`, а старые привязки
    переносятся туда при старте (`database._MISSING_COLUMNS`).
    """

    __tablename__ = "marking_codes"
    __table_args__ = (UniqueConstraint("gtin", "serial", name="marking_codes_gtin_serial"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Код целиком, с криптохвостом и разделителями GS. Text, а не String:
    # длинные коды с подписью 92 бывают за сотню символов.
    code: Mapped[str] = mapped_column(Text, unique=True)
    gtin: Mapped[str] = mapped_column(String(14), index=True)
    serial: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String, default=IN_STOCK)
    # Код пришёл из выгрузки СУЗ, а не впервые увиден при сборке. Если хоть
    # один такой есть, пул считается заведённым и при сборке принимаются
    # только коды из него.
    from_pool: Mapped[bool] = mapped_column(Boolean, default=False)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Номер позиции в `orders.items`: у заказа из двух сортов коды каждого
    # сорта считаются отдельно.
    item_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scanned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Код набран руками, а не отсканирован: разделители восстановлены нами.
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
