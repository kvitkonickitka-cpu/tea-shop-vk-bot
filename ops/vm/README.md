# Файлы для ВМ с базой

Ставятся только по шагу 5 из `docs/monitoring.md`, с резервной копией и
согласия владельца. Ни один из них не меняет существующие юниты: это
отдельный шаблон `ops-heartbeat@.service` и дополнения в `*.service.d/`.

    sudo cp ops-heartbeat@.service /etc/systemd/system/
    sudo mkdir -p /etc/systemd/system/cashflow.service.d /etc/systemd/system/cashflow-backup.service.d
    sudo cp cashflow.service.d/ops-heartbeat.conf /etc/systemd/system/cashflow.service.d/
    sudo cp cashflow-backup.service.d/ops-heartbeat.conf /etc/systemd/system/cashflow-backup.service.d/
    sudo systemctl daemon-reload
    # проверка: отметка появилась — и тут же убираем её, чтобы не висела в отчёте
    sudo systemctl start ops-heartbeat@selftest.service
    docker exec teashop-postgres psql -U teashop -d teashop -c "select * from heartbeats where name = 'selftest'"
    docker exec teashop-postgres psql -U teashop -d teashop -c "delete from heartbeats where name = 'selftest'"

Откат: удалить три файла и `sudo systemctl daemon-reload`.
