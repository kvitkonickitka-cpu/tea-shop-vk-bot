#!/bin/bash
# Postgres для бота на ВМ Timeweb: контейнер teashop-postgres, данные в томе pgdata.
# Порт 5432 публикуется только на 172.17.0.1 (мост docker0): бот и Metabase ходят
# через host.docker.internal, снаружи базы не видно. Правило ufw тут не помогло
# бы — Docker публикует порты в обход ufw. Пускает только по SSL (pg_hba).
# Пароль владельца — в /root/.pg_env (POSTGRES_PASSWORD=…), в git не кладётся.
# Повторный запуск пересоздаёт контейнер, данные в томе остаются.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
log(){ echo "[$(date +%H:%M:%S)] $*"; }
log "swap"; if ! swapon --show | grep -q swapfile; then fallocate -l 1G /swapfile; chmod 600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile; grep -q swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi
log "docker"; apt-get update -qq; apt-get install -y -qq docker.io ufw openssl >/dev/null; systemctl enable --now docker
log "tls"; mkdir -p /opt/pg/certs; cd /opt/pg/certs
[ -f server.key ] || openssl req -new -x509 -days 3650 -nodes -subj "/CN=teashop-db" -keyout server.key -out server.crt 2>/dev/null
chown 999:999 server.key server.crt; chmod 600 server.key; chmod 644 server.crt
cat > /opt/pg/pg_hba.conf <<'H'
local all all trust
hostssl all all 0.0.0.0/0 scram-sha-256
hostssl all all ::/0 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::/0 reject
H
chown 999:999 /opt/pg/pg_hba.conf
log "postgres"; docker rm -f teashop-postgres >/dev/null 2>&1 || true
# Лимит логов и приоритет при нехватке памяти — как у бота в docker-compose.yml.
docker run -d --name teashop-postgres --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 --oom-score-adj=-500 \
  -e POSTGRES_USER=teashop -e POSTGRES_DB=teashop --env-file /root/.pg_env \
  -p 172.17.0.1:5432:5432 -v pgdata:/var/lib/postgresql \
  -v /opt/pg/certs:/certs:ro -v /opt/pg/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro \
  postgres:18 -c ssl=on -c ssl_cert_file=/certs/server.crt -c ssl_key_file=/certs/server.key \
  -c hba_file=/etc/postgresql/pg_hba.conf -c shared_buffers=256MB -c max_connections=60 -c password_encryption=scram-sha-256 >/dev/null
for i in $(seq 1 30); do docker exec teashop-postgres pg_isready -U teashop -d teashop >/dev/null 2>&1 && break; sleep 2; done
docker exec teashop-postgres pg_isready -U teashop -d teashop
log "ufw"; ufw allow 22/tcp >/dev/null; ufw --force enable >/dev/null; ufw status | head -6
log "done"
