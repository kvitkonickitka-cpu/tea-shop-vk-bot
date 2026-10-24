"""Сборка заказа: сканирование кодов маркировки на каждую пачку.

Сборщик открывает страницу по ссылке из карточки оплаченного заказа и
сканирует камерой телефона DataMatrix на каждой пачке. Каждый скан
проверяется здесь, а не на странице: страница только показывает ответ.

**Ссылка** подписана HMAC и привязана к одному заказу; живёт
`packing_link_ttl_hours` часов и перестаёт что-либо менять, как только
заказ собран. Одноразовой её не делали намеренно: мобильный браузер
перезагружает вкладку после разрешения камеры или после сворачивания, и
одноразовая ссылка выбрасывала бы сборщика посреди сборки. Украденная
ссылка позволяет только привязать коды к одному заказу до его сборки — это
видно на странице и в карточке, а новую ссылку даёт
`scripts/api.sh orders/<N>/pack-link`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.core import worktime
from app.messages import templates
from app.core.config import settings
from app.core.database import get_session_factory
from app.modules.catalog import service as catalog_service
from app.modules.marking import codes, pool
from app.modules.marking.models import (
    PACK, RETURNED, SCANNED, SET, SOLD, MarkingCodeRow, OrderMarkingCode,
)
from app.modules.orders.models import Order

logger = logging.getLogger(__name__)


class LinkError(ValueError):
    """Ссылка на сборку не годится. Текст — для сборщика."""


# --- ссылка -----------------------------------------------------------------


def _key() -> bytes:
    """Ключ подписи, выведенный из служебного токена.

    Отдельного секрета не заводим: служебный токен и так живёт в трёх
    местах, четвёртое забыли бы. Смена токена делает старые ссылки
    недействительными — и это правильно.
    """
    token = (settings.internal_api_token or "").strip()
    if not token:
        raise LinkError("Ссылки на сборку выключены: не задан INTERNAL_API_TOKEN.")
    return hmac.new(token.encode("utf-8"), b"packing-link", hashlib.sha256).digest()


def _sign(payload: str) -> str:
    digest = hmac.new(_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:18]).decode("ascii")


def make_token(order_id: int, now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(hours=settings.packing_link_ttl_hours)
    payload = f"{order_id}.{int(expires.timestamp())}"
    return f"{payload}.{_sign(payload)}", expires


def read_token(token: str, now: datetime | None = None) -> int:
    """Номер заказа из ссылки — или объяснение, почему ссылка не годится."""
    now = now or datetime.now(timezone.utc)
    try:
        order_part, expires_part, signature = (token or "").split(".")
        payload = f"{order_part}.{expires_part}"
        order_id, expires = int(order_part), int(expires_part)
    except ValueError:
        raise LinkError("Ссылка на сборку повреждена — возьмите её из карточки заказа заново.")
    if not hmac.compare_digest(signature, _sign(payload)):
        raise LinkError("Ссылка на сборку не подходит — возьмите её из карточки заказа заново.")
    if now.timestamp() > expires:
        raise LinkError(
            "Срок ссылки на сборку истёк. Новую даёт команда "
            f"scripts/api.sh orders/{order_id}/pack-link."
        )
    return order_id


def pack_url(order_id: int, base_url: str = "") -> tuple[str, datetime] | None:
    """Адрес страницы сборки. None — собрать ссылку нечем."""
    base = (base_url or settings.public_base_url or "").rstrip("/")
    if not base:
        return None
    try:
        token, expires = make_token(order_id)
    except LinkError:
        return None
    return f"{base}/pack/{token}", expires


def card_line(order: Order) -> str:
    """Строка со ссылкой для карточки оплаченного заказа. Пусто — не нужна."""
    if not catalog_service.marking_configured():
        return ""
    made = pack_url(order.id)
    if made is None:
        return ""
    url, expires = made
    return templates.pack_card_line(url, expires)


# --- состояние сборки -------------------------------------------------------

NO_GTIN = "Нет GTIN в каталоге"


@dataclass
class Position:
    index: int
    name: str
    quantity: int
    gtin: str
    kind: str = PACK
    codes: list[dict] = field(default_factory=list)

    @property
    def done(self) -> bool:
        # У сета код один на коробку: позиция «2 сета» — два кода коробок,
        # пачки внутри не считаются.
        return bool(self.gtin) and len(self.codes) == self.quantity


@dataclass
class PackingState:
    order_id: int
    positions: list[Position]
    packed_at: datetime | None
    pool_imported: bool
    closed_reason: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.positions) and all(p.done for p in self.positions)

    delivered_without_codes: bool = False
    # Партии сырья по позициям (`stock.packing_view`); пусто — склад по партиям выключен.
    stock: list = field(default_factory=list)

    @property
    def can_finish(self) -> bool:
        return self.complete and self.packed_at is None and not self.closed_reason

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "packed_at": worktime.to_msk(self.packed_at).strftime("%d.%m %H:%M")
            if self.packed_at else None,
            "pool_imported": self.pool_imported,
            "closed_reason": self.closed_reason,
            "delivered_without_codes": self.delivered_without_codes,
            "can_finish": self.can_finish,
            "positions": [
                {
                    "index": p.index, "name": p.name, "quantity": p.quantity,
                    "gtin": p.gtin, "kind": p.kind, "scanned": len(p.codes), "done": p.done,
                    "codes": p.codes,
                    # Из какой партии брать (склад по партиям): по сырью позиции.
                    "lots": [row for row in self.stock if row["index"] == p.index],
                }
                for p in self.positions
            ],
        }


def _closed_reason(order: Order) -> str:
    """Почему заказ уже нельзя собирать, или пусто."""
    if order.status == "refunded":
        return "По заказу оформлен возврат — собирать его не нужно."
    if order.delivered_at is not None and order.settlement_receipt_id and \
            order.settlement_receipt_status in ("pending", "succeeded"):
        return "Заказ вручён, закрывающий чек с кодами уже отправлен."
    if order.not_delivered_at is not None:
        return "Заказ не вручён и возвращается — собирать его заново не нужно."
    return ""


async def _order(session, order_id: int) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        raise LinkError(f"Заказа №{order_id} нет в базе.")
    return order


async def _state(session, order: Order) -> PackingState:
    items = catalog_service.load_items()
    positions = [
        Position(
            index=i,
            name=str(item.get("name", "товар")),
            quantity=int(item.get("quantity", 1) or 1),
            gtin=catalog_service.gtin_for(str(item.get("name", "")), items),
            kind=catalog_service.kind_for(str(item.get("name", "")), items),
        )
        for i, item in enumerate(order.items or [])
    ]
    rows = (
        await session.execute(
            select(OrderMarkingCode)
            .where(OrderMarkingCode.order_id == order.id)
            .order_by(OrderMarkingCode.scanned_at, OrderMarkingCode.id)
        )
    ).scalars().all()
    by_index = {p.index: p for p in positions}
    for row in rows:
        position = by_index.get(row.item_index)
        if position is None:
            continue
        position.codes.append({
            "id": row.id,
            "serial": row.serial,
            "status": row.status,
            "from_pool": row.from_pool,
            "manual": row.manual,
            "by": row.scanned_by or "",
        })
    return PackingState(
        order_id=order.id,
        positions=positions,
        packed_at=order.packed_at,
        pool_imported=await pool.pool_imported(),
        closed_reason=_closed_reason(order),
        # Вручённый заказ без кодов собрать всё ещё можно: так исправляют
        # закрывающий чек, если сборку через страницу пропустили.
        delivered_without_codes=order.delivered_at is not None and order.packed_at is None,
        stock=await _stock_view(order.id),
    )


async def _stock_view(order_id: int) -> list:
    """Партии для страницы сборки. Сбой склада страницу не ломает — коды важнее."""
    from app.modules.stock import service as stock

    try:
        return await stock.packing_view(order_id)
    except Exception:
        logger.exception("Заказ %s: партии для сборки не прочитаны", order_id)
        return []


async def state(order_id: int) -> PackingState:
    session_factory = get_session_factory()
    async with session_factory() as session:
        return await _state(session, await _order(session, order_id))


# --- скан -------------------------------------------------------------------


@dataclass
class ScanResult:
    ok: bool
    message: str
    state: PackingState
    warning: str = ""


def _quoted(names: list[str]) -> str:
    return ", ".join(f"«{name}»" for name in dict.fromkeys(names))


def _wrong_item(current: PackingState, gtin: str) -> str:
    """«Это не та пачка» — что в заказе, что отсканировано и что делать."""
    waiting = [p for p in current.positions if not p.done] or current.positions
    in_order = _quoted([p.name for p in waiting])
    scanned_item = catalog_service.item_by_gtin(gtin)
    scanned = (
        f"«{scanned_item['name']}»" if scanned_item
        else f"GTIN {gtin} (такого товара в каталоге нет)"
    )
    message = f"Это не та пачка: в заказе {in_order}, отсканировано {scanned}."
    if any(p.kind == SET and not p.done for p in current.positions):
        message += " Пачки внутри сета не сканируются — нужен код на коробке."
    without_gtin = [p.name for p in current.positions if not p.gtin]
    if without_gtin:
        message += f" {NO_GTIN} у {_quoted(without_gtin)} — эту позицию с кодами не собрать."
    return message


def _full(position: Position) -> str:
    unit = "сет" if position.kind == SET else "пачк"
    if position.kind == SET:
        counted = f"{position.quantity} {unit}" + ("" if position.quantity == 1 else "а")
    else:
        counted = f"{position.quantity} {unit}" + ("а" if position.quantity == 1 else "и")
    return f"По «{position.name}» уже отсканированы все {counted} — лишняя в коробке?"


async def scan(order_id: int, raw: str, *, by: str = "", manual: bool = False) -> ScanResult:
    """Проверить код и закрепить за позицией заказа.

    Проверки идут в том порядке, в каком сборщику проще понять ответ:
    сначала сам код, потом «этот ли товар», потом «не занят ли код».
    """
    by = (by or "").strip()[:60] or "сборщик"
    session_factory = get_session_factory()
    async with session_factory() as session:
        order = await _order(session, order_id)
        current = await _state(session, order)

        def refuse(message: str) -> ScanResult:
            return ScanResult(False, message, current)

        if current.closed_reason:
            return refuse(current.closed_reason)
        if order.packed_at is not None:
            return refuse("Заказ уже собран — коды за ним закреплены.")

        try:
            parsed = codes.parse(raw)
        except codes.CodeError as error:
            return refuse(str(error))

        # Действующая строка с этой парой — не возвращённая: возвращённый по
        # чеку возврата код снова в обороте и годится для новой продажи.
        active = (
            await session.execute(
                select(OrderMarkingCode).where(
                    OrderMarkingCode.gtin == parsed.gtin,
                    OrderMarkingCode.serial == parsed.serial,
                    OrderMarkingCode.status != RETURNED,
                )
            )
        ).scalar_one_or_none()
        if active is not None and active.order_id == order.id:
            return refuse("Этот код уже отсканирован в этом заказе — пачку посчитали.")

        matching = [p for p in current.positions if p.gtin and p.gtin == parsed.gtin]
        if not matching:
            return refuse(_wrong_item(current, parsed.gtin))
        position = next((p for p in matching if len(p.codes) < p.quantity), None)
        if position is None:
            return refuse(_full(matching[0]))

        if active is not None:
            if active.status == SOLD:
                return refuse(
                    f"Этот код уже продан по заказу №{active.order_id} — "
                    "одну пачку нельзя продать дважды."
                )
            return refuse(f"Этот код уже в заказе №{active.order_id}.")

        in_pool = bool(await session.scalar(select(exists().where(
            MarkingCodeRow.gtin == parsed.gtin,
            MarkingCodeRow.serial == parsed.serial,
            MarkingCodeRow.from_pool.is_(True),
        ))))
        if current.pool_imported and not in_pool:
            return refuse(
                "Кода нет в пуле выпущенных кодов. Это чужая пачка, или код из "
                "выгрузки, которую ещё не загружали (scripts/api.sh codes/import)."
            )
        returned_from = await session.scalar(
            select(OrderMarkingCode.order_id).where(
                OrderMarkingCode.gtin == parsed.gtin,
                OrderMarkingCode.serial == parsed.serial,
                OrderMarkingCode.status == RETURNED,
            ).order_by(OrderMarkingCode.id.desc()).limit(1)
        )

        inserted = await session.execute(
            insert(OrderMarkingCode)
            .values(
                order_id=order.id, item_index=position.index, raw_code=parsed.code,
                gtin=parsed.gtin, serial=parsed.serial, kind=position.kind,
                status=SCANNED, from_pool=in_pool, manual=manual or parsed.restored,
                scanned_at=datetime.now(timezone.utc), scanned_by=by,
            )
            # Уникальный индекс по действующим парам — от гонки: два сборщика
            # с одной пачкой в двух заказах.
            .on_conflict_do_nothing()
            .returning(OrderMarkingCode.id)
        )
        if inserted.scalar_one_or_none() is None:
            return refuse("Этот код только что закрепили за другим заказом.")
        await session.commit()

        fresh = await _state(session, order)

    got = next(p for p in fresh.positions if p.index == position.index)
    message = f"✓ {got.name}: {len(got.codes)} из {got.quantity}"
    notes = []
    if not in_pool:
        notes.append("код не из пула")
    if parsed.restored:
        notes.append("разделитель GS восстановлен — сверьте код с пачкой")
    if returned_from:
        notes.append(f"код возвращался по заказу №{returned_from}")
    logger.info(
        "Сборка заказа %s: код %s…/%s закреплён за позицией %s (%s, %s)",
        order_id, parsed.gtin, parsed.serial[:4], position.index, position.kind, by,
    )
    return ScanResult(True, message, fresh, warning="; ".join(notes))


async def remove(order_id: int, code_id: int) -> PackingState:
    """Убрать ошибочно отсканированный код — пока заказ не собран."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        order = await _order(session, order_id)
        if order.packed_at is not None:
            raise LinkError("Заказ уже собран — коды за ним закреплены.")
        row = await session.get(OrderMarkingCode, code_id)
        if row is None or row.order_id != order.id or row.status != SCANNED:
            raise LinkError("Такого кода в этом заказе нет.")
        # Ошибочный скан забываем, будто его и не было: пачка вернулась на
        # полку, код из пула остаётся в пуле.
        await session.execute(delete(OrderMarkingCode).where(OrderMarkingCode.id == row.id))
        await session.commit()
        return await _state(session, order)


async def finish(order_id: int, *, by: str = "", weights: dict[int, float] | None = None) -> PackingState:
    """«Собрано»: коды есть на все единицы — закрепить их за заказом и списать сырьё.

    Сырьё списывается до отметки «собрано»: нет партии или срок пачки меньше
    допустимого — сборка не проходит, а сборщик видит, что сделать.
    """
    by = (by or "").strip()[:60] or "сборщик"
    session_factory = get_session_factory()
    async with session_factory() as session:
        order = await _order(session, order_id)
        current = await _state(session, order)
        if current.closed_reason:
            raise LinkError(current.closed_reason)
        if order.packed_at is not None:
            return current
        if not current.complete:
            missing = [
                f"{p.name}: {len(p.codes)} из {p.quantity}" + ("" if p.gtin else " (нет GTIN в каталоге)")
                for p in current.positions if not p.done
            ]
            raise LinkError("Коды есть не на все позиции — " + "; ".join(missing))
        from app.modules.stock import service as stock

        try:
            await stock.write_off(order_id, by=by, weights=weights)
        except stock.StockError as error:
            raise LinkError(str(error)) from None
        now = datetime.now(timezone.utc)
        await session.execute(
            update(Order).where(Order.id == order.id, Order.packed_at.is_(None))
            .values(packed_at=now, packed_by=by)
        )
        await session.commit()
        order.packed_at = now
        logger.info("Заказ %s собран (%s)", order_id, by)
        return await _state(session, order)


async def codes_of(order_id: int, statuses: tuple[str, ...] = (SCANNED, SOLD)) -> list[OrderMarkingCode]:
    """Коды заказа в порядке позиций — для закрывающего чека и чека возврата."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(OrderMarkingCode)
                    .where(OrderMarkingCode.order_id == order_id,
                           OrderMarkingCode.status.in_(statuses))
                    .order_by(OrderMarkingCode.item_index, OrderMarkingCode.id)
                )
            ).scalars().all()
        )


async def count_assigned(order_id: int) -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(OrderMarkingCode)
                .where(OrderMarkingCode.order_id == order_id,
                       OrderMarkingCode.status != RETURNED)
            ) or 0
        )


async def mark_sold(order_id: int) -> int:
    """Закрывающий чек зарегистрирован — коды заказа проданы."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            update(OrderMarkingCode)
            .where(OrderMarkingCode.order_id == order_id, OrderMarkingCode.status == SCANNED)
            .values(status=SOLD, sold_at=datetime.now(timezone.utc))
        )
        await session.commit()
    return int(result.rowcount or 0)


async def mark_returned(order_id: int, refund_id: str) -> int:
    """Коды ушли в чек возврата — снова в обороте.

    Условный UPDATE: и ответ на создание возврата, и уведомление
    `refund.succeeded` зовут эту функцию, а отметка ставится один раз.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            update(OrderMarkingCode)
            .where(OrderMarkingCode.order_id == order_id, OrderMarkingCode.status == SOLD)
            .values(status=RETURNED, returned_at=datetime.now(timezone.utc), refund_id=refund_id)
        )
        await session.commit()
    if result.rowcount:
        logger.info("Заказ %s: коды маркировки возвращены по чеку возврата %s — %s шт.",
                    order_id, refund_id, result.rowcount)
    return int(result.rowcount or 0)


async def release_codes(order_id: int) -> int:
    """Освободить коды заказа: посылку не вручили или деньги вернули до вручения.

    Только отсканированные, но не проданные: проданный по чеку код остаётся
    проданным, его судьбу решает чек возврата. Отметку «собрано» не
    трогаем — по ней видно, что заказ собирали.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            delete(OrderMarkingCode)
            .where(OrderMarkingCode.order_id == order_id, OrderMarkingCode.status == SCANNED)
        )
        await session.commit()
    if result.rowcount:
        logger.info("Заказ %s: освобождено кодов маркировки — %s", order_id, result.rowcount)
    return int(result.rowcount or 0)
