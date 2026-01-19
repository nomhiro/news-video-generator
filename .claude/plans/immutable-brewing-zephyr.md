# 画像が横向きになる問題の修正計画（プロンプト最適化）

## 問題の概要

YouTube Shorts / TikTok向けの縦型動画を生成するシステムで、Gemini 3 Pro Image (Nano Banana Pro)で生成される画像が時々横向き（landscape）の構図になってしまう。

**画像サイズ自体は正しい**が、**構図（composition）が横向き**になっている。

---

## 原因分析

現在のプロンプト構成に問題がある:

### 1. ImageGeneratorの`_enhance_prompt` ([image_generator.py:154](src/generators/image_generator.py#L154))

```python
enhanced = f"vertical portrait orientation, tall format, {enhanced}"
```

**問題点**:
- 抽象的な指示（"vertical portrait orientation"）で構図を十分に制御できていない
- Gemini 3 Pro Imageに最適化されていない

### 2. ScriptGeneratorのシステムプロンプト

**日本語** ([script_generator.py:74](src/generators/script_generator.py#L74)):
```
"Scene 1: (描写), cinematic, high quality, 9:16 vertical"
```

**英語** ([script_generator.py:143](src/generators/script_generator.py#L143)):
```
"Scene 1: (visual description), cinematic, high quality, 9:16 vertical"
```

**問題点**:
- "9:16 vertical"はAPIパラメータで制御されており、プロンプト内では重複/無効
- 縦構図を誘導する具体的な視覚指示がない

---

## 実装手順

### Step 1: `_enhance_prompt`メソッドの改善

**ファイル**: [src/generators/image_generator.py](src/generators/image_generator.py)
**行**: 154

**変更前**:
```python
enhanced = f"vertical portrait orientation, tall format, {enhanced}"
```

**変更後**:
```python
# 縦構図を強制する具体的な指示 (Gemini 3 Pro Image最適化)
vertical_composition = (
    "Compose for 9:16 vertical smartphone screen. "
    "Frame the subject with strong vertical emphasis. "
    "Use portrait framing with the main subject filling the vertical space. "
    "Avoid wide panoramic or landscape compositions. "
    "Center of interest should be positioned for tall format viewing."
)
enhanced = f"{vertical_composition} {enhanced}"
```

---

### Step 2: ScriptGenerator日本語プロンプトの改善

**ファイル**: [src/generators/script_generator.py](src/generators/script_generator.py)

#### 2a. image_promptの例を変更 (行 74)

**変更前**:
```
"Scene 1: (ナレーションの該当部分に対応する映像を英語で具体的に描写), cinematic, high quality, 9:16 vertical",
```

**変更後**:
```
"Scene 1: (vertical composition, subject fills the tall frame) + ナレーションの該当部分に対応する映像を英語で具体的に描写, cinematic, high quality",
```

#### 2b. 注意事項を変更 (行 90-91)

**変更前**:
```
- image_promptsは必ず英語で書いてください（Gemini 3 Pro Image用）
- 各image_promptに "cinematic, high quality, 9:16 vertical" を含めてください
```

**変更後**:
```
- image_promptsは必ず英語で書いてください（Gemini 3 Pro Image用）
- 各image_promptは**縦構図（vertical composition）**を意識して書いてください
- 被写体が縦方向に配置される構図を優先してください（建物、人物の全身、滝など縦に伸びる要素）
- 横に広がるパノラマや風景ショットは避けてください
- "cinematic, high quality" を含めてください（"9:16 vertical" はAPIで自動設定されるため不要）
```

---

### Step 3: ScriptGenerator英語プロンプトの改善

**ファイル**: [src/generators/script_generator.py](src/generators/script_generator.py)

#### 3a. image_promptの例を変更 (行 143)

**変更前**:
```
"Scene 1: (visual description matching the narration), cinematic, high quality, 9:16 vertical",
```

**変更後**:
```
"Scene 1: (vertical composition, subject fills the tall frame) + visual description matching the narration, cinematic, high quality",
```

#### 3b. 注意事項を変更 (行 158-159)

**変更前**:
```
- image_prompts must be in English (for Gemini 3 Pro Image)
- Include "cinematic, high quality, 9:16 vertical" in each image_prompt
```

**変更後**:
```
- image_prompts must be in English (for Gemini 3 Pro Image)
- Use **vertical composition** in each image_prompt - frame subjects to fill the tall format
- Prioritize vertically-oriented elements (buildings, full-body figures, waterfalls, towers)
- Avoid wide panoramic or landscape shots
- Include "cinematic, high quality" (no need for "9:16 vertical" as it's set via API)
```

---

## 修正対象ファイル

| ファイル | 変更箇所 |
|----------|----------|
| [src/generators/image_generator.py](src/generators/image_generator.py) | `_enhance_prompt` (line 154) |
| [src/generators/script_generator.py](src/generators/script_generator.py) | 日本語プロンプト (lines 74, 90-91) |
| [src/generators/script_generator.py](src/generators/script_generator.py) | 英語プロンプト (lines 143, 158-159) |

---

## 検証方法

1. **動画生成を実行**
   ```bash
   python main.py "テストニュース" -l ja
   ```

2. **生成された画像を確認**
   - `output/images/` 内の画像の構図が縦向きであることを目視確認
   - 主要な被写体が縦方向に配置されていることを確認

3. **複数回テスト**
   - 異なるニューストピックで3-5回生成し、一貫して縦構図になることを確認
