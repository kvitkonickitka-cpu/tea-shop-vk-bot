"""Страница сборки заказа со сканированием кодов маркировки.

Адрес открыт всему интернету, как и вебхуки: защищает его подпись ссылки
(`packing.read_token`). Токен ссылки идёт в пути, а не в заголовке или
куке: по дороге к контейнеру нестандартные заголовки срезают, а у ссылки
из телеграма ничего, кроме адреса, нет.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.modules.marking import packing

logger = logging.getLogger(__name__)

router = APIRouter(tags=["packing"])

_STATIC = Path(__file__).resolve().parent.parent / "static" / "packing"
# Токен ссылки не должен утечь ни поисковику, ни в Referer.
_PRIVATE = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}


@router.get("/pack/static/zxing-reader.js")
async def scanner_script():
    return FileResponse(
        _STATIC / "zxing-reader.js", media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/pack/static/zxing_reader.wasm")
async def scanner_wasm():
    # application/wasm обязателен для потоковой компиляции в браузере.
    return FileResponse(
        _STATIC / "zxing_reader.wasm", media_type="application/wasm",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/pack/{token}")
async def page(token: str):
    return FileResponse(
        _STATIC / "index.html", media_type="text/html; charset=utf-8",
        headers={**_PRIVATE, "Cache-Control": "no-store"},
    )


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status, headers=_PRIVATE)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/pack/{token}/state")
async def get_state(token: str):
    try:
        order_id = packing.read_token(token)
        current = await packing.state(order_id)
    except packing.LinkError as error:
        return _error(str(error), 403)
    return JSONResponse({"ok": True, "state": current.as_dict()}, headers=_PRIVATE)


@router.post("/pack/{token}/scan")
async def scan(token: str, request: Request):
    data = await _body(request)
    try:
        order_id = packing.read_token(token)
        result = await packing.scan(
            order_id, str(data.get("code") or ""),
            by=str(data.get("by") or ""), manual=bool(data.get("manual")),
        )
    except packing.LinkError as error:
        return _error(str(error), 403)
    return JSONResponse(
        {"ok": result.ok, "message": result.message, "warning": result.warning,
         "state": result.state.as_dict()},
        headers=_PRIVATE,
    )


@router.post("/pack/{token}/remove")
async def remove(token: str, request: Request):
    data = await _body(request)
    try:
        order_id = packing.read_token(token)
        current = await packing.remove(order_id, int(data.get("id") or 0))
    except packing.LinkError as error:
        return _error(str(error))
    except (TypeError, ValueError):
        return _error("Не понял, какой код убрать.")
    return JSONResponse({"ok": True, "message": "Код убран.", "state": current.as_dict()},
                        headers=_PRIVATE)


@router.post("/pack/{token}/finish")
async def finish(token: str, request: Request):
    data = await _body(request)
    try:
        order_id = packing.read_token(token)
        current = await packing.finish(order_id, by=str(data.get("by") or ""))
    except packing.LinkError as error:
        return _error(str(error))
    return JSONResponse(
        {"ok": True, "message": f"Заказ №{order_id} собран, коды закреплены.",
         "state": current.as_dict()},
        headers=_PRIVATE,
    )
