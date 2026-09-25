from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Где код сейчас. Переходы: в наличии → привязан к заказу (скан при сборке)
# → продан (закрывающий чек зарегистрирован). Возвращён — клиент вернул
# товар после продажи. Посылка, которую не вручили, возвращает коды в
# наличие: пачки те же, их можно отправить снова.
IN_STOCK = "in_stock"
ASSIGNED = "assigned"
SOLD = "sold"
RETURNED = "returned"


class MarkingCodeRow(Base):
    """Код маркировки «Честного знака» — один на пачку.

    Уникален дважды: по коду целиком и по паре GTIN + серийный номер. Вторая
    уникальность — настоящая: один и тот же код, отсканированный с
    разделителями и восстановленный без них, даёт одну пару, и привязать его
    ко второму заказу не выйдет.
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
