# Бот на своей ВМ (Timeweb)

**С 13.10.2026 это основной и единственный путь**: ВМ `teashop-db`
(Москва MSK-1, `201.24.120.144`, 1 CPU / 2 ГБ — увеличить нельзя, регион
«Распродано»). Здесь же Postgres, Metabase и cashflow. Yandex остановлен —
раздел «Yandex остановлен» ниже.

Появился как запасной путь, когда Yandex Serverless Containers не принимал
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
- `DATABASE_URL` — Postgres на этой же ВМ: хост `host.docker.internal`,
  `?ssl=require`;
- `PUBLIC_BASE_URL` — `https://<BOT_DOMAIN>` (ссылки на страницу сборки);
- `YOOKASSA_RETURN_URL` — `https://<BOT_DOMAIN>/payments/yookassa`: сюда
  браузер клиента возвращается после оплаты. Адрес Yandex здесь — мёртвая
  страница после оплаты;
- остальное — те же значения, что в секретах GitHub.

## Postgres

`postgres/setup_pg.sh` — контейнер `teashop-postgres` (Postgres 18, том
`pgdata`, SSL обязателен). Порт опубликован только на `172.17.0.1` (мост
`docker0`): снаружи базы не видно, бот и Metabase ходят через
`host.docker.internal`. **Закрывать порт правилом `ufw` бесполезно**: Docker
публикует порты в обход ufw — только адресом в `-p`. Копия перед правками:
`docker exec teashop-postgres pg_dump -U teashop -d teashop -Fc > /root/backups/…`.

## Metabase

Дашборды — `https://stats.<BOT_DOMAIN>` (блок в `Caddyfile`), контейнер из
`metabase/docker-compose.yml` в `/opt/metabase`, свои настройки хранит в базе
`metabase`. Памяти мало, поэтому лимит контейнера 1200 МБ и куча Java 512 МБ:
при нехватке система останавливает Metabase, а не бота и базу. С
`-XX:MaxMetaspaceSize=200m` Metabase не стартует (`OutOfMemoryError: Metaspace`).

```
mkdir -p /opt/metabase && cp metabase/docker-compose.yml /opt/metabase/
cp metabase/env.example /opt/metabase/.env && chmod 600 /opt/metabase/.env   # пароль metabase_app
cd /opt/metabase && docker compose up -d
docker exec timeweb-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Роли — `analytics/metabase_roles.sql`, дашборды — `analytics/metabase/`.
`Caddyfile` смонтирован в контейнер файлом: правка через `sed -i` меняет
inode, и Caddy видит старую версию — дописывать или пересоздавать контейнер.

## Yandex остановлен

С 13.10.2026: у контейнера `tea-shop-vk-bot` закрыт публичный вызов и стоит
пустая ревизия `bbaf4t4cti6c985ed4s6` (без секретов, `min-instances 0`);
рабочая — `bballgouqka68btvgutr`. Таймерных триггеров нет. Старая ВМ с базой
`epd61hgvour5kh6dc2tg` остановлена, Yandex не даёт её запустить
(`Resource allocation is restricted`). **Workflow `deploy-yandex` при пуше в
`main` развернёт контейнер снова** — пока его не отключили в Actions, в `main`
не мержить.

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

Сейчас не планируется. Если понадобится — откатить контейнер на рабочую
ревизию (`yc serverless container rollback`), разрешить публичный вызов,
вернуть таймерные триггеры, открыть базу контейнеру (порт сейчас только
внутри ВМ) и:

1. Вернуть адреса ВК и ЮКассы на Yandex.
2. `sudo rm /etc/cron.d/teashop-bot` и `docker compose down` — иначе тик
   будет идти дважды: и здесь, и таймером Yandex.

Пока бот работает здесь, а Yandex ожил, таймерные триггеры Yandex тоже
стучатся в свой контейнер с той же базой. Отметки «одно событие — одно
сообщение» не дадут задвоить сообщения клиентам, но лучше не держать оба
расписания дольше, чем нужно на переключение.
