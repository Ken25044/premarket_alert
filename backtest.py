"""アラート条件を過去の全営業日に機械的に当てはめるバックテスト

銘柄や日付を手で選ばず、取得できる期間の全営業日 × 全銘柄について次を再現する。

1. その日の朝のウォッチリスト: 前営業日までの日足で strategy.watchlist_mask を判定
2. 前日の過熱フィルター:      strategy.prev_day_ok
3. 通知時点で「見えていた」データ: 実行時刻 (--run-time) から遅延 (--delay) を引いた時刻までの分足
   だけで strategy.evaluate を判定 (ライブの premarket_alert.py と同じ関数)
4. エントリー: 通知を見て発注する時刻 (実行時刻 + --reaction 分) の1分足の始値 + スリッページ
5. 決済: 利確/損切り (同じ足で両方に触れたら損切り優先) → 時間切れ → 大引け、の順
   決済価格にもスリッページ、往復で手数料を引く

比較用に「その日のウォッチリスト全銘柄を同じルールで買った場合」(ベースライン) も出す。
アラート条件に優位性があるなら、ベースラインより成績が良いはず。

使い方:
  python backtest.py                         # 9:25実行・20分遅延・2分後に発注 (今の運用)
  python backtest.py --run-time 09:40        # 実行を遅らせてデータが揃うのを待った場合
  python backtest.py --universe jpx          # 今日のウォッチリストではなく東証全銘柄から再現
  python backtest.py --no-archive            # 保存した1分足を使わず、yfinance の直近30日だけで検証
  python backtest.py --csv trades.csv        # 全トレードをCSVに保存
  python backtest.py --catalysts catalysts.csv  # 材料あり/なしに分けて成績を出す

銘柄の母集団 (--universe):
  auto (既定)  archive.py で保存したデータがあれば archive、なければ watchlist
  archive      保存済みの全銘柄 (過去30日のウォッチリストに1日でも入った銘柄)。途中でリストから
               外れた銘柄も含むので生存バイアスがない
  watchlist    今日の watchlist.txt の銘柄だけ。途中で外れた銘柄が抜ける (生存バイアス)
  jpx          東証全銘柄 (日足を全部取るので時間がかかる)

1分足は archive.py で保存した分と yfinance の直近30日分をつないで使う (--no-archive で直近30日だけ)。

制約:
  - 保存を始める前の期間は yfinance の直近30日分しかない。保存がたまるほど検証期間が延びる
  - 1分足は 9:04〜9:05 から始まる日が大半 (寄り直後の足と寄り板の出来高が欠ける)
  - 上場廃止になった銘柄は yfinance から日足が取れなくなるので検証から抜ける
"""
import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime, time as dtime, timedelta

import pandas as pd

import archive
import strategy
from strategy import SUFFIX, TZ

pd.set_option("display.unicode.east_asian_width", True)
pd.set_option("display.width", 200)


def parse_hhmm(s):
    return datetime.strptime(s, "%H:%M").time()


def shift(t, minutes):
    return (datetime.combine(datetime.today(), t) + timedelta(minutes=minutes)).time()


def load_universe(name):
    if name == "jpx":
        from update_watchlist import get_jpx_codes
        return get_jpx_codes()
    if name == "archive":
        return archive.archived_codes()
    from premarket_alert import load_watchlist
    return load_watchlist()[0]


def simulate(bars, entry_time, args):
    """entry_time 以降の1分足でエントリーから決済までを再現する。戻り値は dict / None"""
    after = bars[bars.index.time >= entry_time]
    if after.empty:
        return None
    entry_at = after.index[0]
    buy = float(after["Open"].iloc[0]) * (1 + args.slippage / 100)
    tp, sl = buy * (1 + args.tp / 100), buy * (1 - args.sl / 100)
    if args.hold_minutes is not None:
        after = after[after.index <= entry_at + timedelta(minutes=args.hold_minutes)]

    # 損切/利確ラインに最初に触れた足を探す (iterrows で1本ずつ見るより桁違いに速い)
    hit_sl = (after["Low"] <= sl).to_numpy()
    hit_tp = (after["High"] >= tp).to_numpy()
    touched = hit_sl | hit_tp
    if touched.any():
        i = int(touched.argmax())
        exit_at, bar_open = after.index[i], after["Open"].iloc[i]
        if hit_sl[i]:                 # 同じ足で利確にも触れていても、保守的に損切り扱い
            exit_type, exit_price = "損切", min(sl, bar_open)
        else:
            exit_type, exit_price = "利確", max(tp, bar_open)
    else:
        exit_type = "大引け" if args.hold_minutes is None else "時間切れ"
        exit_price, exit_at = float(after["Close"].iloc[-1]), after.index[-1]

    sell = exit_price * (1 - args.slippage / 100)
    ret = (sell / buy - 1) * 100 - args.fee * 2
    return {"entry_at": entry_at, "buy": buy, "exit_at": exit_at, "sell": sell,
            "exit_type": exit_type, "return_pct": ret,
            "hold_min": int((exit_at - entry_at).total_seconds() // 60)}


def summarize(label, trades, days):
    print(f"\n=== {label} ===")
    if trades.empty:
        print("トレードなし")
        return
    r = trades["return_pct"]
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    by_type = trades["exit_type"].value_counts().to_dict()
    print(f"トレード数        {len(trades)} 件 ({days} 営業日 / 1日平均 {len(trades) / days:.1f} 件)")
    print(f"勝率              {len(wins) / len(trades):.1%} ({len(wins)}勝 {len(losses)}敗)")
    print(f"平均損益          {r.mean():+.2f}%  (中央値 {r.median():+.2f}%)")
    print(f"平均利益 / 平均損失 {wins.mean() if len(wins) else 0:+.2f}% / {losses.mean() if len(losses) else 0:+.2f}%")
    print(f"プロフィットファクター {pf:.2f}")
    print(f"合計 (1トレード100万円) {r.sum() * 10000:+,.0f} 円")
    print(f"決済の内訳        {by_type}")


def summarize_catalysts(catalysts, sig, base, trade_days):
    """材料ファイルに記録がある日だけに絞って、材料あり/なしで成績を分ける

    記録のない日は「材料なし」ではなく「調べていない」なので、どちらにも入れない。
    """
    covered = sorted(d for d in catalysts if d in trade_days)
    picks = {(d, c) for d in covered for c in catalysts[d]}
    print(f"\n材料ファイルの記録がある日: 検証期間 {len(trade_days)} 営業日のうち {len(covered)} 日"
          f" / 材料銘柄 {len(picks)} 件")
    if not covered:
        print("検証期間内に材料の記録がないので、材料別の集計はしません")
        return
    on_list = set(zip(base["date"], base["code"])) if not base.empty else set()
    outside = sorted(picks - on_list)
    if outside:
        print(f"その日のウォッチリスト外 (or 分足なし) で検証対象外: {len(outside)} 件 "
              + " ".join(f"{d:%m/%d}:{c}" for d, c in outside))

    def split(df):
        if df.empty:
            return df, df
        df = df[df["date"].isin(covered)]
        has = df["catalyst"].notna()
        return df[has], df[~has]

    days = len(covered)
    sig_yes, sig_no = split(sig)
    base_yes, _ = split(base)
    summarize(f"アラート条件 × 材料あり (記録のある {days} 日)", sig_yes, days)
    summarize(f"アラート条件 × 材料なし (記録のある {days} 日)", sig_no, days)
    summarize(f"ベースライン × 材料あり: 材料銘柄をテクニカル条件なしで全部買った場合 (記録のある {days} 日)",
              base_yes, days)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-time", default="09:25", help="アラートの実行時刻 (既定 09:25)")
    p.add_argument("--delay", type=int, default=20, help="データの遅延 (分, 既定 20)")
    p.add_argument("--reaction", type=int, default=2, help="通知から発注までの時間 (分, 既定 2)")
    p.add_argument("--tp", type=float, default=1.5, help="利確ライン %% (既定 1.5)")
    p.add_argument("--sl", type=float, default=1.5, help="損切りライン %% (既定 1.5)")
    p.add_argument("--hold-minutes", type=int, default=None, help="最大保有時間 (分, 既定は大引けまで)")
    p.add_argument("--slippage", type=float, default=0.1, help="片道のスリッページ %% (既定 0.1)")
    p.add_argument("--fee", type=float, default=0.0, help="片道の手数料 %% (既定 0)")
    p.add_argument("--universe", choices=["auto", "archive", "watchlist", "jpx"], default="auto")
    p.add_argument("--no-archive", action="store_true", help="保存した1分足を使わない (yfinance の直近30日だけ)")
    p.add_argument("--csv", help="全トレードを保存するCSVのパス")
    p.add_argument("--catalysts", help="材料ファイル (catalysts.csv)。指定すると材料あり/なしに分けて集計する")
    args = p.parse_args()

    run_time = parse_hhmm(args.run_time)
    data_until = shift(run_time, -args.delay)       # 通知時点で見えている最後の足
    entry_time = shift(run_time, args.reaction)     # 実際に発注できる時刻
    print(f"実行 {run_time:%H:%M} / 見えているデータ {data_until:%H:%M} まで / エントリー {entry_time:%H:%M}"
          f" / 利確 +{args.tp}% / 損切 -{args.sl}% / スリッページ 片道{args.slippage}% / 手数料 片道{args.fee}%")

    catalysts = strategy.load_catalysts(args.catalysts) if args.catalysts else None

    stored = [] if args.no_archive else archive.archived_dates()
    universe = args.universe
    if universe == "auto":
        universe = "archive" if stored else "watchlist"
    if universe == "archive" and not stored:
        raise SystemExit("保存済みの1分足がありません。先に python archive.py --universe jpx を実行してください")
    codes = load_universe(universe)
    symbols = [f"{c}{SUFFIX}" for c in codes]

    now = pd.Timestamp.now(tz=TZ)
    first_day = (now - timedelta(days=strategy.INTRADAY_DAYS)).date()
    if stored:
        first_day = min(first_day, stored[0])
    print(f"\n母集団: {universe} ({len(symbols)} 銘柄) / 1分足: "
          + (f"保存分 {stored[0]}〜{stored[-1]} ({len(stored)}日) + " if stored else "")
          + "yfinance 直近30日")

    # 日足は、検証初日の時点でウォッチリスト条件 (75日線・80日分の履歴) を判定できるだけさかのぼる
    daily_start = min(now - timedelta(days=365), pd.Timestamp(first_day, tz=TZ) - timedelta(days=200))
    print(f"日足を取得中 ({len(symbols)} 銘柄)...")
    daily = {s: d.dropna() for s, d in strategy.download(symbols, start=daily_start, interval="1d").items()}

    # 1分足は重いので、検証期間中に一度でもウォッチリスト条件を満たした銘柄だけ取得する
    masks = {s: strategy.watchlist_mask(d) for s, d in daily.items()}
    candidates = [s for s, m in masks.items() if m[m.index.date >= first_day - timedelta(days=7)].any()]
    print(f"検証期間中にウォッチリスト入りした銘柄: {len(candidates)} / {len(daily)}")
    intraday = strategy.download_intraday(candidates)
    if stored:
        intraday = archive.merge_intraday(intraday, archive.load_intraday(candidates))

    # 全銘柄の1分足にある日付 = 営業日カレンダー。RVOL の比較対象は「直前の N 営業日」をここから取る
    calendar = sorted({d for df in intraday.values() for d in set(df.index.date)})
    signals, baseline, trade_days, no_bars = [], [], set(), 0
    for sym in candidates:
        intra, day, mask = intraday.get(sym), daily[sym], masks[sym]
        # 日付での切り出しは銘柄ごとに1回だけ (毎日 index.date を作り直すと遅い)
        bars_by_day = dict(tuple(intra.groupby(intra.index.date))) if intra is not None else {}
        day_dates = list(day.index.date)
        for k, d in enumerate(calendar):
            lo, hi = bisect_left(day_dates, d), bisect_right(day_dates, d)
            past, today_daily = day.iloc[:lo], day.iloc[lo:hi]
            if past.empty or today_daily.empty or not mask.iloc[lo - 1]:
                continue  # その日の朝のウォッチリストに入っていない (or 売買なし)
            if d not in bars_by_day:
                if k >= strategy.RVOL_MIN_DAYS:
                    no_bars += 1  # リスト入りしていたのに1分足が保存されていない
                continue
            prev = [bars_by_day[x] for x in calendar[max(0, k - strategy.RVOL_DAYS):k] if x in bars_by_day]
            if len(prev) < strategy.RVOL_MIN_DAYS:
                continue  # 期間の最初の数日は RVOL の比較対象がない
            bars = bars_by_day[d]
            past_bars = pd.concat(prev)  # evaluate が見るのは直近 RVOL_DAYS 日分だけ
            trade_days.add(d)
            row = {"date": d, "code": sym.removesuffix(SUFFIX)}
            if catalysts is not None:
                row["catalyst"] = strategy.catalyst_memo(catalysts, d, row["code"])

            trade = simulate(bars, entry_time, args)
            if trade:
                baseline.append({**row, **trade})

            if not strategy.prev_day_ok(past):
                continue
            visible = bars[bars.index.time <= data_until]
            r = strategy.evaluate(visible, past_bars, float(past["Close"].iloc[-1]),
                                  float(today_daily["Open"].iloc[0]))
            if r["status"] == "hit" and trade:
                signals.append({**row, "change": r["change"], "rvol": r["rvol"], **trade})

    days = max(len(trade_days), 1)
    if no_bars:
        print(f"\n※ ウォッチリスト入りしていたのに1分足がない: {no_bars} 件 (銘柄×日)。"
              "保存を始める前の日か、archive.py が動かなかった日の分です")
    sig = pd.DataFrame(signals)
    if not sig.empty:
        view = sig.assign(
            前日比=sig["change"].map("{:+.1%}".format), RVOL=sig["rvol"].map("{:.1f}".format),
            買値=sig["buy"].round(1), 売値=sig["sell"].round(1), 損益=sig["return_pct"].map("{:+.2f}%".format),
            エントリー=sig["entry_at"].dt.strftime("%H:%M"), 決済=sig["exit_at"].dt.strftime("%H:%M"))
        cols = ["date", "code", "前日比", "RVOL", "エントリー", "買値", "決済", "売値", "exit_type", "損益"]
        if catalysts is not None:
            view["材料"] = sig["catalyst"].map(lambda m: "" if m is None else f"{strategy.CATALYST_MARK} {m}".strip())
            cols.append("材料")
        print("\n--- シグナル一覧 ---")
        print(view[cols].sort_values(["date", "code"]).to_string(index=False))
    if trade_days:
        print(f"\n検証期間: {min(trade_days)} 〜 {max(trade_days)}")
    summarize("アラート条件でエントリー", sig, days)
    base = pd.DataFrame(baseline)
    summarize("ベースライン: その日のウォッチリスト全銘柄を同じルールで買った場合", base, days)
    if catalysts is not None:
        summarize_catalysts(catalysts, sig, base, trade_days)

    if args.csv and not sig.empty:
        sig.to_csv(args.csv, index=False)
        print(f"\n{args.csv} に保存しました")


if __name__ == "__main__":
    main()
