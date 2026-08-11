# News Video Generator

ニューストピックから YouTube Shorts / TikTok / 長尺向けの動画を自動生成するツール。

台本・音声・画像・字幕を生成し、ffmpeg で1本の動画に合成する。
YouTube と TikTok へのアップロードにも対応する。

| 工程 | 使用サービス |
|---|---|
| 台本生成 | Azure OpenAI（Responses API + Structured Outputs） |
| 画像生成 | Azure OpenAI `gpt-image-2` |
| 音声合成 | Google Cloud Text-to-Speech（Chirp 3 HD） |
| 動画合成 | ffmpeg |
| ニュース取得 | Google News RSS |
| アップロード | YouTube Data API v3 / TikTok Content Posting API |

出力形式は3種類。

| 形式 | 解像度 | 長さの目安 | 用途 |
|---|---|---|---|
| `short` | 1080x1920 | 約35秒 | YouTube Shorts |
| `tiktok` | 1080x1920 | 60〜90秒 | TikTok |
| `long` | 1920x1080 | 約5分 | YouTube 通常動画 |

---

## セットアップ

### 1. 必要なもの

- **Python 3.13 以上**
- **[uv](https://docs.astral.sh/uv/)** — 依存管理
- **ffmpeg / ffprobe** — PATH に入っていること

```bash
# ffmpeg のインストール
winget install FFmpeg          # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
```

### 2. クラウド側の準備

**Azure OpenAI（必須）**

Azure AI Foundry で **2つのデプロイ**を作成する。

| 用途 | モデル | 備考 |
|---|---|---|
| 台本生成 | `gpt-5.1` 以降 | Responses API と Structured Outputs に対応している世代が必要 |
| 画像生成 | `gpt-image-2` | GA なのでアクセス申請は不要 |

作成したデプロイ名を確認する。**デプロイ名はモデル名と一致しないことが多い**ので、
必ず実際の名前を使うこと。

```bash
az cognitiveservices account deployment list -n <resource> -g <resource-group> -o table
```

> **画像生成のクォータについて**
> `gpt-image-2` の既定クォータは 5 images/min 程度で、これが生成速度の律速になる。
> ショート1本で6枚、長尺で10枚以上使うため、実用するならクォータの引き上げ申請を推奨する。

**Google Cloud Text-to-Speech（必須）**

Text-to-Speech API を有効にしたプロジェクトを用意し、次のいずれかで認証する。

- サービスアカウント JSON を作成し、パスを `GOOGLE_APPLICATION_CREDENTIALS` に設定
- または `gcloud auth application-default login` で ADC を使う（この場合は変数を設定しない）

**YouTube / TikTok アップロード（任意）**

- YouTube: Google Cloud Console で OAuth クライアントを作成し、`client_secrets.json` として置く
- TikTok: TikTok Developer Portal でアプリを作成（必要スコープ: `video.publish`, `video.upload`）

### 3. 環境変数

```bash
cp .env.example .env
# .env を編集して各値を設定する
```

最低限必要なのは以下。全項目の説明は `.env.example` にある。

```dotenv
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=<台本生成のデプロイ名>
AZURE_OPENAI_IMAGE_DEPLOYMENT=<画像生成のデプロイ名>
GOOGLE_CLOUD_PROJECT=<project-id>
GOOGLE_APPLICATION_CREDENTIALS=<service-account.json のパス>
```

### 4. 依存のインストール

```bash
uv sync
```

---

## 使い方

### CLI で1本生成する

```bash
uv run python main.py "OpenAI が新モデルを発表" -l ja -f short -v
```

| オプション | 説明 |
|---|---|
| `-l`, `--languages` | `ja` / `en`（複数指定可。既定は `ja en`） |
| `-f`, `--format` | `short` / `tiktok` / `long`（既定は `short`） |
| `-o`, `--output` | 出力ディレクトリ（既定は `./output`） |
| `-v`, `--verbose` | 詳細ログ |

生成物は `output/` 以下に出る。

```
output/
├── scripts/<timestamp>_<lang>.json   # 台本（レビュー用）
├── images/<timestamp>/image_*.png    # 生成画像
├── audio/<timestamp>_<lang>.mp3      # ナレーション
└── videos/<timestamp>_<lang>.mp4     # 完成動画
```

### Web UI で使う

```bash
uv run python web_app.py
```

<http://127.0.0.1:8000> を開く。ニュースを取得して記事を選び、動画を生成して
そのまま YouTube / TikTok にアップロードできる。

> **既知の制約**: 動画生成中は Web サーバー全体が応答しなくなる。
> 生成が終わるまで画面の操作はできない（リファクタリングで対応予定）。

---

## 開発

```bash
uv run ruff check . && uv run ruff format .   # lint と整形
uv run mypy                                    # 型チェック
uv run pytest                                  # テスト
uv run pytest -m slow                           # ffmpeg を実際に起動するテスト
uv run pytest -m live                           # 実APIを叩く（課金あり）
```

`pytest` は既定で `slow` と `live` を除外するため、外部サービスも ffmpeg も不要で走る。
CI（`.github/workflows/ci.yml`）もこの既定で動く。

CI には週次の cron ジョブがあり、使用中の AI モデルが廃止予定に近づくと失敗する。
使用モデルは `src/model_registry.py` に集約されている。

プロジェクト固有の注意点（デプロイ名とモデル名の違い、画像の生成解像度が
出力解像度と異なる理由、台本スキーマの制約など）は [`CLAUDE.md`](CLAUDE.md) に
まとめてある。
