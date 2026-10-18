# Premarket Alert (日本株 寄り付き・トレンド監視システム)

毎朝、東証の全上場銘柄から強いトレンドを持つ銘柄を抽出し、寄り付き直後に「ギャップアップし、かつ出来高が急増している」銘柄をSlackに自動通知するシステムです。

## ⚙️ システムの流れ（完全自動）

1. **07:55 - Macが自動起床**
   - スリープ状態から自動的に復帰します。
2. **08:00 - ウォッチリストの自動更新** (`update_watchlist.py`)
   - 東証の全銘柄データを取得し、厳しい条件（流動性・トレンド・高値圏）でスクリーニング。
   - 合格した優良銘柄だけを `watchlist.txt` に書き出します。
3. **09:25 - 寄り付き直後の急騰検知** (`premarket_alert.py`)
   - `watchlist.txt` の銘柄群に対し、当日の寄り付き状況を確認。
   - ギャップアップ＆出来高急増などの条件を満たした銘柄をSlackへ通知します。

---

## 🔍 スクリーニング・フィルターの仕様

### 1. ウォッチリスト抽出条件 (8:00時点)
- **株価水準**: 250円 以上
- **流動性**: 25日平均売買代金が 5億円 以上
- **トレンド**: 以下のどちらかを満たす
  - 株価が **25日線** より上 かつ 25日線が **上向き**
  - 株価が **75日線** より上 かつ 75日線が **上向き**
- **高値圏**: 直近株価が、過去60日間の最高値から **10%以内** (抵抗線付近) に位置

### 2. 急騰アラート通知条件 (9:25時点)
- **適度なギャップアップ**: 前日終値比 **+3% 〜 +10% 以内**（ストップ高など上がりすぎている銘柄は除外）
- **陽線キープ**: 現在値が **当日の始値より高い**（寄り天など、寄り付きから下がっている銘柄を除外）
- **高値圏キープ**: 当日の最高値から **1%以内**（上ヒゲをつけて失速していないか）
- **出来高急増**: 1日の終わり時点で、過去5日平均の **1.5倍** になるペースで推移しているか（時間比例で算出）
- **前日急騰の除外**: 前日の騰落率が **+5% 以上**、または前日出来高がその前5日平均の **2倍以上** の銘柄は除外（翌日の利確売りに巻き込まれやすいため）

*(※ しきい値は `premarket_alert.py` および `update_watchlist.py` の上部の定数で変更可能です)*

---

## 🚀 セットアップと手動コマンド

既に設定は完了していますが、設定を変更したり手動でテストしたい場合は以下のコマンドを利用してください。

### 1. ウォッチリスト更新の手動実行
銘柄リストの抽出を今すぐ手動で実行したい場合はこちら。
```bash
cd ~/premarket_alert
./run_update.sh
```
*(※ 100銘柄ずつ2秒間隔で取得するため、完了まで5分〜10分程度かかります。進行状況は画面には出ませんが `update.log` に出力されます)*

### 2. アラート通知の「通常テスト」
設定している実際の条件のまま、今の時間で判定させたい場合はこちら。
```bash
cd ~/premarket_alert
./run.sh
```
*(※ 条件を満たす強い銘柄がなければSlackには何も通知されません。実行結果は `alert.log` を確認してください)*

### 3. アラート通知の「強制テスト」
条件を満たす銘柄があるかどうかにかかわらず、必ずSlackへ通知を飛ばして連携を確認したい場合はこちら。
(※条件を -1% / 要求出来高 0倍 に緩めて強制的に発火させます)
```bash
cd ~/premarket_alert
GAP_THRESHOLD=-1 TARGET_DAILY_RATIO=0 CLOSE_TO_HIGH_THRESHOLD=1.0 ./run.sh
```
*(※ 東証が開いている平日 9:00〜15:30 以外に実行しても当日データがないため通知されません)*

### 4. ログファイルではなく、画面で結果を見ながら直接実行
`run.sh` を使うと結果が `alert.log` に保存されて画面には出ません。エラー文や取得状況をターミナルの画面で直接確認したい場合は、以下のように環境変数と仮想環境のPythonを指定して直接実行します。
```bash
cd ~/premarket_alert
set -a; source secrets.env; set +a
/Users/msngkntr/.venv/bin/python premarket_alert.py
```

### Macの自動起床時間の変更
現在は `07:55` に設定されています。変更したい場合は以下のコマンドを実行します。
```bash
sudo pmset repeat wakeorpoweron MTWRF 07:55:00
```
設定状況の確認: `pmset -g sched`

### 自動実行(launchd)の登録・解除
設定ファイルは `com.msngkntr.update_watchlist.plist` および `com.msngkntr.premarket_alert.plist` です。

**登録を解除して自動実行を止める:**
```bash
launchctl unload ~/Library/LaunchAgents/com.msngkntr.update_watchlist.plist
launchctl unload ~/Library/LaunchAgents/com.msngkntr.premarket_alert.plist
```

**再度登録する:**
```bash
launchctl load ~/Library/LaunchAgents/com.msngkntr.update_watchlist.plist
launchctl load ~/Library/LaunchAgents/com.msngkntr.premarket_alert.plist
```
*(※ `launchctl list | grep premarket` で登録状況を確認できます)*

---

## 📁 構成ファイル一覧
- `premarket_alert.py` : 9:25に動くアラート判定スクリプト
- `update_watchlist.py` : 8:00に動く銘柄リスト更新スクリプト
- `run.sh` / `run_update.sh` : それぞれを実行するためのシェルスクリプト
- `secrets.env` : Slack Webhook URL が記載された機密ファイル
- `watchlist.txt` : 自動更新される監視対象の銘柄一覧
- `*.plist` : Macのlaunchdにスケジュールを登録するための設定ファイル
- `alert.log` / `update.log` : 実行結果のログファイル

