#!/bin/bash
# Внешний сторож бота: живёт на VPS Aeza (другой провайдер, чем ВМ Timeweb)
# и раз в минуту спрашивает /health бота снаружи — через HTTPS, Caddy и DNS,
# то есть тем же путём, каким бота находит ВК.
#
# Нужен потому, что сам бот сообщает только о сбоях, пока жив: если лягут ВМ,
# Docker или Caddy, в Ops тишина. В Yandex это делал алерт Monitoring «нет
# пульса», на Timeweb метрики туда не уходят.
#
# Тревога — после FAILS_TO_ALERT неудач подряд (одиночный сбой сети — не
# поломка), повтор раз в REPEAT_MINUTES, пока не починится; починилось — 🟢.
# База недоступна, но бот отвечает — не наше: об этом пишет сам бот.
#
# Ставится по deploy/watchdog/README.md. Токен и чат — в /etc/teashop-watchdog.env.
set -u

ENV_FILE=/etc/teashop-watchdog.env
STATE=${WATCHDOG_STATE:-/var/lib/teashop-watchdog}
URL=${WATCHDOG_URL:-https://201-24-120-144.sslip.io/health}
FAILS_TO_ALERT=3
REPEAT_MINUTES=60

# shellcheck disable=SC1090
. "$ENV_FILE"
mkdir -p "$STATE"

send() {
  # Токен не в командной строке: так его не видно в списке процессов.
  curl -fsS -m 20 -o /dev/null -K - --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" <<CFG
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
CFG
}

now=$(date +%s)
fails=$(cat "$STATE/fails" 2>/dev/null || echo 0)
alerted_at=$(cat "$STATE/alerted_at" 2>/dev/null || echo 0)

code=$(curl -s -m 20 -o "$STATE/last_body" -w '%{http_code}' "$URL" 2>/dev/null) || code=000

if [ "$code" = 200 ]; then
  if [ "$alerted_at" != 0 ]; then
    down_min=$(( (now - $(cat "$STATE/first_fail_at" 2>/dev/null || echo "$now")) / 60 ))
    send "🟢 Бот снова отвечает (не отвечал ~${down_min} мин). ${URL}" || true
  fi
  echo 0 > "$STATE/fails"; echo 0 > "$STATE/alerted_at"; rm -f "$STATE/first_fail_at"
  exit 0
fi

[ "$fails" = 0 ] && echo "$now" > "$STATE/first_fail_at"
fails=$((fails + 1)); echo "$fails" > "$STATE/fails"

if [ "$fails" -ge "$FAILS_TO_ALERT" ] && { [ "$alerted_at" = 0 ] || [ $((now - alerted_at)) -ge $((REPEAT_MINUTES * 60)) ]; }; then
  if [ "$code" = 000 ]; then what="не отвечает (нет соединения или таймаут 20 с)"; else what="отвечает HTTP ${code}"; fi
  if send "🔴 Бот ${what} уже ${fails} мин подряд. ВМ Timeweb 201.24.120.144: docker ps, docker compose -f /opt/tea-shop-vk-bot/deploy/timeweb/docker-compose.yml logs --tail 50 bot. Runbook: docs/monitoring.md"; then
    echo "$now" > "$STATE/alerted_at"
  fi
fi
exit 0
