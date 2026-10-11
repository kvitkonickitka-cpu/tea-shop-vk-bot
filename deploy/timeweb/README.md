# Бот на своей ВМ (Timeweb)

Запасной путь на случай, когда Yandex Serverless Containers не принимает
запросы (11.10.2026: шлюз `*.containers.yandexcloud.net` рвал соединение на
TLS, бот молчал). Бот тот же, из этого репозитория; отличия от Yandex:

| Yandex | Здесь |
|---|---|
| Serverless Container | `docker compose`: контейнер бота + Caddy (HTTPS) |
| Очередь YMQ и склейка сообщений | без очереди: событие обрабатывается сразу, склейки нет |
| Таймерные триггеры `dialog-reports`, `ops-pulse` | `cron`: тик раз в 5 минут, пульс раз в минуту |
| Метрики в Yandex Monitoring | не пишутся: алерт «бот не работает» в Monitoring будет гореть — это ожидаемо |
| Секреты в GitHub → ревизия | файл `.env.bot` на ВМ (chmod 600, в git не кладётся) |

Claude и Telegram ходят через прокси на Aeza — от Yandex они не зависят.

## Запуск

```
git clone https://github.com/kvitkonickitka-cpu/tea-shop-vk-bot.git /opt/tea-shop-vk-bot
cd /opt/tea-shop-vk-bot/deploy/timeweb
cp env.example .env.bot && chmod 600 .env.bot      # заполнить значения
echo "BOT_DOMAIN=<ip-через-дефисы>.sslip.io" > .env  # или свой домен с A-записью на ВМ
docker compose up -d --build
sudo install -d -m 700 /etc/teashop
sudo sh -c 'grep ^INTERNAL_API_TOKEN= .env.bot | cut -d= -f2- > /etc/teashop/internal_token'
sudo chmod 600 /etc/teashop/internal_token
sudo cp teashop-bot.cron /etc/cron.d/teashop-bot
```

В `.env.bot`:
- `DATABASE_URL` — Postgres на этой же ВМ: хост `host.docker.internal`;
- `PUBLIC_BASE_URL` — `https://<BOT_DOMAIN>` (ссылки на страницу сборки);
- остальное — те же значения, что в секретах GitHub.

Порты 80 и 443 должны быть открыты в файрволе Timeweb: на 80 Caddy получает
сертификат.

Проверка: `curl https://<BOT_DOMAIN>/health` — `"database": "ok"`; через
5 минут в «по расписанию» появится отметка тика.

## Переключить трафик

1. **ВК**: сообщество → Управление → Работа с API → Callback API → адрес
   `https://<BOT_DOMAIN>/vk/callback`, «Подтвердить» (строка подтверждения —
   `VK_CONFIRMATION_TOKEN`, секретный ключ — `VK_SECRET_KEY`, те же).
2. **ЮKassa**: личный кабинет → Интеграция → HTTP-уведомления →
   `https://<BOT_DOMAIN>/payments/yookassa`. Если не поменять, оплату всё
   равно подхватит сверка в тике, но с задержкой до 5 минут.

## Вернуться на Yandex

Когда `https://bba42jg9t25oajtl255q.containers.yandexcloud.net/health`
снова отвечает:

1. Вернуть адреса ВК и ЮКассы на Yandex.
2. `sudo rm /etc/cron.d/teashop-bot` и `docker compose down` — иначе тик
   будет идти дважды: и здесь, и таймером Yandex.

Пока бот работает здесь, а Yandex ожил, таймерные триггеры Yandex тоже
стучатся в свой контейнер с той же базой. Отметки «одно событие — одно
сообщение» не дадут задвоить сообщения клиентам, но лучше не держать оба
расписания дольше, чем нужно на переключение.
