from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Client(Base):
    """Клиент для аналитики: одна строка на человека.

    Раньше клиента как сущности не было — были диалог (`conversations`) и
    заказы по peer_id. Здесь то, что про клиента нужно знать аналитике и
    чего нет больше нигде: псевдонимный ключ, первый контакт, метка
    рекламной кампании и признак тестового аккаунта. Персональных данных
    нет — только VK ID, он в представления не выходит.
    """

    __tablename__ = "clients"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_key: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    first_contact_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Метка кампании из первого message_new (`ref`, `ref_source`): ставится
    # один раз, при первом контакте. NULL — органика.
    ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ref_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


# --- справочники из Google Таблицы (вкладки «Товары», «Состав», «Себестоимость»,
# «Настройки», «Приходы и инвентаризация»). Источник правды — таблица: загрузчик
# `references.sync` перекладывает её сюда раз в 15 минут, Metabase видит данные
# через представления `analytics.v_ref_*` и `v_order_margin`. Персональных
# данных здесь нет — только товары, цены и параметры магазина.


class RefProduct(Base):
    """Товар из вкладки «Товары»: код — общий ключ всех вкладок справочников."""

    __tablename__ = "ref_products"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    weight_g: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3), nullable=True)
    retail_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    supplier_sku: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # «Название в каталоге бота»: по нему позиции заказов связываются с кодом.
    # NULL — «нет в каталоге».
    bot_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefComposition(Base):
    """Состав набора или сета: из чего собран продукт (вкладка «Состав»)."""

    __tablename__ = "ref_composition"

    product_code: Mapped[str] = mapped_column(String, primary_key=True)
    component_code: Mapped[str] = mapped_column(String, primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    packaging: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefCost(Base):
    """Себестоимость единицы с даты «Действует с»: старые строки не удаляются —
    маржа заказа берёт значение, действовавшее на дату оплаты."""

    __tablename__ = "ref_costs"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefSetting(Base):
    """Параметр магазина с даты «Действует с» (вкладка «Настройки»).

    Проценты хранятся долей: «3,4%» → 0.034.
    """

    __tablename__ = "ref_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 6))
    unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefStockMove(Base):
    """Приход, списание или инвентаризация сорта (вкладка «Приходы и инвентаризация»)."""

    __tablename__ = "ref_stock_moves"

    move_date: Mapped[date] = mapped_column(Date, primary_key=True)
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, primary_key=True)
    document: Mapped[str] = mapped_column(String, primary_key=True, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefSync(Base):
    """Когда справочники читались в последний раз — чтобы читать раз в 15 минут."""

    __tablename__ = "ref_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_problems: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
