# 空のセグメントによる音声生成エラーの修正プラン

## 問題の概要

**エラー**: `セグメント6の音声生成に失敗: 400 This voice does not support empty input.`

音声生成時にGoogle Cloud TTS APIが空のテキストを受け取りエラーが発生。

## 根本原因

1. **プロンプトの曖昧さ**: 配列要素数の指定が強調不足で、AIが正確に従わない
2. **検証不足**: `segment_narrations` 配列に空の要素が含まれても検証されていない
3. **防御的コードの欠如**: VoiceGeneratorとScriptGeneratorの両方で空チェックがない

---

## 修正計画（2段階）

### 段階1: プロンプト最適化（根本対策）

**ファイル**: [src/generators/script_generator.py](src/generators/script_generator.py)

#### 現状の問題点

1. **構造が不明確**: プロンプトが長文で、重要な制約が埋もれている
2. **XMLタグ未使用**: Claudeが推奨するXMLタグで構造化されていない
3. **出力例が不完全**: 配列要素数が明示的に示されていない
4. **指示が分散**: 同じ制約が複数箇所に書かれ一貫性がない

#### 最適化後のプロンプト構造（OpenAI/Anthropicベストプラクティス準拠）

```python
SYSTEM_PROMPT_JA = """
<role>
あなたはYouTube ShortsやTikTok向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、35秒程度の短尺動画用の台本をJSON形式で作成してください。
</task>

<critical_constraints>
【最重要】以下の3つの配列は必ず6個ずつ生成してください：
- image_prompts: 6個
- text_overlays: 6個
- segment_narrations: 6個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。必ず内容のあるテキストを入れてください。
</critical_constraints>

<content_rules>
- ナレーション(full_narration): 250〜300文字の自然な話し言葉
- segment_narrations: full_narrationを6つに分割（連結するとfull_narrationと一致）
- image_prompts: 必ず英語で記述（Gemini Image用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度（【】や！で注目を集める）
- hashtags: 5〜8個（"shorts"は必須）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【衝撃】〇〇が××！△△の行方は？",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n💬 コメントで教えて！\\n\\n#shorts #タグ1 #タグ2",
    "hashtags": ["shorts", "タグ1", "タグ2", "タグ3", "タグ4", "タグ5"],
    "hook": "最初の3秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3"],
    "conclusion": "締めの一言",
    "full_narration": "完全なナレーション台本（250〜300文字）",
    "segment_narrations": [
        "セグメント1のナレーション（空でないこと）",
        "セグメント2のナレーション（空でないこと）",
        "セグメント3のナレーション（空でないこと）",
        "セグメント4のナレーション（空でないこと）",
        "セグメント5のナレーション（空でないこと）",
        "セグメント6のナレーション（空でないこと）"
    ],
    "image_prompts": [
        "Scene 1: description, cinematic, high quality",
        "Scene 2: description, cinematic, high quality",
        "Scene 3: description, cinematic, high quality",
        "Scene 4: description, cinematic, high quality",
        "Scene 5: description, cinematic, high quality",
        "Scene 6: description, cinematic, high quality"
    ],
    "text_overlays": [
        "画像1用テキスト",
        "画像2用テキスト",
        "画像3用テキスト",
        "画像4用テキスト",
        "画像5用テキスト",
        "画像6用テキスト"
    ],
    "estimated_duration": 35
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に6個あること
2. image_prompts が正確に6個あること
3. text_overlays が正確に6個あること
4. 全ての要素が空文字列でないこと
5. segment_narrationsを連結するとfull_narrationと一致すること
</verification>
"""
```

**改善ポイント**:
- XMLタグで構造化（`<role>`, `<task>`, `<critical_constraints>`, `<output_format>`, `<verification>`）
- 最重要制約を最上部に配置し、`<critical_constraints>`タグで強調
- 出力例で6個の要素を明示的に表示
- 検証セクションを追加してAIに自己チェックを促す
- 「空でないこと」を明示的に指示

#### 英語プロンプト（SYSTEM_PROMPT_EN）の最適化

同様の構造でXMLタグを使用：

```python
SYSTEM_PROMPT_EN = """
<role>
You are a script writer for YouTube Shorts and TikTok news explainer videos.
</role>

<task>
Create a script for a short video (about 35 seconds) based on the given news topic in JSON format.
</task>

<critical_constraints>
CRITICAL: The following 3 arrays MUST have exactly 6 elements each:
- image_prompts: 6 elements
- text_overlays: 6 elements
- segment_narrations: 6 elements

Video generation will FAIL if array counts differ.
Each element MUST NOT be an empty string. Always include meaningful content.
</critical_constraints>

<content_rules>
- Narration (full_narration): 120-150 words, natural spoken language
- segment_narrations: Split full_narration into 6 parts (concatenation equals full_narration)
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: Short text for each image (8-15 words)
- title: Around 50 characters (use attention-grabbing words)
- hashtags: 5-8 tags (must include "shorts")
</content_rules>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "SHOCKING: Something Amazing Happened!",
    "description": "Summary 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n💬 Comment below!\\n\\n#shorts #tag1 #tag2",
    "hashtags": ["shorts", "tag1", "tag2", "tag3", "tag4", "tag5"],
    "hook": "Opening hook to grab attention in 3 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3"],
    "conclusion": "Short closing statement",
    "full_narration": "Complete narration script (120-150 words)",
    "segment_narrations": [
        "Segment 1 narration (not empty)",
        "Segment 2 narration (not empty)",
        "Segment 3 narration (not empty)",
        "Segment 4 narration (not empty)",
        "Segment 5 narration (not empty)",
        "Segment 6 narration (not empty)"
    ],
    "image_prompts": [
        "Scene 1: description, cinematic, high quality",
        "Scene 2: description, cinematic, high quality",
        "Scene 3: description, cinematic, high quality",
        "Scene 4: description, cinematic, high quality",
        "Scene 5: description, cinematic, high quality",
        "Scene 6: description, cinematic, high quality"
    ],
    "text_overlays": [
        "Image 1 text",
        "Image 2 text",
        "Image 3 text",
        "Image 4 text",
        "Image 5 text",
        "Image 6 text"
    ],
    "estimated_duration": 35
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 6 elements
2. image_prompts has exactly 6 elements
3. text_overlays has exactly 6 elements
4. No element is an empty string
5. Concatenating segment_narrations equals full_narration
</verification>
"""
```

#### ロング形式プロンプトの最適化方針

`SYSTEM_PROMPT_LONG_JA` と `SYSTEM_PROMPT_LONG_EN` も同様に最適化：
- 配列数は固定ではなく「N個（15〜25枚程度）」のため、`<critical_constraints>`では「3配列の要素数は必ず一致」を強調
- 空文字列禁止の制約を明示

---

### 段階2: コード側の防御（フォールバック）

#### 修正2-1: voice_generator.py - 空チェック追加

**ファイル**: [src/generators/voice_generator.py](src/generators/voice_generator.py)
**関数**: `generate_segments_individually()` (221行目)

```python
for i, segment_text in enumerate(segment_narrations):
    # 空のセグメントをチェック
    if not segment_text or not segment_text.strip():
        raise VoiceGenerationError(
            f"セグメント{i+1}が空です。台本生成で問題が発生した可能性があります。"
        )

    segment_path = temp_dir / f"segment_{i}.mp3"
    synthesis_input = texttospeech.SynthesisInput(text=segment_text)
```

#### 修正2-2: script_generator.py - 検証メソッド追加

**ファイル**: [src/generators/script_generator.py](src/generators/script_generator.py)

`_parse_response()` の末尾で検証を呼び出し：

```python
def _parse_response(self, response_text: str, language: str) -> Script:
    # ... 既存のパース処理 ...
    script = Script.from_dict(data)
    self._validate_script(script)
    return script

def _validate_script(self, script: Script) -> None:
    """生成されたスクリプトを検証する。"""
    if not script.segment_narrations:
        raise ScriptGenerationError("segment_narrationsが空です")

    num_segments = len(script.segment_narrations)
    num_images = len(script.image_prompts)
    num_overlays = len(script.text_overlays)

    if num_segments != num_images:
        raise ScriptGenerationError(
            f"配列長の不一致: segment_narrations={num_segments}, image_prompts={num_images}"
        )

    if num_segments != num_overlays:
        raise ScriptGenerationError(
            f"配列長の不一致: segment_narrations={num_segments}, text_overlays={num_overlays}"
        )

    for i, narration in enumerate(script.segment_narrations):
        if not narration or not narration.strip():
            raise ScriptGenerationError(f"セグメント{i+1}のnarrationが空です")
```

---

## 修正対象ファイル一覧

| ファイル | 修正内容 |
|---------|---------|
| `src/generators/script_generator.py` | SYSTEM_PROMPT_JA/EN をXMLタグで構造化、`_validate_script()`追加 |
| `src/generators/voice_generator.py` | 空セグメントチェック追加 |

---

## 検証方法

1. **プロンプト検証**: 複数のニュース記事で台本生成をテストし、配列が常に6個生成されることを確認
2. **エラーハンドリング**: 意図的に空のセグメントを含むデータで検証エラーが発生することを確認
3. **統合テスト**: Web UIから7件程度の一括生成を実行し、全件成功することを確認
