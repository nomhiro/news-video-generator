# News Video Generator プロジェクト概要

## 目的
ニューストピックからYouTube Shorts / TikTok向けのショート動画（30-60秒）を自動生成するシステム。

## 技術スタック

### 動画生成パイプライン
- **台本生成**: Azure OpenAI (GPT-4o)
- **音声生成**: Google Cloud TTS (Chirp 3 HD)
- **画像生成**: Google Gemini 3 Pro Image (Nano Banana Pro)
- **動画合成**: FFmpeg

### Web UI (2026年1月追加)
- **フレームワーク**: FastAPI
- **フロントエンド**: HTMX + Tailwind CSS (CDN)
- **ニュースソース**: Google News RSS
- **データ保存**: JSONファイル (data/news/)

## AI関連ニュース機能 (2026年1月追加)
- **カテゴリ**: 「AI・生成AI」カテゴリを追加（既存8カテゴリに加えて9番目）
- **取得方法**: Google News RSS検索クエリ (`/search?q=...`)
- **デフォルト検索クエリ**: 
  - 生成AI, ChatGPT, Claude AI, Claude Code, Gemini AI, GitHub Copilot
  - 大規模言語モデル LLM, OpenAI, Anthropic
  - Stable Diffusion, Midjourney, 画像生成AI
- **設定**: `config.py` の `ai_search_queries` と `ai_news_limit_per_query` で設定可能
- **環境変数**: `AI_SEARCH_QUERIES` (カンマ区切り), `AI_NEWS_LIMIT_PER_QUERY`

## YouTubeアップロード機能 (2026年1月追加)
- **認証**: OAuth2 (google-auth-oauthlib)
- **API**: YouTube Data API v3
- **ファイル**:
  - `src/uploaders/youtube_auth.py` - OAuth2認証
  - `src/uploaders/youtube_uploader.py` - 動画アップロード

## 動画フォーマット (2026年1月追加)

3つの動画フォーマットをサポート:

| フォーマット | 時間 | セグメント数 | 画像枚数 | 話速 | アスペクト比 |
|-------------|------|-------------|---------|------|-------------|
| **short** | ~35秒 | 6個 | 6枚 | 1.25x | 9:16 (縦) |
| **tiktok** | 60-90秒 | 6個 | 6枚 | 1.15x | 9:16 (縦) |
| **long** | ~5分 | 10個 | 10枚 | 1.1x | 16:9 (横) |

### TikTok収益化対応
- TikTokは60秒以上の動画でないと収益化できないため、`tiktok`フォーマットを追加
- ナレーション文字数を増やして60-90秒を達成（日本語: 500-650文字、英語: 250-350語）
- セグメント数は6のまま維持し、各セグメントの長さを延長

### CLI使用例
```bash
python main.py "ニュース" -f short    # ショート(35秒)
python main.py "ニュース" -f tiktok   # TikTok(60-90秒)
python main.py "ニュース" -f long     # ロング(5分)
```

## 主要エントリーポイント

### CLI (元々の機能)
```bash
python main.py "ニュース内容" -l ja
```

### Web UI (新機能)
```bash
python web_app.py --port 8000
# ブラウザで http://localhost:8000 にアクセス
```

## ディレクトリ構成

```
new-video-generator/
├── main.py              # CLI エントリーポイント
├── web_app.py           # Web UI エントリーポイント
├── config.py            # 設定管理
├── src/
│   ├── pipeline.py      # パイプライン制御
│   ├── generators/      # 各種生成器
│   ├── models/          # データモデル
│   ├── news/            # ニュース取得モジュール
│   ├── web/             # FastAPIルート
│   └── utils/           # ユーティリティ
├── templates/           # Jinja2テンプレート
├── data/news/           # ニュースJSONデータ
└── output/              # 生成物出力先
```

## 音声・テキスト同期機能 (2026年1月修正)

音声と画像/テキストのタイミング同期を実現:

1. **ScriptGenerator**: `segment_narrations`を生成（各画像に対応するナレーション部分）
2. **VoiceGenerator**: `generate_with_timings()`でSSML Markを使用し、各セグメントの開始時刻を取得
3. **VideoComposer**: 
   - `segment_timings`を受け取り、テキストオーバーレイの表示タイミングに直接使用
   - 画像の表示時間も音声タイミングに基づいて計算

## プロンプト最適化 (2026年1月追加)

スクリプト生成プロンプトをOpenAI/Anthropicベストプラクティスに基づいて最適化：
- XMLタグで構造化（`<role>`, `<task>`, `<critical_constraints>`, `<content_rules>`, `<output_format>`, `<verification>`）
- 配列整合性制約を`<critical_constraints>`タグで最上位に配置
- 空文字列禁止を明示
- `_validate_script()`メソッドでスクリプト生成後の検証を追加

## 環境変数 (.env)
- AZURE_OPENAI_ENDPOINT
- AZURE_OPENAI_API_KEY
- AZURE_OPENAI_DEPLOYMENT
- GOOGLE_CLOUD_PROJECT
- GOOGLE_APPLICATION_CREDENTIALS (オプション)
