#!/usr/bin/env bash
# Деплой cashflow на ВМ: git pull в /opt/cashflow/repo → код в /opt/cashflow →
# migrate → import-rules → classify → doctor.
#
# data/*.csv НЕ перезаписываются молча. На проде там могут быть намеренные
# правки, которых нет в git (например, настоящее ФИО владельца в rules.csv —
# в git оно не попадает, см. CLAUDE.md). Если файл отличается от версии в
# git, скрипт останавливается на этом файле, показывает diff и ничего не
# трогает — слияние делает человек руками.
#
# Запускать на самой ВМ с правом sudo на пользователя cashflow:
#   sudo bash /opt/cashflow/repo/cashflow/deploy/deploy.sh [ветка]

set -euo pipefail

BRANCH="${1:-claude/great-darwin-dfl2tn}"
REPO_DIR=/opt/cashflow/repo
LIVE_DIR=/opt/cashflow
ENV_FILE=/etc/cashflow/.env
PY="$LIVE_DIR/.venv/bin/python"

run_cashflow() {
    sudo -u cashflow env CASHFLOW_ENV_FILE="$ENV_FILE" "$PY" -m cashflow "$@"
}

echo "==> Обновляю $REPO_DIR до origin/$BRANCH"
sudo -u cashflow git -C "$REPO_DIR" fetch origin
sudo -u cashflow git -C "$REPO_DIR" checkout "$BRANCH"
sudo -u cashflow git -C "$REPO_DIR" merge --ff-only "origin/$BRANCH"

SRC="$REPO_DIR/cashflow"

echo "==> Резервная копия текущего /opt/cashflow/data (на случай неудачного слияния)"
sudo -u cashflow cp -r "$LIVE_DIR/data" "$LIVE_DIR/data.bak-$(date +%Y%m%d%H%M%S)"

echo "==> Синхронизирую код (migrations, src, тесты, pyproject) — без data/, .env и venv"
sudo -u cashflow rsync -a --delete \
    --exclude '.venv' \
    --exclude '.git' \
    --exclude '.env*' \
    --exclude 'data/' \
    --exclude '.cache' \
    --exclude '.pytest_cache' \
    --exclude '__pycache__' \
    --exclude '*.egg-info' \
    "$SRC"/ "$LIVE_DIR"/

echo "==> Проверяю справочники в data/*.csv на расхождение с git"
conflict=0
for f in "$SRC"/data/*.csv; do
    name="$(basename "$f")"
    live="$LIVE_DIR/data/$name"
    if [[ ! -f "$live" ]]; then
        echo "  -> $name нет на проде, копирую как есть из git"
        sudo -u cashflow cp "$f" "$live"
    elif ! diff -q "$f" "$live" > /dev/null 2>&1; then
        echo
        echo "  !! $name на проде отличается от git — НЕ перезаписываю."
        echo "     Если отличие намеренное (например, настоящее ФИО вместо"
        echo "     плейсхолдера), перенесите руками нужные строки из diff ниже"
        echo "     в $live, затем перезапустите скрипт."
        echo "     --- git версия vs прод ($name) ---"
        diff -u "$f" "$live" || true
        echo
        conflict=1
    fi
done

echo "==> Применяю миграции схемы finance"
run_cashflow migrate

if [[ "$conflict" -eq 1 ]]; then
    echo
    echo "СТОП: есть неслитые расхождения в data/*.csv (см. выше)."
    echo "import-rules и classify не запускаю, чтобы не потерять ручные правки на проде."
    echo "Слейте вручную и перезапустите скрипт — тогда дойдёт до конца."
    exit 1
fi

echo "==> Загружаю справочники (articles, rules, manual_overrides, loan_schedule)"
run_cashflow import-rules

echo "==> Переклассифицирую операции"
run_cashflow classify

echo "==> Самопроверка"
run_cashflow doctor

echo
echo "Деплой завершён. Старые копии data/ (на случай отката) лежат рядом как data.bak-*."
