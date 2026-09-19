from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

# Ссылки на живые задачи: без них сборщик мусора вправе убрать задачу на
# полпути, потому что больше на неё никто не ссылается.
_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro: Coroutine[Any, Any, Any]) -> None:
    """Запускает корутину, не дожидаясь её.

    Для побочных действий, от которых не зависит ответ клиенту: индикатор
    «печатает», уведомление менеджеру. VK отводит на вебхук около восьми
    секунд, и всё, что стоит в этом пути, тратит общий бюджет — а исчерпав
    его, не доводит до конца ни себя, ни то, ради чего запрос затевался.
    """
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
