from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Conversation(Base):
    # "Шапка" диалога — одна строка на клиента (peer_id). Позволяет быстро
    # найти диалог по id и увидеть метаданные, не читая все сообщения, а
    # также служит родительской таблицей для DataLens (join по peer_id).
    __tablename__ = "conversations"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.peer_id"), index=True)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(String)
    # Кто написал реплику с ролью assistant: сам бот или живой менеджер,
    # зашедший в диалог руками. В API Claude ролей всего две, и без этой
    # пометки ответы менеджера неотличимы от собственных слов бота — он
    # приписывал себе чужие реплики, и это же попадало в отчёты.
    # NULL означает «бот»: так читаются все строки, записанные до появления
    # колонки.
    author: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EscalationState(Base):
    __tablename__ = "escalation_states"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProcessedEvent(Base):
    # Дедуп event_id из VK Callback API в БД, а не в памяти процесса — VK
    # ретраит недоставленные вебхуки, и при рестарте контейнера или
    # масштабировании на второй инстанс in-memory set не спасает от
    # повторной обработки одного и того же события.
    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Escalation(Base):
    # Лог каждого фактического обращения к менеджеру (не путать с
    # EscalationState — та хранит только текущий статус "открыта/закрыта"
    # для подавления повторных алертов, а тут — история для статистики).
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    question: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)


class DialogReport(Base):
    # Когда по диалогу последний раз уходил мини-отчёт в Telegram. Отдельная
    # таблица, а не колонка в conversations, потому что миграций в проекте нет:
    # create_all создаёт недостающие таблицы, но не добавляет колонки в
    # существующие, и новое поле в conversations просто не появилось бы.
    __tablename__ = "dialog_reports"

    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
