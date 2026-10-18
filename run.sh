#!/bin/zsh
# launchd から呼ばれるラッパー。secrets.env に認証情報を書いておく。
cd "$(dirname "$0")"
set -a
source ./secrets.env
set +a
# 実行中にスリープしないようにする
caffeinate -i /Users/msngkntr/.venv/bin/python premarket_alert.py >> alert.log 2>&1
# アラートの後に、前日までの1分足をローカルに貯める (yfinance は30日で消えるため)
caffeinate -i /Users/msngkntr/.venv/bin/python archive.py >> archive.log 2>&1
