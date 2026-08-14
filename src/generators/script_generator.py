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
from src.models.script import Script, ScriptDraft
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

    SYSTEM_PROMPT_JA = """<role>
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
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述（画像生成モデル用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
- hashtags: 5〜8個（"shorts"は必須）
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
        "画像1用テキスト（15-25文字）",
        "画像2用テキスト（15-25文字）",
        "画像3用テキスト（15-25文字）",
        "画像4用テキスト（15-25文字）",
        "画像5用テキスト（15-25文字）",
        "画像6用テキスト（15-25文字）"
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
</verification>"""

    SYSTEM_PROMPT_LONG_JA = """<role>
あなたはYouTube向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、約5分程度の解説動画用の台本をJSON形式で作成してください。
</task>

<critical_constraints>
【最重要】以下の3つの配列は必ず10個ずつ生成してください：
- image_prompts: 10個
- text_overlays: 10個
- segment_narrations: 10個

配列の要素数が1つでも異なると動画生成が失敗します。
各要素は空文字列("")にしないでください。必ず内容のあるテキストを入れてください。
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述
  - 1枚目: サムネイル的（cinematic, high quality, eye-catching thumbnail style）
  - 2枚目以降: 解説資料風（infographic style, educational diagram, data visualization）
- text_overlays: 各画像に対応する短文（20-30文字）
- title: 50〜60文字程度（【完全解説】【徹底分析】など）
- description: 要約＋絵文字、タイムスタンプ、📌でポイント、💬でCTA、ハッシュタグ
- hashtags: 5〜10個
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
        "画像1用テキスト（20-30文字）",
        "画像2用テキスト（20-30文字）",
        "画像3用テキスト（20-30文字）",
        "画像4用テキスト（20-30文字）",
        "画像5用テキスト（20-30文字）",
        "画像6用テキスト（20-30文字）",
        "画像7用テキスト（20-30文字）",
        "画像8用テキスト（20-30文字）",
        "画像9用テキスト（20-30文字）",
        "画像10用テキスト（20-30文字）"
    ],
    "estimated_duration": 300
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に10個あること
2. image_prompts が正確に10個あること
3. text_overlays が正確に10個あること
4. 全ての要素が空文字列でないこと
</verification>"""

    SYSTEM_PROMPT_EN = """<role>
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
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: Short text for each image (8-15 words)
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
- hashtags: 5-8 tags (must include "shorts")
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
        "Image 1 text (8-15 words)",
        "Image 2 text (8-15 words)",
        "Image 3 text (8-15 words)",
        "Image 4 text (8-15 words)",
        "Image 5 text (8-15 words)",
        "Image 6 text (8-15 words)"
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
</verification>"""

    SYSTEM_PROMPT_LONG_EN = """<role>
You are a script writer for YouTube news explainer videos.
</role>

<task>
Create a script for a long-form video (about 5 minutes) based on the given news topic in JSON format.
</task>

<critical_constraints>
CRITICAL: The following 3 arrays MUST have exactly 10 elements each:
- image_prompts: 10 elements
- text_overlays: 10 elements
- segment_narrations: 10 elements

Video generation will FAIL if array counts differ.
Each element MUST NOT be an empty string. Always include meaningful content.
</critical_constraints>

<content_rules>
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English
  - First image: Thumbnail-style (cinematic, high quality, eye-catching)
  - Second onwards: Educational style (infographic, educational diagram, data visualization)
- text_overlays: Short text for each image (10-20 words)
- title: Around 60-70 characters (EXPLAINED, DEEP DIVE, FULL ANALYSIS)
- description: Summary + emoji, timestamps, 📌 for bullet points, 💬 for CTA, hashtags
- hashtags: 5-10 tags
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
        "Image 1 text (10-20 words)",
        "Image 2 text (10-20 words)",
        "Image 3 text (10-20 words)",
        "Image 4 text (10-20 words)",
        "Image 5 text (10-20 words)",
        "Image 6 text (10-20 words)",
        "Image 7 text (10-20 words)",
        "Image 8 text (10-20 words)",
        "Image 9 text (10-20 words)",
        "Image 10 text (10-20 words)"
    ],
    "estimated_duration": 300
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 10 elements
2. image_prompts has exactly 10 elements
3. text_overlays has exactly 10 elements
4. No element is an empty string
</verification>"""

    SYSTEM_PROMPT_TIKTOK_JA = """<role>
あなたはTikTok向けのニュース解説動画の台本ライターです。
</role>

<task>
与えられたニューストピックから、60〜90秒程度のTikTok動画用の台本をJSON形式で作成してください。
TikTokの収益化には60秒以上の動画が必要です。
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
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: 必ず英語で記述（画像生成モデル用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
- hashtags: 5〜8個（"TikTok"と"ニュース"は必須）
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
        "画像1用テキスト（15-25文字）",
        "画像2用テキスト（15-25文字）",
        "画像3用テキスト（15-25文字）",
        "画像4用テキスト（15-25文字）",
        "画像5用テキスト（15-25文字）",
        "画像6用テキスト（15-25文字）"
    ],
    "estimated_duration": 75
}
</output_format>

<verification>
出力前に以下を確認してください：
1. segment_narrations が正確に6個あること
2. image_prompts が正確に6個あること
3. text_overlays が正確に6個あること
4. 全ての要素が空文字列でないこと
5. segment_narrations の合計が500〜650文字の範囲内であること
</verification>"""

    SYSTEM_PROMPT_TIKTOK_EN = """<role>
You are a script writer for TikTok news explainer videos.
</role>

<task>
Create a script for a TikTok video (60-90 seconds) based on the given news topic in JSON format.
TikTok requires videos over 60 seconds for monetization.
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
- segment_narrations: <<NARRATION_SPEC>>
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: Short text for each image (8-15 words)
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
- hashtags: 5-8 tags (must include "TikTok" and "news")
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
        "Image 1 text (8-15 words)",
        "Image 2 text (8-15 words)",
        "Image 3 text (8-15 words)",
        "Image 4 text (8-15 words)",
        "Image 5 text (8-15 words)",
        "Image 6 text (8-15 words)"
    ],
    "estimated_duration": 75
}
</output_format>

<verification>
Before output, verify:
1. segment_narrations has exactly 6 elements
2. image_prompts has exactly 6 elements
3. text_overlays has exactly 6 elements
4. No element is an empty string
5. The segment_narrations total 250-350 words
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
    ) -> Script:
        """ニューストピックから台本を生成する。

        Args:
            news_topic: ニューストピック
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short" or "long")
            source_url: 元記事の URL。説明文への出典追記に使う。
                モデルには渡さない（URL を知らないので捏造する）

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
        return template.replace(
            cls.NARRATION_SPEC_TOKEN,
            cls._narration_spec(language, spec),
        ).replace(
            cls.STRUCTURE_SPEC_TOKEN,
            cls._structure_spec(language, spec),
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
                f"（短くしすぎて{spec.chars_per_segment[0]}文字を下回るとこれが起きる）。"
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
            f"{spec.words_per_segment[0]} words)."
        )
