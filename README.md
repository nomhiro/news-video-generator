# News Video Generator

ニューストピックからYouTube Shorts / TikTok向けのショート動画を自動生成するシステム

## 🎯 概要

このプロジェクトは **Spec Kit** を使った仕様駆動開発（Spec-Driven Development）で実装します。

### 主要機能
1. **台本生成**: Claude APIでニュースから動画用台本を生成
2. **音声生成**: ElevenLabs APIでナレーション音声を生成
3. **画像生成**: fal.ai (Flux)でシーン画像を生成
4. **動画合成**: FFmpegで音声+画像を動画に合成

### 出力
- 日本語版動画 (MP4, 1080x1920, 30-60秒)
- 英語版動画 (MP4, 1080x1920, 30-60秒)

---

## 🚀 Spec Kit で実装する手順

### Step 1: プロジェクトをセットアップ

```bash
# このフォルダをダウンロードして移動
cd news_video_generator

# Spec Kit で初期化（specs/ フォルダが既にあるのでスキップ可能）
# uvx --from git+https://github.com/github/spec-kit.git specify init . --ai claude
```

### Step 2: Claude Code を起動

```bash
claude
```

### Step 3: 仕様を確認させる

```
specs/フォルダにある仕様書を読んでプロジェクトの概要を理解してください
```

### Step 4: 実装を開始

```
/speckit.implement
```

または、タスクを1つずつ実行：

```
specs/tasks.md のTask 1.1から順番に実装してください
```

---

## 📁 ドキュメント構成

```
specs/
├── constitution.md    # プロジェクト方針・制約
├── spec.md           # 機能仕様・ユーザーストーリー
├── plan.md           # 技術計画・アーキテクチャ
└── tasks.md          # 実装タスク一覧 ★ここが重要

docs/
├── REQUIREMENTS.md   # 詳細な要件定義
├── SPECIFICATION.md  # 詳細な技術仕様
└── SPECKIT_GUIDE.md  # Spec Kit の使い方
```

### 読む順番
1. `specs/constitution.md` - プロジェクトの基本ルール
2. `specs/spec.md` - 何を作るか
3. `specs/plan.md` - どう作るか
4. `specs/tasks.md` - 具体的な実装タスク

---

## ⚡ クイックスタート（Spec Kit なし）

Spec Kit を使わずに直接 Claude Code に指示する場合：

```
このプロジェクトを実装してください。

仕様: specs/spec.md
技術計画: specs/plan.md
タスク: specs/tasks.md

tasks.md のTask 1.1から順番に実装し、
各タスクのAcceptance Criteriaを満たしてください。
```

---

## 🔧 必要な準備

### APIキー
実装前に以下のアカウントを準備してください：

1. **Anthropic** - Claude API（お持ちの場合はそのまま使用）
2. **ElevenLabs** - https://elevenlabs.io/ （無料枠あり）
3. **fal.ai** - https://fal.ai/ （無料枠あり）

### システム要件
- Python 3.11+
- FFmpeg（動画合成に必要）

---

## 📚 参考ドキュメント

- `docs/REQUIREMENTS.md` - 詳細な要件定義
- `docs/SPECIFICATION.md` - 詳細な技術仕様
- `docs/SPECKIT_GUIDE.md` - Spec Kit の詳しい使い方
