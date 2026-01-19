# 修正計画: overlaysテキスト数の不一致問題

## 問題の概要

スクリプト生成時の`text_overlays`の数と、動画内で実際に使用されるテキストの数が一致しない。

## 根本原因

**開いているJSONファイルの数の比較：**

| フィールド | 数 |
|-----------|---|
| `image_prompts` | 7個 (Scene 1〜7) |
| `text_overlays` | 7個 |
| `segment_narrations` | 6個 |

**問題点**: `segment_narrations`（6個）と`image_prompts`/`text_overlays`（7個）の数が一致していない。

AIがプロンプトの指示（すべて同じ数にする）を守らず、`segment_narrations`を少なく生成してしまうことが原因。

---

## 修正方針

**ScriptGeneratorのプロンプト強化のみ**（バリデーション・再生成は不要）

---

## 修正内容

### 対象ファイル
- [script_generator.py](src/generators/script_generator.py)

### 変更箇所

#### 1. 日本語プロンプト (`SYSTEM_PROMPT_JA`) の強化

**現在（行67-83付近）のJSON出力形式部分に、数の一致を明示的に示す：**

```json
"segment_narrations": [
    "画像1に対応するナレーション部分",
    "画像2に対応するナレーション部分",
    "... (image_promptsと同じN個)"
],
"image_prompts": [
    "Scene 1: ...",
    "Scene 2: ...",
    "... (合計N個)"
],
"text_overlays": [
    "画像1: 短文",
    "画像2: 短文",
    "... (image_promptsと同じN個)"
],
```

**注意事項セクション（行87-98付近）の強化：**

現在:
```
- text_overlaysはimage_promptsと同じ数だけ生成し、対応するsegment_narrationsの内容を15-25文字程度に要約した文章です
- **segment_narrationsはimage_promptsと同じ数だけ生成し、各画像に対応するナレーション部分を含めてください**
```

変更後:
```
## ⚠️ 最重要制約: 配列の要素数
以下の3つの配列は **必ず同じ要素数** にしてください。これが守られないと動画生成が正しく動作しません:
- image_prompts: N個
- text_overlays: N個（image_promptsと完全に対応）
- segment_narrations: N個（image_promptsと完全に対応）

例: image_promptsが6個なら、text_overlaysもsegment_narrationsも必ず6個にする
```

#### 2. 英語プロンプト (`SYSTEM_PROMPT_EN`) の強化

同様に、注意事項セクションに以下を追加：

```
## ⚠️ CRITICAL CONSTRAINT: Array Element Counts
The following 3 arrays MUST have the EXACT SAME number of elements. Video generation will fail if this is not followed:
- image_prompts: N elements
- text_overlays: N elements (must correspond to image_prompts)
- segment_narrations: N elements (must correspond to image_prompts)

Example: If image_prompts has 6 elements, text_overlays and segment_narrations must also have exactly 6 elements
```

---

## 検証方法

1. 修正後にスクリプト生成を実行
2. 生成されたJSONファイルで以下を確認：
   - `len(image_prompts)` == `len(text_overlays)` == `len(segment_narrations)`

---

## 関連ファイル

- [script_generator.py](src/generators/script_generator.py) - スクリプト生成ロジック（修正対象）
- [video_composer.py](src/generators/video_composer.py) - 動画合成・テキストオーバーレイ処理
- [pipeline.py](src/pipeline.py) - パイプライン制御
