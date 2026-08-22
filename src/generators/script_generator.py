"""Script generation using Azure OpenAI Responses API with Structured Outputs."""

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models.formats import FormatSpec, get_spec
from src.models.scene import (
    ITEMS_PER_LAYOUT,
    MAX_DETAIL_CHARS,
    MAX_LABEL_CHARS,
    MAX_RELATION_CHARS,
    SceneLayout,
)
from src.models.script import (
    MAX_HEADLINE_CHARS,
    Script,
    ScriptDraft,
)
from src.utils.grounding import ungrounded_numbers
from src.utils.logger import log_error, log_step, log_success, log_warning


class ScriptGenerationError(Exception):
    """Script generation failed."""

    pass


# 構成のパート名。プロンプトに出す順序そのもの。
_STRUCTURE_PARTS = ("hook", "facts", "mechanism", "impact", "conclusion")


def segment_allocation(segment_count: int) -> dict[str, int]:
    """セグメント数を構成パートに配分する。

    フックと結論に1つずつ割り、残りを 事実:仕組み:インパクト = 1:1:1 で分ける。
    端数は**解説側**（仕組み・インパクト）に寄せる。独自解説を厚くするのが
    この配分の目的なので、余りを事実に回すと逆の効果になる。

    6セグメントなら 1/1/2/1/1、10セグメントなら 1/2/3/3/1 になる。

    Args:
        segment_count: 形式のセグメント数

    Returns:
        dict[str, int]: パート名 → セグメント数。合計は segment_count に一致する

    Raises:
        ValueError: パート数より少ないセグメント数を渡された場合
    """
    # 5パートあるので5未満は配分しようがない。形式の定義上ありえない
    # （最小は SHORT の6）が、0や負の割り当てはセグメント番号の範囲を壊すため
    # 黙って進めずに落とす。
    if segment_count < len(_STRUCTURE_PARTS):
        raise ValueError(
            f"セグメント数が構成パート数を下回っています: {segment_count} < {len(_STRUCTURE_PARTS)}"
        )
    if segment_count == len(_STRUCTURE_PARTS):
        return dict.fromkeys(_STRUCTURE_PARTS, 1)

    body = segment_count - 2  # フックと結論のぶんを除く
    base, remainder = divmod(body, 3)
    allocation = {
        "hook": 1,
        "facts": base,
        "mechanism": base,
        "impact": base,
        "conclusion": 1,
    }
    # 端数は仕組み → インパクトの順に寄せる（余りは最大2）
    for name in ("mechanism", "impact"):
        if remainder <= 0:
            break
        allocation[name] += 1
        remainder -= 1
    return allocation


# 構成パート名 → 表示ラベルの対応。ja/en それぞれ1つの辞書に集約する
# （プロンプトの `_structure_spec` は別の言い回し「フック。冒頭で...」を使うが、
# ここは画面に描く短いラベルなので分けて持つ）。
_CHAPTER_LABELS: dict[str, dict[str, str]] = {
    "ja": {
        "hook": "フック",
        "facts": "事実",
        "mechanism": "仕組み",
        "impact": "インパクト",
        "conclusion": "結論",
    },
    "en": {
        "hook": "Hook",
        "facts": "Facts",
        "mechanism": "How it works",
        "impact": "Impact",
        "conclusion": "Takeaway",
    },
}


def chapter_labels(segment_count: int, language: str) -> list[str]:
    """セグメントごとの章ラベル（フック/事実/仕組み/インパクト/結論）を返す。

    `segment_allocation` と同じ構造的事実を、LLM への指示（プロンプト文字列）
    ではなく画面表示（Remotion の画面上部）向けに見ているだけなので、
    配分の算出自体は再実装せず `segment_allocation` にそのまま委ねる
    （`_structure_spec` がスパンを作る手順と同じ、カーソルを進める方式）。

    LLM に出させる必要は無い。章がどのセグメントかは segment_index から
    一意に決まる構造的な事実であり、モデルの出力ではないため。

    **`segment_allocation` が ValueError を投げる短さでも例外にしない。**
    章ラベルは装飾であり、動画に描かれなくても動画自体は成立する
    （`SceneVisual.relation` や見出しとは違う）。装飾のために本体である
    レンダリングを丸ごと落とすのは本末転倒なので、配分できない短さでは
    空文字列で埋めて呼び出し元に「描く文字が無い」と伝える。production の
    形式（short/tiktok=6, long=10）は常に配分できる長さなので、実際に
    劣化が起きるのはテストの小さいダミーデータだけである。

    Args:
        segment_count: 形式のセグメント数
        language: 言語コード（"ja" or "en"）

    Returns:
        list[str]: セグメントごとのラベル。要素数は常に segment_count に一致する。
            1パートが複数セグメントを占める場合、同じラベルが繰り返される
            （例: 6セグメントの仕組みが2つなら ["...", "仕組み", "仕組み", ...]）。
            `segment_count` が構成パート数（5）未満なら、配分できないので
            全て空文字列になる
    """
    if segment_count < len(_STRUCTURE_PARTS):
        return [""] * segment_count

    labels = _CHAPTER_LABELS["ja" if language == "ja" else "en"]
    allocation = segment_allocation(segment_count)
    result: list[str] = []
    for name in _STRUCTURE_PARTS:
        result.extend([labels[name]] * allocation[name])
    return result


class ScriptGenerator:
    """Azure OpenAI Responses API (Structured Outputs) で台本を生成するクラス。

    `Script` Pydantic モデルをそのまま出力スキーマとして渡すため、
    モデルの出力は常にスキーマに適合する。JSON の抽出やパースは行わない。

    Attributes:
        client: OpenAI APIクライアント
        model: 使用するモデル（デプロイメント名）
    """

    # 通信エラー・レートリミット・5xx に対する試行回数
    API_RETRIES = 4
    # スキーマは適合しているが内容の整合性検証（配列長の一致、
    # 分量の超過など）で弾かれた場合の再生成回数
    VALIDATION_RETRIES = 3

    # プロンプト内で分量の指示を差し込む位置。
    # formats.py の仕様から生成するため、プロンプト側には値を書かない。
    NARRATION_SPEC_TOKEN = "<<NARRATION_SPEC>>"

    # プロンプト内で構成順序の指示を差し込む位置。
    # セグメント番号は segment_count から計算するので、プロンプト側には書かない
    # （short/tiktok は6、long は10で異なる）。
    STRUCTURE_SPEC_TOKEN = "<<STRUCTURE_SPEC>>"

    # プロンプト内でシーンの指示を差し込む位置。
    # レイアウトの種類と要素数は models/scene.py が単一の情報源なので、
    # プロンプト側には値を書かない（書くと定義とずれる）。
    SCENES_SPEC_TOKEN = "<<SCENES_SPEC>>"

    # プロンプト内で見出し（text_overlays）の指示を差し込む位置。
    # 上限は models/script.py の MAX_HEADLINE_CHARS が単一の情報源なので、
    # プロンプト側には値を書かない（書くと検査と指示が食い違う）。
    OVERLAY_SPEC_TOKEN = "<<OVERLAY_SPEC>>"

    # 出力例の scenes 配列を差し込む位置。
    # 要素数は segment_count から作る（short/tiktok は6、long は10）。
    SCENES_EXAMPLE_TOKEN = "<<SCENES_EXAMPLE>>"

    # プロンプト内で挿絵（illustration_concept）の指示を差し込む位置。
    # 各語の長さ上限は models/scene.py の MAX_UNIT_CHARS /
    # MAX_FIELD_CHARS / MAX_EMPHASIS_CHARS が単一の情報源なので、
    # プロンプト側には値を書かない。
    ILLUSTRATION_SPEC_TOKEN = "<<ILLUSTRATION_SPEC>>"

    SYSTEM_PROMPT_JA = """<role>
あなたはYouTube ShortsやTikTok向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、35秒程度の短尺動画用の台本をJSON形式で作成してください。
</task>

<critical_constraints>
【最重要】以下の4つの配列は必ず6個ずつ生成してください：
- image_prompts: 6個
- text_overlays: 6個
- segment_narrations: 6個
- scenes: 6個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。必ず内容のあるテキストを入れてください。
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述（画像生成モデル用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: <<OVERLAY_SPEC>>
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
- hashtags: 5〜8個（"shorts"は必須）
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【衝撃】〇〇が××！△△の行方は？",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n💬 コメントで教えて！\\n\\n#shorts #タグ1 #タグ2",
    "hashtags": ["shorts", "タグ1", "タグ2", "タグ3", "タグ4", "タグ5"],
    "hook": "最初の3秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3"],
    "conclusion": "締めの一言",
    "technical_insight": "技術的にどういう仕組みで実現しているのかの解説（40文字以上）",
    "practical_impact": "実務・現場で何がどう変わるのかの考察（40文字以上）",
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
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 35
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に6個あること
2. image_prompts が正確に6個あること
3. text_overlays が正確に6個あること
4. 全ての要素が空文字列でないこと
5. scenes が正確に6個あること
6. illustration_concept の subject が図として描ける具体物で、key_details がちょうど2個の短い英語の句、labels が0〜4個の8字以内の日本語で、人物・抽象量・数字・スタイル語（画材・配色・技法）を含まないこと
</verification>"""

    SYSTEM_PROMPT_LONG_JA = """<role>
あなたはYouTube向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、約5分程度の解説動画用の台本をJSON形式で作成してください。
</task>

<critical_constraints>
【最重要】以下の4つの配列は必ず10個ずつ生成してください：
- image_prompts: 10個
- text_overlays: 10個
- segment_narrations: 10個
- scenes: 10個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。必ず内容のあるテキストを入れてください。
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述
  - 1枚目: サムネイル的（cinematic, high quality, eye-catching thumbnail style）
  - 2枚目以降: 解説資料風（infographic style, educational diagram, data visualization）
- text_overlays: <<OVERLAY_SPEC>>
- title: 50〜60文字程度（【完全解説】【徹底分析】など）
- description: 要約＋絵文字、タイムスタンプ、📌でポイント、💬でCTA、ハッシュタグ
- hashtags: 5〜10個
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【完全解説】〇〇の真相！△△について徹底分析",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n⏱️ タイムスタンプ\\n00:00 イントロ\\n00:30 〜について\\n\\n💬 コメントで教えて！\\n👍 チャンネル登録お願いします！\\n\\n#タグ1 #タグ2",
    "hashtags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5", "ニュース", "解説"],
    "hook": "最初の10秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3", "ポイント4", "ポイント5", "ポイント6"],
    "conclusion": "締めの言葉（まとめと今後の展望）",
    "technical_insight": "技術的にどういう仕組みで実現しているのかの解説（40文字以上）",
    "practical_impact": "実務・現場で何がどう変わるのかの考察（40文字以上）",
    "segment_narrations": [
        "セグメント1のナレーション（空でないこと）",
        "セグメント2のナレーション（空でないこと）",
        "セグメント3のナレーション（空でないこと）",
        "セグメント4のナレーション（空でないこと）",
        "セグメント5のナレーション（空でないこと）",
        "セグメント6のナレーション（空でないこと）",
        "セグメント7のナレーション（空でないこと）",
        "セグメント8のナレーション（空でないこと）",
        "セグメント9のナレーション（空でないこと）",
        "セグメント10のナレーション（空でないこと）"
    ],
    "image_prompts": [
        "Scene 1: thumbnail image description, cinematic, high quality, eye-catching",
        "Scene 2: infographic explaining concept, educational diagram, clean design",
        "Scene 3: description, infographic style",
        "Scene 4: description, infographic style",
        "Scene 5: description, infographic style",
        "Scene 6: description, infographic style",
        "Scene 7: description, infographic style",
        "Scene 8: description, infographic style",
        "Scene 9: description, infographic style",
        "Scene 10: description, infographic style"
    ],
    "text_overlays": [
        "画像1用テキスト",
        "画像2用テキスト",
        "画像3用テキスト",
        "画像4用テキスト",
        "画像5用テキスト",
        "画像6用テキスト",
        "画像7用テキスト",
        "画像8用テキスト",
        "画像9用テキスト",
        "画像10用テキスト"
    ],
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 300
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に10個あること
2. image_prompts が正確に10個あること
3. text_overlays が正確に10個あること
4. 全ての要素が空文字列でないこと
5. scenes が正確に10個あること
6. illustration_concept の subject が図として描ける具体物で、key_details がちょうど2個の短い英語の句、labels が0〜4個の8字以内の日本語で、人物・抽象量・数字・スタイル語（画材・配色・技法）を含まないこと
</verification>"""

    SYSTEM_PROMPT_EN = """<role>
You are a script writer for YouTube Shorts and TikTok news explainer videos.
</role>

<task>
Create a script for a short video (about 35 seconds) based on the given news topic in JSON format.
</task>

<critical_constraints>
CRITICAL: The following 4 arrays MUST have exactly 6 elements each:
- image_prompts: 6 elements
- text_overlays: 6 elements
- segment_narrations: 6 elements
- scenes: 6 elements

Video generation will FAIL if array counts differ.
Each element MUST NOT be an empty string. Always include meaningful content.
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: <<OVERLAY_SPEC>>
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
- hashtags: 5-8 tags (must include "shorts")
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "SHOCKING: Something Amazing Happened!",
    "description": "Summary 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n💬 Comment below!\\n\\n#shorts #tag1 #tag2",
    "hashtags": ["shorts", "tag1", "tag2", "tag3", "tag4", "tag5"],
    "hook": "Opening hook to grab attention in 3 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3"],
    "conclusion": "Short closing statement",
    "technical_insight": "How it actually works under the hood (at least 40 characters)",
    "practical_impact": "What changes in real-world practice (at least 40 characters)",
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
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 35
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 6 elements
2. image_prompts has exactly 6 elements
3. text_overlays has exactly 6 elements
4. No element is an empty string
5. scenes has exactly 6 elements
6. illustration_concept's subject names something literally drawable, key_details holds exactly two short English phrases, labels holds 0-4 Japanese labels of at most 8 characters each, and none of them names a human figure, an abstract quantity, a numeral, or a style word (medium, palette, technique)
</verification>"""

    SYSTEM_PROMPT_LONG_EN = """<role>
You are a script writer for YouTube news explainer videos.
</role>

<task>
Create a script for a long-form video (about 5 minutes) based on the given news topic in JSON format.
</task>

<critical_constraints>
CRITICAL: The following 4 arrays MUST have exactly 10 elements each:
- image_prompts: 10 elements
- text_overlays: 10 elements
- segment_narrations: 10 elements
- scenes: 10 elements

Video generation will FAIL if array counts differ.
Each element MUST NOT be an empty string. Always include meaningful content.
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English
  - First image: Thumbnail-style (cinematic, high quality, eye-catching)
  - Second onwards: Educational style (infographic, educational diagram, data visualization)
- text_overlays: <<OVERLAY_SPEC>>
- title: Around 60-70 characters (EXPLAINED, DEEP DIVE, FULL ANALYSIS)
- description: Summary + emoji, timestamps, 📌 for bullet points, 💬 for CTA, hashtags
- hashtags: 5-10 tags
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "EXPLAINED: The Truth Behind [Topic] | Complete Analysis",
    "description": "Full breakdown 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n⏱️ Timestamps\\n00:00 Intro\\n00:30 Overview\\n\\n💬 Comment below!\\n👍 Subscribe!\\n\\n#tag1 #tag2",
    "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5", "news", "explained"],
    "hook": "Opening hook to grab attention in 10 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5", "Point 6"],
    "conclusion": "Closing statement (summary, future outlook)",
    "technical_insight": "How it actually works under the hood (at least 40 characters)",
    "practical_impact": "What changes in real-world practice (at least 40 characters)",
    "segment_narrations": [
        "Segment 1 narration (not empty)",
        "Segment 2 narration (not empty)",
        "Segment 3 narration (not empty)",
        "Segment 4 narration (not empty)",
        "Segment 5 narration (not empty)",
        "Segment 6 narration (not empty)",
        "Segment 7 narration (not empty)",
        "Segment 8 narration (not empty)",
        "Segment 9 narration (not empty)",
        "Segment 10 narration (not empty)"
    ],
    "image_prompts": [
        "Scene 1: thumbnail image, cinematic, high quality, eye-catching",
        "Scene 2: infographic, educational diagram, clean design",
        "Scene 3: description, infographic style",
        "Scene 4: description, infographic style",
        "Scene 5: description, infographic style",
        "Scene 6: description, infographic style",
        "Scene 7: description, infographic style",
        "Scene 8: description, infographic style",
        "Scene 9: description, infographic style",
        "Scene 10: description, infographic style"
    ],
    "text_overlays": [
        "Image 1 text",
        "Image 2 text",
        "Image 3 text",
        "Image 4 text",
        "Image 5 text",
        "Image 6 text",
        "Image 7 text",
        "Image 8 text",
        "Image 9 text",
        "Image 10 text"
    ],
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 300
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 10 elements
2. image_prompts has exactly 10 elements
3. text_overlays has exactly 10 elements
4. No element is an empty string
5. scenes has exactly 10 elements
6. illustration_concept's subject names something literally drawable, key_details holds exactly two short English phrases, labels holds 0-4 Japanese labels of at most 8 characters each, and none of them names a human figure, an abstract quantity, a numeral, or a style word (medium, palette, technique)
</verification>"""

    SYSTEM_PROMPT_TIKTOK_JA = """<role>
あなたはTikTok向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、60〜90秒程度のTikTok動画用の台本をJSON形式で作成してください。
TikTokの収益化には60秒以上の動画が必要です。
</task>

<critical_constraints>
【最重要】以下の4つの配列は必ず6個ずつ生成してください：
- image_prompts: 6個
- text_overlays: 6個
- segment_narrations: 6個
- scenes: 6個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。必ず内容のあるテキストを入れてください。
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述（画像生成モデル用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: <<OVERLAY_SPEC>>
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
- hashtags: 5〜8個（"TikTok"と"ニュース"は必須）
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【衝撃】〇〇が××！△△の行方は？",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n💬 コメントで教えて！\\n\\n#TikTok #ニュース #タグ1 #タグ2",
    "hashtags": ["TikTok", "ニュース", "タグ1", "タグ2", "タグ3", "タグ4"],
    "hook": "最初の5秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3", "ポイント4"],
    "conclusion": "締めの一言（アクションを促す）",
    "technical_insight": "技術的にどういう仕組みで実現しているのかの解説（40文字以上）",
    "practical_impact": "実務・現場で何がどう変わるのかの考察（40文字以上）",
    "segment_narrations": [
        "セグメント1のナレーション（80-110文字、空でないこと）",
        "セグメント2のナレーション（80-110文字、空でないこと）",
        "セグメント3のナレーション（80-110文字、空でないこと）",
        "セグメント4のナレーション（80-110文字、空でないこと）",
        "セグメント5のナレーション（80-110文字、空でないこと）",
        "セグメント6のナレーション（80-110文字、空でないこと）"
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
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 75
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に6個あること
2. image_prompts が正確に6個あること
3. text_overlays が正確に6個あること
4. 全ての要素が空文字列でないこと
5. scenes が正確に6個あること
6. illustration_concept の subject が図として描ける具体物で、key_details がちょうど2個の短い英語の句、labels が0〜4個の8字以内の日本語で、人物・抽象量・数字・スタイル語（画材・配色・技法）を含まないこと
7. segment_narrations の合計が500〜650文字の範囲内であること
</verification>"""

    SYSTEM_PROMPT_TIKTOK_EN = """<role>
You are a script writer for TikTok news explainer videos.
</role>

<task>
Create a script for a TikTok video (60-90 seconds) based on the given news topic in JSON format.
TikTok requires videos over 60 seconds for monetization.
</task>

<critical_constraints>
CRITICAL: The following 4 arrays MUST have exactly 6 elements each:
- image_prompts: 6 elements
- text_overlays: 6 elements
- segment_narrations: 6 elements
- scenes: 6 elements

Video generation will FAIL if array counts differ.
Each element MUST NOT be an empty string. Always include meaningful content.
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: <<OVERLAY_SPEC>>
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
- hashtags: 5-8 tags (must include "TikTok" and "news")
- scenes: <<SCENES_SPEC>>
- illustration_concept: <<ILLUSTRATION_SPEC>>
</content_rules>

<narrative_structure>
<<STRUCTURE_SPEC>>
</narrative_structure>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "SHOCKING: Something Amazing Happened!",
    "description": "Summary 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n💬 Comment below!\\n\\n#TikTok #news #tag1 #tag2",
    "hashtags": ["TikTok", "news", "tag1", "tag2", "tag3", "tag4"],
    "hook": "Opening hook to grab attention in 5 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3", "Point 4"],
    "conclusion": "Short closing statement with call to action",
    "technical_insight": "How it actually works under the hood (at least 40 characters)",
    "practical_impact": "What changes in real-world practice (at least 40 characters)",
    "segment_narrations": [
        "Segment 1 narration (40-60 words, not empty)",
        "Segment 2 narration (40-60 words, not empty)",
        "Segment 3 narration (40-60 words, not empty)",
        "Segment 4 narration (40-60 words, not empty)",
        "Segment 5 narration (40-60 words, not empty)",
        "Segment 6 narration (40-60 words, not empty)"
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
    "scenes": <<SCENES_EXAMPLE>>,
    "illustration_concept": {"subject": "...", "key_details": ["...", "..."], "labels": ["...", "..."]},
    "estimated_duration": 75
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 6 elements
2. image_prompts has exactly 6 elements
3. text_overlays has exactly 6 elements
4. No element is an empty string
5. scenes has exactly 6 elements
6. illustration_concept's subject names something literally drawable, key_details holds exactly two short English phrases, labels holds 0-4 Japanese labels of at most 8 characters each, and none of them names a human figure, an abstract quantity, a numeral, or a style word (medium, palette, technique)
7. The segment_narrations total 250-350 words
</verification>"""

    def __init__(self, endpoint: str, api_key: str, deployment: str):
        """ScriptGeneratorを初期化する。

        Args:
            endpoint: Azure OpenAI endpoint URL
            api_key: Azure OpenAI API key
            deployment: Azure OpenAI deployment name (model)
        """
        # Azure OpenAI v1 endpoint format for Responses API
        base_url = endpoint.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = deployment

    def generate(
        self,
        news_topic: str,
        language: str = "ja",
        video_format: str = "short",
        source_url: str = "",
        *,
        enforce_scene_grounding: bool = True,
    ) -> Script:
        """ニューストピックから台本を生成する。

        Args:
            news_topic: ニューストピック
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short" or "long")
            source_url: 元記事の URL。説明文への出典追記に使う。
                モデルには渡さない（URL を知らないので捏造する）
            enforce_scene_grounding: シーンのラベルに記事外の数値があったら
                台本生成を失敗させるか。**既定は厳格**（引数を渡し忘れた
                呼び出し元が安全側に落ちるため）。ラベルを描かないレンダラ
                （`VideoRenderer.draws_scene_text is False`）のときだけ
                呼び出し元が False にする。False でも検査は走り、警告に残る

        Returns:
            Script: 生成された台本

        Raises:
            ScriptGenerationError: 台本生成に失敗した場合
        """
        spec = get_spec(video_format)
        budget = spec.char_budget(language)
        log_step(f"台本を生成中... ({language}, {spec.label})", "")

        instructions = self._build_system_prompt(language, video_format)

        # モデルの出力がスキーマに適合していても、内容が使えないことがある。
        # 引き直しで直る種類の問題（配列長の不一致、空セグメント、
        # 分量の超過）はここで再試行する。
        last_problem: str | None = None
        for attempt in range(self.VALIDATION_RETRIES):
            remaining = self.VALIDATION_RETRIES - attempt - 1
            try:
                draft = self._request_script(instructions, news_topic)
            except ValidationError as e:
                last_problem = self._summarize_validation_error(e)
                if remaining:
                    log_warning(
                        f"台本の検証に失敗（{attempt + 1}/{self.VALIDATION_RETRIES}）。"
                        f"再生成します: {last_problem}"
                    )
                    continue
                break
            except Exception as e:
                log_error(f"API呼び出しエラー: {e}")
                raise ScriptGenerationError(f"台本生成に失敗しました: {e}") from e

            # 分量の検査。プロンプトの文字数指示は守られないため
            # （実測で47%超過）、ここで弾いて引き直す。
            problem = draft.check_length_budget(language, budget)
            if problem is not None:
                last_problem = problem
                if remaining:
                    log_warning(
                        f"分量が範囲外（{attempt + 1}/{self.VALIDATION_RETRIES}）。"
                        f"再生成します: {problem}"
                    )
                    continue
                # 最終試行でも収まらなければ、生成を止めるより
                # 長いまま進める方が実用的。警告だけ残す。
                log_warning(f"分量が範囲外のまま採用します: {problem}")

            # セグメント単位の分量の検査。全体の予算に収まっていても配分は
            # 偏りうる（実測で50文字前後のセグメントが3つ通り、字幕の1行目が
            # 切れた）。**最終試行では通す**——`Subtitle` が収まるサイズまで
            # 自動で縮めるので、長いセグメントは「文字が小さくなる」だけで
            # 済み、生成を落とすほどの害ではない。数値の捏造（下）と違って
            # 画面に嘘が出るわけではない。
            segment_problem = draft.check_segment_budget(language, spec.segment_char_cap(language))
            if segment_problem is not None:
                last_problem = segment_problem
                if remaining:
                    log_warning(
                        f"セグメントが長すぎる（{attempt + 1}/{self.VALIDATION_RETRIES}）。"
                        f"再生成します: {segment_problem}"
                    )
                    continue
                log_warning(f"セグメントが長いまま採用します: {segment_problem}")

            # 数値の根拠の検査。ラベルを描くレンダラでは**分量と違い、
            # 最終試行でも通さない。** 記事に無い数値が画面に描かれるのは、
            # ニュースを扱う以上最も害が大きい種類の誤りで、警告して採用する
            # 選択肢が無い。
            #
            # 描かないレンダラ（既定の ffmpeg）では警告だけに留める。画面に
            # 一切出ない数値のために再試行を使い切って FAILED にすると、
            # 「既定のままなら振る舞いは変わらない」という前提が失敗経路の側で
            # 崩れる。検査自体は走らせる — 切り替える前に捏造の頻度を知る
            # 唯一の経路であり、`Pipeline.run_from_article` が本文を
            # `content[:2000]` で切る影響（切り捨てた先のバージョン番号が
            # 捏造に見える）もここに出る。
            ungrounded = self._ungrounded_scene_numbers(draft, news_topic)
            if ungrounded and not enforce_scene_grounding:
                log_warning(
                    "シーンのラベルに記事にない数値がありますが、"
                    f"このレンダラは描かないので採用します: {sorted(ungrounded)}"
                )
            elif ungrounded:
                last_problem = f"シーンのラベルに記事にない数値があります: {sorted(ungrounded)}"
                if remaining:
                    log_warning(
                        f"数値の根拠が無い（{attempt + 1}/{self.VALIDATION_RETRIES}）。"
                        f"再生成します: {last_problem}"
                    )
                    continue
                log_error(f"台本の検証に失敗: {last_problem}")
                raise ScriptGenerationError(f"生成された台本が不正です: {last_problem}")

            # full_narration はセグメントの連結で導出される。
            # estimated_duration はモデルの自己申告ではなく文字数から推定する。
            script = draft.to_script(language, source_url=source_url)
            log_success(
                f"{language}台本を生成しました "
                f"({len(script.segment_narrations)}セグメント, "
                f"{len(script.full_narration)}文字, "
                f"推定{script.estimated_duration}秒)"
            )
            return script

        log_error(f"台本の検証に失敗: {last_problem}")
        raise ScriptGenerationError(f"生成された台本が不正です: {last_problem}")

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        ),
        stop=stop_after_attempt(API_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _request_script(self, instructions: str, news_topic: str) -> ScriptDraft:
        """Responses API を Structured Outputs で呼び出して台本を得る。

        `responses.parse` に Pydantic モデルを渡すと、SDK がモデルを
        JSON スキーマへ変換して `text.format` に設定し、検証済みの
        オブジェクトを `output_parsed` に返す。従来の
        「正規表現で JSON を抜き出して json.loads する」経路が不要になり、
        末尾カンマなどの些細な逸脱で失敗することがなくなる。

        Args:
            instructions: システムプロンプト
            news_topic: ニューストピック

        Returns:
            ScriptDraft: 検証済みの台本の下書き

        Raises:
            ValidationError: スキーマ検証に失敗した場合
            ScriptGenerationError: モデルが出力を拒否した場合
        """
        response = self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=news_topic,
            text_format=ScriptDraft,
        )

        draft = response.output_parsed
        if draft is None:
            # 安全機構による拒否や打ち切りで parse できなかった場合
            raise ScriptGenerationError(
                f"モデルが台本を出力しませんでした "
                f"(status={response.status!r}, incomplete={response.incomplete_details!r})"
            )
        return draft

    @staticmethod
    def _summarize_validation_error(error: ValidationError | None) -> str:
        """ValidationError を1行のメッセージに要約する。

        Args:
            error: Pydantic の ValidationError（None 可）

        Returns:
            str: 人が読める要約
        """
        if error is None:
            return "原因不明"
        messages = []
        for err in error.errors():
            location = ".".join(str(p) for p in err["loc"]) or "(root)"
            messages.append(f"{location}: {err['msg']}")
        return " / ".join(messages)

    @classmethod
    def _build_system_prompt(cls, language: str, video_format: str = "short") -> str:
        """言語別・形式別のシステムプロンプトを構築する。

        インスタンスの状態を使わないので classmethod にしてある
        （OpenAI クライアントを作らずにテストからプロンプトを検査できる）。

        Args:
            language: 言語コード
            video_format: 動画形式 ("short", "tiktok", or "long")

        Returns:
            str: システムプロンプト
        """
        if video_format == "long":
            template = cls.SYSTEM_PROMPT_LONG_JA if language == "ja" else cls.SYSTEM_PROMPT_LONG_EN
        elif video_format == "tiktok":
            template = (
                cls.SYSTEM_PROMPT_TIKTOK_JA if language == "ja" else cls.SYSTEM_PROMPT_TIKTOK_EN
            )
        else:  # "short"
            template = cls.SYSTEM_PROMPT_JA if language == "ja" else cls.SYSTEM_PROMPT_EN

        # 分量と構成の指示はプロンプトに埋め込まず、formats.py から生成する。
        # ハードコードしていると仕様と指示がずれる（実際にずれていた）。
        spec = get_spec(video_format)
        return (
            template.replace(
                cls.NARRATION_SPEC_TOKEN,
                cls._narration_spec(language, spec),
            )
            .replace(
                cls.STRUCTURE_SPEC_TOKEN,
                cls._structure_spec(language, spec),
            )
            .replace(
                cls.OVERLAY_SPEC_TOKEN,
                cls._overlay_spec(language),
            )
            .replace(
                cls.SCENES_SPEC_TOKEN,
                cls._scenes_spec(language, spec),
            )
            .replace(
                cls.SCENES_EXAMPLE_TOKEN,
                cls._scenes_example(spec),
            )
            .replace(
                cls.ILLUSTRATION_SPEC_TOKEN,
                cls._illustration_spec(language),
            )
        )

    @staticmethod
    def _narration_spec(language: str, spec: FormatSpec) -> str:
        """`formats.py` の仕様から、分量の指示文を組み立てる。

        Args:
            language: 言語コード
            spec: 形式の仕様

        Returns:
            str: プロンプトに差し込む1行
        """
        n = spec.segment_count
        if language == "ja":
            per_low, per_high = spec.chars_per_segment
            total_low, total_high = spec.total_chars
            return (
                f"ナレーションを{n}個のセグメントに分けて書く。"
                f"各セグメントは{per_low}〜{per_high}文字の自然な話し言葉"
                f"（全体で{total_low}〜{total_high}文字）。"
                f"{n}個すべてに内容を入れ、空のセグメントを作らない。"
                f"全体で{total_high}文字を超えないこと（超えると尺が伸びすぎて再生成になる）"
            )
        per_low, per_high = spec.words_per_segment
        total_low, total_high = spec.total_words
        return (
            f"Write the narration as {n} segments. "
            f"Each segment is {per_low}-{per_high} words of natural spoken English "
            f"({total_low}-{total_high} words total). "
            f"Fill all {n}; never leave a segment empty. "
            f"Do not exceed {total_high} words total — going over forces a regeneration"
        )

    @staticmethod
    def _overlay_spec(language: str) -> str:
        """見出し（text_overlays）の指示を `MAX_HEADLINE_CHARS` から組み立てる。

        上限をプロンプトに直接書かない。以前はプロンプトが目安の字数
        （15-25文字 / 8-15 words）だけを言い、バリデータ側に上限が無かった。
        Remotion では見出しが 92px で描かれ、`AbsoluteFill` はスクロールしない
        ので、長い見出しは字幕に重なる。検査を足したうえで、**指示と検査を
        同じ値から作る**（食い違うとモデルが指示どおりに書いても弾かれる）。

        形式（short/tiktok/long）で分けない。上限は画面の幅と高さから来る値で、
        尺とは無関係だからである。

        Args:
            language: 言語コード

        Returns:
            str: プロンプトに差し込む1行
        """
        if language == "ja":
            return (
                f"各シーンの見出し。画面中央に大きく描くので、2〜3行で読み切れる短文にする"
                f"（目安15〜25文字、**最大{MAX_HEADLINE_CHARS}文字**）。"
                f"超えると検査で弾かれて再生成になる"
            )
        return (
            f"The headline drawn large in the middle of each scene. Keep it to a short "
            f"phrase readable in 2-3 lines (aim for 6-10 words, "
            f"**at most {MAX_HEADLINE_CHARS} characters**). "
            f"Going over is rejected by the check and forces a regeneration"
        )

    @staticmethod
    def _illustration_spec(language: str) -> str:
        """挿絵（illustration_concept）の指示を組み立てる。

        Remotion レンダラは動画1本につき挿絵を**1枚だけ**生成し、画面上部
        48% に通しで表示する（`image_prompts` のような1シーン1枚とは違う）。

        ここまでに2つの構造を試し、どちらも実物を見て否決している。

        1. 自由文の英語1文（`illustration_subject`）——「主題」ではなく
           「場面」を作らせた。ルーティングでコストを1/10にする記事に対し、
           モデルは「オフィスでコーヒーを片手に働く人々」を描いた。
        2. `unit` / `field` / `emphasis` の3語（2026-08-17）——場面は消えたが
           **抽象化しすぎて何の話か分からない絵**になった。実際に生成したのは
           「10本の棒のうち1本だけがティール」で、比率は伝わるが*何の*比率かは
           絵から読めない。

        だから `src/social/card_visual.py` の `CardVisual` と同じ形
        （subject / key_details / labels）に寄せた（2026-08-20）。あちらは
        「説明図＋短い日本語の名札」のために設計され、実測で「2要素＋名札」の
        構図が最も明快だと確定している。**名札を許すことが本質的な変更**で、
        文字を全面禁止していたから構図しか手段が残らず抽象に振れていた。

        禁止として残すのは3つ。**人物**（"expert" のような語が人間と読まれて
        ピクトグラムになる）、**抽象量**（「効率」「コスト」は描けないので
        別の物体に置き換わる）、**数字**（カードで記事に無い「¥980」が絵に
        描かれた前例があり、挿絵は接地検査の対象外）。**画材・配色・技法などの
        スタイル語も禁じ続ける**——書かせると、コード側が前置する固定の
        スタイル文（`ILLUSTRATION_STYLE_PROMPT`）と矛盾した指示が1つの
        プロンプトに混ざる（`ImageGenerator.generate_batch` の
        `enhance=False` の項で実際に踏んだ壊れ方と同じ構造）。

        形式（short/tiktok/long）で分けない。挿絵は動画全体で共有する1枚
        であり、尺とは無関係だからである（`_overlay_spec` と同じ判断）。

        Args:
            language: 言語コード

        Returns:
            str: プロンプトに差し込む1行
        """
        if language == "ja":
            return (
                "動画全体で共有する挿絵1枚を「名札付きの説明図」として、"
                "subject（1枚で説明する仕組みを英語1文で）・"
                "key_details（描く視覚要素とその関係を**ちょうど2個**、"
                f"英語の短い句で、各{MAX_DETAIL_CHARS}字以内）・"
                f"labels（画像内に描く短い**日本語**の名札を0〜4個、各{MAX_LABEL_CHARS}字以内）"
                "の3つで表す。"
                "**記事の要約ではなく、仕組みが図として伝わる具体物に翻訳すること。**"
                "読み手が絵だけで「どう動くのか」を掴めるのが目標で、"
                "名札はその部分が何かを示すために付ける。"
                "key_details は**場面やパネルの説明を書かないこと**"
                "（1項目に複数の要素を詰めると、モデルはそれをコマ1枚として描き、"
                "図がコマ割りになってスマホで読めなくなる。1項目 = 図の中の1要素）。"
                "「対比する2つ」または「原因と結果の2つ」を選ぶと図として成立しやすい。"
                "**人物や人物のピクトグラムを名指ししないこと**"
                '（"expert" "model" のような語は画像生成モデルに人間と'
                "読まれて人物を描かせてしまう）。"
                "**「効率」「コスト」「性能」のような抽象量を主題にしないこと**"
                "（描けないので別の物体に置き換わり、意味が失われる）。"
                "**数字を書かないこと**（金額・割合・日付・バージョン・個数。"
                "記事に無い数字を絵に描かれた前例がある）。"
                "画材・配色・レンダリング技法などのスタイル語も"
                "書かないこと（固定のスタイル文をコード側が別途前置するため、"
                "書くと矛盾した指示になる）"
            )
        return (
            "The single illustration shared across the whole video, expressed "
            "as a labelled explanatory diagram: subject (one English sentence "
            "naming the mechanism to draw), key_details (**exactly two** short "
            f"English phrases for the visual elements and their relation, at most "
            f"{MAX_DETAIL_CHARS} characters each), and labels (0-4 short "
            f"**Japanese** labels to render inside the image, at most "
            f"{MAX_LABEL_CHARS} characters each). "
            "**Translate the mechanism into something literally drawable — do "
            "not summarise the article.** The goal is that a reader grasps how "
            "the thing works from the figure alone; the labels name its parts. "
            "key_details must **not describe a scene or a panel** — packing "
            "several elements into one item makes the model draw it as a comic "
            "frame, which is unreadable at phone size. One item = one element "
            "of the figure. Two contrasting things, or a cause and its effect, "
            "work best. "
            '**Never name a human figure** — words like "expert" or "model" '
            "get read by the image model as a person and drawn as one. "
            "**Never make an abstract quantity the subject** (efficiency, cost, "
            "performance — these cannot be drawn and get replaced by an "
            "unrelated object, losing the meaning). **Never write a numeral** "
            "(prices, percentages, dates, versions, counts) — an image has "
            "already been produced with a figure the article never gave. "
            "Do NOT name a medium, palette, or rendering technique/style "
            "(code prepends a fixed style prompt separately; naming one here "
            "produces a contradictory prompt)"
        )

    @staticmethod
    def _structure_spec(language: str, spec: FormatSpec) -> str:
        """独自解説を含む構成順序の指示を組み立てる。

        フック → 事実 → 技術的な仕組み → 実務インパクト → 結論/CTA の順を
        セグメント番号で指定する。番号は `segment_count` から計算するので、
        プロンプト文字列には書かない（short/tiktok は6、long は10）。

        分量の上限をここでも繰り返す理由: 構成を5パートに割った直後の実測で、
        ショート3本すべてが予算（180〜240文字）を超えた（307/310/378文字、
        1本は63秒で上限60秒を超えた）。パートを増やすとモデルは
        各パートに書き足す。解説は事実のなぞりを**置き換える**もので、
        総量を増やすものではないと明示する必要がある。

        Args:
            language: 言語コード
            spec: 形式の仕様

        Returns:
            str: プロンプトに差し込む構成の指示
        """
        parts = segment_allocation(spec.segment_count)
        # 各パートが占めるセグメント番号の範囲（1始まり）を作る
        spans: dict[str, str] = {}
        cursor = 1
        for name, count in parts.items():
            last = cursor + count - 1
            spans[name] = str(cursor) if count == 1 else f"{cursor}〜{last}"
            cursor = last + 1

        if language == "ja":
            return (
                "segment_narrations は次の順序で構成する（この順序を崩さない）。\n"
                f"- セグメント{spans['hook']}: フック。冒頭で視聴者を引き付ける\n"
                f"- セグメント{spans['facts']}: 事実。何が起きたのかを簡潔に\n"
                f"- セグメント{spans['mechanism']}: 技術的な仕組み。"
                "technical_insight の内容をここで語る\n"
                f"- セグメント{spans['impact']}: 実務インパクト。"
                "practical_impact の内容をここで語る\n"
                f"- セグメント{spans['conclusion']}: 結論とCTA\n"
                "\n"
                "【重要】technical_insight と practical_impact は"
                "フィールドを埋めるだけでは不十分で、"
                "対応するセグメントのナレーション本文に必ず反映すること。"
                "ニュースをなぞるだけの台本は採用しない"
                "（要約アカウントとの差別化が目的であり、"
                "再利用コンテンツと判定されるリスクを避けるため）。\n"
                "また text_overlays のうち**少なくとも1枚**は"
                "考察パート（仕組み または 実務インパクト）の要点にすること。\n"
                "\n"
                "【分量】この構成にしても分量の予算は増えない。"
                f"各セグメントは{spec.chars_per_segment[0]}〜{spec.chars_per_segment[1]}文字に収め、"
                f"全体で{spec.total_chars[1]}文字を超えないこと。"
                "解説は事実の説明を**置き換える**もので、足すものではない。"
                "仕組みは専門用語を1つに絞って言い切り、"
                "インパクトは「誰の何がどう変わるか」を1点だけ挙げる。\n"
                "**各セグメントは単独で文として言い切ること。**"
                "セグメントの境界は画像が切り替わる位置なので、"
                "文を途中で切って次のセグメントに続けてはならない"
                f"（短くしすぎて{spec.chars_per_segment[0]}文字を下回るとこれが起きる）。\n"
                # 上限は「全体で超えないこと」だけでは守られない。実測では
                # 全体240文字の枠に収まったまま、50文字前後のセグメントが3つ
                # 出て字幕が画面に収まらなかった。**1セグメントの上限を
                # 数字で明示する**（検査側の上限と同じ値を書く）。
                f"1つのセグメントが{spec.segment_char_cap(language)}文字を超えては"
                "**ならない**。字幕は1セグメントを1画面に出すので、"
                "長いセグメントは画面に収まらず文字が読めなくなる。"
                "全体を短くするのではなく、セグメント間で均等に配ること。"
            )

        spans_en = {k: v.replace("〜", "-") for k, v in spans.items()}
        return (
            "Structure segment_narrations in this exact order (do not reorder):\n"
            f"- Segment {spans_en['hook']}: Hook. Grab attention immediately\n"
            f"- Segment {spans_en['facts']}: Facts. What happened, concisely\n"
            f"- Segment {spans_en['mechanism']}: How it works technically. "
            "Deliver the technical_insight content here\n"
            f"- Segment {spans_en['impact']}: Practical impact. "
            "Deliver the practical_impact content here\n"
            f"- Segment {spans_en['conclusion']}: Conclusion and CTA\n"
            "\n"
            "IMPORTANT: filling the technical_insight and practical_impact fields is "
            "not enough — their substance MUST appear in the narration of the "
            "corresponding segments. A script that only restates the news is rejected "
            "(the goal is to differentiate from summary accounts and to avoid being "
            "flagged as reused content).\n"
            "At least ONE of the text_overlays must carry a point from the analysis "
            "part (the mechanism or the practical impact).\n"
            "\n"
            "LENGTH: this structure does NOT raise the budget. Keep every segment "
            f"between {spec.words_per_segment[0]} and {spec.words_per_segment[1]} words "
            f"({spec.total_words[1]} words total maximum). The analysis REPLACES "
            "restated facts; it is not added on top. State the mechanism with a "
            "single technical term, and name exactly one concrete change for the "
            "practical impact.\n"
            "**Every segment must stand alone as a complete sentence.** Segment "
            "boundaries are where the image changes, so never split a sentence across "
            f"two segments (this happens when a segment drops below "
            f"{spec.words_per_segment[0]} words).\n"
            # 上限は「全体で超えないこと」だけでは守られない（日本語側の
            # コメント参照。実測で全体の枠に収まったまま個別のセグメントが
            # 溢れ、字幕が切れた）。1セグメントの上限を数字で明示する。
            f"No single segment may exceed {round(spec.words_per_segment[1] * 1.2)} words. "
            "The subtitle shows one segment per screen, so an over-long segment does "
            "not fit and becomes unreadable. Distribute evenly across segments rather "
            "than shortening the whole."
        )

    @staticmethod
    def _scenes_spec(language: str, spec: FormatSpec) -> str:
        """シーンの指示文を `models/scene.py` の定義から組み立てる。

        レイアウト名・要素数・statement の上限をプロンプトに直接書かない。
        書くとスキーマの定義とプロンプトがずれる（`formats.py` の冒頭に
        書いてある失敗そのもの）。

        Args:
            language: 言語コード
            spec: 形式の仕様

        Returns:
            str: プロンプトに差し込む指示
        """
        statement_limit = spec.segment_count // 2
        compare_items = ITEMS_PER_LAYOUT[SceneLayout.COMPARE]

        if language == "ja":
            return (
                f"各セグメントに対応する図解の構造を{spec.segment_count}個。"
                f"layout は次の3つから選ぶ。\n"
                f"  - compare: 対比する2つを並べる。items を{compare_items}個\n"
                f"  - flow: 原因 → 結果を矢印で繋ぐ。items を{compare_items}個\n"
                f"  - statement: 図なし。見出しだけを見せる。items は空配列\n"
                f"items は図に入れる**日本語の名札**で、各{MAX_LABEL_CHARS}文字以内。"
                f"説明文を入れてはならない（名札であって文ではない）。\n"
                f"**statement は最大{statement_limit}個まで。** 図が無いシーンばかりだと"
                f"静止画を並べただけの動画に戻る。フックと結論に使い、"
                f"本体は compare か flow にする。\n"
                f"relation は2つの要素の**関係性を表す語**で、"
                f"各{MAX_RELATION_CHARS}文字以内。"
                f"「切替」「1/10」「並列化」のような単語1つで、"
                f"「→」「vs」のような記号や接続語ではない。"
                f"compare と flow では必須、statement では空文字列にする。\n"
                f"**items に数値を書くときは、記事本文に出てくる数値だけを使うこと。**"
                f"価格・割合・日付・バージョン番号・件数を自分で作ってはならない"
                f"（検査で弾かれて再生成になる）。"
            )
        return (
            f"Provide {spec.segment_count} scene structures, one per segment. "
            f"Choose layout from exactly these three:\n"
            f"  - compare: two things side by side. Exactly {compare_items} items\n"
            f"  - flow: cause -> effect joined by an arrow. Exactly {compare_items} items\n"
            f"  - statement: no diagram, headline only. items must be an empty array\n"
            f"items are short Japanese name tags drawn inside the diagram, "
            f"each at most {MAX_LABEL_CHARS} characters. Never put a sentence there.\n"
            f"**At most {statement_limit} statement scenes.** Too many turns the video "
            f"back into a slideshow. Use them for the hook and the conclusion; "
            f"make the body compare or flow.\n"
            f"relation is the word for how the two items relate, "
            f"at most {MAX_RELATION_CHARS} characters. Something like "
            f'"switch" or "1/10" or "parallelized" — a single word, not a '
            'connector like "->" or "vs". '
            "Required for compare and flow, empty string for statement.\n"
            f"**Any number in items MUST appear in the source article.** Never invent "
            f"prices, percentages, dates, version numbers, or counts "
            f"(the check rejects them and forces a regeneration)."
        )

    @staticmethod
    def _scenes_example(spec: FormatSpec) -> str:
        """出力例の scenes 配列を組み立てる。

        要素数を `segment_count` から作る。プロンプトに固定で書くと、
        形式ごとに数が違う（short/tiktok は6、long は10）ため必ずずれる。

        Args:
            spec: 形式の仕様

        Returns:
            str: JSON 配列の文字列（`<output_format>` に差し込む）
        """
        n = spec.segment_count
        entries: list[str] = []
        for i in range(n):
            if i == 0 or i == n - 1:
                # フックと結論は図なしにするのが自然
                entries.append('        {"layout": "statement", "items": [], "relation": ""}')
            elif i % 2 == 1:
                entries.append(
                    '        {"layout": "compare", "items": ["名札A", "名札B"], '
                    '"relation": "対比語"}'
                )
            else:
                entries.append(
                    '        {"layout": "flow", "items": ["原因", "結果"], "relation": "変化語"}'
                )
        return "[\n" + ",\n".join(entries) + "\n    ]"

    @staticmethod
    def _ungrounded_scene_numbers(draft: ScriptDraft, news_topic: str) -> set[str]:
        """シーンのラベルに、記事に根拠が無い数値が無いか調べる。

        カードでは「画像側は機械的に検査できないのでスタイル文で閉じた」
        （880c95f。記事に無い ¥980 が絵の小物に描かれた）。Remotion では
        **描く文字がデータなので突き合わせられる**ので、検査で閉じる。

        スキーマ側（`SceneVisual`）では検査できない。`ScriptDraft` が
        `language` を持たないのと同じ理由で、記事本文を持たないため。

        Args:
            draft: 検証する下書き
            news_topic: モデルに渡した記事のテキスト（タイトル＋本文）

        Returns:
            set[str]: 根拠の無い数値。空なら合格
        """
        labels = " ".join(item for scene in draft.scenes for item in scene.items)
        if not labels.strip():
            return set()
        return ungrounded_numbers(labels, news_topic)
