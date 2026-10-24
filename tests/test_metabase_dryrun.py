"""Пробный прогон сборки Metabase (analytics/metabase/mb_dryrun.py): записи не уходят, сравнение — по месту.

Настоящего Metabase в тестах нет: «живой» дашборд подставляется, сравнение проверяется
на нём. Главное — пробный прогон ничего не пишет, а отличие в названии, запросе или
месте карточки он видит.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

METABASE = Path(__file__).resolve().parents[1] / "analytics" / "metabase"

LIVE = {"id": 11, "name": "Пульс", "description": "Каждый день", "parameters": [],
        "tabs": [{"id": 57, "name": "Сегодня"}],
        "dashcards": [{"dashboard_tab_id": 57, "row": 0, "col": 0, "size_x": 6, "size_y": 3, "card_id": 1663,
                       "visualization_settings": {},
                       "card": {"name": "Свободно", "display": "scalar", "description": None,
                                "visualization_settings": {"scalar.compact_primary_number": False},
                                "dataset_query": {"stages": [{"native": "select 1"}]}}}]}


@pytest.fixture
def dry(monkeypatch):
    writes = []

    def real(method, path, body=None):
        if method == "GET" and path == "/dashboard/11":
            return LIVE
        writes.append((method, path))
        raise AssertionError(f"пробный прогон не должен ходить в Metabase с {method} {path}")

    fake = types.ModuleType("mbapi")
    fake.BASE, fake.call = "https://stats.example/api", real
    monkeypatch.setitem(sys.modules, "mbapi", fake)
    monkeypatch.syspath_prepend(str(METABASE))
    sys.modules.pop("mb_dryrun", None)
    import mb_dryrun  # noqa: PLC0415

    yield mb_dryrun, writes
    sys.modules.pop("mb_dryrun", None)


def _build(dry, name="Свободно", query="select 1", col=0):
    """То, что сделала бы сборка: вопрос, вкладки и раскладка — через подменённый call."""
    cid = dry.fake("POST", "/card", {"name": name, "display": "scalar", "description": None,
                                     "visualization_settings": {"scalar.compact_primary_number": False},
                                     "dataset_query": {"type": "native", "native": {"query": query}}})["id"]
    dry.fake("PUT", "/dashboard/11", {"tabs": [{"id": 57, "name": "Сегодня"}], "dashcards": [
        {"dashboard_tab_id": 57, "row": 0, "col": col, "size_x": 6, "size_y": 3, "card_id": cid,
         "visualization_settings": {}}], "parameters": []})
    return dry.DASH[11]


def test_same_build_has_no_differences_and_writes_nothing(dry):
    module, writes = dry
    assert module.compare(11, _build(module)) == []
    assert writes == []


def test_renamed_or_moved_card_is_reported(dry):
    module, _ = dry
    diffs = module.compare(11, _build(module, name="Денег хватит до", query="select 2"))
    assert any("name" in d and "Денег хватит до" in d for d in diffs)
    assert any("query" in d and "select 2" in d for d in diffs)
    module.DASH.clear()
    moved = module.compare(11, _build(module, col=6))
    assert any("в живом есть «Свободно»" in d for d in moved) and any("сборка ставит «Свободно»" in d for d in moved)
