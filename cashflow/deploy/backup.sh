#!/usr/bin/env bash
# Резервная копия ТОЛЬКО схемы finance. Данных клиентов не касается.
# Снимки диска ВМ включаются отдельно в консоли Yandex Cloud — они защищают всю машину.
set -euo pipefail

ENV_FILE="${CASHFLOW_ENV_FILE:-/etc/cashflow/.env}"
BACKUP_DIR="${CASHFLOW_BACKUP_DIR:-/var/backups/cashflow}"
KEEP_DAYS="${CASHFLOW_BACKUP_KEEP_DAYS:-30}"

if [[ ! -r "$ENV_FILE" ]]; then
    echo "Не читается $ENV_FILE" >&2
    exit 1
fi

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M)"
target="$BACKUP_DIR/finance-$stamp.sql.gz"

PGPASSWORD="$PGPASSWORD" pg_dump \
    --host "$PGHOST" --port "${PGPORT:-5432}" \
    --username "$PGUSER" --dbname "$PGDATABASE" \
    --schema finance --no-owner --no-privileges \
    | gzip > "$target"

chmod 600 "$target"
echo "$(date --iso-8601=seconds) копия готова: $target ($(du -h "$target" | cut -f1))"

# Старые копии удаляем, чтобы не забить диск.
find "$BACKUP_DIR" -name 'finance-*.sql.gz' -mtime "+$KEEP_DAYS" -delete
