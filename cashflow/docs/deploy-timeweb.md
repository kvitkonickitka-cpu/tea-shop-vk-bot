# cashflow на ВМ Timeweb

С 13.10.2026 cashflow работает на ВМ Timeweb `teashop-db` (`201.24.120.144`),
рядом с ботом и Postgres. Прежняя ВМ в Yandex Cloud недоступна вместе с её
диском: история выписки загружена заново из T-API, правила и ручная разметка —
из `data/` этого каталога. Порядок установки — как в [deploy.md](deploy.md),
ниже только отличия.

## Чем отличается от deploy.md

| deploy.md | Timeweb |
|---|---|
| Сертификаты Минцифры ставятся в системное хранилище | Только для cashflow: `/etc/cashflow/ca-bundle.pem` (системный набор + Russian Trusted Root/Sub CA), на него указывают `REQUESTS_CA_BUNDLE` и `SSL_CERT_FILE` в `.env`. Системное хранилище ВМ не тронуто |
| `PGHOST=127.0.0.1` | `PGHOST=172.17.0.1` и `PGSSLMODE=require`: Postgres в Docker слушает только адрес моста `docker0`, без SSL не пускает |
| Миграция 001 создаёт `datalens_ro`, 007 — `finance_datalens_ro` | DataLens больше нет. 001 применена, `datalens_ro` сразу удалена (`drop owned by`, `drop role`); 007 отмечена применённой без выполнения. Витрины читает роль Metabase `metabase_ro` — её права выдаются в репозитории бота (`analytics/metabase_roles.sql`) |
| Песочница, потом прод | Сразу прод: у токена только «Счета и выписки», ошибиться в чтении нельзя |
| Статический IP Yandex | Токен привязан к IP `201.24.120.144`. Сменится IP ВМ — выпустить токен заново |

Токен, счета и дату первого дня владелец вписывает сам скриптом
`/root/cashflow_set_token.sh` на ВМ — значения на экран не выводятся и в
переписку не попадают.

## Где что лежит

- код — `/opt/cashflow` (распакован из этой ветки), окружение — `/opt/cashflow/.venv`;
- настройки и секреты — `/etc/cashflow/.env` (600, владелец `cashflow`);
- логи — `/var/log/cashflow/`, копии схемы `finance` — `/var/backups/cashflow/`;
- таймеры `cashflow.timer` (07:00 МСК) и `cashflow-backup.timer` (06:30 МСК);
- отметки для ежедневного отчёта Ops — `ops-heartbeat@.service` и
  `*.service.d/ops-heartbeat.conf` из `ops/vm/` репозитория бота. Задача,
  не отработавшая больше 26 часов, видна в отчёте как «давно не было».

## Правила и ручная разметка

Правят `data/rules.csv` и `data/manual_overrides.csv` здесь, в git, потом
переносят на ВМ в `/opt/cashflow/data/` и запускают:

```bash
cd /opt/cashflow
sudo -u cashflow .venv/bin/python -m cashflow import-rules
sudo -u cashflow .venv/bin/python -m cashflow classify
```

**Запятая в комментарии ломает CSV**: строка получает лишний столбец, и
`import-rules` падает целиком (справочники в базе остаются прежними). В
комментариях — без запятых.
