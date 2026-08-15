"""画像カードの視覚指示。

なぜ記事本文をそのままプロンプトに入れないか
------------------------------------------
OpenAI のガイドは `background/scene -> subject -> key details ->
constraints` の順と、長文1段落ではなくラベル付きの短いセグメントを
推奨している。数千字の日本語記事を投げると、モデルが「何を1枚に描くか」を
自分で決めることになり、毎回違うものが出る。

既存の動画パイプラインと同じ2段構えにする。LLM が英語の視覚指示を作り、
コード側が固定のスタイル文を前置して gpt-image-2 に渡す
（`images.generate` に system prompt は無いため、固定の指示は前置しかない）。
"""

from __future__ import annotations

import json

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models.news import NewsArticle

# X のタイムラインで 16:9 より縦の面積を取れる。
# 両辺が16の倍数という gpt-image-2 の制約も満たす。
CARD_IMAGE_SIZE = "1024x1024"

# 固定のスタイル指定文。**ここが単一の情報源。**
#
# ガイドの推奨順（scene -> subject -> details -> constraints）に沿って
# ラベルで区切っている。1段落に流すと、モデルが指示を取りこぼす。
CARD_STYLE_PROMPT = """\
Medium: hand-drawn illustrated sketch — an engineer's whiteboard explainer
  redrawn cleanly. Loose ink linework with visible stroke ends, light marker
  fills, faint paper grain.
Palette: off-white paper ground, near-black ink, one accent (deep teal), one
  highlight (warm amber). Flat fills only — no gradients, no glossy 3D render.
Composition: a single explanatory diagram, centred, front-on flat view,
  generous margins. One idea only — no comic panels, no multi-step timeline.
Intended use: an explanatory illustration for a technology news post.
Constraints: render NO Japanese or CJK characters. No watermark, no logos, no
  UI chrome, no photorealism. Do not depict any real, identifiable person; use
  simple silhouettes if a figure is needed."""


class CardVisual(BaseModel):
    """1枚の概念図に描くもの。

    LLM への出力契約そのもの。フィールドを増やすと生成される
    JSON スキーマが変わる。
    """

    subject: str = Field(description="1枚で説明する概念を英語1文で")
    key_details: list[str] = Field(
        min_length=2, max_length=4, description="描く視覚要素とその関係（英語）"
    )
    labels: list[str] = Field(
        default_factory=list, max_length=4, description="画像に入れる短いラベル（英大文字）"
    )
    caption_ja: str = Field(description="投稿本文に載せる日本語の1文。画像には入れない")

    @field_validator("labels")
    @classmethod
    def _labels_must_be_ascii_upper(cls, value: list[str]) -> list[str]:
        """ラベルは英大文字だけに限る。

        gpt-image-2 の CJK 描画は保証されていない。日本語は投稿本文に
        持たせれば確実に読めるので、画像に賭ける必要がない。
        """
        for label in value:
            if not label.isascii() or label != label.upper():
                raise ValueError(f"ラベルは英大文字のみ: {label!r}")
        return value


def build_card_prompt(visual: CardVisual) -> str:
    """gpt-image-2 に渡すプロンプトを組む。

    Args:
        visual: LLM が作った視覚指示

    Returns:
        str: 固定のスタイル文を前置したプロンプト
    """
    parts = [CARD_STYLE_PROMPT, f"Subject: {visual.subject}"]
    parts.append("Key details: " + "; ".join(visual.key_details))
    if visual.labels:
        quoted = ", ".join(f'"{label}"' for label in visual.labels)
        parts.append(
            f"Labels: render exactly these words, {quoted}, in a small hand-lettered "
            "sans-serif placed beside the element each one names."
        )
    else:
        parts.append("Labels: none. Render no words at all.")
    return "\n".join(parts)


class CardVisualGenerationError(Exception):
    """視覚指示の生成、またはその検証に失敗した。"""


# `CardVisual` をそのまま Structured Outputs のスキーマに使わない理由
# --------------------------------------------------------------------
# `CardVisual` にはラベルの英大文字チェックという pydantic バリデータが
# 付いている。SDK 側のスキーマ検証と JSON パース処理の間でこのバリデータが
# 例外を起こすと、失敗の位置が SDK 内部になり `_complete` の再試行対象
# （通信・レートリミット・5xx）と混ざってしまう。応答の受け取り（この
# ペイロード）と業務上の検証（CardVisual への変換）を分けることで、
# 「取れたが不正」を再試行せず即座に CardVisualGenerationError にできる。
class _CardVisualPayload(BaseModel):
    """LLM の応答スキーマ。業務上の検証は持たない。"""

    subject: str
    key_details: list[str]
    labels: list[str]
    caption_ja: str


SYSTEM_PROMPT_CARD_VISUAL = """<role>
あなたは技術ニュースを1枚の説明図に翻訳するビジュアルディレクターです。
</role>

<task>
与えられた記事から、hand-drawn な説明図に描くべき視覚要素を
英語で構造化して出力してください。記事の要約文ではなく、
画像生成モデルへの指示（何を描くか）を作ります。
</task>

<content_rules>
- subject: 1枚で説明する概念を英語1文で（記事のトーンではなく、
  図として描ける具体物に翻訳する）
- key_details: 描く視覚要素とその関係を2〜4個、英語で
  （例: "a funnel narrowing", "two arrows returning to a store"）
- labels: 画像内に入れる短いラベルを0〜4個、英大文字のみ
  （日本語・小文字は不可。gpt-image-2 は CJK の描画を保証しないため）
- caption_ja: 投稿本文に載せる日本語の1文（画像には使わない、読者向けの説明）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{
    "subject": "...",
    "key_details": ["...", "..."],
    "labels": ["...", "..."],
    "caption_ja": "..."
}
</output_format>"""

# API 呼び出しの通信エラー・レートリミット・5xx に対する試行回数。
_API_RETRIES = 4


class CardVisualGenerator:
    """Azure OpenAI で画像カードの視覚指示を生成するクラス。

    `PostGenerator` と同じ形（Structured Outputs、`_complete` を
    テストの差し込み点として切り出す）にしている。
    """

    def __init__(self, endpoint: str, api_key: str, deployment: str):
        """CardVisualGeneratorを初期化する。

        Args:
            endpoint: Azure OpenAI endpoint URL
            api_key: Azure OpenAI API key
            deployment: 視覚指示生成モデルのデプロイ名

        Raises:
            ValueError: endpoint / api_key / deployment が空の場合
        """
        if not endpoint:
            raise ValueError("Azure OpenAI endpoint が指定されていません")
        if not api_key:
            raise ValueError("Azure OpenAI API key が指定されていません")
        if not deployment:
            raise ValueError("視覚指示生成モデルのデプロイ名が指定されていません")

        # Azure OpenAI v1 エンドポイント形式（ScriptGenerator と同じ組み立て）
        base_url = endpoint.rstrip("/")
        if not base_url.endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = deployment

    def generate(self, article: NewsArticle) -> CardVisual:
        """記事から画像カードの視覚指示を生成する。

        記事の本文をこのメソッドの外（画像生成プロンプト）に渡すことは
        しない。渡すのはこの LLM 呼び出しの入力だけで、出力である
        `CardVisual`（英語の短い構造化データ）だけが画像生成に流れる。

        Args:
            article: 元記事

        Returns:
            CardVisual: 検証済みの視覚指示

        Raises:
            CardVisualGenerationError: 応答が JSON として解釈できない、
                またはスキーマ・業務ルール（ラベルの英大文字など）を
                満たさない場合
        """
        user_prompt = f"タイトル: {article.title}\n本文: {article.content}"
        raw = self._complete(user_prompt)
        try:
            payload = _CardVisualPayload.model_validate_json(raw)
            return CardVisual(**payload.model_dump())
        except (ValidationError, json.JSONDecodeError) as e:
            raise CardVisualGenerationError(f"視覚指示が検証を満たしません: {e}") from e

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        ),
        stop=stop_after_attempt(_API_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _complete(self, user_prompt: str) -> str:
        """1回の補完を Structured Outputs で呼び、JSON 文字列を返す。

        `PostGenerator._complete` と同じ理由で `responses.parse` を使う
        （JSON モードでは必須フィールドの省略を防げない）。

        Args:
            user_prompt: ユーザープロンプト

        Returns:
            str: 応答を JSON 文字列化したもの

        Raises:
            CardVisualGenerationError: モデルが出力を拒否した場合
        """
        response = self.client.responses.parse(
            model=self.model,
            instructions=SYSTEM_PROMPT_CARD_VISUAL,
            input=user_prompt,
            text_format=_CardVisualPayload,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise CardVisualGenerationError(
                f"モデルが視覚指示を出力しませんでした "
                f"(status={response.status!r}, incomplete={response.incomplete_details!r})"
            )
        return parsed.model_dump_json()
