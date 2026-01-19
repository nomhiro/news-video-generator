"""Script generation using Azure OpenAI Responses API."""

import json
import re
import time
from typing import Optional

from openai import OpenAI

from src.models.script import Script
from src.utils.logger import log_step, log_success, log_error


class ScriptGenerationError(Exception):
    """Script generation failed."""

    pass


class ScriptGenerator:
    """Azure OpenAI Responses APIを使用して台本を生成するクラス。

    Attributes:
        client: OpenAI APIクライアント
        model: 使用するモデル（デプロイメント名）
    """

    MAX_RETRIES = 3
    BASE_DELAY = 1.0

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
- ナレーション(full_narration): 250〜300文字の自然な話し言葉
- segment_narrations: full_narrationを6つに分割（連結するとfull_narrationと一致）
- image_prompts: 必ず英語で記述（Gemini Image用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
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
    "full_narration": "完全なナレーション台本（250〜300文字、自然な話し言葉）",
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
5. segment_narrationsを連結するとfull_narrationと一致すること
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
- ナレーション(full_narration): 2000〜2500文字の自然な話し言葉
- segment_narrations: full_narrationを10個に分割（各セグメント200-250文字程度、連結するとfull_narrationと一致）
- image_prompts: 必ず英語で記述
  - 1枚目: サムネイル的（cinematic, high quality, eye-catching thumbnail style）
  - 2枚目以降: 解説資料風（infographic style, educational diagram, data visualization）
- text_overlays: 各画像に対応する短文（20-30文字）
- title: 50〜60文字程度（【完全解説】【徹底分析】など）
- description: 要約＋絵文字、タイムスタンプ、📌でポイント、💬でCTA、ハッシュタグ
- hashtags: 5〜10個
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【完全解説】〇〇の真相！△△について徹底分析",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n⏱️ タイムスタンプ\\n00:00 イントロ\\n00:30 〜について\\n\\n💬 コメントで教えて！\\n👍 チャンネル登録お願いします！\\n\\n#タグ1 #タグ2",
    "hashtags": ["タグ1", "タグ2", "タグ3", "タグ4", "タグ5", "ニュース", "解説"],
    "hook": "最初の10秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3", "ポイント4", "ポイント5", "ポイント6"],
    "conclusion": "締めの言葉（まとめと今後の展望）",
    "full_narration": "完全なナレーション台本（2000〜2500文字、自然な話し言葉）",
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
5. segment_narrationsを連結するとfull_narrationと一致すること
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
- Narration (full_narration): 120-150 words, natural spoken language
- segment_narrations: Split full_narration into 6 parts (concatenation equals full_narration)
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: Short text for each image (8-15 words)
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
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
    "full_narration": "Complete narration script (120-150 words, natural spoken language)",
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
5. Concatenating segment_narrations equals full_narration
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
- Narration (full_narration): 750-900 words, natural spoken language
- segment_narrations: Split full_narration into 10 parts (each segment ~75-90 words, concatenation equals full_narration)
- image_prompts: In English
  - First image: Thumbnail-style (cinematic, high quality, eye-catching)
  - Second onwards: Educational style (infographic, educational diagram, data visualization)
- text_overlays: Short text for each image (10-20 words)
- title: Around 60-70 characters (EXPLAINED, DEEP DIVE, FULL ANALYSIS)
- description: Summary + emoji, timestamps, 📌 for bullet points, 💬 for CTA, hashtags
- hashtags: 5-10 tags
</content_rules>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "EXPLAINED: The Truth Behind [Topic] | Complete Analysis",
    "description": "Full breakdown 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n⏱️ Timestamps\\n00:00 Intro\\n00:30 Overview\\n\\n💬 Comment below!\\n👍 Subscribe!\\n\\n#tag1 #tag2",
    "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5", "news", "explained"],
    "hook": "Opening hook to grab attention in 10 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5", "Point 6"],
    "conclusion": "Closing statement (summary, future outlook)",
    "full_narration": "Complete narration script (750-900 words, natural spoken language)",
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
5. Concatenating segment_narrations equals full_narration
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
- ナレーション(full_narration): 500〜650文字の自然な話し言葉（60〜90秒に相当）
- segment_narrations: full_narrationを6つに分割（各セグメント80-110文字程度、連結するとfull_narrationと一致）
- image_prompts: 必ず英語で記述（Gemini Image用）、各プロンプトに "cinematic, high quality" を含める
- text_overlays: 各画像に対応する短文（15-25文字）
- title: 40文字程度（【】や！で注目を集める、数字や疑問形を活用）
- description: 1行目に要約＋絵文字、📌でポイント箇条書き、💬でCTA、最後にハッシュタグ
- hashtags: 5〜8個（"TikTok"と"ニュース"は必須）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "title": "【衝撃】〇〇が××！△△の行方は？",
    "description": "動画の要約文🔥\\n\\n📌 この動画でわかること\\n・ポイント1\\n・ポイント2\\n・ポイント3\\n\\n💬 コメントで教えて！\\n\\n#TikTok #ニュース #タグ1 #タグ2",
    "hashtags": ["TikTok", "ニュース", "タグ1", "タグ2", "タグ3", "タグ4"],
    "hook": "最初の5秒で視聴者を引き付けるフック",
    "main_points": ["ポイント1", "ポイント2", "ポイント3", "ポイント4"],
    "conclusion": "締めの一言（アクションを促す）",
    "full_narration": "完全なナレーション台本（500〜650文字、自然な話し言葉）",
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
5. segment_narrationsを連結するとfull_narrationと一致すること
6. full_narrationが500〜650文字の範囲内であること
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
- Narration (full_narration): 250-350 words, natural spoken language (equals 60-90 seconds)
- segment_narrations: Split full_narration into 6 parts (each segment ~40-60 words, concatenation equals full_narration)
- image_prompts: In English, include "cinematic, high quality" in each
- text_overlays: Short text for each image (8-15 words)
- title: Around 50 characters (use attention-grabbing words like SHOCKING, BREAKING)
- description: Line 1 summary + emoji, 📌 for bullet points, 💬 for CTA, end with hashtags
- hashtags: 5-8 tags (must include "TikTok" and "news")
</content_rules>

<output_format>
Output ONLY the following JSON format. Do not include any text other than JSON.

{
    "title": "SHOCKING: Something Amazing Happened!",
    "description": "Summary 🔥\\n\\n📌 What you'll learn:\\n• Point 1\\n• Point 2\\n• Point 3\\n\\n💬 Comment below!\\n\\n#TikTok #news #tag1 #tag2",
    "hashtags": ["TikTok", "news", "tag1", "tag2", "tag3", "tag4"],
    "hook": "Opening hook to grab attention in 5 seconds",
    "main_points": ["Point 1", "Point 2", "Point 3", "Point 4"],
    "conclusion": "Short closing statement with call to action",
    "full_narration": "Complete narration script (250-350 words, natural spoken language)",
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
5. Concatenating segment_narrations equals full_narration
6. full_narration is within 250-350 words
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

    def generate(self, news_topic: str, language: str = "ja", video_format: str = "short") -> Script:
        """ニューストピックから台本を生成する。

        Args:
            news_topic: ニューストピック
            language: 言語コード ("ja" or "en")
            video_format: 動画形式 ("short" or "long")

        Returns:
            Script: 生成された台本

        Raises:
            ScriptGenerationError: 台本生成に失敗した場合
        """
        format_labels = {"long": "ロング", "tiktok": "TikTok", "short": "ショート"}
        format_label = format_labels.get(video_format, "ショート")
        log_step(f"台本を生成中... ({language}, {format_label})", "")

        instructions = self._build_system_prompt(language, video_format)

        for attempt in range(self.MAX_RETRIES):
            try:
                # Use Responses API
                response = self.client.responses.create(
                    model=self.model,
                    instructions=instructions,
                    input=news_topic,
                )

                # Get response text using output_text helper
                response_text = response.output_text
                script = self._parse_response(response_text, language)
                log_success(f"{language}台本を生成しました")
                return script

            except json.JSONDecodeError as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"JSONパースエラー: {e}")
                raise ScriptGenerationError(f"台本のJSONパースに失敗しました: {e}")

            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.BASE_DELAY * (2**attempt)
                    time.sleep(delay)
                    continue
                log_error(f"API呼び出しエラー: {e}")
                raise ScriptGenerationError(f"台本生成に失敗しました: {e}")

        raise ScriptGenerationError("最大リトライ回数を超えました")

    def _build_system_prompt(self, language: str, video_format: str = "short") -> str:
        """言語別・形式別のシステムプロンプトを構築する。

        Args:
            language: 言語コード
            video_format: 動画形式 ("short", "tiktok", or "long")

        Returns:
            str: システムプロンプト
        """
        if video_format == "long":
            if language == "ja":
                return self.SYSTEM_PROMPT_LONG_JA
            return self.SYSTEM_PROMPT_LONG_EN
        elif video_format == "tiktok":
            if language == "ja":
                return self.SYSTEM_PROMPT_TIKTOK_JA
            return self.SYSTEM_PROMPT_TIKTOK_EN
        else:  # "short"
            if language == "ja":
                return self.SYSTEM_PROMPT_JA
            return self.SYSTEM_PROMPT_EN

    def _parse_response(self, response_text: str, language: str) -> Script:
        """APIレスポンスをパースしてScriptオブジェクトを生成する。

        Args:
            response_text: APIレスポンステキスト
            language: 言語コード

        Returns:
            Script: パースされた台本

        Raises:
            json.JSONDecodeError: JSONパースに失敗した場合
            ScriptGenerationError: スクリプト検証に失敗した場合
        """
        # Try to extract JSON from response
        json_match = re.search(r"\{[\s\S]*\}", response_text)
        if json_match:
            json_str = json_match.group()
        else:
            json_str = response_text

        data = json.loads(json_str)
        data["language"] = language

        script = Script.from_dict(data)
        self._validate_script(script)
        return script

    def _validate_script(self, script: Script) -> None:
        """生成されたスクリプトを検証する。

        Args:
            script: 検証対象のスクリプト

        Raises:
            ScriptGenerationError: 検証に失敗した場合
        """
        # segment_narrationsの存在チェック
        if not script.segment_narrations:
            raise ScriptGenerationError("segment_narrationsが空です")

        # 配列長の一致チェック
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

        # 各セグメントの空チェック
        for i, narration in enumerate(script.segment_narrations):
            if not narration or not narration.strip():
                raise ScriptGenerationError(f"セグメント{i+1}のnarrationが空です")
