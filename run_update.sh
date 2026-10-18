#!/bin/zsh
# ウォッチリストを自動更新する
cd "$(dirname "$0")"
/Users/msngkntr/.venv/bin/python update_watchlist.py >> update.log 2>&1

