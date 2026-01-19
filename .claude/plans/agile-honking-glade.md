# 修正計画: ショート動画の縦長画像生成問題

## 問題概要
ショート動画（9:16縦長）の生成時に、横向きの画像が生成されてしまう場合がある。

## 根本原因
[image_generator.py:146-172](src/generators/image_generator.py#L146-L172) の `_enhance_prompt()` メソッドで、コメントには「縦向き（portrait orientation）を明示的に指定」と記載があるが、**実際には何も追加していない**：

```python
# 縦向き（portrait orientation）を明示的に指定
# YouTube Shorts / TikTok 向けの 9:16 縦長フォーマット
# 注意: 構図の制御はAPI側のaspect_ratio="9:16"に任せる
enhanced = f"{enhanced}"  # ← これは何も追加していない
```

Gemini APIの `aspect_ratio="9:16"` パラメータだけでは、モデルが常に縦長構図を生成するとは限らない。プロンプト自体に明示的な指示が必要。

## 修正内容

### ファイル: [src/generators/image_generator.py](src/generators/image_generator.py)

#### 1. `_enhance_prompt()` メソッドに `video_format` パラメータを追加

**変更箇所**: 146行目
```python
# Before
def _enhance_prompt(self, prompt: str, language: str = "ja") -> str:

# After
def _enhance_prompt(self, prompt: str, language: str = "ja", video_format: str = "short") -> str:
```

#### 2. 縦向き/横向きの明示的な指示をプロンプトに追加

**変更箇所**: 158-161行目
```python
# Before
enhanced = f"{enhanced}"

# After
if video_format == "short":
    # 縦長 9:16 の構図を明示的に指示
    enhanced = f"IMPORTANT: Vertical portrait orientation (9:16 aspect ratio), tall composition optimized for mobile viewing. {enhanced}"
else:
    # 横長 16:9 の構図を明示的に指示
    enhanced = f"IMPORTANT: Horizontal landscape orientation (16:9 aspect ratio), wide composition optimized for desktop viewing. {enhanced}"
```

#### 3. `generate_batch()` から `_enhance_prompt()` への呼び出しを修正

**変更箇所**: 75行目
```python
# Before
enhanced_prompt = self._enhance_prompt(prompt, language)

# After
enhanced_prompt = self._enhance_prompt(prompt, language, video_format)
```

## 追加の改善（推奨）

### script_generator.py のシステムプロンプト更新

[src/generators/script_generator.py:88-95](src/generators/script_generator.py#L88-L95) の `image_prompts` のガイドラインに、縦長構図に適したシーンの指示を追加：

```
- image_prompts must be in English (for Gemini 3 Pro Image)
- Include "cinematic, high quality" in each image_prompt
- **Design scenes with vertical composition in mind (9:16 aspect ratio for shorts)**
- Use close-up shots, single subjects, or vertically-arranged elements
```

## 検証方法

1. Web UIでショート動画を1本生成
2. `output/images/` フォルダ内の生成画像を確認
3. 画像が縦長（1080x1920または9:16比率）になっていることを確認
4. 最終動画で画像に黒い余白（letterbox）が入っていないことを確認

## 影響範囲
- ショート動画（short format）の画像生成
- ロング動画（long format）の画像生成（横向きの明示的指示も追加）
- 既存の動画に影響なし（新規生成のみ）
