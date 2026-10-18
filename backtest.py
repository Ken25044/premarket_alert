import pandas as pd
import yfinance as yf
from datetime import timedelta

# 日本語（全角文字）が含まれていても表の列を綺麗に揃える設定
pd.set_option('display.unicode.east_asian_width', True)

def backtest_daytrade(
    targets,
    target_date=None,
    buy_time_str="09:30",
    take_profit_pct=1.5,
    stop_loss_pct=1.5,
    max_hold_minutes=None
):
    """
    targets:
      - 銘柄と日付のペアリスト: [('3692.T', '2026-10-13'), ('3994.T', '2026-10-10'), ...]
      - 辞書形式: {'3692.T': '2026-10-13', '3994.T': '2026-10-10'}
      - 銘柄リスト (target_date と併用): ['3692.T', '3994.T'], target_date='2026-10-13'
    take_profit_pct: 利確ライン (%) (デフォルト: 1.5%)
    stop_loss_pct:   損切りライン (%) (デフォルト: 1.5%)
    max_hold_minutes: 最大保有時間(分)。指定しない場合(None)は大引けまで監視。
    """
    # targets の形式を [(ticker, date), ...] に統一
    trade_list = []
    if isinstance(targets, dict):
        trade_list = list(targets.items())
    elif isinstance(targets, list):
        if len(targets) > 0 and isinstance(targets[0], (tuple, list)):
            trade_list = targets
        elif target_date is not None:
            trade_list = [(t, target_date) for t in targets]
        else:
            raise ValueError("各銘柄の日付を指定してください。例: [('3692.T', '2026-10-13'), ('3994.T', '2026-10-10')]")
    else:
        raise ValueError("targets にはリストまたは辞書を指定してください。")

    print(f"\n--- バックテスト開始 (エントリー: {buy_time_str} / 利確: +{take_profit_pct}% / 損切: -{stop_loss_pct}%) ---")

    results = []

    for ticker, t_date in trade_list:
        stock = yf.Ticker(ticker)
        # 1分足データを取得 (直近7日以内)
        df = stock.history(start=t_date, end=pd.to_datetime(t_date) + timedelta(days=1), interval="1m")

        if df.empty:
            print(f"⚠️ {ticker} ({t_date}): データが取得できなかったよ。")
            continue

        # タイムゾーンを日本時間に合わせる
        df.index = df.index.tz_convert('Asia/Tokyo')
        target_buy_time = pd.to_datetime(f"{t_date} {buy_time_str}").tz_localize('Asia/Tokyo')

        # 9:30以降の足を取得
        df_from_buy = df[df.index >= target_buy_time]
        if df_from_buy.empty:
            print(f"⚠️ {ticker} ({t_date}): {buy_time_str} 以降のデータがないよ。")
            continue

        # 1. 9:30時点（最初の足）の株価を買値とする
        actual_buy_time = df_from_buy.index[0]
        buy_price = df_from_buy.iloc[0]['Open']

        # 利確・損切りの目標価格
        tp_price = buy_price * (1 + take_profit_pct / 100)
        sl_price = buy_price * (1 - stop_loss_pct / 100)

        # 監視対象データ
        df_trade = df_from_buy.copy()
        if max_hold_minutes is not None:
            limit_time = actual_buy_time + timedelta(minutes=max_hold_minutes)
            df_trade = df_trade[df_trade.index <= limit_time]

        exit_type = None
        exit_price = None
        exit_time = None
        hold_minutes = 0

        # 時系列順（1分足ごと）に利確・損切りを判定
        for t, row in df_trade.iterrows():
            minutes_passed = int((t - actual_buy_time).total_seconds() / 60)

            hit_tp = row['High'] >= tp_price
            hit_sl = row['Low'] <= sl_price

            if hit_tp and hit_sl:
                # 同一の足で両方に達した場合（保守的に損切りを優先）
                exit_type = "損切"
                exit_price = sl_price
                exit_time = t
                hold_minutes = minutes_passed
                break
            elif hit_tp:
                exit_type = "利確"
                exit_price = tp_price
                exit_time = t
                hold_minutes = minutes_passed
                break
            elif hit_sl:
                exit_type = "損切"
                exit_price = sl_price
                exit_time = t
                hold_minutes = minutes_passed
                break

        # どちらのラインにも達しなかった場合
        if exit_price is None:
            exit_type = "引け手仕舞い" if max_hold_minutes is None else "時間切れ"
            exit_price = df_trade.iloc[-1]['Close']
            exit_time = df_trade.index[-1]
            hold_minutes = int((exit_time - actual_buy_time).total_seconds() / 60)

        profit = exit_price - buy_price
        return_pct = (profit / buy_price) * 100

        # 結果を保存
        results.append({
            '日付': t_date,
            '銘柄': ticker,
            '9:30買値': round(buy_price, 1),
            '決済価格': round(exit_price, 1),
            '結果': exit_type,
            '損益(円)': round(profit, 1),
            '回収率(%)': round(return_pct, 2),
            '保有時間': f"{hold_minutes}分"
        })

    df_results = pd.DataFrame(results)

    if df_results.empty:
        return "テストできる銘柄がなかったよ。"

    # --- 全体の集計 ---
    total_trades = len(df_results)
    tp_count = len(df_results[df_results['結果'] == '利確'])
    sl_count = len(df_results[df_results['結果'] == '損切'])
    other_count = total_trades - tp_count - sl_count

    win_trades = len(df_results[df_results['損益(円)'] > 0])
    win_rate = (win_trades / total_trades) * 100
    avg_return = df_results['回収率(%)'].mean()
    total_profit = df_results['損益(円)'].sum()
    total_profit_100 = int(round(total_profit * 100))

    print("\n--- 個別結果 ---")
    print(df_results.to_string(index=False))

    print(f"\n--- 全体集計 (全 {total_trades} トレード) ---")
    print(f"【トータル損益】   {total_profit:+,.1f} 円 (1株) / {total_profit_100:+,.0f} 円 (100株運用)")
    print(f"【勝率】           {win_rate:5.1f}% ({win_trades}勝 {total_trades - win_trades}敗)")
    print(f"【平均回収率】     {avg_return:+6.2f}%")
    print(f"【利確 (+{take_profit_pct}%)】   {tp_count} 件")
    print(f"【損切 (-{stop_loss_pct}%)】   {sl_count} 件")
    if other_count > 0:
        label = "引け手仕舞い" if max_hold_minutes is None else "時間切れ手仕舞い"
        print(f"【{label}】 {other_count} 件")

    return df_results


# ----------------------------------------------------
# 実行例：銘柄ごとにそれぞれ日付を指定
# ----------------------------------------------------
targets = [
    ('3692.T', '2026-10-08'),
    ('3994.T', '2026-10-09'),
    ('9107.T', '2026-10-13'),
    ('6264.T', '2026-10-13'),
]

# 利確 +1.5% / 損切り -1.5% でバックテスト実行
df = backtest_daytrade(targets, take_profit_pct=1.5, stop_loss_pct=1.5)