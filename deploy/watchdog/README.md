# Внешний сторож бота

Раз в минуту с VPS Aeza (`94.228.170.170`, там же прокси для Anthropic и
Telegram) спрашивает `https://201-24-120-144.sslip.io/health`. Три неудачи
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
WATCHDOG_URL=https://201-24-120-144.sslip.io/health teashop-watchdog   # 🟢
```

Боевое состояние — `/var/lib/teashop-watchdog`.
