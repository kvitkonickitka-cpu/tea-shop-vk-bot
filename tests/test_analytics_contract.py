"""Аналитика, А5: договор с DataLens и роль только на чтение."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "analytics" / "contract.yaml"


def load_contract() -> dict[str, list[tuple[str, str]]]:
    raw = yaml.safe_load(CONTRACT.read_text())
    return {view: [next(iter(column.items())) for column in columns] for view, columns in raw["views"].items()}


def breaches(contract: dict, actual: dict) -> list[str]:
    """Чем база расходится с договором — по-русски, чтобы было понятно, что чинить."""
    problems = []
    for view in contract.keys() - actual.keys():
        problems.append(f"нет представления {view}")
    for view in actual.keys() - contract.keys():
        problems.append(f"представление {view} не записано в договор")
    for view in contract.keys() & actual.keys():
        promised, real = contract[view], actual[view]
        real_types = dict(real)
        for name, kind in promised:
            if name not in real_types:
                problems.append(f"{view}.{name}: столбец пропал или переименован")
            elif real_types[name] != kind:
                problems.append(f"{view}.{name}: тип {real_types[name]} вместо {kind}")
        promised_names = {name for name, _ in promised}
        for name, _ in real:
            if name not in promised_names:
                problems.append(f"{view}.{name}: новый столбец не записан в договор")
        common = [name for name, _ in real if name in promised_names]
        if common != [name for name, _ in promised if name in real_types]:
            problems.append(f"{view}: столбцы переставлены — новые только в конец")
    return sorted(problems)


async def actual_views(session) -> dict[str, list[tuple[str, str]]]:
    rows = (await session.execute(text(
        "select table_name, column_name,"
        " case when data_type = 'numeric' then 'numeric(' || numeric_precision || ',' || numeric_scale || ')'"
        " else data_type end"
        " from information_schema.columns where table_schema = 'analytics'"
        " order by table_name, ordinal_position"
    ))).all()
    result: dict[str, list[tuple[str, str]]] = {}
    for view, column, kind in rows:
        result.setdefault(view, []).append((column, kind))
    return result


async def test_views_match_the_contract(db):
    async with db() as session:
        assert breaches(load_contract(), await actual_views(session)) == []


def test_breaches_are_caught():
    contract = {"v": [("a", "integer"), ("b", "text")]}
    assert breaches(contract, {"v": [("a", "integer"), ("b", "text")]}) == []
    assert breaches(contract, {"v": [("a", "integer")]}) == ["v.b: столбец пропал или переименован"]
    assert breaches(contract, {"v": [("a", "integer"), ("bb", "text")]}) == [
        "v.b: столбец пропал или переименован", "v.bb: новый столбец не записан в договор",
    ]
    assert breaches(contract, {"v": [("a", "integer"), ("b", "text"), ("c", "date")]}) == [
        "v.c: новый столбец не записан в договор",
    ]
    assert breaches(contract, {"v": [("a", "bigint"), ("b", "text")]}) == ["v.a: тип bigint вместо integer"]
    assert breaches(contract, {"v": [("b", "text"), ("a", "integer")]}) == [
        "v: столбцы переставлены — новые только в конец",
    ]
    assert breaches(contract, {}) == ["нет представления v"]


def test_contract_lists_every_view_in_code():
    from app.modules.analytics import views

    assert list(load_contract()) == [view.name for view in views.VIEWS]


@pytest.mark.skipif(shutil.which("psql") is None, reason="нет psql")
async def test_datalens_role_reads_analytics_only(db):
    """Скрипт роли: SELECT на analytics есть, рабочие таблицы закрыты, запись запрещена."""
    url = make_url(settings.database_url)
    host = url.query.get("host") or url.host or "localhost"
    port = str(url.query.get("port") or url.port or 5432)
    base = ["psql", "-X", "-q", "-h", host, "-p", port, "-d", url.database]
    env = {**os.environ, "DATALENS_DB_PASSWORD": "pw-for-tests"}
    for _ in range(2):  # повторный запуск безопасен
        script = (ROOT / "analytics" / "datalens_role.sql").read_text()
        done = subprocess.run(base + ["-U", url.username or "postgres"], input=script,
                              env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    def as_reader(sql: str) -> subprocess.CompletedProcess:
        return subprocess.run(base + ["-U", "datalens_reader", "-At", "-c", sql],
                              env={**os.environ, "PGPASSWORD": "pw-for-tests"}, capture_output=True, text=True)

    assert as_reader("select count(*) from analytics.v_orders").returncode == 0
    assert "permission denied" in as_reader("select count(*) from public.orders").stderr
    assert "permission denied" in as_reader("select count(*) from public.conversation_messages").stderr
    assert "read-only" in as_reader("create table analytics.x (a int)").stderr
