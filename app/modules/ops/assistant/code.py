"""Код и история проекта для ассистента Ops — той версии, что работает.

Без этого ассистент отвечал на «выкатилось ли X?» только ревизией из
/health: какой это коммит и что в нём, он не знал, как не знал и устройство
бота. Теперь он видит последние коммиты (и какой из них сейчас в
контейнере), ищет по коду и читает файлы и документацию.

Откуда: каталог `.git` репозитория на ВМ смонтирован в контейнер только на
чтение (deploy/timeweb/docker-compose.yml), читаем его dulwich — git в
образе нет. Код берём на коммите, который выкачен (APP_REVISION), а не на
свежем main: отвечать надо про то, что работает.

Чего не отдаём: `.env*` (кроме примеров), выгрузки и правила выписки
(`cashflow/data/` — там контрагенты), csv, pdf, html и картинки. Секретов
в git нет вовсе — они в `.env.bot`, который git не видит.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.config import settings

_MAX_FILE_BYTES = 400_000
_MAX_READ_LINES = 250
_MAX_MATCHES = 40
_LINE_CHARS = 220
_DENY = (
    "cashflow/data/*", "*.csv", "*.pdf", "*.html", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif",
    "*.ico", "*.svg", "*.woff*", "*.dump", "*.sqlite*",
)
_ENV_FILE = re.compile(r"(^|/)\.env(?!\.example$)")


def _repo():
    from dulwich.repo import Repo

    return Repo(settings.ops_assistant_repo_path)


def _allowed(path: str) -> bool:
    if _ENV_FILE.search(path):
        return False
    return not any(fnmatch.fnmatch(path, pattern) for pattern in _DENY)


def _commit_time(commit) -> str:
    return datetime.fromtimestamp(commit.commit_time, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")


def _subject(commit) -> str:
    return commit.message.decode("utf-8", errors="replace").strip().splitlines()[0]


def _resolve(repo, short: str):
    """Коммит по короткому хешу — ищем среди последних на main."""
    if not short:
        return None
    short = short.lower()
    for entry in repo.get_walker(include=[repo.head()], max_entries=400):
        if entry.commit.id.decode().startswith(short):
            return entry.commit
    return None


def deployed_commit(repo):
    """Выкаченный коммит: APP_REVISION из деплоя, иначе HEAD."""
    return _resolve(repo, settings.app_revision) or repo[repo.head()]


def _changed_files(repo, commit) -> list[str]:
    from dulwich.diff_tree import tree_changes

    parent_tree = repo[commit.parents[0]].tree if commit.parents else None
    files = []
    for change in tree_changes(repo.object_store, parent_tree, commit.tree):
        # У удалённого файла нет new, у добавленного — old.
        side = change.new if change.new is not None and change.new.path else change.old
        path = (side.path if side is not None and side.path else b"").decode("utf-8", errors="replace")
        files.append(path)
    return files


def recent_commits(limit=15) -> dict:
    repo = _repo()
    try:
        limit = max(1, min(int(limit), 40))
    except (TypeError, ValueError):
        limit = 15
    deployed = deployed_commit(repo)
    commits, seen_deployed = [], False
    for entry in repo.get_walker(include=[repo.head()], max_entries=limit):
        commit = entry.commit
        is_deployed = commit.id == deployed.id
        seen_deployed = seen_deployed or is_deployed
        files = _changed_files(repo, commit)
        commits.append({
            "hash": commit.id.decode()[:7], "date": _commit_time(commit), "subject": _subject(commit),
            # Ровно этот коммит сейчас в контейнере (ревизия из /health).
            "is_running_revision": is_deployed,
            # Входит в работающую версию: выкаченный коммит и всё, что старше
            # него. Раньше здесь был флаг только у самого выкаченного, и
            # ассистент читал «deployed: false» у старых коммитов как «не
            # выкатилось» (15.10.2026, про очередь b38f096).
            "in_running_version": seen_deployed,
            # Новее выкаченного: в main есть, в контейнере ещё нет.
            "not_deployed_yet": not seen_deployed,
            "files": files[:15] + ([f"… ещё {len(files) - 15}"] if len(files) > 15 else []),
        })
    return {"deployed": deployed.id.decode()[:7], "commits": commits}


def show_commit(hash) -> dict:
    repo = _repo()
    commit = _resolve(repo, str(hash or ""))
    if commit is None:
        return {"error": f"коммита {hash} среди последних 400 нет"}
    message = commit.message.decode("utf-8", errors="replace").strip()
    # Строки соавторства — шум для разбора.
    message = "\n".join(line for line in message.splitlines() if not line.startswith("Co-Authored-By"))
    return {
        "hash": commit.id.decode()[:7], "date": _commit_time(commit), "message": message[:3000],
        "files": [f for f in _changed_files(repo, commit) if _allowed(f)][:60],
    }


def _tree_files(repo, commit) -> list[tuple[str, bytes]]:
    from dulwich.object_store import iter_tree_contents

    files = []
    for entry in iter_tree_contents(repo.object_store, commit.tree):
        path = entry.path.decode("utf-8", errors="replace")
        if _allowed(path):
            files.append((path, entry.sha))
    return files


@lru_cache(maxsize=4)
def _snapshot(commit_id: bytes) -> dict[str, list[str]]:
    """Тексты всех разрешённых файлов коммита: поиск идёт по ним многократно."""
    repo = _repo()
    commit = repo[commit_id]
    texts: dict[str, list[str]] = {}
    for path, sha in _tree_files(repo, commit):
        blob = repo[sha]
        data = blob.data
        if len(data) > _MAX_FILE_BYTES or b"\x00" in data[:4000]:
            continue
        texts[path] = data.decode("utf-8", errors="replace").split("\n")
    return texts


def _files(path_prefix: str = "") -> tuple[str, dict[str, list[str]]]:
    repo = _repo()
    commit = deployed_commit(repo)
    texts = _snapshot(commit.id)
    prefix = (path_prefix or "").strip().lstrip("./")
    if prefix:
        texts = {p: t for p, t in texts.items() if p.startswith(prefix)}
    return commit.id.decode(), texts


def search_code(query, path=None, regex=False) -> dict:
    query = str(query or "").strip()
    if not query:
        return {"error": "пустой запрос"}
    try:
        matcher = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    except re.error:
        return {"error": "неверное регулярное выражение"}
    revision, texts = _files(path or "")
    matches, files_hit = [], set()
    for file_path in sorted(texts):
        for number, line in enumerate(texts[file_path], start=1):
            if matcher.search(line):
                files_hit.add(file_path)
                if len(matches) < _MAX_MATCHES:
                    matches.append({"path": file_path, "line": number, "text": line.strip()[:_LINE_CHARS]})
    return {"revision": revision[:7], "matches": matches, "files_with_matches": len(files_hit),
            "truncated": len(matches) >= _MAX_MATCHES}


def read_file(path, start=1, end=None) -> dict:
    path = str(path or "").strip().lstrip("./")
    if not _allowed(path):
        return {"error": "этот файл закрыт"}
    revision, texts = _files()
    if path not in texts:
        near = [p for p in texts if p.endswith(path) or path in p][:10]
        return {"error": f"файла {path} нет", "maybe": near}
    lines = texts[path]
    try:
        start = max(1, int(start or 1))
        end = int(end) if end else start + _MAX_READ_LINES - 1
    except (TypeError, ValueError):
        start, end = 1, _MAX_READ_LINES
    end = min(end, start + _MAX_READ_LINES - 1, len(lines))
    numbered = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
    return {"revision": revision[:7], "path": path, "start": start, "end": end,
            "total_lines": len(lines), "text": numbered}


def list_files(path=None) -> dict:
    revision, texts = _files(path or "")
    paths = sorted(texts)
    return {"revision": revision[:7], "count": len(paths), "files": paths[:300]}


def available() -> bool:
    try:
        _repo()
        return True
    except Exception:
        return False


TOOLS = [
    {
        "name": "recent_commits",
        "description": (
            "Последние коммиты main: хеш, дата, тема, изменённые файлы. is_running_revision — ровно этот "
            "коммит сейчас в контейнере; in_running_version — коммит входит в работающую версию (выкаченный и "
            "все старше него); not_deployed_yet — новее выкаченного, ещё не в контейнере. Смотри первым, когда "
            "спрашивают, выкатилось ли что-то."
        ),
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "До 40, по умолчанию 15."}}},
    },
    {
        "name": "show_commit",
        "description": "Полное описание коммита по хешу (зачем сделано) и список изменённых файлов.",
        "input_schema": {"type": "object", "properties": {"hash": {"type": "string"}}, "required": ["hash"]},
    },
    {
        "name": "search_code",
        "description": (
            "Поиск по коду и документации выкаченной версии (без учёта регистра): путь, номер строки, строка. "
            "Документация: CLAUDE.md (главное об устройстве), docs/infrastructure.md, docs/monitoring.md, "
            "deploy/timeweb/README.md. path — ограничить каталогом, например app/modules/queue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "regex": {"type": "boolean", "description": "query — регулярное выражение"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Прочитать файл выкаченной версии с номерами строк, до 250 строк за раз (start, end).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "Список файлов выкаченной версии, можно в каталоге (path, например app/modules/ops).",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]

def _threaded(fn):
    # Чтение git — диск и распаковка: в одном процессе с ботом это не должно
    # стоять в цикле событий, где ждут ответа клиенты.
    async def run(**kwargs):
        return await asyncio.to_thread(fn, **kwargs)

    return run


HANDLERS = {
    name: _threaded(fn)
    for name, fn in {
        "recent_commits": recent_commits,
        "show_commit": show_commit,
        "search_code": search_code,
        "read_file": read_file,
        "list_files": list_files,
    }.items()
}
