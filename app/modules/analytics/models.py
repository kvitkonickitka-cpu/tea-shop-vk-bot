from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
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
