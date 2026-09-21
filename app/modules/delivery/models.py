from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OzonDeliveryPoint(Base):
    """Пункт выдачи Ozon, выгруженный к себе.

    У Ozon нет поиска пункта по адресу или городу: каталог отдаётся целиком,
    постранично, а адреса добираются вторым запросом. Искать так по ходу
    диалога невозможно, поэтому держим копию и ищем по ней.
    """

    __tablename__ = "ozon_delivery_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Слова адреса без «улица», «дом» и прочего шума — по ним и ищем.
    # Отдельной колонкой, чтобы не нормализовать на каждый запрос.
    search_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Закрытые пункты Ozon из каталога не удаляет, а помечает. Предлагать их
    # клиенту нельзя, но и удалять у себя не стоит: пункт может открыться
    # снова, а заказы с ним в истории останутся.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # pvz или postamat.
    kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # На каком проходе каталога пункт встретился в последний раз. Ozon не
    # говорит, что пункт исчез, — он просто перестаёт его отдавать. Сравнение
    # с номером завершённого прохода и есть способ это заметить.
    seen_pass: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OzonSyncState(Base):
    """Где остановилась выгрузка каталога.

    Каталог не влезает в один заход таймера, поэтому курсор переживает
    перезапуск: следующий тик продолжает с того же места.
    """

    __tablename__ = "ozon_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cursor: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Сколько пунктов записали за текущий проход по каталогу.
    seen: Mapped[int] = mapped_column(default=0)
    # Номер текущего прохода. Растёт, когда проход дошёл до конца.
    pass_number: Mapped[int] = mapped_column(default=1)
    # Когда последний раз дошли до конца каталога.
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
