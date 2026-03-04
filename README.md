# 家族カレンダー LINE Bot

LINEで「3/4 飲み会」と送ると自動でカレンダーに登録されるBotです。

## セットアップ手順

### 1. GitHubにアップロード
- GitHubで新しいリポジトリを作成（例：`family-calendar`）
- このフォルダのファイルをすべてアップロード

### 2. Renderでデプロイ
1. render.com にログイン
2. 「New +」→「Web Service」
3. GitHubのリポジトリを選択
4. 以下を設定：
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
5. 「Environment Variables」に以下を追加：
   - `LINE_CHANNEL_ACCESS_TOKEN` = LINE DevelopersのChannel Access Token
   - `LINE_CHANNEL_SECRET` = LINE DevelopersのChannel Secret
   - `APP_URL` = RenderのURL（例：https://family-calendar.onrender.com）
6. 「Create Web Service」でデプロイ

### 3. LINE DevelopersでWebhook設定
1. LINE Developers → チャンネル設定 → Messaging API
2. Webhook URL に `https://あなたのRenderのURL/webhook` を入力
3. 「Webhookの利用」をONにする
4. 「応答メッセージ」をOFFにする（Botが二重返信しないように）

### 使い方
- LINEでBotに「3/4 飲み会」「10/15 病院」などと送信
- 自動でカレンダーに登録されます
- カレンダーは `https://あなたのURL/calendar` で確認
- URLを旦那さんと共有すれば二人で見られます！
