from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Consent(Base):
    """Журнал согласий покупателя — доказательство для покупателя и Роскомнадзора.

    **Только дописывается.** Каждое действие — новая строка: согласие, отказ,
    отзыв, повторное нажатие той же кнопки. Ни одна строка не меняется и не
    удаляется — ни при отзыве, ни при обезличивании данных покупателя.

    Что действует сейчас — последняя строка этого вида (`type`): действует,
    если в ней `accepted`, нет `revoked_at` и (для `pd_processing`) редакция
    — текущая. Отзыв — строка с `accepted = false`, `revoked_at` и
    `revoke_source`.
    """

    __tablename__ = "consents"
    __table_args__ = (Index("ix_consents_user_type", "vk_user_id", "type", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vk_user_id: Mapped[int] = mapped_column(BigInteger)
    # pd_processing — обработка ПДн для заказа; marketing — рекламные сообщения.
    type: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    consent_url: Mapped[str] = mapped_column(String)
    accepted: Mapped[bool] = mapped_column(Boolean)
    # UTC: столбец timestamptz, база хранит момент без часового пояса.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Доказательство: сообщение ВК с нажатием или словами покупателя и событие
    # Callback API, которым оно пришло.
    vk_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    event_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Как дано или отозвано: кнопка, слова, нет ответа и т. п. — для разбора.
    method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # bot — сам покупатель в диалоге; email — письмом; manager — менеджер по обращению.
    revoke_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
