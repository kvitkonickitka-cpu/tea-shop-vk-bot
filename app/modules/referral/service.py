"""Реферальная программа (пакет D, флаг referral).

Клиент с оплаченным и вручённым заказом без возврата получает персональный
код `ЧАЙ-XXXX-XXXX` (алфавит без 0/O и 1/I/L, `secrets`, 31⁸ ≈ 2³⁹·⁶) и ссылку
`vk.me/<сообщество>?ref=r_XXXXXXXX`. Друг по коду получает скидку на первый
заказ (`REFERRAL_DISCOUNT_PERCENT`, только товары, потолок и минимальная
сумма — настройки). Скидку считает и применяет только код: модель вызывает
инструмент `apply_referral_code`, по ссылке код применяется сам.

Проверки:
- при вводе: друг — не владелец, у друга нет оплаченных заказов, он ещё не
  получал скидку; лимиты кода (в месяц и всего); не больше 5 неверных кодов
  в сутки — дальше ввод закрыт до завтра; каждая попытка — в журнал;
- при счёте (`check_before_invoice`): данные получателя — телефон, почта,
  ФИО + пункт — по хешам не совпадают с владельцем и с уже оплаченными
  заказами и прошлыми скидками. Не прошло — скидка снимается, клиент видит
  новый итог до ссылки.

Жизненный цикл: счёт — `reserved`, оплата — `applied` (`referral_redeemed`),
истёк или отменён счёт — `voided`, возврат — `voided`. Сигналы
злоупотребления (тот же пункт или похожее ФИО у разных друзей одного кода,
всплеск применений за сутки) — ⚠️ менеджеру, без блокировки. Награда
пригласившему (`REFERRER_REWARD`, по умолчанию off) — только после
вручения заказа друга и срока возврата без возврата.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.core import flags, worktime
from app.core.client_key import client_key
from app.core.config import settings
from app.core.database import get_session_factory
from app.messages import funnel
from app.modules.referral.models import ReferralAttempt, ReferralCode, ReferralRedemption, ReferralReward

logger = logging.getLogger(__name__)

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PREFIX = "ЧАЙ"
WRONG_PER_DAY = 5
# Похожие кириллические буквы — в латиницу: клиент перепечатывает код руками.
_LOOKALIKE = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
_CODE_IN_TEXT = re.compile(r"(?:ЧАЙ|CHAI|ЧАИ)?[\s\-_]*([0-9A-ZА-Я]{4})[\s\-_]*([0-9A-ZА-Я]{4})", re.IGNORECASE)


def new_code() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{PREFIX}-{body[:4]}-{body[4:]}"


def normalize(raw: str) -> str | None:
    """`чай-abcd efgh`, `r_ABCDEFGH`, `ABCD-EFGH` → `ЧАЙ-ABCD-EFGH`. Не похоже на код — None."""
    value = (raw or "").strip().upper()
    if value.startswith("R_"):
        value = value[2:]
    value = value.replace("ЧАЙ", "").replace("ЧАИ", "").replace("CHAI", "")
    value = re.sub(r"[\s\-_]", "", value).translate(_LOOKALIKE)
    if len(value) != 8 or any(ch not in ALPHABET for ch in value):
        return None
    return f"{PREFIX}-{value[:4]}-{value[4:]}"


def link_ref(code: str) -> str:
    return "r_" + code.replace(f"{PREFIX}-", "").replace("-", "")


def link(code: str) -> str:
    group = "".join(ch for ch in (settings.vk_group_id or "") if ch.isdigit())
    return f"https://vk.me/club{group}?ref={link_ref(code)}"


def _hash(kind: str, value: str | None) -> str | None:
    value = (value or "").strip().casefold()
    if not value:
        return None
    secret = (settings.client_key_secret or "referral").encode()
    return hmac.new(secret, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:40]


def _phone(value: str | None) -> str | None:
    from app.modules.orders import contacts

    return contacts.normalize_phone(value or "") or None


def identity(details: dict) -> dict:
    """Хеши получателя: телефон, почта, ФИО + пункт выдачи."""
    point = str(details.get("ozon_point_id") or details.get("delivery_point") or "")
    name = " ".join(sorted((details.get("recipient_name") or "").casefold().split()))
    return {
        "phone_hash": _hash("phone", _phone(details.get("recipient_phone"))),
        "email_hash": _hash("email", details.get("recipient_email")),
        "name_point_hash": _hash("name_point", f"{name}|{point}") if name and point else None,
        "name_hash": _hash("name", name) if name else None,
        "point_id": point or None,
    }


async def enabled(peer_id: int) -> bool:
    return await flags.enabled_for(flags.REFERRAL, peer_id)


def terms() -> dict:
    return {"percent": settings.referral_discount_percent, "cap": settings.referral_discount_cap,
            "min_total": settings.referral_min_items_total}


# --- выдача кода -------------------------------------------------------------------


async def _eligible(peer_id: int) -> bool:
    """Оплаченный и вручённый заказ без возврата. Тестовым аккаунтам — без заказа (проверка в бою)."""
    from app.modules.analytics import service as analytics

    if peer_id in await analytics.test_peer_ids():
        return True
    async with get_session_factory()() as session:
        found = (await session.execute(text(
            "select 1 from orders where peer_id = :p and payment_status = 'succeeded' and delivered_at is not null"
            " and status not in ('refunded', 'canceled') limit 1"), {"p": peer_id})).first()
    return found is not None


async def code_for(peer_id: int) -> ReferralCode | None:
    """Активный код клиента; нет — выдать, если положено. Не положено — None."""
    async with get_session_factory()() as session:
        existing = (await session.execute(select(ReferralCode).where(
            ReferralCode.owner_peer_id == peer_id, ReferralCode.status == "active"))).scalar_one_or_none()
    if existing is not None:
        return existing
    if not await _eligible(peer_id):
        return None
    for _ in range(5):
        row = ReferralCode(code=new_code(), owner_peer_id=peer_id, owner_client_key=client_key(peer_id),
                           status="active", limit_month=settings.referral_limit_month,
                           limit_total=settings.referral_limit_total)
        try:
            async with get_session_factory()() as session:
                session.add(row)
                await session.commit()
        except Exception:
            logger.warning("Код совпал или клиент уже получил код — пробуем ещё", exc_info=True)
            existing = await _active_code(peer_id)
            if existing is not None:
                return existing
            continue
        await funnel.record(peer_id, "referral_code_issued", source_=funnel.CODE)
        return row
    return None


async def _active_code(peer_id: int) -> ReferralCode | None:
    async with get_session_factory()() as session:
        return (await session.execute(select(ReferralCode).where(
            ReferralCode.owner_peer_id == peer_id, ReferralCode.status == "active"))).scalar_one_or_none()


async def invite_text(peer_id: int) -> str:
    from app.messages import templates

    if not await enabled(peer_id):
        return templates.referral_unavailable()
    row = await code_for(peer_id)
    if row is None:
        return templates.referral_not_yet()
    return templates.referral_invite(row.code, link(row.code), percent=settings.referral_discount_percent)


# --- применение ----------------------------------------------------------------------


async def _attempt(peer_id: int, code: str, ok: bool, reason: str | None, via: str) -> None:
    async with get_session_factory()() as session:
        session.add(ReferralAttempt(peer_id=peer_id, code=code[:40], ok=ok, reason=reason, via=via))
        await session.commit()


async def _wrong_today(peer_id: int) -> int:
    start = datetime.combine(worktime.to_msk(datetime.now(timezone.utc)).date(), datetime.min.time(), worktime.MSK)
    async with get_session_factory()() as session:
        return int((await session.execute(select(func.count()).select_from(ReferralAttempt).where(
            ReferralAttempt.peer_id == peer_id, ReferralAttempt.ok.is_(False),
            ReferralAttempt.reason == "not_found", ReferralAttempt.at >= start))).scalar() or 0)


async def _has_paid_order(peer_id: int) -> bool:
    async with get_session_factory()() as session:
        return (await session.execute(text(
            "select 1 from orders where peer_id = :p and payment_status = 'succeeded' limit 1"), {"p": peer_id})).first() is not None


async def _used_before(peer_id: int) -> bool:
    async with get_session_factory()() as session:
        return (await session.execute(select(ReferralRedemption.id).where(
            ReferralRedemption.friend_peer_id == peer_id,
            ReferralRedemption.status.in_(("reserved", "applied"))).limit(1))).first() is not None


async def _limits_hit(code: ReferralCode) -> str | None:
    month_start = worktime.to_msk(datetime.now(timezone.utc)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with get_session_factory()() as session:
        total = int((await session.execute(select(func.count()).select_from(ReferralRedemption).where(
            ReferralRedemption.code_id == code.id, ReferralRedemption.status == "applied"))).scalar() or 0)
        month = int((await session.execute(select(func.count()).select_from(ReferralRedemption).where(
            ReferralRedemption.code_id == code.id, ReferralRedemption.status == "applied",
            ReferralRedemption.updated_at >= month_start))).scalar() or 0)
    if total >= code.limit_total:
        return "limit_total"
    if month >= code.limit_month:
        return "limit_month"
    return None


REJECT_TEXT = {
    "blocked_today": "Слишком много неверных кодов за сегодня — попробуйте завтра.",
    "not_found": "Такого кода нет — проверьте, пожалуйста, написание (вида ЧАЙ-XXXX-XXXX).",
    "own_code": "Это ваш собственный код — им делятся с друзьями, себе он скидку не даёт.",
    "not_first_order": "Скидка по коду друга — только на первый заказ, а у вас уже есть оплаченные.",
    "already_used": "Скидку по коду друга вы уже получали — она одна на человека.",
    "limit_month": "Лимит этого кода на месяц исчерпан — попросите друга поделиться им позже.",
    "limit_total": "Лимит этого кода исчерпан.",
    "same_person": "Код друга не применится к заказу на того же получателя, что у владельца кода.",
    "known_buyer": "Скидка — только новым покупателям, а по этим данным заказы уже были.",
    "disabled": "Код сейчас не принимается.",
}


async def apply(peer_id: int, raw_code: str, draft=None, *, via: str = "text") -> tuple[bool, str]:
    """Применить код к черновику (или запомнить до черновика). (успех, текст для модели/клиента)."""
    from app.messages import templates

    if not await enabled(peer_id):
        return False, REJECT_TEXT["disabled"]
    if await _wrong_today(peer_id) >= WRONG_PER_DAY:
        await _attempt(peer_id, raw_code, False, "blocked_today", via)
        return False, REJECT_TEXT["blocked_today"]
    code_text = normalize(raw_code)
    async with get_session_factory()() as session:
        code = (await session.execute(select(ReferralCode).where(
            ReferralCode.code == code_text, ReferralCode.status == "active"))).scalar_one_or_none() if code_text else None
    reason = None
    if code is None:
        reason = "not_found"
    elif code.owner_peer_id == peer_id:
        reason = "own_code"
    elif await _has_paid_order(peer_id):
        reason = "not_first_order"
    elif await _used_before(peer_id):
        reason = "already_used"
    else:
        reason = await _limits_hit(code)
    if reason:
        await _attempt(peer_id, code_text or raw_code, False, reason, via)
        await funnel.record(peer_id, "referral_rejected", source_=funnel.CODE, reason=reason)
        if reason in ("limit_month", "limit_total"):
            await _ops(f"⚠️ <b>Реферальный код #{code.id}: лимит ({'месяц' if reason == 'limit_month' else 'всего'})</b>\n"
                       "Друг пытался применить код сверх лимита — отказано.")
        return False, REJECT_TEXT[reason]
    await _attempt(peer_id, code.code, True, None, via)
    if draft is None:
        from app.modules.orders import state

        draft = await state.get_draft(peer_id)
    if draft is None:
        # Черновика ещё нет — код применится, как только он появится (propose_order).
        await _remember_pending(peer_id, code.code)
        return True, templates.referral_pending(settings.referral_discount_percent)
    await attach(peer_id, draft, code)
    return True, templates.referral_applied(draft.details["referral"])


async def _remember_pending(peer_id: int, code: str) -> None:
    from app.messages.models import ClientPreference

    async with get_session_factory()() as session:
        row = await session.get(ClientPreference, peer_id)
        if row is None:
            row = ClientPreference(peer_id=peer_id)
            session.add(row)
        info = dict(row.client_info or {})
        info["referral_pending"] = code
        row.client_info = info
        await session.commit()


async def pending_code(peer_id: int) -> str | None:
    """Код, введённый до черновика, или пришедший по ссылке друга в этой сессии."""
    from app.messages.models import ClientPreference
    from app.modules.dialog import sessions

    async with get_session_factory()() as session:
        row = await session.get(ClientPreference, peer_id)
    code = ((row.client_info or {}) if row else {}).get("referral_pending")
    if code:
        return code
    session_id = await sessions.current_id(peer_id)
    current = await sessions.get(session_id) if session_id else None
    if current is not None and (current.source or "").startswith("referral:"):
        return current.ref or None
    return None


async def attach(peer_id: int, draft, code: ReferralCode) -> None:
    from app.modules.orders import state

    draft.details["referral"] = {"code_id": code.id, "code": code.code, **terms()}
    await state.set_draft(peer_id, draft)  # пересчёт цен — в set_draft
    await funnel.record(peer_id, "referral_code_applied", source_=funnel.CODE, code_id=code.id,
                        discount=draft.details["referral"].get("discount"))


async def auto_apply(peer_id: int, draft) -> str:
    """При создании черновика: код по ссылке или введённый раньше — применить сам. Строка для модели."""
    if draft.details.get("referral") or not await enabled(peer_id):
        return ""
    raw = await pending_code(peer_id)
    if not raw:
        return await _apply_reward(peer_id, draft)
    ok, said = await apply(peer_id, raw, draft, via="link")
    if ok:
        from app.messages.models import ClientPreference

        async with get_session_factory()() as session:
            row = await session.get(ClientPreference, peer_id)
            if row is not None and (row.client_info or {}).get("referral_pending"):
                row.client_info = {k: v for k, v in row.client_info.items() if k != "referral_pending"}
                await session.commit()
    return f"\n{said}"


async def _apply_reward(peer_id: int, draft) -> str:
    """Награда пригласившему — скидка на его следующий заказ, сама (не суммируется с кодом)."""
    async with get_session_factory()() as session:
        reward = (await session.execute(select(ReferralReward).where(
            ReferralReward.owner_peer_id == peer_id, ReferralReward.status == "granted",
            ReferralReward.used_order_id.is_(None)).order_by(ReferralReward.id).limit(1))).scalar_one_or_none()
    if reward is None:
        return ""
    from app.messages import templates
    from app.modules.orders import state

    draft.details["referral"] = {"kind": "reward", "reward_id": reward.id, "percent": reward.percent,
                                 "cap": settings.referral_discount_cap, "min_total": 0}
    await state.set_draft(peer_id, draft)
    return "\n" + templates.referral_applied(draft.details["referral"])


# --- счёт, оплата, возврат -----------------------------------------------------------------


async def check_before_invoice(peer_id: int, draft) -> str | None:
    """Перед счётом: данные получателя не совпадают с владельцем и прежними покупателями.

    Не прошло — скидка снимается (цены снова прайсовые), возвращается текст для модели.
    """
    terms_ = draft.details.get("referral")
    if not terms_ or not terms_.get("active") or terms_.get("kind") == "reward":
        return None
    me = identity(draft.details)
    reason = await _identity_clash(peer_id, terms_["code_id"], me)
    if reason is None:
        return None
    from app.modules.orders import state

    draft.details.pop("referral", None)
    await state.set_draft(peer_id, draft)
    await funnel.record(peer_id, "referral_rejected", source_=funnel.CODE, reason=reason)
    return f"Скидка по коду друга снята: {REJECT_TEXT[reason]}"


async def _identity_clash(peer_id: int, code_id: int, me: dict) -> str | None:
    hashes = {k: me[k] for k in ("phone_hash", "email_hash") if me.get(k)}
    async with get_session_factory()() as session:
        owner = (await session.execute(select(ReferralCode.owner_peer_id).where(ReferralCode.id == code_id))).scalar()
        orders = (await session.execute(text(
            "select peer_id, details from orders where payment_status = 'succeeded'"))).all()
        used = (await session.execute(select(ReferralRedemption.phone_hash, ReferralRedemption.email_hash).where(
            ReferralRedemption.status.in_(("reserved", "applied")),
            ReferralRedemption.friend_peer_id != peer_id))).all()
    for other_peer, details in orders:
        theirs = identity(details or {})
        same_contact = any(theirs.get(k) and theirs.get(k) == v for k, v in hashes.items())
        if other_peer == owner and (same_contact or (me.get("name_point_hash")
                                                     and theirs.get("name_point_hash") == me["name_point_hash"])):
            return "same_person"
        if same_contact:
            return "known_buyer"
    for phone_hash, email_hash in used:
        if (hashes.get("phone_hash") and phone_hash == hashes["phone_hash"]) or \
                (hashes.get("email_hash") and email_hash == hashes["email_hash"]):
            return "already_used"
    return None


async def reserve(peer_id: int, draft, order_id: int) -> None:
    """Счёт выставлен со скидкой — резерв применения (одна строка на заказ)."""
    terms_ = draft.details.get("referral")
    if not terms_ or not terms_.get("active"):
        return
    if terms_.get("kind") == "reward":
        async with get_session_factory()() as session:
            await session.execute(text("update referral_rewards set used_order_id = :o where id = :r and status = 'granted'"),
                                  {"o": order_id, "r": terms_["reward_id"]})
            await session.commit()
        return
    me = identity(draft.details)
    async with get_session_factory()() as session:
        await session.execute(text(
            "update referral_redemptions set status = 'voided', reason = 'reinvoiced', updated_at = now()"
            " where order_id = :o and status = 'reserved'"), {"o": order_id})
        session.add(ReferralRedemption(
            code_id=terms_["code_id"], friend_peer_id=peer_id, friend_client_key=client_key(peer_id),
            order_id=order_id, status="reserved", discount=terms_.get("discount"),
            phone_hash=me["phone_hash"], email_hash=me["email_hash"], name_point_hash=me["name_point_hash"],
            point_id=me["point_id"]))
        await session.commit()
    await _abuse_signals(terms_["code_id"], me)


async def _set_status(order_id: int, old: tuple, new: str, reason: str | None = None) -> list:
    async with get_session_factory()() as session:
        rows = (await session.execute(select(ReferralRedemption).where(
            ReferralRedemption.order_id == order_id, ReferralRedemption.status.in_(old)))).scalars().all()
        for row in rows:
            row.status, row.reason, row.updated_at = new, reason or row.reason, datetime.now(timezone.utc)
        await session.commit()
        return [(r.id, r.friend_peer_id, r.code_id) for r in rows]


async def on_event(peer_id: int, event: str, order_id: int | None) -> None:
    """Из журнала воронки: оплата — применено, счёт закрыт — снят резерв, возврат — аннулировано."""
    if order_id is None:
        return
    try:
        if event == "payment_succeeded":
            async with get_session_factory()() as session:
                await session.execute(text("update referral_rewards set status = 'used' where used_order_id = :o"),
                                      {"o": order_id})
                await session.commit()
            for _, friend, code_id in await _set_status(order_id, ("reserved",), "applied"):
                await funnel.record(friend, "referral_redeemed", order_id=order_id, source_=funnel.YOOKASSA,
                                    code_id=code_id)
        elif event in ("invoice_expired", "invoice_canceled", "canceled_by_client", "payment_declined"):
            await _set_status(order_id, ("reserved",), "voided", event)
            async with get_session_factory()() as session:
                await session.execute(text("update referral_rewards set used_order_id = null"
                                           " where used_order_id = :o and status = 'granted'"), {"o": order_id})
                await session.commit()
        elif event == "refunded":
            await _set_status(order_id, ("applied", "reserved"), "voided", "refund")
    except Exception:
        logger.exception("Рефералка: не обработали %s по заказу %s", event, order_id)


async def _abuse_signals(code_id: int, me: dict) -> None:
    """⚠️ менеджеру, без блокировки: тот же пункт или похожее ФИО у разных друзей, всплеск за сутки."""
    from app.messages import client as client_messages

    async with get_session_factory()() as session:
        rows = (await session.execute(select(ReferralRedemption).where(
            ReferralRedemption.code_id == code_id,
            ReferralRedemption.status.in_(("reserved", "applied"))))).scalars().all()
    friends = {r.friend_peer_id for r in rows}
    same_point = me.get("point_id") and len({r.friend_peer_id for r in rows if r.point_id == me["point_id"]}) > 1
    day = [r for r in rows if r.created_at and r.created_at > datetime.now(timezone.utc) - timedelta(days=1)]
    spike = len({r.friend_peer_id for r in day}) > settings.referral_spike_per_day
    for signal, on in (("same_point", same_point), ("spike", spike)):
        if not on or len(friends) < 2:
            continue
        key = f"referral_abuse:{code_id}:{signal}:{worktime.to_msk(datetime.now(timezone.utc)).date()}"
        if await client_messages.claim_once(key, "referral_abuse", 0):
            what = "у разных друзей один пункт выдачи" if signal == "same_point" else "много применений за сутки"
            await _ops(f"⚠️ <b>Реферальный код #{code_id}: {what}</b>\nПрименений: {len(rows)}, друзей: {len(friends)}. "
                       "Не блокируем — посмотрите заказы.", manager=True)


async def _ops(text_: str, manager: bool = False) -> None:
    from app.messages import manager as manager_messages

    await manager_messages.notify(manager_messages.REFERRAL, text_,
                                  chat_id=None if manager else (settings.telegram_ops_chat_id or None))


# --- награда пригласившему ------------------------------------------------------------------


def reward_percent() -> int | None:
    raw = (settings.referrer_reward or "off").strip().casefold()
    if raw.startswith("percent:"):
        try:
            return max(1, min(50, int(raw.split(":", 1)[1])))
        except ValueError:
            return None
    return None


async def grant_rewards(now: datetime | None = None) -> dict:
    """Тик: заказ друга вручён и срок возврата прошёл без возврата — награда пригласившему."""
    percent = reward_percent()
    if percent is None:
        return {"skipped": "награда выключена"}
    now = now or datetime.now(timezone.utc)
    if worktime.is_quiet(now):
        return {"skipped": "тихие часы"}
    cutoff = now - timedelta(days=settings.referral_return_days)
    async with get_session_factory()() as session:
        rows = (await session.execute(text(
            "select r.id, c.owner_peer_id from referral_redemptions r join referral_codes c on c.id = r.code_id"
            " join orders o on o.id = r.order_id where r.status = 'applied' and o.delivered_at < :cut"
            " and o.status <> 'refunded' and not exists (select 1 from referral_rewards w where w.redemption_id = r.id)"),
            {"cut": cutoff})).all()
        for redemption_id, owner in rows:
            session.add(ReferralReward(redemption_id=redemption_id, owner_peer_id=owner, percent=percent))
        await session.commit()
    from app.messages import client as client_messages, templates

    for redemption_id, owner in rows:
        await funnel.record(owner, "referral_reward_granted", source_=funnel.CODE, percent=percent)
        await client_messages.send(peer_id=owner, ref=f"referral_reward:{redemption_id}", event_type="referral_reward",
                                   text=templates.referral_reward(percent))
    return {"наград": len(rows)}
