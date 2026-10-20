"""Фича-флаги в базе и команды администраторов в чате Ops."""

from __future__ import annotations

import pytest

from app.core import flags
from app.core.config import settings
from app.modules.analytics import service as analytics
from app.modules.ops import commands

TESTER, CLIENT = 7301, 7302
FLAG = "test_flag_for_tests"


@pytest.fixture(autouse=True)
def known_flag(monkeypatch):
    monkeypatch.setitem(flags.KNOWN, FLAG, flags.Flag(FLAG, "Флаг для тестов"))
    monkeypatch.setattr(settings, "test_vk_ids", str(TESTER))
    monkeypatch.setattr(analytics, "_test_ids", None)
    monkeypatch.setattr(settings, "ops_admin_tg_ids", "")
    monkeypatch.setattr(settings, "ops_assistant_allowed_tg_ids", "")


async def test_flag_is_off_until_switched(clean):
    assert await flags.mode(FLAG) == flags.OFF
    assert not await flags.enabled_for(FLAG, TESTER)


async def test_allowlist_only_for_test_accounts(clean):
    await flags.set_mode(FLAG, flags.ALLOWLIST, by="test")
    assert await flags.enabled_for(FLAG, TESTER)
    assert not await flags.enabled_for(FLAG, CLIENT)
    # Без клиента allowlist — не «включено»: рассылка без адресата не для тестовых.
    assert not await flags.enabled_for(FLAG, None)


async def test_on_for_everyone_and_history_of_who(clean):
    previous = await flags.set_mode(FLAG, flags.ON, by="tg:1 nikita")
    assert previous == flags.OFF
    assert await flags.enabled_for(FLAG, CLIENT)
    [row] = [r for r in await flags.describe() if r["name"] == FLAG]
    assert row["mode"] == flags.ON and row["updated_by"] == "tg:1 nikita"


async def test_cache_is_kept_for_a_minute(clean, monkeypatch):
    assert await flags.mode(FLAG) == flags.OFF
    # Запись мимо set_mode (другой контейнер): эта копия увидит её после кэша.
    from sqlalchemy import text

    async with clean() as session:
        await session.execute(text("insert into feature_flags (name, mode) values (:n, 'on')"), {"n": FLAG})
        await session.commit()
    assert await flags.mode(FLAG) == flags.OFF
    monkeypatch.setattr(flags, "_loaded_at", flags._loaded_at - flags.CACHE_SECONDS - 1)
    assert await flags.mode(FLAG) == flags.ON


async def test_unknown_flag_or_mode_is_refused(clean):
    with pytest.raises(ValueError):
        await flags.set_mode("no_such_flag", flags.ON, by="test")
    with pytest.raises(ValueError):
        await flags.set_mode(FLAG, "maybe", by="test")


def test_command_parsing():
    assert commands.parse("/flag", "teabot") == ("flag", [])
    assert commands.parse("/flag@teabot x on", "teabot") == ("flag", ["x", "on"])
    assert commands.parse("/flag@otherbot x on", "teabot") is None
    assert commands.parse("/unknown", "teabot") is None
    assert commands.parse("что с флагами?", "teabot") is None


async def test_flag_command_lists_and_switches(clean):
    sender = {"id": 42, "username": "nikita"}
    listing = await commands.run("flag", [], sender)
    assert FLAG in listing and "off" in listing
    done = await commands.run("flag", [FLAG, "allowlist"], sender)
    assert "off → <b>allowlist</b>" in done
    assert await flags.mode(FLAG) == flags.ALLOWLIST
    assert "Нужно имя флага" in await commands.run("flag", [FLAG], sender)


async def test_commands_only_for_admins(clean, monkeypatch):
    monkeypatch.setattr(settings, "ops_admin_tg_ids", "1, 2")
    assert "только для администраторов" in await commands.run("flag", [FLAG, "on"], {"id": 3})
    assert await flags.mode(FLAG) == flags.OFF
    assert "allowlist" not in await commands.run("flag", [FLAG, "on"], {"id": 2})
    assert await flags.mode(FLAG) == flags.ON


async def test_own_command_marks_client(clean):
    await analytics.ensure_client(CLIENT, ref="autumn")
    assert "помечен «свои»" in await commands.run("own", [str(CLIENT)], {"id": 1})
    assert "в базе нет" in await commands.run("own", ["999"], {"id": 1})
    assert "Нужен peer_id" in await commands.run("own", ["abc"], {"id": 1})
    from sqlalchemy import text

    async with clean() as session:
        own = (await session.execute(text("select own_at is not null from clients where peer_id = :p"),
                                     {"p": CLIENT})).scalar()
    assert own
    assert "снята" in await commands.run("own", [str(CLIENT), "off"], {"id": 1})


async def test_ops_chat_routes_commands_past_the_model(clean, monkeypatch):
    from app.modules.ops.assistant import service

    monkeypatch.setattr(settings, "telegram_ops_chat_id", "-100")
    handled = []

    async def fake_handle(message, me):
        handled.append(message["text"])
        return True

    monkeypatch.setattr(commands, "handle", fake_handle)
    import time

    update = {"message": {"chat": {"id": -100}, "from": {"id": 1}, "text": "/flag", "message_id": 5,
                          "date": int(time.time())}}
    assert service.handle_update(update, {"id": 99, "username": "teabot"})
    for task in list(service._tasks):
        await task
    assert handled == ["/flag"]
