# ショート・ロング動画切り替え機能の実装プラン

## 概要
現在のショート動画（30-60秒）生成に加え、3-5分のロング動画生成をサポートし、UIで選択可能にする。

## 主要な違い

| 項目 | ショート動画 | ロング動画 |
|------|-------------|-----------|
| 長さ | 30-60秒 | 約5分 |
| アスペクト比 | 9:16（縦長） | 16:9（横長） |
| 解像度 | 1080x1920 | 1920x1080 |
| 画像スタイル | シネマティック | 1枚目: イメージ画像、2枚目以降: 説明解説資料風 |
| 画像枚数 | 6-7枚（固定） | AIが内容に応じて決定 |
| 話速 | 1.25倍速 | 1.1倍速 |

## ユーザーの仮定の検証結果

| 仮定 | 結果 | 理由 |
|------|------|------|
| バックエンドフローは変更不要 | **ほぼ正しい** | パイプライン構造（Script→Image→Voice→Video）は汎用的で変更不要 |
| プロンプト変更が必要 | **正しい** | ナレーション長、画像スタイル、アスペクト比の制約を変更する必要あり |

**追加で必要な変更**:
- `video_format`パラメータの受け渡し
- ImageGenerator/VideoComposerのアスペクト比対応

## 変更が必要なファイル

### 1. ScriptGenerator (主要変更)
**ファイル**: [script_generator.py](src/generators/script_generator.py)

**変更内容**:
- ロング動画用プロンプト `SYSTEM_PROMPT_LONG_JA` / `SYSTEM_PROMPT_LONG_EN` を追加
- `generate()` メソッドに `video_format` パラメータを追加
- `_build_system_prompt()` を `video_format` に対応させる

**ロング動画のプロンプト仕様**:
| 項目 | ショート (35秒) | ロング (約5分) |
|------|-----------------|----------------|
| 日本語ナレーション | 250-300文字 | 約2000-2500文字（5分相当） |
| 英語ナレーション | 120-150 words | 約750-900 words（5分相当） |
| 画像枚数 | 6-7枚（固定） | AIが解説内容に応じて決定 |
| メインポイント | 3-4個 | 8-12個 |
| estimated_duration | 35 | 約300（5分） |
| 画像スタイル | シネマティック | 1枚目: イメージ画像、2枚目以降: 説明解説資料風（図解・チャート） |

### 2. Pipeline (パラメータ追加)
**ファイル**: [pipeline.py](src/pipeline.py)

**変更内容**:
- `run()` メソッドに `video_format: str = "short"` パラメータを追加
- `script_generator.generate()` 呼び出し時に `video_format` を渡す

### 3. Web Routes (パラメータ受け渡し)
**ファイル**: [routes.py](src/web/routes.py)

**変更内容**:
- `/generate` エンドポイントに `video_format: str = Form("short")` を追加
- `generate_videos_task()` に `video_format` パラメータを追加
- `pipeline.run()` 呼び出し時に `video_format` を渡す

### 4. UI Template (セレクター追加)
**ファイル**: [selected_panel.html](templates/partials/selected_panel.html)

**変更内容**:
- 動画形式選択用ラジオボタンを追加
- フォームで `video_format` を送信するように修正

### 5. VoiceGenerator (話速調整)
**ファイル**: [voice_generator.py](src/generators/voice_generator.py)

**変更内容**:
- `generate()` と `generate_with_timings()` に `speaking_rate` パラメータを追加
- ショート動画: 1.25倍速（現状維持）
- ロング動画: 1.1倍速（長時間視聴に適した自然なペース）

### 6. ImageGenerator (アスペクト比・スタイル対応)
**ファイル**: [image_generator.py](src/generators/image_generator.py)

**変更内容**:
- `generate_batch()` に `video_format` パラメータを追加
- アスペクト比を `video_format` に応じて変更:
  - ショート: 9:16（縦長）
  - ロング: 16:9（横長）
- プロンプト強化はScriptGeneratorのプロンプトに任せる（画像プロンプト自体に1枚目/2枚目以降のスタイル指示が含まれる）

### 7. VideoComposer (解像度対応)
**ファイル**: [video_composer.py](src/generators/video_composer.py)

**変更内容**:
- `compose()` に `video_format` パラメータを追加
- 出力解像度を `video_format` に応じて変更:
  - ショート: 1080x1920（縦長）
  - ロング: 1920x1080（横長）
- テキストオーバーレイの位置を解像度に応じて調整

## 実装手順

### Step 1: ScriptGenerator にロング動画プロンプトを追加
1. `SYSTEM_PROMPT_LONG_JA` 定数を追加
   - 1400-2100文字のナレーション
   - 画像枚数は固定せず「解説内容に応じて適切な枚数を決定」と指示
   - 画像プロンプトのスタイル:
     - **1枚目**: 動画内容を把握できるイメージ画像（サムネイル的、cinematic）
     - **2枚目以降**: 説明解説資料風（図解・チャート・要点まとめ、infographic style）
2. `SYSTEM_PROMPT_LONG_EN` 定数を追加（同様の変更）
3. `generate()` メソッドのシグネチャを変更
4. `_build_system_prompt()` を更新

### Step 2: Pipeline にパラメータを追加
1. `run()` に `video_format` パラメータを追加
2. `run_from_article()` にも同様に追加
3. `script_generator.generate()` 呼び出しを更新

### Step 3: Web Routes を更新
1. `/generate` エンドポイントにFormパラメータを追加
2. `generate_videos_task()` のシグネチャを更新
3. バックグラウンドタスク呼び出しを更新

### Step 4: VoiceGenerator に話速パラメータを追加
1. `generate()` に `speaking_rate` パラメータを追加（デフォルト: 1.25）
2. `generate_with_timings()` に `speaking_rate` パラメータを追加（デフォルト: 1.25）
3. ハードコードされた `speaking_rate=1.25` を引数で受け取るように変更（114行目、233行目）

### Step 5: Pipeline から VoiceGenerator に話速を渡す
1. `video_format` に応じて話速を決定
   - "short": 1.25
   - "long": 1.1
2. `voice_generator.generate_with_timings()` 呼び出し時に `speaking_rate` を渡す

### Step 6: ImageGenerator にアスペクト比対応を追加
1. `generate_batch()` に `video_format` パラメータを追加（デフォルト: "short"）
2. アスペクト比を `video_format` に応じて選択
   - "short": "9:16"
   - "long": "16:9"
3. `_enhance_prompt()` は `video_format` に関係なく共通処理（スタイルはScriptGeneratorのプロンプトで制御）

### Step 7: VideoComposer に解像度対応を追加
1. `compose()` に `video_format` パラメータを追加（デフォルト: "short"）
2. 出力解像度を `video_format` に応じて選択
   - "short": 1080x1920
   - "long": 1920x1080
3. テキストオーバーレイのフォントサイズ・位置を調整

### Step 8: UIにセレクターを追加
1. ラジオボタンUIを追加（ショート/ロング選択）
2. HTMXフォームに `video_format` を送信するように修正

## 変更しないコンポーネント

以下は変更不要:
- **Script Model**: 既存フィールドで対応可能

## UI デザイン

```
┌─────────────────────────────────────┐
│ 動画形式を選択                       │
├─────────────────┬───────────────────┤
│   📱 ショート   │   🎬 ロング       │
│   30-60秒       │   約5分          │
│  [選択中]       │   [ ]            │
└─────────────────┴───────────────────┘
```

## 検証方法

1. **Web UIテスト**:
   - `python web_app.py` で起動
   - ニュース記事を選択
   - 「ショート」と「ロング」を切り替えて動画生成

2. **ショート動画の確認**:
   - 長さ: 30-60秒
   - アスペクト比: 9:16（縦長）
   - 解像度: 1080x1920
   - 画像枚数: 6-7枚
   - 画像スタイル: シネマティック

3. **ロング動画の確認**:
   - 長さ: 約5分
   - アスペクト比: 16:9（横長）
   - 解像度: 1920x1080
   - 画像枚数: AIが決定した枚数
   - 画像スタイル:
     - 1枚目: イメージ画像（cinematic）
     - 2枚目以降: 説明解説資料風（図解・チャート）

4. **スクリプトJSON確認**:
   - `output/scripts/` のJSONファイルで `estimated_duration` と配列長を確認
   - ロング動画では画像プロンプトが「infographic」「diagram」等を含むことを確認

## 考慮事項

### 生成時間
- ショート: 約2-3分
- ロング: 約10-15分（画像生成が主なボトルネック）

### API コスト
- ロング動画は画像生成APIコールが約4-5倍に増加
- UI に警告メッセージを表示することを推奨

### FFmpeg タイムアウト
- 現在300秒のタイムアウト設定
- ロング動画では必要に応じて延長を検討
