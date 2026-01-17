# Specification

## Overview

ニューストピックからYouTube Shorts / TikTok向けのショート動画を自動生成するCLIアプリケーション。

### Target Users
- コンテンツクリエイター
- ニュースメディア運営者
- SNSマーケター

### Success Metrics
- 1本の動画を10分以内に生成
- 日本語・英語両方の動画を出力
- エラー時に明確なメッセージを表示

---

## User Stories

### US-1: 基本的な動画生成

**As a** コンテンツクリエイター  
**I want to** ニューストピックを入力して動画を生成したい  
**So that** 手作業なしでショート動画コンテンツを作成できる

#### Acceptance Criteria
- [ ] CLIでニューストピック（テキスト）を入力できる
- [ ] 日本語版の動画（MP4）が生成される
- [ ] 英語版の動画（MP4）が生成される
- [ ] 生成された動画は30-60秒の長さである
- [ ] 動画は1080x1920（9:16）の縦型フォーマットである

#### Example
```bash
$ python main.py "Google Veo 3.1発表 - AI動画生成の新時代"

📝 台本を生成中...
✅ 日本語台本を生成しました
✅ 英語台本を生成しました

🎨 画像を生成中...
✅ 4枚の画像を生成しました

🎙️ 音声を生成中...
✅ 日本語音声を生成しました (45秒)
✅ 英語音声を生成しました (42秒)

🎬 動画を合成中...
✅ 日本語動画を生成しました
✅ 英語動画を生成しました

🎉 完了!
   日本語: output/videos/20250115_123456_ja.mp4
   英語:   output/videos/20250115_123456_en.mp4
```

---

### US-2: 言語選択

**As a** ユーザー  
**I want to** 生成する言語を選択したい  
**So that** 必要な言語の動画だけを生成できる

#### Acceptance Criteria
- [ ] `--languages ja` で日本語のみ生成
- [ ] `--languages en` で英語のみ生成
- [ ] `--languages ja en` で両方生成（デフォルト）
- [ ] 画像は共通で使い回される

#### Example
```bash
# 日本語のみ
$ python main.py "ニュース内容" -l ja

# 英語のみ
$ python main.py "ニュース内容" -l en

# 両方（デフォルト）
$ python main.py "ニュース内容" -l ja en
```

---

### US-3: 出力ディレクトリ指定

**As a** ユーザー  
**I want to** 出力先ディレクトリを指定したい  
**So that** ファイルを整理しやすくなる

#### Acceptance Criteria
- [ ] `--output` オプションで出力先を指定できる
- [ ] デフォルトは `./output`
- [ ] ディレクトリが存在しない場合は自動作成
- [ ] サブディレクトリ（audio, images, videos, scripts）も自動作成

#### Example
```bash
$ python main.py "ニュース内容" --output ./my_videos
```

---

### US-4: 進捗表示

**As a** ユーザー  
**I want to** 処理の進捗を確認したい  
**So that** 正常に動作しているか分かる

#### Acceptance Criteria
- [ ] 各ステップの開始時にメッセージを表示
- [ ] 各ステップの完了時に✅を表示
- [ ] エラー時に❌とエラー内容を表示
- [ ] 処理時間を表示

---

### US-5: エラーリカバリー

**As a** ユーザー  
**I want to** エラー時に原因と対処法を知りたい  
**So that** 問題を解決できる

#### Acceptance Criteria
- [ ] APIキー未設定時に明確なエラーメッセージ
- [ ] API呼び出し失敗時に自動リトライ（3回）
- [ ] リトライ失敗時に詳細なエラー情報を表示
- [ ] 中間ファイルは保存され、手動での再開が可能

#### Example Error Messages
```
❌ エラー: ANTHROPIC_API_KEY が設定されていません
   .envファイルに以下を追加してください:
   ANTHROPIC_API_KEY=sk-ant-xxxxx

❌ エラー: ElevenLabs API呼び出しに失敗しました (429: Rate Limited)
   → 3回リトライしましたが失敗しました
   → 数分後に再実行してください
```

---

## Functional Requirements

### FR-1: 台本生成 (Script Generation)

| 項目 | 内容 |
|-----|------|
| 入力 | ニューストピック（テキスト）、言語 |
| 出力 | Script オブジェクト |
| 使用API | Claude API (claude-sonnet-4-20250514) |

#### 出力フォーマット
```json
{
  "language": "ja",
  "title": "動画タイトル（15文字以内）",
  "hook": "最初の5秒で視聴者を引き付けるフック",
  "main_points": ["ポイント1", "ポイント2", "ポイント3"],
  "conclusion": "締めの一言（CTA含む）",
  "full_narration": "完全なナレーション台本",
  "image_prompts": [
    "Scene 1: ...",
    "Scene 2: ...",
    "Scene 3: ...",
    "Scene 4: ..."
  ],
  "estimated_duration": 45
}
```

#### 制約
- image_promptsは必ず英語で出力
- full_narrationは自然な話し言葉
- 4つ以上のimage_promptsを生成

---

### FR-2: 音声生成 (Voice Generation)

| 項目 | 内容 |
|-----|------|
| 入力 | テキスト、言語 |
| 出力 | MP3ファイル |
| 使用API | ElevenLabs API |

#### 設定
- Model: `eleven_multilingual_v2`
- Voice ID (日本語): 環境変数で指定（デフォルト: `EXAVITQu4vr4xnSDxMaL`）
- Voice ID (英語): 環境変数で指定（デフォルト: `21m00Tcm4TlvDq8ikWAM`）
- Stability: 0.5
- Similarity Boost: 0.75

---

### FR-3: 画像生成 (Image Generation)

| 項目 | 内容 |
|-----|------|
| 入力 | プロンプト（英語） |
| 出力 | PNGファイル |
| 使用API | fal.ai (Flux Schnell) |

#### 設定
- Model: `fal-ai/flux/schnell`
- Image Size: `portrait_16_9` (9:16)
- Inference Steps: 4
- 自動追加プロンプト: `high quality, detailed, professional, cinematic lighting`

---

### FR-4: 動画合成 (Video Composition)

| 項目 | 内容 |
|-----|------|
| 入力 | 音声ファイル、画像ファイル群 |
| 出力 | MP4ファイル |
| 使用ツール | FFmpeg |

#### 出力仕様
- 解像度: 1080x1920
- フレームレート: 30fps
- コーデック: H.264 / AAC
- 各画像の表示時間: 音声長 ÷ 画像枚数

---

## Non-Functional Requirements

### NFR-1: パフォーマンス
- 1本の動画生成: 10分以内
- 台本生成: 30秒以内
- 音声生成: 1分以内
- 画像生成（4枚）: 2分以内
- 動画合成: 3分以内

### NFR-2: 信頼性
- API呼び出しは3回までリトライ
- 指数バックオフ（1秒、2秒、4秒）
- 中間ファイルは常に保存

### NFR-3: セキュリティ
- APIキーは環境変数で管理
- .envファイルは.gitignoreに追加

---

## Review & Acceptance Checklist

### 機能テスト
- [ ] 日本語トピックで日本語動画が生成される
- [ ] 英語トピックで英語動画が生成される
- [ ] 両言語を同時に生成できる
- [ ] 画像が正しく生成される（4枚以上）
- [ ] 音声が正しく生成される
- [ ] 動画が正しく合成される（再生可能）

### 品質テスト
- [ ] 動画の解像度が1080x1920
- [ ] 動画の長さが30-60秒
- [ ] 音声と画像が同期している
- [ ] エラー時に適切なメッセージが表示される

### ドキュメント
- [ ] READMEにセットアップ手順が記載されている
- [ ] .env.exampleが提供されている
- [ ] 各関数にdocstringがある
