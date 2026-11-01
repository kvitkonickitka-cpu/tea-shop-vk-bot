# Тесты

Нужна живая Postgres: бот без базы уходит в резервное хранение в памяти, и
проверки журнала отправок, очереди уведомлений и черновиков проверяли бы
другой код.

```sh
pip install -r requirements-dev.txt
# своя база: адрес любой, лишь бы пустая
TEST_DATABASE_URL="postgresql://postgres@localhost/tea_test" pytest
```

Без `TEST_DATABASE_URL` берётся адрес по умолчанию из `tests/conftest.py`;
если базы там нет, тесты с фикстурой `db` пропускаются, а остальные
работают.

Внешние службы (ВК, ЮKassa, Telegram, СДЭК, Ozon) не вызываются: их
подменяет `monkeypatch` в самих тестах. Живые проверки лежат отдельно, в
`scripts/` — они ходят в настоящие API и запускаются руками.

## Обязательный прогон перед коммитом

```sh
TEST_DATABASE_URL="postgresql://postgres@localhost/tea_test" \
TESTS_REQUIRE_DATABASE=1 python -m pytest -q -rs --durations=10
```

Использовать отдельную PostgreSQL с UTF-8 локалью (например en_US.UTF-8):
русский lower/сопоставление каталога зависят от локали. При
TESTS_REQUIRE_DATABASE=1 недоступная БД — ошибка, не пропуск. Полный прогон
должен пройти до коммита; далее обязательный CI PR и main перед деплоем.

Последний подтверждённый прогон приложения для временного демо — 1151 passed.
[Результаты и границы живой проверки](../docs/current-state.md).
В обычном pytest OpenAI подменяется; `tests/live_openai.py` запускается отдельно
с RUN_OPENAI_LIVE=1 и серверным ключом. Не путать SDK MockTransport с реальным
провайдером. Локальный Codex остаётся на входе ChatGPT по подписке.
