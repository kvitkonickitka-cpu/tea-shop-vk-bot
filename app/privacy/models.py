from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PiiEntry(Base):
    """Значение метки персональных данных: [NAME_1] → «Иванов Иван».

    Клиент — только псевдонимный `client_key`, VK ID здесь нет. Значение
    зашифровано (AES-GCM, ключ `PII_ENCRYPTION_KEY` из окружения), а рядом —
    его слепой отпечаток (HMAC): по нему «тот же телефон — та же метка»
    находится без расшифровки. Метки нумеруются в пределах клиента, поэтому
    [PHONE_1] у двух клиентов — разные значения, и чужую метку подставить
    нельзя: её нет у этого клиента.
    """

    __tablename__ = "pii_vault"
    __table_args__ = (
        UniqueConstraint("client_key", "label", name="uq_pii_vault_label"),
        UniqueConstraint("client_key", "value_hash", name="uq_pii_vault_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_key: Mapped[str] = mapped_column(String, index=True)
    # NAME_1, PHONE_2 — без скобок.
    label: Mapped[str] = mapped_column(String)
    # NAME, PHONE, EMAIL, ADDR.
    kind: Mapped[str] = mapped_column(String)
    value_hash: Mapped[str] = mapped_column(String)
    # base64(nonce ‖ шифротекст ‖ тег). Связанные данные шифра — клиент и
    # метка: строку нельзя переставить другому клиенту или другой метке.
    value_enc: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
