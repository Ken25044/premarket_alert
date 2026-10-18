#!/bin/zsh
# cron から呼ばれるラッパー。secrets.env に認証情報を書いておく。
cd "$(dirname "$0")"
set -a
source ./secrets.env
set +a
/Users/msngkntr/.venv/bin/python premarket_alert.py >> alert.log 2>&1
