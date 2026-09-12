# Установка на виртуальную машину

Инструкция рассчитана на то, что вы не программист. Команды можно копировать целиком.
Всё, что вы сделаете, находится в отдельной схеме `finance` и не касается данных
магазина и клиентов.

Перед началом убедитесь, что у вас есть:

- доступ к ВМ по SSH;
- пароль пользователя `postgres` или другой доступ суперпользователя к базе;
- доступ в личный кабинет Т-Бизнеса (чтобы выпустить токен);
- доступ в консоль Yandex Cloud.

---

## Шаг 1. Безопасность: сначала копии

Пока ничего не меняли — сделайте так, чтобы любую ошибку можно было откатить.

1. **Снимки диска ВМ.** В консоли Yandex Cloud: Compute Cloud → ваша ВМ → Снимки →
   расписание снимков. Поставьте ежедневные снимки и хранение хотя бы 7 дней.
   Это защищает и данные клиентов тоже.
2. **Копия базы.** На ВМ:

   ```bash
   sudo -u postgres pg_dump ИМЯ_БАЗЫ | gzip > ~/before-cashflow-$(date +%F).sql.gz
   ```

## Шаг 2. Статический IP

Токен Т-Банка привязывается к IP-адресу. Если адрес ВМ динамический, после
перезагрузки он поменяется и банк перестанет отвечать.

В консоли Yandex Cloud: Virtual Private Cloud → IP-адреса. Найдите адрес вашей ВМ.
Если в колонке «Тип» написано «Эфемерный» — нажмите «Сделать статическим».

Узнать текущий адрес машины можно прямо на ВМ:

```bash
curl -s -H "Metadata-Flavor: Google" \
  http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip
```

Запишите этот адрес — он понадобится при выпуске токена.

## Шаг 3. Сертификаты Минцифры

Сайты Т-Банка используют российские сертификаты. Без них Python не сможет проверить
подлинность соединения и будет ругаться на TLS.

```bash
sudo mkdir -p /usr/local/share/ca-certificates/russian_trusted
cd /usr/local/share/ca-certificates/russian_trusted
sudo curl -O https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer
sudo curl -O https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer
sudo openssl x509 -inform DER -in russian_trusted_root_ca.cer -out russian_trusted_root_ca.crt 2>/dev/null \
  || sudo cp russian_trusted_root_ca.cer russian_trusted_root_ca.crt
sudo openssl x509 -inform DER -in russian_trusted_sub_ca.cer -out russian_trusted_sub_ca.crt 2>/dev/null \
  || sudo cp russian_trusted_sub_ca.cer russian_trusted_sub_ca.crt
sudo update-ca-certificates
```

Если адреса файлов изменятся, актуальные ссылки есть на Госуслугах в разделе про
российские сертификаты безопасности. Проверить результат:

```bash
grep -c "Russian Trusted" /etc/ssl/certs/ca-certificates.crt   # должно быть больше нуля
```

## Шаг 4. Пользователь и каталоги

Скрипт работает от отдельного пользователя без права входа в систему — так утечка
одного сервиса не даёт доступа ко всему остальному.

```bash
sudo useradd --system --home /opt/cashflow --shell /usr/sbin/nologin cashflow
sudo mkdir -p /opt/cashflow /etc/cashflow /var/log/cashflow /var/backups/cashflow
sudo chown -R cashflow:cashflow /opt/cashflow /var/log/cashflow /var/backups/cashflow
sudo chown root:cashflow /etc/cashflow && sudo chmod 750 /etc/cashflow
```

## Шаг 5. Код и виртуальное окружение

```bash
sudo -u cashflow git clone ВАШ_РЕПОЗИТОРИЙ /opt/cashflow/repo
sudo -u cashflow cp -r /opt/cashflow/repo/cashflow/. /opt/cashflow/
sudo -u cashflow python3 -m venv /opt/cashflow/.venv
sudo -u cashflow /opt/cashflow/.venv/bin/pip install -e /opt/cashflow
```

Ставим именно через `-e` (режим разработки): так команда `python -m cashflow`
находит каталоги `migrations/` и `data/` рядом с собой.

## Шаг 6. Роли и схема в базе

Эта команда создаёт **новую** схему `finance` и две новые роли. Существующие
таблицы не затрагиваются. Придумайте два разных пароля и сохраните их в менеджере паролей.

```bash
cd /opt/cashflow
sudo -u postgres psql -d ИМЯ_БАЗЫ \
  -v sync_password=ПАРОЛЬ_ДЛЯ_СИНХРОНИЗАЦИИ \
  -v ro_password=ПАРОЛЬ_ДЛЯ_DATALENS \
  -f migrations/001_schema_and_roles.sql
```

Проверьте, что схема появилась и чужого ничего не изменилось:

```bash
sudo -u postgres psql -d ИМЯ_БАЗЫ -c "\dn"
```

## Шаг 7. Файл с секретами

```bash
sudo cp /opt/cashflow/.env.example /etc/cashflow/.env
sudo chown cashflow:cashflow /etc/cashflow/.env
sudo chmod 600 /etc/cashflow/.env
sudo nano /etc/cashflow/.env
```

Заполните: `TBANK_ACCOUNTS` (номера счетов через запятую), `TBANK_FIRST_DAY`
(дата открытия счёта), `PGDATABASE`, `PGPASSWORD` (пароль роли `finance_sync`).
Токен пока оставьте пустым — выпустим на следующем шаге.

## Шаг 8. Токен песочницы и первая проверка

Сначала работаем с песочницей: она возвращает выдуманные операции, так что ошибиться
безопасно. Выпустите токен песочницы в кабинете разработчика Т-Банка и впишите его
в `TBANK_TOKEN`, а `TBANK_API_BASE` оставьте со словом `sandbox`.

```bash
cd /opt/cashflow
sudo -u cashflow .venv/bin/python -m cashflow migrate
sudo -u cashflow .venv/bin/python -m cashflow import-rules
sudo -u cashflow .venv/bin/python -m cashflow doctor
```

Команда `doctor` выведет список проверок. Все строки должны быть `[ ok ]`
или `[ ? ]`. Если есть `[ !! ]` — читайте текст рядом, там написано, что чинить.

## Шаг 9. Боевой токен

Когда песочница отработала:

1. В кабинете Т-Бизнеса выпустите токен с доступом **только «Счета и выписки»**.
   Никаких прав на платежи выдавать нельзя.
2. Укажите в настройках токена статический IP из шага 2.
3. Замените в `/etc/cashflow/.env`: `TBANK_TOKEN` на новый и `TBANK_API_BASE`
   на `https://business.tbank.ru/openapi` (без `sandbox`).
4. Снова `doctor` — он должен написать «прод».

## Шаг 10. Загрузка истории

```bash
cd /opt/cashflow
sudo -u cashflow .venv/bin/python -m cashflow backfill --from 2024-01-01   # дата открытия счёта
sudo -u cashflow .venv/bin/python -m cashflow classify
```

Дальше посмотрите, что не разобралось, и получите черновик правил:

```bash
sudo -u cashflow .venv/bin/python -m cashflow suggest-rules
cat /opt/cashflow/data/suggested_rules.csv
```

Как настраивать правила — в [operations.md](operations.md).

## Шаг 11. Автозапуск

```bash
sudo cp /opt/cashflow/deploy/cashflow.{service,timer} /etc/systemd/system/
sudo cp /opt/cashflow/deploy/cashflow-backup.{service,timer} /etc/systemd/system/
sudo cp /opt/cashflow/deploy/logrotate-cashflow /etc/logrotate.d/cashflow
sudo chmod +x /opt/cashflow/deploy/backup.sh
sudo systemctl daemon-reload
sudo systemctl enable --now cashflow.timer cashflow-backup.timer
systemctl list-timers 'cashflow*'
```

Проверить, как отработал последний запуск:

```bash
systemctl status cashflow.service
tail -50 /var/log/cashflow/cashflow.log
```

## Шаг 12. Проверка, что всё сошлось

```bash
sudo -u postgres psql -d ИМЯ_БАЗЫ -c \
  "SELECT period_start, closing_balance, fcf, check_diff, unclassified_share_amount
     FROM finance.v_cash_summary WHERE period_type='M' ORDER BY period_start;"
```

Что должно получиться:

- `check_diff` равен нулю в каждом месяце;
- `closing_balance` последнего месяца совпадает с остатком в приложении банка;
- `unclassified_share_amount` меньше 0,05 после настройки правил.

Дальше — [подключение DataLens](datalens.md).
