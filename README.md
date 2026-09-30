# TabiLog Bot

世界一周の費用ログをDiscord上で記録・集計・割り勘精算できるBotです。

> **初めて使う人へ**: Discord上の操作は [使い方ガイド](docs/使い方.md) を見てください（図付き）。

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `/log_ex` | 支出を記録（金額入力→タグ・割り勘・集計対象を選択） |
| `/log_in` | **個人予算**を記録（所有者＝実行者） |
| `/log_memo` | 自分用の買い物メモを記録（共有ログには入らない） |
| `/log_memo_ls` / `rm` | 自分の買い物メモの一覧・削除 |
| `/log_ct` | このスレッド/チャンネルを国/地域に紐付け |
| `/log_tag_add` / `rm` / `ls` | タグの追加・削除・一覧 |
| `/log_sum` | 共有集計＋個人実質負担・個人予算残高（自分だけ） |
| `/log_all` | ①共有/国別 ②メモ ③実質負担 ④建て替え確定 |
| `/log_ls` | ID付きで直近の記録を一覧表示（自分だけに表示） |
| `/log_ed` | IDを指定して記録を確認・修正 |
| `/log_rm` | IDを複数指定して削除 |
| `/log_wari` | 割り勘のライブ精算（確定スナップショットがあれば併記） |
| `/log_settle` | 現在の `/log_wari` 結果を建て替え確定として保存 |
| `/log_settle_ls` | 最新の建て替え確定を再表示 |

金額は現地通貨で入力すると、自動的に円換算されます。

### はる単独運用

同行者クローズ後は、同一 Log を継続したまま個人予算運用に寄せます。

1. `DEFAULT_SPLIT_PARTICIPANT_IDS` を空にする（割り勘の既定参加者なし）
2. `/log_wari` で確認 → `/log_settle` で建て替えを確定保存
3. 以後の `/log_ex` は原則「割り勘なし」（決済者1人）
4. `/log_in` は実行者の個人予算。残高は `/log_sum` / `/log_all` ③で確認
5. 国別・全体の共有支出は `/log_all` ①のまま（Log は分割しない）

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

任意で、以下も設定できます。

- **DEFAULT_SPLIT_PARTICIPANT_IDS**
  割り勘の参加者欄に最初から選択しておくユーザーID（カンマ区切り、最大10人）。
  **単独運用では空のまま**にしてください（`.env.example` の既定）。
  複数人で割り勘する場合のみ例のように設定:
  `DEFAULT_SPLIT_PARTICIPANT_IDS=123456789012345678,223456789012345678`
  IDは Discord の設定 → 詳細設定で「開発者モード」をONにし、
  ユーザーを右クリック →「ユーザーIDをコピー」で取得できます。

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

## 3. 常時稼働（Northflankへのデプロイ）

ローカルでの動作確認ができたら、常時稼働用に [Northflank](https://northflank.com) へデプロイします。  
無料の **Developer Sandbox** で、スリープなしの常時稼働ができます（サービス2つまで）。

> Railway と同時に起動すると Bot が二重起動します。Northflank 側でオンラインを確認したら、Railway のサービスは停止してください。

1. このフォルダを GitHub リポジトリに push（`.env` は `.gitignore` で除外されるので安全です）
2. [Northflank](https://app.northflank.com/signup) に GitHub で登録する
   - プランは **Developer Sandbox** を選ぶ
   - カード登録を求められることがあるが、Sandbox のままなら通常は課金されない（料金画面は時々確認する）
3. **Create New → Project** でプロジェクトを作る（リージョンは US / UK どちらでも可）
4. **Create New → Service → Combined** を選ぶ
   - Repository: このリポジトリ / ブランチ `main`
   - Build: **Dockerfile**（リポジトリ直下の `Dockerfile` が自動検出される）
   - Networking: **ポートは追加しない**（Discord Bot に公開 URL は不要）
   - Resources: Sandbox なら **nf-compute-20**（512MB）を推奨
5. Runtime variables に `.env` と同じ環境変数を登録する
   - `DISCORD_BOT_TOKEN`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`（JSON を1行のまま貼る。Northflank は JSON 値に対応）
   - `GOOGLE_SHEET_ID`
   - （任意）`DEFAULT_SPLIT_PARTICIPANT_IDS`（単独運用なら空／未設定）
6. Create Service → ビルド〜起動まで数分待つ
7. サービスログに `Logged in as ...` が出て、Discord 上で Bot が「オンライン」になれば完了

### うまくいかないとき

- **ビルドは成功するがすぐ落ちる**: Runtime variables が足りないか、値の引用符が余分。ログの `DISCORD_BOT_TOKEN` / Sheets 関連エラーを確認
- **カード認証で止まる**: Sandbox でも初回はカード確認がある。プリペイドは通らないことがある
- **無料枠を超えて課金されそう**: サービスは1つ、プランは Developer Sandbox、インスタンス数は1のままにする

## トラブルシューティング

- **スラッシュコマンドが出てこない**: Botを再起動して数分待つか、Discordクライアントを再起動してください（コマンド登録がDiscord側に反映されるまで時間がかかることがあります）
- **為替レートの取得に失敗する**: 通貨コードが正しいか確認してください（例: タイバーツは `THB`、米ドルは `USD`）
- **Google Sheets関連のエラー**: サービスアカウントのメールアドレスをスプレッドシートに「編集者」として共有できているか確認してください
