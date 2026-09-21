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
    grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' 
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
    # Потолок по времени обязателен: без него curl при недоступном контейнере
    # висит молча и бесконечно, а это ровно тот молчаливый отказ, от которого
    # скрипт и заводился. Холодный старт занимает пару секунд, 20 хватает.
    # Заголовок шлём и сюда: /health его не требует, но отвечает, что именно
    # до него дошло. Без этого не отличить «токен не тот» от «заголовок не
    # доехал» — а это разные поломки.
    curl -sS --max-time 20 "$CONTAINER/health" -H "x-internal-token: ${TOKEN:-}"
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

# Служебные эндпоинты работают дольше: выгрузка каталога тратит до 20 секунд
# сама по себе, расчёт ходит в Ozon. Минута — с запасом, но не навсегда.
BODY=$(curl -sS --max-time 60 -X POST "$URL" -H "x-internal-token: $TOKEN")
echo "$BODY"

# `forbidden` значит, что токен в .env и токен в ревизии разошлись. Сравнить
# значения напрямую нельзя — их нельзя ни показать, ни переслать, — поэтому
# сверяем отпечатки: /health отдаёт свой, а здесь считаем свой.
if [ "$BODY" = "forbidden" ]; then
    if command -v shasum > /dev/null 2>&1; then
        MINE=$(printf '%s' "$TOKEN" | shasum -a 256 | cut -c1-8)
    else
        MINE=$(printf '%s' "$TOKEN" | openssl dgst -sha256 | sed 's/.*= //' | cut -c1-8)
    fi
    echo >&2
    echo "Токен не подошёл. Отпечаток токена из .env: $MINE" >&2
    echo "Сравни с полем token в ответе: scripts/api.sh health" >&2
    echo "Разные — значит в секрете GitHub INTERNAL_API_TOKEN другое значение." >&2
    echo "Поправь его и передеплой: Actions -> Run workflow." >&2
fi
