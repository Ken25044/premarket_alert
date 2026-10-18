"""寄り直後/プレマーケットの強い銘柄チェック → Slack通知 (yfinance のみ・無料)

条件の詳細は README.md の「売買手法とフィルター条件」を参照。判定ロジックは strategy.py。

環境変数:
  SLACK_WEBHOOK_URL  Slack Incoming Webhook URL (必須)
  MARKET             JP (既定) / US
  MIN_CHANGE                前日比の下限 (既定 0.03)  ※旧名 GAP_THRESHOLD
  MAX_CHANGE                前日比の上限 (既定 0.10)  ※旧名 MAX_GAP_THRESHOLD
  RVOL_THRESHOLD            時刻別RVOLの下限 (既定 1.5) ※旧名 TARGET_DAILY_RATIO
  CLOSE_TO_HIGH_THRESHOLD   当日高値からの下落率の上限 (既定 0.01 = 1%以内)
  PREV_DAY_MAX_RETURN       前日騰落率がこれ以上なら除外 (既定 0.05 = +5%)
  PREV_DAY_MAX_VOL_RATIO    前日出来高 / その前5日平均 がこれ以上なら除外 (既定 2.0)
  LATEST_RUN_TIME           JPでこの時刻を過ぎて起動したら判定せずに終了 (既定 09:40)
  FORCE_RUN                 1 なら LATEST_RUN_TIME のガードを無視 (手動テスト用)
  CATALYST_MODE             材料フィルター。off (既定・テクニカルのみ) / tag (材料銘柄に📰を付ける)
                            / only (材料銘柄だけ通知)
  CATALYST_FILE             材料ファイルのパス (既定 catalysts.csv)
"""
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import strategy
from strategy import CURRENCY, MARKET, SUFFIX, TZ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt" if MARKET == "JP" else "watchlist_us.txt")
LATEST_RUN_TIME = os.environ.get("LATEST_RUN_TIME", "09:40")
MAX_FAIL_RATIO = 0.10      # 取得失敗がこれを超えたら警告
MAX_DATA_LAG_MIN = 30      # データ時刻が実行時刻からこれ以上遅れていたら警告
CATALYST_MODE = os.environ.get("CATALYST_MODE", "off").strip().lower()
CATALYST_FILE = os.environ.get("CATALYST_FILE", os.path.join(BASE_DIR, "catalysts.csv"))


def load_watchlist():
    """銘柄コード/ティッカーと、ヘッダーのメタ情報 (更新日など) を読み込む"""
    codes, meta = [], {}
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"#\s*(\S+?):\s*(.+)", line)
            if m:
                meta[m.group(1)] = m.group(2).strip()
            line = line.split("#")[0].strip()
            if line:
                codes.append(line)
    return codes, meta


def run_checks(codes, now, catalysts=None):
    symbols = [f"{c}{SUFFIX}" for c in codes]
    intraday = strategy.download(symbols, period="8d", interval="1m", prepost=(MARKET != "JP"))
    daily = strategy.download(symbols, period="1mo", interval="1d")

    today = now.date()
    hits, stats = [], {"total": len(codes), "failed": 0, "no_today": 0, "nodata": 0, "evaluated": 0}
    data_times = []
    for code, sym in zip(codes, symbols):
        intra, day = intraday.get(sym), daily.get(sym)
        if intra is None or day is None:
            stats["failed"] += 1
            continue
        today_bars = intra[intra.index.date == today]
        if today_bars.empty:
            stats["no_today"] += 1
            continue
        past = day[day.index.date < today]
        if not strategy.prev_day_ok(past):
            stats["evaluated"] += 1
            continue
        today_daily = day[day.index.date == today]
        # 1分足は寄り直後の数本が欠けるので、始値は日足の当日Openを優先する
        today_open = float(today_daily["Open"].iloc[0]) if len(today_daily) else float(today_bars["Open"].iloc[0])
        r = strategy.evaluate(today_bars, intra[intra.index.date < today],
                              float(past["Close"].iloc[-1]), today_open)
        if "data_time" in r:
            data_times.append(r["data_time"])
        if r["status"] == "nodata":
            stats["nodata"] += 1
            continue
        stats["evaluated"] += 1
        if r["status"] == "hit":
            hits.append({"code": code, "catalyst": strategy.catalyst_memo(catalysts or {}, today, code), **r})
    stats["tech_hits"] = len(hits)
    if CATALYST_MODE == "only":
        hits = [h for h in hits if h["catalyst"] is not None]
    stats["hits"] = len(hits)
    stats["data_time"] = max(data_times) if data_times else None
    return hits, stats


def catalyst_status(now, catalysts, codes):
    """材料ファイルの状況を (サマリーに付ける文字列, 警告のリスト) で返す"""
    if CATALYST_MODE == "off":
        return "", []
    if catalysts is None:
        return f" | 材料[{CATALYST_MODE}] ファイルなし", [f"材料ファイルがない ({os.path.basename(CATALYST_FILE)})"]
    today = now.date()
    if today not in catalysts:
        return f" | 材料[{CATALYST_MODE}] 今日の記録なし", ["今日の材料が記録されていない"]
    picks = catalysts[today]
    outside = [c for c in picks if c not in set(codes)]
    note = f" (うちリスト外 {len(outside)}: {' '.join(outside)})" if outside else ""
    return f" | 材料[{CATALYST_MODE}] {len(picks)}銘柄{note}", []


def status_line(now, stats, meta, catalyst=("", [])):
    """毎朝送る1行サマリー。異常 (取得失敗・リストが古い・データ遅延) があれば ⚠️ を付ける"""
    warnings = []
    s = stats
    ok = s["total"] - s["failed"]
    if s["total"] and s["failed"] / s["total"] > MAX_FAIL_RATIO:
        warnings.append("取得失敗が多い")
    if ok and s["no_today"] == ok:
        warnings.append("当日データなし (休場日 or 取得失敗)")

    if s["data_time"] is not None:
        lag = int((now - s["data_time"]).total_seconds() // 60)
        data = f"データ時刻 {s['data_time']:%H:%M} (遅れ{lag}分)"
        if lag > MAX_DATA_LAG_MIN:
            warnings.append("データの遅れが大きい")
    else:
        data = "データ時刻 -"

    updated = meta.get("更新日", "不明")
    if not updated.startswith(now.strftime("%Y-%m-%d")):
        warnings.append("ウォッチリストが今日更新されていない")
    catalyst_note, catalyst_warnings = catalyst
    warnings += catalyst_warnings
    fetched = f" (取得 {meta['取得成功']})" if "取得成功" in meta else ""

    icon = ":warning:" if warnings else ":clipboard:"
    line = (f"{icon} {now:%m/%d %H:%M} [{MARKET}] {s['total']}銘柄チェック / 取得失敗 {s['failed']}"
            f" / 当日データなし {s['no_today']} / データ不足 {s['nodata']} / 判定 {s['evaluated']}"
            f" / 該当 {s['hits']}" + (f" (テクニカルのみ {s['tech_hits']})" if CATALYST_MODE == "only" else "")
            + f" | {data} | リスト更新 {updated}{fetched}{catalyst_note}")
    if warnings:
        line += "\n:warning: " + " / ".join(warnings)
    return line


def hits_message(hits):
    only = " (材料銘柄のみ)" if CATALYST_MODE == "only" else ""
    lines = [f":rotating_light: *強い銘柄アラート* [{MARKET}]{only} ({hits[0]['data_time']:%Y-%m-%d})",
             strategy.condition_summary()]
    for h in sorted(hits, key=lambda x: x["change"], reverse=True):
        mark, memo = "", ""
        if CATALYST_MODE != "off" and h.get("catalyst") is not None:
            mark = f"{strategy.CATALYST_MARK} "
            memo = f"\n    {strategy.CATALYST_MARK} {h['catalyst']}" if h["catalyst"] else ""
        lines.append(
            f"• {mark}*{h['code']}*  {h['data_time']:%H:%M}時点 {h['price']:,.2f}{CURRENCY} "
            f"(前日比 {h['change']:+.1%} / 寄りギャップ {h['open_gap']:+.1%} / 高値 {h['high']:,.2f} から -{h['drop_from_high']:.1%})  "
            f"RVOL {h['rvol']:.1f}倍 (同時刻までの出来高 {h['volume']:,.0f} / 過去平均 {h['base_vol']:,.0f}){memo}"
        )
    return "\n".join(lines)


def post_slack(webhook_url, text):
    r = requests.post(webhook_url, json={"text": text}, timeout=10)
    r.raise_for_status()


def main():
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        sys.exit("SLACK_WEBHOOK_URL を環境変数に設定してください")
    if CATALYST_MODE not in strategy.CATALYST_MODES:
        sys.exit(f"CATALYST_MODE は {' / '.join(strategy.CATALYST_MODES)} のどれかにしてください ({CATALYST_MODE!r})")

    now = datetime.now(ZoneInfo(TZ))
    # スリープ復帰で launchd が遅れて起動した場合、古いシグナルで発注しないよう何もしない
    if MARKET == "JP" and now.strftime("%H:%M") > LATEST_RUN_TIME and os.environ.get("FORCE_RUN") != "1":
        msg = f":double_vertical_bar: {now:%m/%d %H:%M} [{MARKET}] 起動が {LATEST_RUN_TIME} を過ぎたので判定をスキップしました"
        print(msg)
        post_slack(webhook_url, msg)
        return

    codes, meta = load_watchlist()
    catalysts = None
    if CATALYST_MODE != "off" and os.path.exists(CATALYST_FILE):
        catalysts = strategy.load_catalysts(CATALYST_FILE)
    hits, stats = run_checks(codes, now, catalysts)
    status = status_line(now, stats, meta, catalyst_status(now, catalysts, codes))
    print(status)

    text = f"{hits_message(hits)}\n{status}" if hits else status
    post_slack(webhook_url, text)
    print(f"{len(hits)}件をSlackに通知しました")


if __name__ == "__main__":
    main()
