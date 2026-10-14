#!/bin/bash
# Владелец вписывает ключи S3-пользователя бакета teashop-offsite-backups. На экран не выводятся.
set -euo pipefail
read -rp  "S3 Access Key: " AK
read -rsp "S3 Secret Access Key (при вводе не отображается): " SK; echo
[ -n "$AK" ] && [ -n "$SK" ] || { echo "Пусто — ничего не записал."; exit 1; }
( umask 077; printf 'S3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' "$AK" "$SK" > /etc/teashop/s3.env )
export RCLONE_CONFIG_T_TYPE=s3 RCLONE_CONFIG_T_PROVIDER=Other RCLONE_CONFIG_T_ENDPOINT=https://s3.twcstorage.ru \
       RCLONE_CONFIG_T_REGION=ru-1 RCLONE_CONFIG_T_ACCESS_KEY_ID="$AK" RCLONE_CONFIG_T_SECRET_ACCESS_KEY="$SK" RCLONE_CONFIG_T_NO_CHECK_BUCKET=true
if echo ok | rclone rcat -q t:teashop-offsite-backups/.check && rclone deletefile -q t:teashop-offsite-backups/.check; then
  echo "Ключи записаны, запись в бакет работает. Напишите Claude «ключи s3 вписаны»."
else
  echo "Ключи записаны, но записать в бакет не вышло — проверьте, что у пользователя есть доступ на запись."
fi
