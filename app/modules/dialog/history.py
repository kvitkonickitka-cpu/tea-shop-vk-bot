# В памяти процесса, как и остальное состояние сейчас: переживёт до
# перезапуска/передеплоя, не шарится между инстансами. При росте — Redis/БД.
_MAX_HISTORY_MESSAGES = 20

_histories: dict[int, list[dict]] = {}


def get_history(peer_id: int) -> list[dict]:
    return list(_histories.get(peer_id, []))


def append_exchange(peer_id: int, user_text: str, assistant_text: str) -> None:
    history = _histories.setdefault(peer_id, [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    if len(history) > _MAX_HISTORY_MESSAGES:
        del history[: len(history) - _MAX_HISTORY_MESSAGES]
