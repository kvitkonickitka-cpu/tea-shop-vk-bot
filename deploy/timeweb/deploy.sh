#!/bin/bash
# Деплой бота на ВМ Timeweb из main.
#
# Запускает кнопка «Deploy to Timeweb» в GitHub Actions: она заходит по SSH
# ключом, которому в authorized_keys разрешена только эта команда (копия
# скрипта — /usr/local/sbin/teashop-deploy, ставится руками, см. README).
# Можно запустить и на ВМ: sudo teashop-deploy.
#
# Порядок: копия базы → main из GitHub → сборка и перезапуск бота → /health.
# Не собралось, не поднялось или бот не ответил с базой за 90 с — откат на
# прежний коммит.
set -euo pipefail
exec 9>/run/teashop-deploy.lock
flock -n 9 || { echo "Деплой уже идёт — дождитесь его конца."; exit 1; }

REPO=/opt/tea-shop-vk-bot
D=$REPO/deploy/timeweb
BRANCH=main
TS=$(date -u +%Y%m%dT%H%M%SZ)

healthy() {
  for _ in $(seq 1 45); do
    curl -fsS -m 5 http://127.0.0.1:8080/health 2>/dev/null | grep -q '"database":"ok"' && return 0
    sleep 2
  done
  return 1
}

# Собрать и поднять то, что сейчас лежит в рабочей копии.
# $1 — менялся ли Caddyfile: он смонтирован в контейнер файлом, и после
# git checkout (новый inode) Caddy видит старую версию, пока контейнер не пересоздан.
#
# Каждый шаг проверяется явно: функцию вызывают под `if`, а там `set -e` не
# действует. Раньше она шла без проверок, и при упавшей сборке скрипт выходил
# сразу, без отката: рабочая копия оставалась на неудачном коммите, а
# следующий деплой брал его за «прежний» и откатывался бы на него же.
up() {
  sed -i "s/^APP_REVISION=.*/APP_REVISION=$(git -C "$REPO" rev-parse --short=12 HEAD)/" "$D/.env.bot" || return 1
  cd "$D" || return 1
  docker compose build -q bot || return 1
  # Все сервисы, а не только бот: compose пересоздаёт лишь те, у которых
  # поменялась конфигурация (логи, лимиты в docker-compose.yml).
  docker compose up -d || return 1
  if [ "$1" = 1 ]; then docker compose up -d --force-recreate caddy || return 1; fi
  cmp -s "$D/teashop-bot.cron" /etc/cron.d/teashop-bot || cp "$D/teashop-bot.cron" /etc/cron.d/teashop-bot || return 1
  if ! cmp -s "$D/metabase/docker-compose.yml" /opt/metabase/docker-compose.yml; then
    cp "$D/metabase/docker-compose.yml" /opt/metabase/docker-compose.yml || return 1
    (cd /opt/metabase && docker compose up -d) || return 1
  fi
}

cd "$REPO"
OLD=$(git rev-parse HEAD)
git fetch -q origin "$BRANCH"
NEW=$(git rev-parse FETCH_HEAD)
echo "Было: $(git log -1 --format='%h %s' "$OLD")"
echo "Будет: $(git log -1 --format='%h %s' "$NEW")"

mkdir -p /root/backups
# Сначала во временный файл: упавший pg_dump не должен оставить обрывок под
# видом нормальной копии.
docker exec teashop-postgres pg_dump -U teashop -d teashop -Fc > "/root/backups/.teashop_predeploy_$TS.part"
mv "/root/backups/.teashop_predeploy_$TS.part" "/root/backups/teashop_predeploy_$TS.dump"
echo "Копия базы: /root/backups/teashop_predeploy_$TS.dump"
# Храним 7 последних копий перед деплоем.
ls -1t /root/backups/teashop_predeploy_*.dump | tail -n +8 | xargs -r rm -f

CADDY=0
git diff --quiet "$OLD" "$NEW" -- deploy/timeweb/Caddyfile || CADDY=1
git checkout -q -f -B "$BRANCH" "$NEW"

if up "$CADDY" && healthy; then
  echo "Готово: $(curl -fsS -m 5 http://127.0.0.1:8080/health | cut -c1-120)"
  exit 0
fi

echo "!!! Новая версия не собралась, не поднялась или не ответила с базой за 90 с — откатываюсь на $(git -C "$REPO" rev-parse --short "$OLD")"
docker compose -f "$D/docker-compose.yml" logs --tail 40 bot || true
cd "$REPO"
git checkout -q -f -B "$BRANCH" "$OLD"
if up "$CADDY" && healthy; then
  echo "Откат удался, бот работает на прежней версии."
else
  echo "!!! И после отката бот не отвечает — нужен человек: docker compose -f $D/docker-compose.yml logs bot"
fi
exit 1
