"""premarket_alert.py / backtest.py / update_watchlist.py で共有する判定ロジック

ライブの通知とバックテストが必ず同じ条件で判定されるよう、条件はここにだけ書く。
"""
import csv
import os
import time
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import yfinance as yf

MARKET = os.environ.get("MARKET", "JP").upper()
if MARKET == "JP":
    SUFFIX, CURRENCY, TZ = ".T", "円", "Asia/Tokyo"
    # yfinance の日本株1分足は 9:04〜9:05 から始まる日が大半で、寄り付き直後の数本と
    # 寄り板の出来高が欠けている。日によって有無がばらつくと RVOL が歪むので、
    # 出来高比較は全日でそろう 9:05 以降の足だけで行う。
    RVOL_WINDOW_START = dtime(9, 5)
else:
    SUFFIX, CURRENCY, TZ = "", "ドル", "America/New_York"
    RVOL_WINDOW_START = dtime(9, 30)


def _env(name, default, legacy=None):
    """環境変数を float で読む。旧名 (legacy) が設定されていればそれも受け付ける"""
    value = os.environ.get(name)
    if value is None and legacy:
        value = os.environ.get(legacy)
    return float(value if value is not None else default)


INTRADAY_DAYS = 29         # yfinance の1分足は直近30日以内しか取れない
INTRADAY_WINDOW_DAYS = 7   # 1リクエストで取れる1分足は最大8日分

# --- ステップ1: ウォッチリスト抽出 (日足) ---
MIN_PRICE = 250            # 最低株価
MIN_VOLUME_YEN = 5 * 10**8 # 25日平均売買代金 (5億円)
HIGH_RANGE_PCT = 0.10      # 過去60日高値から10%以内
MIN_HISTORY_DAYS = 80      # 75日線を計算するのに必要な日数

# --- ステップ2: 当日判定 (分足) ---
# 「前日比」= 現在値 / 前日終値 - 1。寄りでギャップしていなくても、寄った後に上がれば通る
MIN_CHANGE = _env("MIN_CHANGE", "0.03", legacy="GAP_THRESHOLD")
MAX_CHANGE = _env("MAX_CHANGE", "0.10", legacy="MAX_GAP_THRESHOLD")
# 時刻別RVOL = 当日の累積出来高 / 過去N日の「同じ時刻までの」累積出来高の平均
RVOL_THRESHOLD = _env("RVOL_THRESHOLD", "1.5", legacy="TARGET_DAILY_RATIO")
RVOL_DAYS = 5
RVOL_MIN_DAYS = 3          # 比較対象の過去日がこれ未満なら判定しない
CLOSE_TO_HIGH_THRESHOLD = _env("CLOSE_TO_HIGH_THRESHOLD", "0.01")
PREV_DAY_MAX_RETURN = _env("PREV_DAY_MAX_RETURN", "0.05")
PREV_DAY_MAX_VOL_RATIO = _env("PREV_DAY_MAX_VOL_RATIO", "2.0")
PREV_VOLUME_DAYS = 5


def watchlist_mask(daily):
    """日足から「その日の引け時点でウォッチリスト条件を満たすか」を日付ごとに返す

    最終行を見れば翌朝のウォッチリスト判定、過去の行を見ればその時点の判定になる
    (バックテストで当時のウォッチリストを再現するのに使う)。
    """
    # 配当落ちで移動平均や高値がずれないよう、調整後終値で判定する (元の auto_adjust=True と同じ)
    close = daily["Adj Close"] if "Adj Close" in daily else daily["Close"]
    volume = daily["Volume"]
    turnover25 = (close * volume).rolling(25).mean()
    sma25 = close.rolling(25).mean()
    sma75 = close.rolling(75).mean()
    trend = ((close > sma25) & (sma25 > sma25.shift())) | ((close > sma75) & (sma75 > sma75.shift()))
    near_high = close >= close.rolling(60).max() * (1 - HIGH_RANGE_PCT)
    enough = pd.Series(range(1, len(daily) + 1), index=daily.index) >= MIN_HISTORY_DAYS
    return (turnover25 >= MIN_VOLUME_YEN) & (close >= MIN_PRICE) & trend & near_high & enough


def prev_day_ok(past_daily):
    """前日の過熱フィルター。past_daily は当日を含まない日足"""
    if len(past_daily) < PREV_VOLUME_DAYS + 2:
        return False
    close, volume = past_daily["Close"], past_daily["Volume"]
    # 前日急騰 (翌日の利確売りに巻き込まれやすい)
    if close.iloc[-1] / close.iloc[-2] - 1 >= PREV_DAY_MAX_RETURN:
        return False
    # 前日出来高 vs その前の5日平均 (前日自身を平均に含めない)
    base_vol = volume.iloc[-(PREV_VOLUME_DAYS + 1):-1].mean()
    if base_vol and volume.iloc[-1] / base_vol >= PREV_DAY_MAX_VOL_RATIO:
        return False
    return True


def _window_volume(bars, until):
    tod = bars.index.time
    return float(bars["Volume"][(tod >= RVOL_WINDOW_START) & (tod <= until)].sum())


def evaluate(today_bars, past_bars, prev_close, today_open):
    """当日判定。today_bars は「見えている」分足のみ (遅延分は呼び出し側で切る)

    past_bars: 当日より前の分足 (複数日)
    戻り値: dict。"status" は "hit" / "miss" / "nodata"
    """
    if today_bars.empty:
        return {"status": "nodata", "reason": "当日の分足なし"}

    data_time = today_bars.index[-1]
    cutoff = data_time.time()
    if cutoff < RVOL_WINDOW_START:
        return {"status": "nodata", "reason": f"{RVOL_WINDOW_START:%H:%M}以降の足がまだない", "data_time": data_time}

    # 時刻別RVOL: 過去日の「同じ時刻まで」の累積出来高と比べる (出来高のU字型を吸収)
    past_days = [g for _, g in past_bars.groupby(past_bars.index.date)][-RVOL_DAYS:]
    past_vols = [v for v in (_window_volume(g, cutoff) for g in past_days) if v > 0]
    if len(past_vols) < RVOL_MIN_DAYS:
        return {"status": "nodata", "reason": "比較用の過去分足が足りない", "data_time": data_time}
    base_vol = sum(past_vols) / len(past_vols)
    volume = _window_volume(today_bars, cutoff)
    rvol = volume / base_vol

    price = float(today_bars["Close"].iloc[-1])
    high = float(today_bars["High"].max())
    change = price / prev_close - 1
    drop_from_high = 1 - price / high if high > 0 else 0

    hit = (
        MIN_CHANGE <= change <= MAX_CHANGE        # 前日比が下限〜上限 (上がりすぎは除外)
        and rvol >= RVOL_THRESHOLD                # 出来高が平常時のN倍ペース
        and drop_from_high <= CLOSE_TO_HIGH_THRESHOLD  # 高値から落ちすぎていない
        and price >= today_open                   # 陽線 (寄り天を除外)
    )
    return {
        "status": "hit" if hit else "miss",
        "data_time": data_time,
        "prev_close": prev_close,
        "open": today_open,
        "open_gap": today_open / prev_close - 1,
        "price": price,
        "high": high,
        "drop_from_high": drop_from_high,
        "change": change,
        "volume": volume,
        "base_vol": base_vol,
        "rvol": rvol,
    }


def condition_summary():
    return (f"前日比+{MIN_CHANGE:.0%}〜{MAX_CHANGE:.0%} / 陽線 / 時刻別RVOL {RVOL_THRESHOLD}倍以上 "
            f"/ 高値から{CLOSE_TO_HIGH_THRESHOLD:.1%}以内")


# --- 材料 (前夜に PTS / 株探などで調べて手で記録した銘柄) ---
CATALYST_MODES = ("off", "tag", "only")
CATALYST_MARK = "📰"


def load_catalysts(path):
    """catalysts.csv を {取引日: {コード: メモ}} で返す

    1行 = date,code,memo。date はその銘柄を狙う取引日 (YYYY-MM-DD か YYYY/MM/DD)。
    # で始まる行・空行・ヘッダー行 (date,code,memo) は読み飛ばす。
    memo は省略可で、カンマを含んでもよい (3列目以降をつなげる)。
    code が空の行は「その日は調べたが該当なし」の印 (その日を記録済みとして扱う)。
    """
    catalysts = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.reader(f), 1):
            if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            if row[0].strip().lower() == "date":
                continue
            raw_date = row[0].strip().replace("/", "-")
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                print(f"{path}:{n}: 日付が読めないので無視します ({row[0]!r})")
                continue
            picks = catalysts.setdefault(d, {})
            code = row[1].strip().upper().removesuffix(SUFFIX) if len(row) > 1 else ""
            if code:
                picks[code] = ",".join(row[2:]).strip()
    return catalysts


def catalyst_memo(catalysts, d, code):
    """その日にその銘柄が材料ありならメモ (空文字のこともある) を、なければ None を返す"""
    return catalysts.get(d, {}).get(str(code).upper().removesuffix(SUFFIX))


def download_intraday(symbols, since=None):
    """1分足を since (date) から今日まで、7日ずつに分けて取得してつなぐ。

    since を省略するか30日より前なら、取れるだけ (直近 INTRADAY_DAYS 日) 取る。
    """
    now = pd.Timestamp.now(tz=TZ)
    end = now.normalize() + timedelta(days=1)
    start = (now - timedelta(days=INTRADAY_DAYS)).ceil("h")
    if since is not None:
        start = max(start, pd.Timestamp(since, tz=TZ))
    parts = {}
    while start < end:
        stop = min(start + timedelta(days=INTRADAY_WINDOW_DAYS), end)
        print(f"  1分足 {start:%m/%d}〜{stop:%m/%d} を取得中...")
        for sym, df in download(symbols, start=start, end=stop, interval="1m").items():
            parts.setdefault(sym, []).append(df)
        start = stop
    return {sym: pd.concat(dfs).sort_index().loc[lambda d: ~d.index.duplicated()] for sym, dfs in parts.items()}


def download(symbols, chunk_size=100, on_chunk=None, pause=0, **kwargs):
    """yf.download をまとめて呼び、{symbol: DataFrame(現地時刻)} を返す。取れなかった銘柄は含めない"""
    result = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        try:
            data = yf.download(chunk, group_by="ticker", progress=False, threads=True,
                               auto_adjust=False, **kwargs)
        except Exception as e:
            print(f"取得エラー ({chunk[0]}〜): {e}")
            continue
        multi = isinstance(data.columns, pd.MultiIndex)
        got = set(data.columns.get_level_values(0)) if multi else set()
        for sym in chunk:
            if multi:
                if sym not in got:
                    continue
                df = data[sym]
            else:
                df = data
            df = df.dropna(how="all")
            if df.empty:
                continue
            if df.index.tz is not None:
                df = df.tz_convert(TZ)
            result[sym] = df
        if on_chunk:
            on_chunk(min(i + chunk_size, len(symbols)), len(symbols))
        if pause and i + chunk_size < len(symbols):
            time.sleep(pause)  # 取得制限を避けるための間隔
    return result
