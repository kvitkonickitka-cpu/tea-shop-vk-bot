# Внешний сторож бота

Раз в минуту с VPS Aeza (`94.228.170.170`, там же прокси для Anthropic и
Telegram) спрашивает `https://bot.teapotwice.ru/health`. Три неудачи
подряд — 🔴 в чат Ops, повтор раз в час, починилось — 🟢.

Зачем: бот пишет в Ops о сбоях сам, но только пока жив. Если лягут ВМ
Timeweb, Docker или Caddy, сказать некому. В Yandex это делал алерт
Monitoring «нет пульса»; с Timeweb метрики в Monitoring не уходят. Сторож
стоит у другого провайдера, поэтому падение Timeweb его не задевает.

Что он не ловит: недоступную базу при живом боте (`/health` отвечает 200 с
«резервным режимом») — об этом бот пишет сам.

## Установка (на VPS Aeza, root)

```
install -m 755 teashop-watchdog.sh /usr/local/bin/teashop-watchdog
install -m 600 /dev/null /etc/teashop-watchdog.env
# в /etc/teashop-watchdog.env две строки:
#   TELEGRAM_BOT_TOKEN=<токен бота>
#   TELEGRAM_CHAT_ID=<TELEGRAM_OPS_CHAT_ID>
cp teashop-watchdog.cron /etc/cron.d/teashop-watchdog
```

Проверить тревогу, не трогая боевое состояние (своя папка, иначе минутный
запуск по cron собьёт счётчик):

```
export WATCHDOG_STATE=/tmp/wd-test WATCHDOG_URL=https://127.0.0.1:1/
teashop-watchdog; teashop-watchdog; teashop-watchdog      # 🔴 в чат Ops
WATCHDOG_URL=https://bot.teapotwice.ru/health teashop-watchdog   # 🟢
```

Боевое состояние — `/var/lib/teashop-watchdog`.

## Кто сторожит сам Aeza

Если ляжет VPS Aeza, бот перестанет отвечать клиентам (Claude идёт через
прокси на нём), а написать об этом в Telegram не сможет: с ВМ Timeweb
Telegram напрямую недоступен, только через тот же прокси. Поэтому Aeza
проверяет **монитор Timeweb** «Aeza прокси (Claude, Telegram)» — раз в
минуту `https://94-228-170-170.nip.io/health` из Петербурга и Амстердама
(30 ₽/мес). Тревогу шлёт сам Timeweb — его уведомления подключены к чату
Teapotwice Alerts (Профиль → Уведомления → Телеграм → Групповой чат).

`/health` — блок `handle /health { respond "ok" 200 }` в `/etc/caddy/Caddyfile`
на Aeza (с 15.10.2026): отвечает сам Caddy, без Anthropic и Telegram.

## Баланс Aeza

Если сервер Aeza не продлится за неуплату, бот замолчит так же, как при
поломке, а монитор Timeweb заметит это, только когда сервер уже погас.
Поэтому `aeza-balance.py` раз в день в 10:00 МСК спрашивает API Aeza
(`/api/v2/accounts/me` и `/api/v2/services`, суммы в копейках). Тревога
приходит в чат Ops от того же бота, что и у сторожа:

- 💳 — до автопродления какой-то услуги осталось 14 дней или меньше, а
  на счёте (с бонусами) меньше, чем стоят эти продления. Повторяется каждый
  день, пока счёт не пополнят; за 3 дня и меньше — 🔴;
- ⚠️ — услуга не в статусе `active`, или API не ответил (например, ключ
  отозван: 401), чтобы проверка не замолчала незаметно.

Ключ — `AEZA_API_KEY` и `AEZA_API_BASE` в `/etc/teashop-watchdog.env`.
Выпускается в my.aeza.ru → Настройки → API ключи, записывается скриптом
`/root/aeza_set_key.sh` на Aeza (ввод скрыт, ключ сначала проверяется).

```
install -m 755 aeza-balance.py /usr/local/bin/aeza-balance
cp aeza-balance.cron /etc/cron.d/aeza-balance
```

Проверить, ничего не отправляя: `AEZA_BALANCE_DRY_RUN=1 aeza-balance`;
`WARN_DAYS=60` — увидеть текст тревоги заранее. Журнал —
`/var/log/aeza-balance.log`.
