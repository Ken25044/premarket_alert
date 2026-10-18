"""1分足とウォッチリストを毎日ローカルに保存し、30日より前もバックテストできるようにする

yfinance の1分足は直近30日分しか取れないので、消える前に data/<MARKET>/ に貯めていく。

  data/JP/intraday/2026-10-16.csv.gz   その日の1分足 (code,time,Open,High,Low,Close,Volume)
  data/JP/watchlist/2026-10-16.txt     その日の朝に実際に使ったウォッチリスト (update_watchlist.py が保存)

保存する銘柄 (--universe):
  watchlist (既定)  直近30日のウォッチリストに1日でも入った銘柄。途中でリストから外れた銘柄も残るので、
                    バックテストの生存バイアスがなくなる。約700銘柄で1日あたり約1.4MB
  jpx               東証全銘柄の日足から、直近30日に1日でもウォッチリスト条件を満たした銘柄を再現して保存。
                    初回 (スナップショットがまだない) や取りこぼしの穴埋め用。日足を全銘柄取るので15分ほどかかる

使い方:
  python archive.py                  # 毎朝 run.sh から (アラートの後に) 呼ばれる
  python archive.py --universe jpx   # 初回: 直近30日分をまとめて保存

保存するのは「終わった日」だけ (今日の分は翌日以降に保存される)。既にある日のファイルには、
まだ入っていない銘柄だけを書き足す。新しくリスト入りした銘柄は、RVOL の比較用に過去数日分もさかのぼって保存する。
"""
import argparse
import glob
import os
from datetime import date, time as dtime, timedelta

import pandas as pd

import strategy
from strategy import MARKET, SUFFIX, TZ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", MARKET)
INTRADAY_DIR = os.path.join(DATA_DIR, "intraday")
WATCHLIST_DIR = os.path.join(DATA_DIR, "watchlist")
COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
LOOKBACK_DAYS = 12        # 毎回さかのぼって取り直す日数 (新しくリスト入りした銘柄の RVOL 用の過去分)
DAY_COMPLETE_AT = dtime(16, 0)  # この時刻を過ぎたら当日分も「終わった日」として保存する


# --- ウォッチリストのスナップショット ---

def save_watchlist_snapshot(codes, d):
    os.makedirs(WATCHLIST_DIR, exist_ok=True)
    with open(os.path.join(WATCHLIST_DIR, f"{d:%Y-%m-%d}.txt"), "w", encoding="utf-8") as f:
        f.write("".join(f"{c}\n" for c in codes))


def load_watchlist_snapshots(since=None):
    """{日付: [コード]} を返す"""
    result = {}
    for path in sorted(glob.glob(os.path.join(WATCHLIST_DIR, "*.txt"))):
        d = date.fromisoformat(os.path.basename(path)[:10])
        if since is None or d >= since:
            with open(path, encoding="utf-8") as f:
                result[d] = [line.strip() for line in f if line.strip()]
    return result


# --- 1分足の保存・読み込み ---

def _day_path(d):
    return os.path.join(INTRADAY_DIR, f"{d:%Y-%m-%d}.csv.gz")


def archived_dates():
    return sorted(date.fromisoformat(os.path.basename(p)[:10])
                  for p in glob.glob(os.path.join(INTRADAY_DIR, "*.csv.gz")))


def _read_day(d):
    df = pd.read_csv(_day_path(d), dtype={"code": str})
    df.index = pd.DatetimeIndex(pd.to_datetime(f"{d:%Y-%m-%d} " + df["time"]), name="Datetime").tz_localize(TZ)
    return df


def save_day(d, bars_by_code):
    """1日分 {コード: 1分足} を保存する。既存ファイルにない銘柄だけ書き足す。戻り値は書き足した銘柄数"""
    path = _day_path(d)
    existing = _read_day(d) if os.path.exists(path) else None
    have = set(existing["code"]) if existing is not None else set()
    new = []
    for code, bars in bars_by_code.items():
        if code in have or bars.empty:
            continue
        rows = bars[COLUMNS].copy()
        rows.insert(0, "time", rows.index.strftime("%H:%M"))
        rows.insert(0, "code", code)
        new.append(rows)
    if not new:
        return 0
    out = pd.concat(([existing[["code", "time"] + COLUMNS]] if existing is not None else []) + new)
    os.makedirs(INTRADAY_DIR, exist_ok=True)
    tmp = path + ".tmp"
    out.to_csv(tmp, index=False, compression="gzip", float_format="%.10g")
    os.replace(tmp, path)
    return len(new)


def load_intraday(symbols=None, since=None):
    """保存済みの1分足を {シンボル (.T 付き): DataFrame} で返す。symbols / since で絞れる"""
    codes = None if symbols is None else {s.removesuffix(SUFFIX) for s in symbols}
    parts = {}
    for d in archived_dates():
        if since is not None and d < since:
            continue
        df = _read_day(d)
        if codes is not None:
            df = df[df["code"].isin(codes)]
        for code, g in df.groupby("code"):
            parts.setdefault(f"{code}{SUFFIX}", []).append(g[COLUMNS].astype(float))
    return {sym: pd.concat(dfs).sort_index() for sym, dfs in parts.items()}


def archived_codes():
    """保存済みの全銘柄コード (スナップショットに載った銘柄も含む)"""
    codes = set()
    for d in archived_dates():
        codes |= set(pd.read_csv(_day_path(d), usecols=["code"], dtype={"code": str})["code"])
    for day_codes in load_watchlist_snapshots().values():
        codes |= set(day_codes)
    return sorted(codes)


def merge_intraday(*sources):
    """複数の {シンボル: 1分足} をつなぐ。同じ時刻の足は先に渡したほうを残す"""
    merged = {}
    for src in sources:
        for sym, df in src.items():
            merged.setdefault(sym, []).append(df)
    return {sym: pd.concat(dfs).sort_index().loc[lambda d: ~d.index.duplicated()] for sym, dfs in merged.items()}


# --- 保存する銘柄の決め方 ---

def universe_from_snapshots(today):
    from premarket_alert import WATCHLIST_FILE, load_watchlist
    codes = set(load_watchlist()[0]) if os.path.exists(WATCHLIST_FILE) else set()
    for day_codes in load_watchlist_snapshots(since=today - timedelta(days=strategy.INTRADAY_DAYS)).values():
        codes |= set(day_codes)
    return sorted(codes)


def universe_from_jpx(today):
    """東証全銘柄の日足から、直近30日に1日でもウォッチリスト条件を満たした銘柄を返す"""
    from update_watchlist import get_jpx_codes
    codes = get_jpx_codes()
    print(f"日足を取得中 ({len(codes)} 銘柄)...")
    daily = strategy.download([f"{c}{SUFFIX}" for c in codes], period="6mo", interval="1d", pause=2)
    first = today - timedelta(days=strategy.INTRADAY_DAYS + 7)  # RVOL の比較用に1週間前から
    picked = []
    for sym, d in daily.items():
        m = strategy.watchlist_mask(d.dropna())
        if m[m.index.date >= first].any():
            picked.append(sym.removesuffix(SUFFIX))
    return sorted(picked)


def run(universe):
    now = pd.Timestamp.now(tz=TZ)
    today = now.date()
    codes = universe_from_jpx(today) if universe == "jpx" else universe_from_snapshots(today)
    if not codes:
        print("保存する銘柄がありません (ウォッチリストのスナップショットがまだない)")
        return
    done = archived_dates()
    since = today - timedelta(days=LOOKBACK_DAYS)
    if universe == "jpx" or not done:
        since = None                          # 取れるだけ (30日分) 取る
    elif done[-1] < since:
        since = done[-1] + timedelta(days=1)  # しばらく動いていなかった日の穴埋め
    print(f"{len(codes)} 銘柄の1分足を保存します ({'取れるだけ' if since is None else f'{since} から'})")

    intraday = strategy.download_intraday([f"{c}{SUFFIX}" for c in codes], since=since)
    by_day = {}
    for sym, df in intraday.items():
        for d, g in df.groupby(df.index.date):
            if d < today or (d == today and now.time() >= DAY_COMPLETE_AT):
                by_day.setdefault(d, {})[sym.removesuffix(SUFFIX)] = g
    for d in sorted(by_day):
        added = save_day(d, by_day[d])
        size = os.path.getsize(_day_path(d)) / 1e6 if os.path.exists(_day_path(d)) else 0
        print(f"  {d}: {len(by_day[d])} 銘柄取得 / {added} 銘柄を書き足し ({size:.1f}MB)")
    total = sum(os.path.getsize(p) for p in glob.glob(os.path.join(INTRADAY_DIR, "*.csv.gz"))) / 1e6
    print(f"保存済み: {len(archived_dates())} 日分 / 合計 {total:.0f}MB ({INTRADAY_DIR})")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--universe", choices=["watchlist", "jpx"], default="watchlist")
    run(p.parse_args().universe)


if __name__ == "__main__":
    main()
