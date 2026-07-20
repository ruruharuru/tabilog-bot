# TabiLog Bot

世界一周の費用ログをDiscord上で記録・集計・割り勘精算できるBotです。

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `/log_ex` | 支出を記録（モーダル入力。割り勘ONなら参加者選択が続く） |
| `/log_in` | 収入を記録（モーダル入力） |
| `/log_ct` | このチャンネルを国/地域に紐付け |
| `/log_sum` | 国・タグで絞り込み集計 |
| `/log_all` | 全体サマリー（国別内訳つき） |
| `/log_ls` | ID付きで直近の記録を一覧表示 |
| `/log_ed` | IDを指定して項目を修正 |
| `/log_rm` | IDを指定して削除 |
| `/log_wari` | 割り勘の精算額を計算 |

金額は現地通貨で入力すると、[frankfurter.app](https://frankfurter.app)（無料・APIキー不要）で自動的に円換算されます。

## 1. ローカルでのセットアップ

```bash
cd tabilog-bot
python3 -m venv venv
source venv/bin/activate   # Windowsの場合: venv\Scripts\activate
pip install -r requirements.txt
```

### 環境変数の設定

`.env.example` をコピーして `.env` を作成します。

```bash
cp .env.example .env
```

`.env` を開いて、以下の3つを入力してください。

1. **DISCORD_BOT_TOKEN**
   Discord Developer Portal → Bot ページで取得したトークン

2. **GOOGLE_SERVICE_ACCOUNT_JSON**
   Google Cloudでダウンロードしたサービスアカウントの JSON ファイルの中身を、
   **1行のJSON文字列として** そのまま貼り付けます。
   ターミナルで以下のコマンドを実行すると、1行に整形されたJSONが得られます。

   ```bash
   python3 -c "import json; print(json.dumps(json.load(open('service_account.json'))))"
   ```

   （`service_account.json` の部分はダウンロードしたファイル名に置き換えてください）

3. **GOOGLE_SHEET_ID**
   スプレッドシートのURL `https://docs.google.com/spreadsheets/d/【この部分】/edit` の【この部分】

### 起動

```bash
python3 bot.py
```

ターミナルに `Logged in as ...` と表示されればOKです。Discordのメンバーリストでbotが「オンライン」になっているはずです。

## 2. 動作確認

Discordの任意のテキストチャンネルで:

```
/log_ct 国:タイ
```

と打って、そのチャンネルをタイに紐付けます。続けて:

```
/log_ex
```

と打つとモーダル（入力フォーム）が開くので、金額や通貨を入力して送信してみてください。

## 3. 常時稼働（Railwayへのデプロイ）

ローカルでの動作確認ができたら、常時稼働用にRailway等へデプロイします。

1. このフォルダをGitHubリポジトリにpush（`.env` は `.gitignore` で除外されるので安全です）
2. [Railway](https://railway.app) にログイン →「New Project」→「Deploy from GitHub repo」→ このリポジトリを選択
3. Railwayのプロジェクト設定 →「Variables」タブで、`.env` に書いた3つの環境変数を1つずつ登録
   - `DISCORD_BOT_TOKEN`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`
   - `GOOGLE_SHEET_ID`
4. 「Settings」→「Start Command」に `python bot.py` を設定（自動検出されない場合）
5. デプロイが完了すると、Discord上でbotが常時「オンライン」になります

## トラブルシューティング

- **スラッシュコマンドが出てこない**: Botを再起動して数分待つか、Discordクライアントを再起動してください（コマンド登録がDiscord側に反映されるまで時間がかかることがあります）
- **為替レートの取得に失敗する**: 通貨コードが正しいか確認してください（例: タイバーツは `THB`、米ドルは `USD`）
- **Google Sheets関連のエラー**: サービスアカウントのメールアドレスをスプレッドシートに「編集者」として共有できているか確認してください
