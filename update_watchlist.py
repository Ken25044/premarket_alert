"""毎日実行して watchlist.txt を自動更新するスクリプト

1. JPX(東証)から最新の銘柄一覧(Excel)を取得し、普通株式のみを抽出
2. yfinance で過去半年分の日足データを一括取得 (API制限を避けるため小分けに取得)
3. 以下の条件でスクリーニング
   - 株価 >= 250円
   - 25日平均売買代金 >= 5億円
   - (株価 > 25日線 かつ 25日線が上向き) または (株価 > 75日線 かつ 75日線が上向き)
   - 過去60日の最高値から 10% 以内に位置する (高値圏での推移)
4. 条件を満たした銘柄を watchlist.txt に上書き保存
   (取得に成功した銘柄が半分未満なら、前日のリストを残したまま異常終了する)

条件そのものは strategy.watchlist_mask に書いてある (バックテストと共通)。
"""
import os
import re
import sys
import urllib.request

import pandas as pd
from bs4 import BeautifulSoup

import archive
import strategy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")

CHUNK_SIZE = 100           # yfinanceAPIの一括取得サイズ
SLEEP_TIME = 2             # 取得間隔 (秒)
MIN_FETCH_RATIO = 0.5      # 取得成功がこれ未満ならリストを更新しない

def get_jpx_codes():
    print("東証の最新銘柄一覧を取得中...")
    url = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
    html = urllib.request.urlopen(url).read().decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    # data_j.xlsx のURLを抽出
    link = soup.find("a", href=re.compile(r"data_j\.xlsx?"))["href"]
    excel_url = f"https://www.jpx.co.jp{link}"
    
    # ExcelをPandasで読み込む
    df = pd.read_excel(excel_url)
    
    # 普通株式（内国株式）に絞る (ETF, REIT, ETN等を除外)
    df = df[df["市場・商品区分"].str.contains("内国株式", na=False)]
    
    codes = df["コード"].astype(str).tolist()
    return codes

def update_watchlist():
    codes = get_jpx_codes()
    print(f"対象銘柄数: {len(codes)} 銘柄")

    print(f"yfinance からデータ取得中 ({CHUNK_SIZE} 銘柄ずつ一括取得)...")
    daily = strategy.download(
        [f"{c}.T" for c in codes], chunk_size=CHUNK_SIZE, period="6mo", interval="1d", pause=SLEEP_TIME,
        on_chunk=lambda done, total: print(f"進捗: {done} / {total} 完了"))

    if len(daily) < len(codes) * MIN_FETCH_RATIO:
        sys.exit(f"取得成功が {len(daily)} / {len(codes)} 銘柄しかないため、watchlist.txt は更新しません")

    passed_codes = [c for c in codes
                    if f"{c}.T" in daily and strategy.watchlist_mask(daily[f"{c}.T"].dropna()).iloc[-1]]
    print(f"\nフィルタリング完了。条件を満たした銘柄数: {len(passed_codes)} 銘柄")
    
    # ファイルに書き込み
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        f.write("# 自動更新された監視銘柄リスト\n")
        f.write(f"# 更新日: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 取得成功: {len(daily)}/{len(codes)}\n")
        for c in passed_codes:
            f.write(f"{c}\n")
    # 日付つきで残しておき、バックテストで「その日に実際に使ったリスト」を再現できるようにする
    archive.save_watchlist_snapshot(passed_codes, pd.Timestamp.now(tz=strategy.TZ).date())

if __name__ == "__main__":
    update_watchlist()
