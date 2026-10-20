"""Что бот делает по ответам о согласиях — кнопками и словами.

Ответы решает код, модель их не видит: «Согласен» — юридически значимое
действие, и прочитать его как-то иначе модель не должна. Каждое нажатие —
новая строка журнала, даже повторное.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.messages import keyboard as keyboards
from app.messages import templates
from app.modules.consent import service as consent

logger = logging.getLogger(__name__)

# Кнопки и шаги, где бот просит или записывает личное: без согласия — запрос согласия.
PD_ACTIONS = {"pt", "ship", "email_yes", "email_no", "new_link", "checkout", "offer_ok", "repeat",
              "last_recipient", "geo"}
# Корзина без личного: можно и до согласия, но вместо «куда везти?» — запрос согласия.
CART_ACTIONS = {"take", "add", "add_item", "add_more"}
# Вопросы, которые без согласия задавать нельзя: снимаем их из готовых ответов.
_PD_ASKS = (templates.ASK_WHERE, templates.ASK_WHERE_GEO, templates.ASK_POINT_AND_RECIPIENT,
            templates.ASK_POINT, templates.ASK_RECIPIENT)


def _press(**kwargs):
    from app.modules.orders.buttons import Press

    return Press(**kwargs)


async def ask_press(peer_id: int):
    """Запрос согласия ответом на нажатие или слова."""
    text, keyboard = await consent.pd_prompt(peer_id)
    await consent.claim_pd_prompt(peer_id)
    return _press(reply=text, keyboard=keyboard)


async def with_prompt(peer_id: int, press):
    """Ответ про корзину — без вопроса «куда везти», с запросом согласия."""
    if press is None or not press.reply or press.to_model:
        return press
    reply = press.reply
    for ask in _PD_ASKS:
        reply = reply.replace(ask, "")
    reply = "\n".join(line for line in reply.split("\n") if line.strip())
    text, keyboard = await consent.pd_prompt(peer_id)
    await consent.claim_pd_prompt(peer_id)
    return _press(reply=f"{reply}\n\n{text}", keyboard=keyboard)


async def _next_step(peer_id: int) -> tuple[list[str], dict | None]:
    """С того же шага, где остановились: что нужно по черновику дальше."""
    from app.modules.orders import buttons, state

    draft = await state.get_draft(peer_id)
    if draft is None:
        return [templates.pd_consent_next()], None
    keyboard, _ = await buttons.for_reply(peer_id)
    step = buttons._missing(draft)
    return ([step] if step else []), keyboard


async def on_pd_yes(peer_id: int, payload: dict):
    from app.modules.dialog import history as dialog_history
    from app.modules.orders import service as orders_service

    before = await consent.answers(peer_id)
    had = await consent.has_pd(peer_id)
    await consent.record(peer_id, consent.PD, True, method=payload.get("via") or "button")
    hidden = await consent.had_hidden(peer_id, before)
    if not had and await orders_service.resume_after_consent(peer_id):
        # Заказ из «Товаров» ждал согласия — теперь он прочитал адрес и
        # получателя из ВК и сам написал расчёт доставки.
        await dialog_history.append_message(peer_id, "user", templates.PD_YES_LABEL)
        return _press()
    lines = [templates.pd_consent_thanks()]
    if hidden:
        lines.append(templates.pd_resend())
    steps, keyboard = await _next_step(peer_id)
    return _press(reply="\n".join(lines + steps), keyboard=keyboard)


async def on_pd_no(peer_id: int, payload: dict):
    await consent.record(peer_id, consent.PD, False, method=payload.get("via") or "button")
    keyboard = consent.retry_keyboard()
    return _press(reply=templates.pd_consent_declined(), keyboard=keyboard)


async def on_pd_retry(peer_id: int, payload: dict):
    if await consent.has_pd(peer_id):
        steps, keyboard = await _next_step(peer_id)
        return _press(reply="\n".join(steps), keyboard=keyboard)
    return await ask_press(peer_id)


def _my_data_keyboard(marketing_active: bool) -> dict | None:
    row = [keyboards.text_button(templates.REVOKE_LABEL, {"a": "pd_revoke"}, color="negative")]
    if marketing_active:
        row.append(keyboards.text_button(templates.UNSUBSCRIBE_LABEL, {"a": "mk_off"}))
    return keyboards.inline([row])


def _my_data_button() -> dict | None:
    return keyboards.inline([[keyboards.text_button(templates.MY_DATA_LABEL, {"a": "my_data"})]])


async def _opt_in(peer_id: int) -> None:
    """Согласие на рассылку снимает прежнее «стоп»: решение клиента — последнее."""
    from sqlalchemy import update

    from app.core.database import get_session_factory
    from app.messages.models import ClientPreference

    try:
        async with get_session_factory()() as session:
            await session.execute(
                update(ClientPreference).where(ClientPreference.peer_id == peer_id).values(marketing_opt_out=False)
            )
            await session.commit()
    except Exception:
        logger.exception("Не сняли отписку peer_id=%s", peer_id)


async def on_mk_yes(peer_id: int, payload: dict):
    await consent.record(peer_id, consent.MARKETING, True, method=payload.get("via") or "button")
    await _opt_in(peer_id)
    return _press(reply=f"{templates.marketing_consent_yes()}\n{templates.MY_DATA_HINT}", keyboard=_my_data_button())


async def on_mk_no(peer_id: int, payload: dict):
    await consent.record(peer_id, consent.MARKETING, False, method=payload.get("via") or "button")
    return _press(reply=f"{templates.marketing_consent_no()}\n{templates.MY_DATA_HINT}", keyboard=_my_data_button())


async def on_mk_off(peer_id: int, payload: dict):
    from app.messages import marketing

    await consent.revoke(peer_id, (consent.MARKETING,), method=payload.get("via") or "unsubscribe")
    await marketing.opt_out(peer_id, record_consent=False)
    return _press(reply=templates.marketing_unsubscribed())


async def on_my_data(peer_id: int, payload: dict):
    pd_active = await consent.has_pd(peer_id)
    marketing_active = await consent.marketing_allowed(peer_id)
    return _press(
        reply=templates.my_data(pd_active=pd_active, marketing_active=marketing_active, url=settings.pd_consent_url),
        keyboard=_my_data_keyboard(marketing_active),
    )


async def on_revoke(peer_id: int, payload: dict):
    """Отзыв согласия: строки в журнал, черновик долой, менеджеру — задача на удаление.

    Заказы, чеки и коды маркировки не трогаем: их срок хранения задаёт закон
    (бухучёт, 54-ФЗ). Обезличивание — командой менеджера, после проверки.
    """
    from app.messages import client as client_messages
    from app.messages import manager as manager_messages
    from app.messages import marketing
    from app.modules.dialog import vk_client
    from app.modules.orders import state

    await consent.revoke(peer_id, (consent.PD, consent.MARKETING), method=payload.get("via") or "button")
    await marketing.opt_out(peer_id, record_consent=False)
    try:
        await state.clear_draft(peer_id)
    except Exception:
        logger.exception("Отзыв согласия: не убрали черновик peer_id=%s", peer_id)
    today = datetime.now(timezone.utc).date()
    due = consent.working_days_after(today, settings.pd_delete_working_days)
    # Повторный отзыв в тот же день — та же задача, второй карточки не нужно.
    if await client_messages.claim_once(f"pd_delete:{peer_id}:{today.isoformat()}", "pd_delete_task", peer_id):
        await manager_messages.notify(
            manager_messages.PD_DELETE,
            templates.manager_pd_delete(peer_id, due, vk_client.dialog_link(peer_id)),
            peer_id=peer_id,
        )
    logger.info("peer_id=%s отозвал согласие, удалить до %s", peer_id, due)
    return _press(reply=templates.pd_revoked())


HANDLERS = {
    "pd_yes": on_pd_yes,
    "pd_no": on_pd_no,
    "pd_retry": on_pd_retry,
    "mk_yes": on_mk_yes,
    "mk_no": on_mk_no,
    "mk_off": on_mk_off,
    "pd_revoke": on_revoke,
    "my_data": on_my_data,
}


async def intercept(peer_id: int, text: str):
    """Слова про согласия — код, а не модель. None — это обычная реплика.

    «Согласен» и «Не согласен» засчитываются, только пока висит запрос
    согласия; «да» и «нет» — только в ответ на вопрос о рассылке. Отзыв,
    «отписаться» и «мои данные» — всегда.
    """
    action = consent.words_of(text)
    if action in ("pd_yes", "pd_no") and not await consent.pd_prompt_pending(peer_id):
        action = None
    if action is None and consent.marketing_word(text) and await consent.marketing_question_pending(peer_id):
        action = consent.marketing_word(text)
    if action is None:
        return None
    logger.info("peer_id=%s: согласие словами — %s", peer_id, action)
    return await HANDLERS[action](peer_id, {"a": action, "via": "text"})
