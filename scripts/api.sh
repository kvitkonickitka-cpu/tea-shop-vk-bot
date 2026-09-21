#!/bin/sh
# Дёрнуть служебный эндпоинт контейнера.
#
# Адрес и токен берутся из .env, а не из переменных окружения: переменные
# живут только до закрытия окна терминала, и запрос в новой вкладке уходил
# в никуда — с -s молча, без единого слова об ошибке. В аргументах токену
# тоже не место: он осел бы в истории команд.
#
#   scripts/api.sh health
#   scripts/api.sh ozon/sync
#   scripts/api.sh 'ozon/quote?city=Уфа&weight=400&value=1500'
#   scripts/api.sh 'ozon/posting?number=0123-0001-1&cancel=1'

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Нет $ENV_FILE — скопируй .env.example и заполни." >&2
    exit 1
fi

# Значение из .env: последнее присваивание, без кавычек и комментария строки.
env_value() {
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

CONTAINER=$(env_value CONTAINER_URL)
TOKEN=$(env_value INTERNAL_API_TOKEN)

if [ -z "$CONTAINER" ]; then
    echo "В .env нет CONTAINER_URL — адрес контейнера из консоли Yandex Cloud." >&2
    exit 1
fi

PATH_PART=${1:-health}

# /health токена не требует и отвечает на GET — по нему удобно проверять,
# какая ревизия сейчас живая.
if [ "$PATH_PART" = "health" ]; then
    curl -sS "$CONTAINER/health"
    echo
    exit 0
fi

if [ -z "$TOKEN" ]; then
    echo "В .env нет INTERNAL_API_TOKEN — служебные эндпоинты без него закрыты." >&2
    exit 1
fi

# Токен идёт заголовком, а не в адресе: так он не попадёт ни в лог запросов
# контейнера, ни на скриншот адресной строки.
# -sS: тихо, но об ошибках говорить. Молчаливый провал уже стоил нам вечера.
case "$PATH_PART" in
    /*) URL="$CONTAINER$PATH_PART" ;;
    *)  URL="$CONTAINER/internal/$PATH_PART" ;;
esac

curl -sS -X POST "$URL" -H "x-internal-token: $TOKEN"
echo
