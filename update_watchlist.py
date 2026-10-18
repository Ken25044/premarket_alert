"""毎日実行して watchlist.txt を自動更新するスクリプト

1. JPX(東証)から最新の銘柄一覧(Excel)を取得し、普通株式のみを抽出
2. yfinance で過去半年分の日足データを一括取得 (API制限を避けるため小分けに取得)
3. 以下の条件でスクリーニング
   - 株価 >= 250円
   - 25日平均売買代金 >= 5億円
   - (株価 > 25日線 かつ 25日線が上向き) または (株価 > 75日線 かつ 75日線が上向き)
   - 過去60日の最高値から 10% 以内に位置する (高値圏での推移)
4. 条件を満たした銘柄を watchlist.txt に上書き保存
"""
import os
import time
import urllib.request
import re
from bs4 import BeautifulSoup
import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")

# --- フィルター設定 ---
MIN_PRICE = 250            # 最低株価
MIN_VOLUME_YEN = 5 * 10**8 # 25日平均売買代金 (5億円)
HIGH_RANGE_PCT = 0.10      # 過去60日高値から10%以内
CHUNK_SIZE = 100           # yfinanceAPIの一括取得サイズ
SLEEP_TIME = 2             # 取得間隔 (秒)

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
    
    passed_codes = []
    
    print(f"yfinance からデータ取得中 (API制限回避のため {CHUNK_SIZE} 銘柄ずつ {SLEEP_TIME} 秒間隔で取得)...")
    
    for i in range(0, len(codes), CHUNK_SIZE):
        chunk_codes = codes[i:i+CHUNK_SIZE]
        tickers_str = " ".join([f"{c}.T" for c in chunk_codes])
        
        try:
            data = yf.download(tickers_str, period="6mo", auto_adjust=True, progress=False, group_by="ticker")
        except Exception as e:
            print(f"取得エラー: {e}")
            continue
            
        for code in chunk_codes:
            ticker_symbol = f"{code}.T"
            
            # yf.download の返り値が1銘柄だけの場合と複数銘柄の場合でデータ構造が違うため吸収
            if len(chunk_codes) == 1:
                df = data
            else:
                if ticker_symbol not in data:
                    continue
                df = data[ticker_symbol]
                
            df = df.dropna()
            
            if len(df) < 80:  # 75日線などを計算するには十分な日数が必要
                continue
                
            close = df["Close"]
            volume = df["Volume"]
            
            # 1. 売買代金 (株価 * 出来高)
            turnover = close * volume
            avg_turnover_25 = turnover.tail(25).mean()
            if avg_turnover_25 < MIN_VOLUME_YEN:
                continue
                
            # 2. 株価水準
            latest_close = close.iloc[-1]
            if latest_close < MIN_PRICE:
                continue
                
            # 3. トレンド (移動平均)
            sma25 = close.rolling(window=25).mean()
            sma75 = close.rolling(window=75).mean()
            
            # 25日線が上向きか (直近 > 1日前)
            sma25_up = sma25.iloc[-1] > sma25.iloc[-2]
            # 75日線が上向きか
            sma75_up = sma75.iloc[-1] > sma75.iloc[-2]
            
            cond_25 = (latest_close > sma25.iloc[-1]) and sma25_up
            cond_75 = (latest_close > sma75.iloc[-1]) and sma75_up
            
            if not (cond_25 or cond_75):
                continue
                
            # 4. 高値圏 (過去60日の最高値付近)
            recent_high = close.tail(60).max()
            if latest_close < recent_high * (1 - HIGH_RANGE_PCT):
                continue
                
            # 全ての条件をクリア
            passed_codes.append(code)
            
        print(f"進捗: {min(i+CHUNK_SIZE, len(codes))} / {len(codes)} 完了 (条件クリア: {len(passed_codes)}件)")
        time.sleep(SLEEP_TIME)
    
    print(f"\nフィルタリング完了。条件を満たした銘柄数: {len(passed_codes)} 銘柄")
    
    # ファイルに書き込み
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        f.write("# 自動更新された監視銘柄リスト\n")
        f.write(f"# 更新日: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        for c in passed_codes:
            f.write(f"{c}\n")

if __name__ == "__main__":
    update_watchlist()
