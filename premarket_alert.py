"""寄り直後/プレマーケットの強い銘柄チェック → Slack通知 (yfinance のみ・無料)

条件:
  1. 現在値が前日終値比 +3% 以上
  2. 当日出来高(プレ含む・ここまで)が、過去5営業日の平均出来高の一定割合以上

環境変数:
  SLACK_WEBHOOK_URL  Slack Incoming Webhook URL (必須)
  MARKET             US (既定) / JP
  GAP_THRESHOLD             前日比の下限しきい値 (既定 0.03)
  MAX_GAP_THRESHOLD         前日比の上限しきい値 (既定 0.10 = 10%超えは除外)
  TARGET_DAILY_RATIO        1日の終わり時点での期待出来高倍率 (既定 1.5)
  CLOSE_TO_HIGH_THRESHOLD   当日高値からの下落率の上限 (既定 0.01 = 1%以内)
  PREV_DAY_MAX_RETURN       前日騰落率がこれ以上なら除外 (既定 0.05 = +5%)
  PREV_DAY_MAX_VOL_RATIO    前日出来高 / その前5日平均 がこれ以上なら除外 (既定 2.0)
  ※ 当日の始値 >= 現在値 (陰線) の場合は除外されます。
"""
import os
import sys
from datetime import datetime

import requests
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MARKET = os.environ.get("MARKET", "JP").upper()
if MARKET == "JP":
    WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")
    SUFFIX, CURRENCY = ".T", "円"
else:
    WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist_us.txt")
    SUFFIX, CURRENCY = "", "ドル"

GAP_THRESHOLD = float(os.environ.get("GAP_THRESHOLD", "0.03"))
MAX_GAP_THRESHOLD = float(os.environ.get("MAX_GAP_THRESHOLD", "0.10"))
TARGET_DAILY_RATIO = float(os.environ.get("TARGET_DAILY_RATIO", "1.5"))
CLOSE_TO_HIGH_THRESHOLD = float(os.environ.get("CLOSE_TO_HIGH_THRESHOLD", "0.01"))
PREV_DAY_MAX_RETURN = float(os.environ.get("PREV_DAY_MAX_RETURN", "0.05"))
PREV_DAY_MAX_VOL_RATIO = float(os.environ.get("PREV_DAY_MAX_VOL_RATIO", "2.0"))
VOLUME_DAYS = 5


def load_watchlist():
    """銘柄コード/ティッカーを読み込む (1行1銘柄, # でコメント)"""
    codes = []
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                codes.append(line)
    return codes


def check_symbol(code):
    """条件を満たせば結果dictを、満たさなければ None を返す"""
    ticker = yf.Ticker(f"{code}{SUFFIX}")

    # 当日(取引所の現地日付)の分足。プレマーケット含む
    intra = ticker.history(period="1d", interval="1m", prepost=True)
    if intra.empty:
        return None
    trade_date = intra.index[-1].date()  # 取引所タイムゾーンの日付
    if trade_date != datetime.now(intra.index[-1].tzinfo).date():
        return None  # 休場日などで当日データなし (前営業日分の再通知を防ぐ)

    daily = ticker.history(period="1mo")
    past = daily[daily.index.date < trade_date]
    if len(past) < VOLUME_DAYS + 2:
        return None

    # 前日急騰フィルター (翌日の利確売りに巻き込まれやすいため除外)
    prev_day_return = past["Close"].iloc[-1] / past["Close"].iloc[-2] - 1
    if prev_day_return >= PREV_DAY_MAX_RETURN:
        return None
    # 前日出来高 vs その前の5日平均 (前日自身を平均に含めない)
    base_vol = past["Volume"].iloc[-(VOLUME_DAYS + 1):-1].mean()
    if base_vol and past["Volume"].iloc[-1] / base_vol >= PREV_DAY_MAX_VOL_RATIO:
        return None

    prev_close = float(past["Close"].iloc[-1])
    price = float(intra["Close"].iloc[-1])
    volume = float(intra["Volume"].sum())
    avg_vol = float(past["Volume"].tail(VOLUME_DAYS).mean())
    if not (prev_close and avg_vol):
        return None

    gap = price / prev_close - 1
    
    # 日本株の取引時間(330分)に基づく時間比例の進捗計算
    now = datetime.now()
    minutes_passed = 330  # 終了後など
    if MARKET == "JP":
        if now.hour < 9:
            minutes_passed = 1
        elif now.hour < 11 or (now.hour == 11 and now.minute <= 30):
            minutes_passed = max(1, (now.hour - 9) * 60 + now.minute)
        elif now.hour < 12 or (now.hour == 12 and now.minute < 30):
            minutes_passed = 150  # 昼休み
        elif now.hour < 15 or (now.hour == 15 and now.minute <= 30):
            minutes_passed = 150 + (now.hour - 12) * 60 + (now.minute - 30)

    # 「この時間までに到達しているべき出来高倍率」
    # 例: 9:25(経過25分)で 1日の1.5倍ペースなら、 1.5 * (25/330) = 約 11.3%
    required_ratio = TARGET_DAILY_RATIO * (minutes_passed / 330.0)
    current_ratio = volume / avg_vol
    
    # 当日の始値を取得 (当日の最初の分足のOpen)
    open_price = float(intra["Open"].iloc[0])

    # 当日の高値と現在値の乖離
    high = float(intra["High"].max())
    drop_from_high = 1 - (price / high) if high > 0 else 0
    
    # 除外条件判定
    # 1. ギャップが下限未満、または上限超過 (上がりすぎ除外)
    if not (GAP_THRESHOLD <= gap <= MAX_GAP_THRESHOLD):
        return None
    # 2. 出来高ペースが足りない
    if current_ratio < required_ratio:
        return None
    # 3. 高値から落ちすぎている
    if drop_from_high > CLOSE_TO_HIGH_THRESHOLD:
        return None
    # 4. 陰線 (寄り付きから下がっている「寄り天」除外)
    if price < open_price:
        return None

    return {
        "code": code,
        "prev_close": prev_close,
        "price": price,
        "high": high,
        "drop_from_high": drop_from_high,
        "gap": gap,
        "volume": volume,
        "avg_vol": avg_vol,
        "ratio": current_ratio,
        "required_ratio": required_ratio,
        "date": trade_date,
    }


def notify_slack(webhook_url, hits):
    lines = [f":rotating_light: *強い銘柄アラート* [{MARKET}] ({hits[0]['date']})",
             f"前日比+{GAP_THRESHOLD:.0%}〜{MAX_GAP_THRESHOLD:.0%} / 陽線 / 1日{TARGET_DAILY_RATIO}倍ペース / 高値から{CLOSE_TO_HIGH_THRESHOLD:.1%}以内"]
    for h in sorted(hits, key=lambda x: x["gap"], reverse=True):
        lines.append(
            f"• *{h['code']}*  現在値 {h['price']:,.2f}{CURRENCY} "
            f"(前日比 {h['gap']:+.1%} / 高値 {h['high']:,.2f} から -{h['drop_from_high']:.1%})  "
            f"出来高 {h['volume']:,.0f}株 (5日平均の {h['ratio']:.1%} / 要求ライン {h['required_ratio']:.1%})"
        )
    r = requests.post(webhook_url, json={"text": "\n".join(lines)}, timeout=10)
    r.raise_for_status()


def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("SLACK_WEBHOOK_URL を環境変数に設定してください")

    hits = []
    for code in load_watchlist():
        try:
            result = check_symbol(code)
            if result:
                hits.append(result)
        except Exception as e:
            print(f"{code}: 取得失敗 ({e})")

    if hits:
        notify_slack(webhook_url, hits)
        print(f"{len(hits)}件をSlackに通知しました")
    else:
        print("条件に一致する銘柄はありませんでした (通知なし)")


if __name__ == "__main__":
    main()
