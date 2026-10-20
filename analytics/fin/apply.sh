#!/usr/bin/env bash
# Применить миграции схемы fin, которых ещё нет в fin.schema_migrations,
# и обновить права (grants.sql). Каждая миграция — одной транзакцией.
#
#   analytics/fin/apply.sh --dry-run   # что будет применено, ничего не меняет
#   analytics/fin/apply.sh             # применить
#
# База — через PSQL (по умолчанию контейнер teashop-postgres на ВМ). На
# копию: PSQL="docker exec -i fin-copy psql -U teashop -d teashop" apply.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PSQL="${PSQL:-docker exec -i teashop-postgres psql -U teashop -d teashop}"
DRY="${1:-}"

applied="$($PSQL -At -c "select version from fin.schema_migrations" 2>/dev/null || true)"
pending=()
for f in "$HERE"/migrations/*.sql; do
  v="$(basename "$f" .sql)"
  grep -qx "$v" <<<"$applied" || pending+=("$f")
done

if [ ${#pending[@]} -eq 0 ]; then echo "Новых миграций нет"; else printf 'К применению: %s\n' "${pending[@]##*/}"; fi
[ "$DRY" = "--dry-run" ] && exit 0

for f in "${pending[@]}"; do
  echo "→ ${f##*/}"
  $PSQL -v ON_ERROR_STOP=1 -q -1 -f - < "$f"
done
if $PSQL -At -c "select 1 from pg_roles where rolname = 'metabase_fin'" | grep -q 1; then
  $PSQL -v ON_ERROR_STOP=1 -q -f - < "$HERE/grants.sql" && echo "Права обновлены"
else
  echo "Роли metabase_fin нет — права не выданы (сначала roles.sql)"
fi
