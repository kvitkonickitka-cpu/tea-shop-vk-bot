"""Ассистент Ops смотрит код и историю — выкаченной версии, без закрытых файлов."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.modules.ops.assistant import code, service, tools
from tests.test_ops_assistant import ME, message, said, tool_use, wire  # noqa: F401

REAL_RUN = tools.run


def _commit(repo_path: Path, files: dict[str, str], text: str) -> str:
    from dulwich import porcelain

    for name, content in files.items():
        target = repo_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    porcelain.add(str(repo_path), paths=[str(repo_path / name) for name in files])
    sha = porcelain.commit(str(repo_path), message=text.encode(), author=b"T <t@t>", committer=b"T <t@t>")
    return sha.decode()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from dulwich import porcelain

    porcelain.init(str(tmp_path))
    first = _commit(tmp_path, {
        "app/modules/queue/local.py": "def enqueue():\n    # кладём событие в queued_events\n    return 1\n",
        "CLAUDE.md": "# Бот\nОчередь — queued_events.\n",
        "cashflow/data/rules.csv": "Иванов Иван,перевод\n",
        ".env": "SECRET=1\n",
        "deploy/timeweb/env.example": "ANTHROPIC_MODEL=\n",
    }, "Queue VK events in Postgres")
    second = _commit(tmp_path, {"app/modules/queue/local.py": "def enqueue():\n    return 2  # новое\n"},
                     "Change enqueue after deploy")
    monkeypatch.setattr(settings, "ops_assistant_repo_path", str(tmp_path))
    monkeypatch.setattr(settings, "app_revision", first[:12])
    code._snapshot.cache_clear()
    return {"first": first, "second": second}


def test_recent_commits_mark_what_is_deployed(repo):
    data = code.recent_commits()
    assert data["deployed"] == repo["first"][:7]
    newest, deployed = data["commits"]
    assert newest["subject"] == "Change enqueue after deploy" and newest["not_deployed_yet"] and not newest["deployed"]
    assert deployed["deployed"] and not deployed["not_deployed_yet"]
    assert "app/modules/queue/local.py" in deployed["files"]


def test_code_is_read_at_the_deployed_revision(repo):
    found = code.search_code("queued_events")
    assert {m["path"] for m in found["matches"]} == {"app/modules/queue/local.py", "CLAUDE.md"}
    text = code.read_file("app/modules/queue/local.py")["text"]
    assert "return 1" in text and "return 2" not in text


def test_closed_files_are_not_given(repo):
    assert "error" in code.read_file("cashflow/data/rules.csv")
    assert "error" in code.read_file(".env")
    assert code.read_file("deploy/timeweb/env.example")["text"].startswith("1: ANTHROPIC_MODEL=")
    assert not code.search_code("Иванов")["matches"]
    listed = code.list_files()["files"]
    assert ".env" not in listed and "cashflow/data/rules.csv" not in listed


async def test_assistant_reads_code_and_links_it(wire, repo, monkeypatch):
    monkeypatch.setattr(tools, "run", REAL_RUN)  # wire подменяет инструменты — здесь нужны настоящие
    wire["script"] = [
        tool_use("recent_commits"),
        tool_use("read_file", path="app/modules/queue/local.py"),
        said("Очередь выкачена: коммит `" + repo["first"][:7] + "`."),
    ]
    await service._answer_safely(message("message_queue выкатился?"), ME)
    dumped = json.dumps(wire["requests"], ensure_ascii=False, default=str)
    assert "Queue VK events in Postgres" in dumped and "queued_events" in dumped
    sent = wire["sent"][0]
    assert "/commits/main" in sent
    assert f"/blob/{repo['first'][:7]}/app/modules/queue/local.py#L1-L4" in sent
    assert "скрыто" not in sent
