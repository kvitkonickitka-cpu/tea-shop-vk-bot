"""Согласия покупателя по 152-ФЗ: журнал, «действует ли», ворота и фильтр рассылок.

Два вида согласия:

- `pd_processing` — обработка ПДн для заказа (имя, телефон, почта, адрес или
  пункт выдачи). Обязательно: без него бот консультирует, но заказ с данными
  не оформляет и данных не просит. Сменилась редакция (`PD_CONSENT_VERSION`) —
  согласие нужно заново;
- `marketing` — новости и скидки. По желанию, спрашивается один раз после
  оплаты; нет ответа — считается «нет». Любая продающая рассылка идёт только
  через `marketing_allowed` / `marketing_filter`.

Журнал `consents` только дописывается: согласие, отказ и отзыв — каждое
отдельной строкой; действует последняя строка своего вида.

**Без базы согласия нет** (ворота закрыты): записать его негде, а заказ без
записи согласия — тот же сбор данных без согласия.
"""

from __future__ import annotations

import contextvars
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import keyboard as keyboards
from app.messages import templates
from app.modules.consent.models import Consent

logger = logging.getLogger(__name__)

PD = "pd_processing"
MARKETING = "marketing"
_MARKETING_VERSION = "1"

# Источник отзыва.
REVOKE_BOT = "bot"
REVOKE_EMAIL = "email"
REVOKE_MANAGER = "manager"

# Доказательство текущего действия: сообщение ВК и событие Callback API, с
# которыми пришло нажатие или слова покупателя. Ставит приём сообщения.
_evidence: contextvars.ContextVar[tuple[int | None, str | None]] = contextvars.ContextVar(
    "consent_evidence", default=(None, None)
)


def set_evidence(message: dict | None, event_id: str | None) -> None:
    message = message or {}
    raw = message.get("id") or message.get("conversation_message_id")
    try:
        message_id = int(raw) if raw else None
    except (TypeError, ValueError):
        message_id = None
    _evidence.set((message_id, event_id))


def _version(kind: str) -> str:
    return str(settings.pd_consent_version) if kind == PD else _MARKETING_VERSION


async def record(peer_id: int, kind: str, accepted: bool, *, method: str) -> Consent:
    """Новая строка журнала: согласие или отказ. Ничего не перезаписывает."""
    message_id, event_id = _evidence.get()
    row = Consent(
        vk_user_id=peer_id, type=kind, version=_version(kind), consent_url=settings.pd_consent_url,
        accepted=accepted, vk_message_id=message_id, event_id=event_id, method=method,
        created_at=datetime.now(timezone.utc),
    )
    async with get_session_factory()() as session:
        session.add(row)
        await session.commit()
    logger.info("Согласие %s peer_id=%s: %s (%s)", kind, peer_id, "да" if accepted else "нет", method)
    return row


async def revoke(peer_id: int, kinds: tuple[str, ...] = (PD, MARKETING), *, source: str = REVOKE_BOT,
                 method: str = "revoke") -> None:
    """Отзыв — строкой на каждый вид, даже если согласия и не было: отзыв зафиксирован."""
    message_id, event_id = _evidence.get()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        for kind in kinds:
            session.add(Consent(
                vk_user_id=peer_id, type=kind, version=_version(kind), consent_url=settings.pd_consent_url,
                accepted=False, vk_message_id=message_id, event_id=event_id, method=method,
                created_at=now, revoked_at=now, revoke_source=source,
            ))
        await session.commit()
    logger.info("Согласие отозвано peer_id=%s: %s (%s)", peer_id, ", ".join(kinds), source)


async def _latest(peer_id: int, kind: str) -> Consent | None:
    async with get_session_factory()() as session:
        return await session.scalar(
            select(Consent).where(Consent.vk_user_id == peer_id, Consent.type == kind)
            .order_by(Consent.created_at.desc(), Consent.id.desc()).limit(1)
        )


def _active(row: Consent | None, kind: str) -> bool:
    return bool(row and row.accepted and row.revoked_at is None and row.version == _version(kind))


async def has_pd(peer_id: int) -> bool:
    """Действует ли согласие на обработку ПДн текущей редакции."""
    try:
        return _active(await _latest(peer_id, PD), PD)
    except RuntimeError:
        return False  # базы нет — согласия не записать, ворота закрыты
    except Exception:
        logger.exception("Не проверили согласие peer_id=%s — считаем, что его нет", peer_id)
        return False


async def pd_declined(peer_id: int) -> bool:
    """Последний ответ на запрос текущей редакции — «Не согласен» (не отзыв)."""
    try:
        row = await _latest(peer_id, PD)
    except Exception:
        return False
    return bool(row and not row.accepted and row.revoked_at is None and row.version == _version(PD))


async def marketing_allowed(peer_id: int) -> bool:
    try:
        return _active(await _latest(peer_id, MARKETING), MARKETING)
    except Exception:
        logger.exception("Не проверили согласие на рассылку peer_id=%s — не шлём", peer_id)
        return False


async def marketing_filter(peer_ids) -> set[int]:
    """Из списка — только те, кому можно слать рекламу: последняя строка `marketing` — согласие.

    Любая будущая рассылка обязана пропустить адресатов через этот фильтр.
    """
    wanted = {int(p) for p in peer_ids}
    if not wanted:
        return set()
    try:
        async with get_session_factory()() as session:
            ranked = (
                select(Consent.vk_user_id, Consent.accepted, Consent.revoked_at, Consent.version,
                       func.row_number().over(partition_by=Consent.vk_user_id,
                                              order_by=(Consent.created_at.desc(), Consent.id.desc())).label("n"))
                .where(Consent.type == MARKETING, Consent.vk_user_id.in_(wanted))
                .subquery()
            )
            rows = (await session.execute(select(ranked).where(ranked.c.n == 1))).all()
    except Exception:
        logger.exception("Фильтр рассылки не сработал — не шлём никому")
        return set()
    return {r.vk_user_id for r in rows if r.accepted and r.revoked_at is None and r.version == _MARKETING_VERSION}


async def journal(peer_id: int) -> list[dict]:
    """Журнал согласий покупателя — для его запроса или проверки Роскомнадзора."""
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Consent).where(Consent.vk_user_id == peer_id).order_by(Consent.created_at, Consent.id)
        )).scalars().all()
    return [
        {
            "id": r.id, "vk_user_id": r.vk_user_id, "type": r.type, "version": r.version,
            "consent_url": r.consent_url, "accepted": r.accepted,
            "created_at": r.created_at.astimezone(timezone.utc).isoformat() if r.created_at else None,
            "vk_message_id": r.vk_message_id, "event_id": r.event_id, "method": r.method,
            "revoked_at": r.revoked_at.astimezone(timezone.utc).isoformat() if r.revoked_at else None,
            "revoke_source": r.revoke_source,
        }
        for r in rows
    ]


async def _count(peer_id: int, kind: str) -> int:
    async with get_session_factory()() as session:
        return int(await session.scalar(
            select(func.count()).select_from(Consent).where(Consent.vk_user_id == peer_id, Consent.type == kind)
        ) or 0)


# --- запрос согласия на ПДн --------------------------------------------------


def pd_keyboard() -> dict | None:
    return keyboards.inline([[
        keyboards.text_button(templates.PD_YES_LABEL, {"a": "pd_yes"}, color="positive"),
        keyboards.text_button(templates.PD_NO_LABEL, {"a": "pd_no"}),
    ]])


def retry_keyboard() -> dict | None:
    return keyboards.inline([[keyboards.text_button(templates.PD_RETRY_LABEL, {"a": "pd_retry"})]])


async def _ask_ref(peer_id: int) -> str:
    # Номер запроса — число ответов в журнале: после «Не согласен» следующий
    # запрос — новое событие, а до ответа повторный — то же самое.
    return f"consent:{peer_id}:{await _count(peer_id, PD)}"


async def pd_prompt(peer_id: int) -> tuple[str, dict | None]:
    """Текст запроса согласия и кнопки — с подсказкой словами, если кнопок не видно."""
    keyboard = pd_keyboard()
    shown = await keyboards.for_peer(peer_id, keyboard) is not None
    return templates.pd_consent_request(settings.pd_consent_url, buttons=shown), keyboard if shown else None


async def claim_pd_prompt(peer_id: int) -> bool:
    """Отметить, что запрос согласия отправлен (в этом же ответе). True — первый раз на этот запрос."""
    from app.messages import client as client_messages

    try:
        return await client_messages.claim_once(await _ask_ref(peer_id), templates.PD_CONSENT_ASK, peer_id)
    except Exception:
        logger.exception("Не отметили запрос согласия peer_id=%s", peer_id)
        return True


async def pd_prompt_pending(peer_id: int) -> bool:
    """Запрос согласия отправлен, ответа ещё нет — «согласен» словами засчитывается."""
    from app.messages import client as client_messages

    try:
        return await client_messages.already_sent(await _ask_ref(peer_id), templates.PD_CONSENT_ASK)
    except Exception:
        return False


async def _claimed(ref: str, event_type: str) -> bool:
    from app.messages import client as client_messages

    return await client_messages.already_sent(ref, event_type)


async def send_pd_prompt(peer_id: int, *, intro: str = "") -> bool:
    """Отдельным сообщением — для кнопок, витрины, геопозиции. True — отправлено сейчас."""
    from app.messages import client as client_messages

    text, keyboard = await pd_prompt(peer_id)
    return await client_messages.send(
        peer_id=peer_id, ref=await _ask_ref(peer_id), event_type=templates.PD_CONSENT_ASK,
        text=f"{intro}\n\n{text}" if intro else text, keyboard=keyboard,
    )


# --- вопрос о рассылке после оплаты ---------------------------------------------


def marketing_keyboard() -> dict | None:
    return keyboards.inline([[
        keyboards.text_button(templates.MARKETING_YES_LABEL, {"a": "mk_yes"}),
        keyboards.text_button(templates.MARKETING_NO_LABEL, {"a": "mk_no"}),
    ]])


async def ask_marketing_once(peer_id: int) -> bool:
    """Один раз за всё время: уже спрашивали или уже есть ответ — молчим."""
    from app.messages import client as client_messages

    try:
        if await _count(peer_id, MARKETING):
            return False
        keyboard = marketing_keyboard()
        shown = await keyboards.for_peer(peer_id, keyboard) is not None
        return await client_messages.send(
            peer_id=peer_id, ref=f"peer:{peer_id}", event_type=templates.MARKETING_CONSENT_ASK,
            text=templates.marketing_consent_ask(settings.marketing_max_per_month, buttons=shown),
            keyboard=keyboard if shown else None,
        )
    except Exception:
        logger.exception("Не спросили про рассылку peer_id=%s", peer_id)
        return False


async def marketing_question_pending(peer_id: int) -> bool:
    """Вопрос о рассылке задан, ответа нет — «да»/«нет» словами засчитывается."""
    try:
        return await _claimed(f"peer:{peer_id}", templates.MARKETING_CONSENT_ASK) and not await _count(
            peer_id, MARKETING
        )
    except Exception:
        return False


# --- отзыв --------------------------------------------------------------------


def working_days_after(start: date, days: int) -> date:
    """Через N рабочих дней (пн–пт). Праздники не учитываются — запас за менеджером."""
    current, left = start, days
    while left > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            left -= 1
    return current


# --- данные до согласия --------------------------------------------------------

# Поля черновика с личным: без согласия их в черновике быть не может.
PD_DETAIL_KEYS = (
    "recipient_name", "recipient_phone", "recipient_email", "pending_recipient", "email_suggestion",
    "storefront_recipient", "offer", "vk_order_address", "storefront_street",
)
HIDDEN = "[скрыто до согласия]"
_HIDDEN_EVENT = "pd_hidden"


def strip_pd(details: dict, delivery_method: str | None) -> list[str]:
    """Убрать из черновика личное. Возвращает, какие поля сняты (для лога)."""
    removed = [key for key in PD_DETAIL_KEYS if details.get(key)]
    for key in removed:
        details.pop(key, None)
    if delivery_method == "cdek_courier" and details.get("address"):
        # У курьера в address — улица и дом, у пунктов — только город.
        details.pop("address", None)
        removed.append("address")
    return removed


async def hide_pd(peer_id: int, text: str) -> tuple[str, int]:
    """Телефон, почта, адрес с домом и ФИО рядом с ними — до согласия не сохраняем.

    Текст меняется до записи в базу (`inbound_messages`, история) и до модели:
    у нас не остаётся ни значения, ни метки. Покупателю после согласия бот
    скажет прислать данные снова.
    """
    from app import privacy
    from app.privacy import detect

    if not text:
        return text, 0
    found = detect.emails(text) + detect.phones(text) + detect.street_addresses(text)
    if found:
        try:
            stop = await privacy.stop_list()
            blanked = text
            for item in found:
                blanked = blanked[:item.start] + " " * (item.end - item.start) + blanked[item.end:]
            found += detect.recipient_names(blanked, stop)
        except Exception:
            logger.exception("Не искали ФИО рядом с контактами peer_id=%s", peer_id)
    taken: list[tuple[int, int]] = []
    for item in sorted(found, key=lambda f: f.start, reverse=True):
        if any(item.start < end and start < item.end for start, end in taken):
            continue
        text = text[:item.start] + HIDDEN + text[item.end:]
        taken.append((item.start, item.end))
    if taken:
        await mark_hidden(peer_id)
        logger.info("peer_id=%s: до согласия скрыто данных — %s", peer_id, len(taken))
    return text, len(taken)


async def mark_hidden(peer_id: int) -> None:
    """Отметка «присылал данные до согласия» — после согласия попросим их снова."""
    try:
        await _claimed_once(f"consent:{peer_id}:{await _count(peer_id, PD)}", _HIDDEN_EVENT, peer_id)
    except Exception:
        logger.exception("Не отметили скрытые данные peer_id=%s", peer_id)


async def _claimed_once(ref: str, event_type: str, peer_id: int) -> bool:
    from app.messages import client as client_messages

    return await client_messages.claim_once(ref, event_type, peer_id)


async def had_hidden(peer_id: int, answers_before: int) -> bool:
    """Присылал ли покупатель данные, пока ждал этот запрос согласия."""
    try:
        return await _claimed(f"consent:{peer_id}:{answers_before}", _HIDDEN_EVENT)
    except Exception:
        return False


async def answers(peer_id: int) -> int:
    try:
        return await _count(peer_id, PD)
    except Exception:
        return 0


# --- ответы словами --------------------------------------------------------------

def _plain(text: str) -> str:
    return " ".join((text or "").casefold().replace("ё", "е").strip(" .!,)(👍🙏").split())


_YES = {"согласен", "согласна", "согласны", "я согласен", "я согласна", "да согласен", "да, согласен",
        "да согласна", "да, согласна", "даю согласие", "согласен на обработку", "согласна на обработку"}
_NO = {"не согласен", "не согласна", "не согласны", "я не согласен", "я не согласна", "не даю согласие",
       "нет не согласен", "нет, не согласен", "нет не согласна", "нет, не согласна"}
_REVOKE = ("отзываю согласие", "отозвать согласие", "отзываю свое согласие", "отзыв согласия",
           "удалите мои данные", "удалите мои персональные данные", "удалить мои данные",
           "удалите мои личные данные", "удалите мои пд", "удалить мои персональные данные")
_UNSUBSCRIBE = {"отписаться", "отписаться от рассылки", "отпишите от рассылки", "отписаться от новостей",
                "не хочу рассылку", "отказаться от рассылки"}
_MY_DATA = {"мои данные", "мои персональные данные", "какие данные вы храните", "какие у вас мои данные"}
_MK_YES = {"да", "да хочу", "да, хочу", "хочу", "давайте", "да давайте", "да, давайте", "подписаться"}
_MK_NO = {"нет", "не надо", "нет спасибо", "нет, спасибо", "не хочу", "не нужно"}


def words_of(text: str) -> str | None:
    """Что покупатель сказал словами про согласия — действие или None."""
    plain = _plain(text)
    if not plain or len(plain) > 120:
        return None
    if plain in _NO:
        return "pd_no"
    if plain in _YES:
        return "pd_yes"
    if any(phrase in plain for phrase in _REVOKE):
        return "pd_revoke"
    if plain in _UNSUBSCRIBE:
        return "mk_off"
    if plain in _MY_DATA:
        return "my_data"
    return None


def marketing_word(text: str) -> str | None:
    plain = _plain(text)
    if plain in _MK_YES:
        return "mk_yes"
    if plain in _MK_NO:
        return "mk_no"
    return None
