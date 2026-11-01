# Бот на своей ВМ (Timeweb)

**С 13.10.2026 это основной и единственный путь**: ВМ `teashop-db`
(Москва MSK-1, `201.24.120.144`, 1 CPU / 4 ГБ — память
поддержка Timeweb увеличила 15.10.2026, ядро пока одно). Здесь же Postgres, Metabase и cashflow. Yandex остановлен —
раздел «Yandex остановлен» ниже.

Появился как запасной путь, когда Yandex Serverless Containers не принимал
запросы (11.10.2026: шлюз `*.containers.yandexcloud.net` рвал соединение на
TLS, бот молчал). Бот тот же, из этого репозитория; отличия от Yandex:

| Yandex | Здесь |
|---|---|
| Serverless Container | `docker compose`: контейнер бота + Caddy (HTTPS) |
| Очередь YMQ и склейка сообщений | своя очередь в Postgres (`queued_events`, с 15.10.2026): вебхук отвечает ВК сразу, ход в фоне, склейка работает |
| Таймерные триггеры `dialog-reports`, `ops-pulse` | `cron`: тик раз в 5 минут, пульс раз в минуту |
| Метрики в Yandex Monitoring | не пишутся; «бот не работает» сообщает внешний сторож на Aeza (`deploy/watchdog`) — это ожидаемо |
| Секреты в GitHub → ревизия | файл `.env.bot` на ВМ (chmod 600, в git не кладётся) |

OpenAI и Telegram ходят через прокси на Aeza — от Yandex они не зависят.

## Текущая конфигурация и демо

[Снимок на 01.11.2026](../../docs/current-state.md) отделяет рабочие настройки
от возможностей кода. Публичное демо включено флагом INVESTOR_DEMO_ENABLED;
[правила и быстрый откат](../../docs/investor-demo.md). Не сбрасывать TEST_VK_IDS.
Откат демо — выключение флага, не удаление заказов и не смена PII-ключа.

## Запуск

```
git clone https://github.com/kvitkonickitka-cpu/tea-shop-vk-bot.git /opt/tea-shop-vk-bot
cd /opt/tea-shop-vk-bot/deploy/timeweb
cp env.example .env.bot && chmod 600 .env.bot      # заполнить значения
docker compose up -d --build
sudo install -d -m 700 /etc/teashop
sudo sh -c 'grep ^INTERNAL_API_TOKEN= .env.bot | cut -d= -f2- > /etc/teashop/internal_token'
sudo chmod 600 /etc/teashop/internal_token
sudo cp teashop-bot.cron /etc/cron.d/teashop-bot
```

В `.env.bot`:
- `DATABASE_URL` — Postgres на этой же ВМ: хост `host.docker.internal`,
  `?ssl=require`;
- `PUBLIC_BASE_URL` — `https://bot.teapotwice.ru` (ссылки на страницу сборки);
- `YOOKASSA_RETURN_URL` — `https://bot.teapotwice.ru/payments/yookassa`: сюда
  браузер клиента возвращается после оплаты. Адрес Yandex здесь — мёртвая
  страница после оплаты;
- остальное — те же значения, что в секретах GitHub.

## Деплой кнопкой

До запуска: локальные тесты → PR с зелёными обязательными проверками → merge →
зелёные тесты main. После: сверить точную revision и database=ok в `/health`,
контейнеры, свежие логи и функции. При сбое дальнейший выпуск остановить.


Actions → **Deploy to Timeweb** → Run workflow. Выкатывается `main`:
`deploy.sh` делает копию базы (`/root/backups/teashop_predeploy_*.dump`, 7
последних), забирает `main`, собирает и перезапускает бота, пересоздаёт Caddy,
если менялся `Caddyfile`, обновляет cron и Metabase, если менялись их файлы, и
ждёт `/health` с `"database": "ok"`. Не собралось, не поднялось или нет
ответа за 90 с — откат на прежний коммит, кнопка краснеет. Вручную на ВМ:
`sudo teashop-deploy`.

Ключ кнопки — секрет репозитория `TIMEWEB_DEPLOY_KEY`. Его публичная часть в
`/root/.ssh/authorized_keys` ограничена одной командой:

```
restrict,command="/usr/local/sbin/teashop-deploy" ssh-ed25519 AAAA… github-actions-deploy
```

`/usr/local/sbin/teashop-deploy` — копия `deploy.sh`, ставится руками
(`install -m 750 deploy.sh /usr/local/sbin/teashop-deploy`): так коммит в
репозиторий не меняет то, что выполняется по ключу. Поменяли `deploy.sh` —
переустановить копию.

## Память, логи, копии

**Память — 4 ГБ на всё** (бот, Postgres, Caddy, Metabase, cashflow), swap 1 ГБ.
Лимиты: бот 1 ГБ (обычно ест ~200 МБ; больше — утечка, лучше перезапуск
контейнера, чем голодная база), Metabase 1800 МБ. Кто
первым идёт под нож при нехватке, решает `oom_score_adj`: Metabase `800`,
бот, Caddy и Postgres `-500`. Тот же смысл у `vm.swappiness=10`
(`/etc/sysctl.d/99-teashop.conf`): ядро сначала отдаёт кэш, а не
выталкивает в swap бота, которому на ответ ВК даёт 8 секунд.

**Логи Docker ротируются**: 10 МБ × 5 файлов у бота, Caddy и Postgres, 10 МБ
× 3 у Metabase (`logging` в compose-файлах, `--log-opt` в `setup_pg.sh`). По
умолчанию Docker журнал не режет, и пульс раз в минуту вместе со сканерами
рано или поздно съел бы диск. У Postgres лимит вступает в силу при следующем
пересоздании контейнера (`setup_pg.sh`).

**Копии базы**: перед каждым деплоем (`teashop_predeploy_*`, 7 последних) и
каждую ночь в 23:45 МСК (`teashop_nightly_*`, 7 последних, строка в
`teashop-bot.cron`) — в `/root/backups`. Это тот же диск; наружу их уносят
бэкапы сервера в панели Timeweb (ежедневно с 00:15 МСК, поэтому копия —
за полчаса до них), поэтому они должны быть включены. На них одних не
полагаемся: 13–14.10.2026 они не делались из-за сбоя у Timeweb, поэтому есть
ещё зашифрованная копия в S3 в другом дата-центре — «Копия вне ВМ (S3)».

## Postgres

`postgres/setup_pg.sh` — контейнер `teashop-postgres` (Postgres 18, том
`pgdata`, SSL обязателен). Порт опубликован только на `172.17.0.1` (мост
`docker0`): снаружи базы не видно, бот и Metabase ходят через
`host.docker.internal`. **Закрывать порт правилом `ufw` бесполезно**: Docker
публикует порты в обход ufw — только адресом в `-p`. Копия перед правками:
`docker exec teashop-postgres pg_dump -U teashop -d teashop -Fc > /root/backups/…`.

## Metabase

Дашборды — `https://stats.teapotwice.ru` (блок в `Caddyfile`), контейнер из
`metabase/docker-compose.yml` в `/opt/metabase`, свои настройки хранит в базе
`metabase`. Лимит контейнера 1800 МБ и куча Java 1 ГБ (до 15.10.2026, при 2 ГБ на ВМ, —
1200 МБ и 512 МБ: тяжёлые вкладки дашборда упирались в кучу):
при нехватке система останавливает Metabase, а не бота и базу. **Версия
закреплена** (`v0.64.2` с digest): `latest` при пересоздании подтянул бы новую
мажорную версию, а Metabase мигрирует свою базу без пути назад. Обновлять —
сменить тег, сделать копию базы `metabase`, задеплоить. С
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

**Вход — только через Google** (с 15.10.2026): проект Google Cloud
`teapotwice-metabase` (аккаунт nikitakvitkochina@gmail.com), OAuth-клиент
«Metabase» с источником `https://stats.teapotwice.ru`. Приложение в режиме
«Testing» — войти может только почта из списка Audience → Test users, новому
сотруднику её туда добавить (и завести в Metabase на ту же почту). Пароль
бесплатный Metabase отключать не умеет, поэтому вход паролем, сброс и «забыл
пароль» режет `Caddyfile` (403). **Аварийный вход паролем** — в обход Caddy:

```
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' metabase-metabase-1   # на ВМ
ssh -L 3000:<этот IP>:3000 root@201.24.120.144                                                       # с ноутбука
```

и открыть `http://localhost:3000`.

## Копия вне ВМ (S3)

Каждую ночь в 05:15 МСК `teashop-offsite-backup.timer` кладёт в бакет
`teashop-offsite-backups` (S3 Timeweb, Санкт-Петербург — другой дата-центр,
приватный, 10 ГБ) один архив: дамп базы бота (с `analytics` и `finance`), дамп
базы `metabase` и настройки ВМ (`.env` бота с `PII_ENCRYPTION_KEY`, `.env`
Metabase и cashflow, `/etc/teashop`, пароль Postgres, сертификаты, cron,
таймеры). По нему ВМ поднимается с нуля. `daily/` хранится 30 дней,
`monthly/` (копия 1-го числа) — 400 дней. Отметка `offsite-backup` в
`heartbeats` — в ежедневном отчёте Ops.

Архив зашифрован открытым ключом age (`/etc/teashop/offsite_age.pub`).
**Закрытый ключ — только в менеджере паролей владельца**, на ВМ его нет:
без него копии не открыть. Ключи S3 — пользователь бакета `teashop-backup`
(чтение и запись), в `/etc/teashop/s3.env`; вписывает владелец
`offsite/offsite_set_s3.sh`. У такого пользователя нет прав проверять или
создавать бакет, поэтому rclone идёт с `NO_CHECK_BUCKET=true`, иначе запись —
`AccessDenied`.

Установка: `offsite/` → `/usr/local/sbin/teashop-offsite-backup` и
`/etc/systemd/system/`, `apt install rclone age`, `systemctl enable --now
teashop-offsite-backup.timer`. Запустить сейчас: `systemctl start
teashop-offsite-backup.service`, лог — `/var/log/teashop-offsite-backup.log`.

Восстановить (ключ age — в файл `key.txt`):

```
rclone copyto offsite:teashop-offsite-backups/daily/teashop-<дата>.tar.age b.age
age -d -i key.txt b.age | tar -xf -        # teashop.dump, metabase.dump, config.tar.gz
tar -C / -xzf config.tar.gz                # настройки на место
docker exec -i teashop-postgres pg_restore -U teashop -d teashop --clean --if-exists < teashop.dump
```

## Yandex остановлен

С 13.10.2026: у контейнера `tea-shop-vk-bot` закрыт публичный вызов и стоит
пустая ревизия `bbaf4t4cti6c985ed4s6` (без секретов, `min-instances 0`);
рабочая — `bballgouqka68btvgutr`. Таймерных триггеров нет. Старая ВМ с базой
`epd61hgvour5kh6dc2tg` остановлена, Yandex не даёт её запустить
(`Resource allocation is restricted`). Workflow деплоя в Yandex
(`deploy-yandex.yml`) удалён 14.10.2026, в истории git он есть.

Порты 80 и 443 должны быть открыты в файрволе Timeweb: на 80 Caddy получает
сертификат.

Проверка: `curl https://bot.teapotwice.ru/health` — `"database": "ok"`; через
5 минут в «по расписанию» появится отметка тика.

## Доступ к ВМ и защита

Снаружи открыты только 22, 80 и 443 (облачный файрвол Timeweb; проверено с
Aeza 16.10.2026: 5432, 8080, 10050 закрыты). Postgres слушает только
`172.17.0.1`.

- **SSH — только по ключу**: `/etc/ssh/sshd_config.d/10-teashop-hardening.conf`
  (`PermitRootLogin prohibit-password`, пароль и keyboard-interactive выключены).
  Ключи в `/root/.ssh/authorized_keys`: `tea-shop-vk-bot-vm` (наш), `github-actions-deploy`
  (только `teashop-deploy`, forced command). Ключ `otp_vds@20251027111519` 14.10.2026
  дописал гипервизор Timeweb (`qemu-guest-agent`, `guest-exec`) во время обращения в
  поддержку; входов им не было, 16.10.2026 он удалён (копия файла —
  `/root/authorized_keys.bak-*`). Поддержка Timeweb может выполнять команды на ВМ через
  гостевой агент и без ключа и при новом обращении допишет ключ снова — это свойство
  хостинга; `qemu-guest-agent` и `zabbix-agent-timeweb` не трогаем.
- **fail2ban** (`/etc/fail2ban/jail.d/teashop.local`): только SSH, читает
  `/var/log/auth.log`; 6 неудач за 10 минут — бан на час, повторно до суток.
  Не банит Timeweb (`92.53.116.0/24`) и Aeza (`94.228.170.170`). Через журнал systemd
  с уточнённым `journalmatch` новые записи не ловились — не возвращать.
  Себя разбанить: `fail2ban-client set sshd unbanip <адрес>` (из консоли Timeweb,
  если SSH закрыт). Посмотреть: `fail2ban-client status sshd`.
- **Агент Zabbix** (`zabbix-agent-timeweb`, порт 10050) — графики загрузки в панели
  Timeweb, отвечает только серверам Timeweb, снаружи порт закрыт.
- Боты перебирают пароли постоянно (≈1500 попыток в сутки) — это фон; вход по
  паролю выключен, подобрать нечего.

## Адреса: teapotwice.ru

С 15.10.2026 у магазина свой домен `teapotwice.ru` (Timeweb, автопродление
включено). A-записи `teapotwice.ru`, `bot.` и `stats.` в панели Timeweb
привязаны к ВМ `teashop-db`, а не вписаны IP-адресом: сменится IP — записи
переедут сами. `bot.teapotwice.ru` — бот (ВК Callback API, уведомления
ЮKassa, `PUBLIC_BASE_URL`, `YOOKASSA_RETURN_URL`), `stats.teapotwice.ru` —
Metabase. Сертификаты Let's Encrypt получает и продлевает Caddy; платный
SSL Timeweb не нужен. Прежние `*.sslip.io` убраны 15.10.2026, вместе с
монитором Timeweb на них.

**Строка подтверждения ВК меняется.** При смене адреса сервера в Callback
API ВК шлёт `confirmation` и ждёт строку со страницы «Работа с API» —
она должна совпадать с `VK_CONFIRMATION_TOKEN` в `.env.bot`. 15.10.2026 в
`.env.bot` лежала старая, и ВК ответил «Сервер вернул неправильный ответ»;
до подтверждения новый адрес событий не получает. Править `.env.bot` и
`docker compose up -d bot`, потом снова «Подтвердить».

## Переключить трафик

1. **ВК**: сообщество → Управление → Работа с API → Callback API → адрес
   `https://bot.teapotwice.ru/vk/callback`, «Подтвердить» (строка подтверждения —
   `VK_CONFIRMATION_TOKEN`, секретный ключ — `VK_SECRET_KEY`, те же).
2. **ЮKassa**: личный кабинет → Интеграция → HTTP-уведомления →
   `https://bot.teapotwice.ru/payments/yookassa`. Если не поменять, оплату всё
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

## Защищённые ссылки на диалоги (31.10.2026)

Новые Telegram-карточки, ответы Ops и повторно отправляемые сообщения очереди
получают ссылки вида `https://stats.teapotwice.ru/secure-dialog/<opaque-token>`.
В них нет открытого VK ID: токен зашифрован AES-GCM существующим
`PII_ENCRYPTION_KEY` с отдельным контекстом. Ключ не уходит в URL или в Metabase.
Смена этого ключа делает старые защищённые ссылки недействительными (так же,
как затрагивает хранилище ПДн); копию ключа необходимо сохранять.

Переход работает так: Caddy направляет `/secure-dialog/*` в бота. Бот проверяет
cookie `metabase.SESSION` через внутренний `http://metabase:3000/api/user/current`.
Только **активный администратор Metabase** получает HTTP 303 на диалог VK.
Права проверяются на каждом переходе; API-ключи и переданные клиентом заголовки
с ролью для этого не принимаются. Выдача прав администратора Metabase теперь
также даёт доступ к ссылкам на клиентские диалоги.

Без сессии открывается страница входа через уже настроенный Google-клиент
Metabase. Она создаёт обычную сессию через `/api/session/google_auth` на том же
домене и перезагружается — после входа пользователь сразу попадает в VK.
Если сессия уже есть, повторный вход не нужен. Во встроенном браузере Telegram
Google может потребовать открыть страницу во внешнем браузере; это указано
на странице входа. Пароль и новый OAuth-клиент не нужны.

Telegram-превью не имеет сессии и не получает адрес VK. Ответы не кэшируются,
`Referrer-Policy: no-referrer` не передаёт токен в Referer. При недоступном
Metabase раскрытие ссылки блокируется (503). При отсутствии ключа шифрования
карточка не отправляется с открытым ID, а остаётся на повторную доставку.
Уже отправленные ранее сообщения Telegram этим выпуском не редактируются.

Настройки контейнера: `DIALOG_LINKS_ENABLED=true`,
`DIALOG_LINKS_ORIGIN=https://stats.teapotwice.ru`,
`DIALOG_LINKS_METABASE_URL=http://metabase:3000`. Они заданы в compose;
по умолчанию вне Timeweb функция выключена.

Проверки перед выпуском: `tests/test_protected_dialog_links.py`, полный pytest
на PostgreSQL с UTF-8 локалью, CI PR и main. После выпуска — `/health` с точной
ревизией и `database=ok`, Caddy/логи, анонимный запрос к защищённой ссылке без
раскрытия адреса VK. Старые карточки с командой, содержащей VK ID, при повторной
отправке получают `[клиент]` вместо ID; для работы с БД идентификатор берётся
из диалога после авторизации, а не из Telegram.
