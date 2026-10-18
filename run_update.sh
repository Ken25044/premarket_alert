#!/bin/zsh
# ウォッチリストを自動更新する
cd "$(dirname "$0")"
# 9:25 のアラートまでスリープさせない (7:55 に起こしても途中で寝ると launchd が遅れて実行するため)
# ※ バッテリー駆動でフタを閉じている場合は caffeinate でも寝てしまう
awake_until=$(date -j -f "%H:%M:%S" "09:45:00" +%s)
now=$(date +%s)
if (( awake_until > now )); then
  caffeinate -i -t $(( awake_until - now )) &
fi
/Users/msngkntr/.venv/bin/python update_watchlist.py >> update.log 2>&1
wait
