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

# ラベル1つの最大文字数。図が文字で埋まると縮小表示で読めなくなるので、
# 説明は `caption_ja` の1行に寄せ、ラベルは名札の役割に留める。
MAX_LABEL_CHARS = 8

# 視覚要素1つの最大文字数。
#
# 実測（2026-08-16）で決めた値。上限を置かないと、モデルは1項目に
# 「パネル1枚ぶんの記述」（250〜350字）を書く。4項目あれば4パネルになり、
# スタイル文の "One idea only — no comic panels" は具体的な詳細指示に負けて、
# スマホで読めない密度の図になった。
#
# 一方、図の要素としてまともな句は 40〜100字に収まる。最初 90 にしたら
# 99字の正常な句（"a game box with two tags ..."）を3回連続で弾き、
# カードを1枚も作れなかった。**壊れた出力と正常な出力の実測値の間に置く。**
MAX_DETAIL_CHARS = 120

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
Intended use: an explanatory illustration for a technology news post, read on a phone.
Typography: every word in this image MUST be Japanese, rendered accurately and
  large enough to read on a phone. Correct Japanese glyphs matter more than
  decoration — do not invent, distort, or romanise characters. Use a clean
  hand-lettered Japanese sans-serif. Keep the total amount of text small: a
  handful of short labels and at most one caption line.
Constraints: no watermark, no logos, no UI chrome, no photorealism. Do not
  depict any real, identifiable person; use simple silhouettes if a figure is
  needed."""


class CardVisual(BaseModel):
    """1枚の概念図に描くもの。

    LLM への出力契約そのもの。フィールドを増やすと生成される
    JSON スキーマが変わる。
    """

    subject: str = Field(description="1枚で説明する概念を英語1文で（画像モデルへの指示）")
    key_details: list[str] = Field(
        min_length=2, max_length=3, description="描く視覚要素とその関係（英語の短い句）"
    )
    labels: list[str] = Field(
        default_factory=list, max_length=4, description="画像に入れる短い日本語ラベル"
    )
    caption_ja: str = Field(description="画像の下に1行で描く日本語の要点")

    @field_validator("key_details")
    @classmethod
    def _details_must_be_phrases(cls, value: list[str]) -> list[str]:
        """視覚要素は「句」であって「場面の説明」ではない。

        長い記述を渡すと、モデルはそれをパネル1枚として描く。項目が
        3〜4あればコマ割りの図になり、スタイル文の "One idea only" は
        無視される（具体的な指示のほうが強い）。実測でそうなった。
        """
        for detail in value:
            if len(detail) > MAX_DETAIL_CHARS:
                raise ValueError(
                    f"視覚要素が長すぎます（{len(detail)}字、最大 {MAX_DETAIL_CHARS}字）。"
                    f"場面の説明ではなく短い句にしてください: {detail[:40]!r}..."
                )
        return value

    @field_validator("labels")
    @classmethod
    def _labels_must_be_short(cls, value: list[str]) -> list[str]:
        """ラベルは短さだけを強制する。

        当初は「英大文字のみ」に限っていた。`gpt-image-2` の CJK 描画が
        保証されていないという理解で、崩れた日本語が入るくらいなら
        英語にしておく方が安全だと考えたため。

        **実測でその前提が誤りだと分かった**（2026-08-16）。日本語ラベルと
        日本語1行の説明を入れた画像を実際に生成したところ、どちらも
        字形が正確で、スマホでも読める大きさで描かれた。
        読み手が日本語話者なので、英語ラベルは「読めるが分からない」状態を
        作るだけだった。

        代わりに長さだけを見る。長い文をラベルに入れると図が文字で埋まり、
        タイムラインの縮小表示で読めなくなる（説明は `caption_ja` の1行に持たせる）。
        """
        for label in value:
            if len(label) > MAX_LABEL_CHARS:
                raise ValueError(
                    f"ラベルが長すぎます（{len(label)}字、最大 {MAX_LABEL_CHARS}字）: {label!r}"
                )
            if not label.strip():
                raise ValueError("空のラベルは入れられません")
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
            f"Labels: render exactly these Japanese words, {quoted}, in a hand-lettered "
            "Japanese sans-serif placed beside the element each one names."
        )
    else:
        parts.append("Labels: none.")
    # 要点の1行は画像に描く。読み手は日本語話者で、図だけでは「何が言いたい絵か」が
    # 伝わらない。実測では、この1行がある版のほうが構図も締まった（図の役割が
    # 「1行を支える絵」に定まるため、要素を盛り込みにくくなる）。
    parts.append(
        f"Caption: render this exact Japanese sentence as a single line across the bottom, "
        f'slightly larger than the labels: "{visual.caption_ja}"'
    )
    return "\n".join(parts)


class CardVisualGenerationError(Exception):
    """視覚指示の生成、またはその検証に失敗した。"""


# `CardVisual` をそのまま Structured Outputs のスキーマに使わない理由
# --------------------------------------------------------------------
# `CardVisual` にはラベルの長さチェックという pydantic バリデータが
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
- key_details: 描く視覚要素とその関係を2〜3個、英語の**短い句**で（各120文字以内）
  （例: "a funnel narrowing", "two arrows returning to a store"）
  **場面やパネルの説明を書かないこと。** 1項目に複数の要素・吹き出し・小見出しを
  詰めると、モデルはそれをコマ1枚として描き、図がコマ割りになって
  スマホでは読めなくなる。1項目 = 図の中の1要素。
- labels: 画像内に入れる短い**日本語**のラベルを0〜4個。各8文字以内
  （名札として使う。長い説明はここに入れず caption_ja に持たせる。
  読み手は日本語話者なので、英語ラベルは「読めるが分からない」だけになる）
- caption_ja: 画像の下に1行で描く日本語の要点（30文字程度。図が何を言いたいのかを
  1文で言い切る。図だけでは伝わらないので、この1行が絵の意味を決める）
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

# 検証に失敗したときに引き直す回数（初回 + フィードバック2回）。
# `PostGenerator.VALIDATION_ATTEMPTS` と同じ理由で上限を置く。テストは
# `_complete` を定数応答に固定するため、上限が無いとハングする。
_VALIDATION_ATTEMPTS = 3


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
                またはスキーマ・業務ルール（ラベルや視覚要素の長さなど）を
                満たさない場合
        """
        base_prompt = f"タイトル: {article.title}\n本文: {article.content}"
        prompt = base_prompt
        last_error: Exception | None = None

        # **検証に失敗したら、何が悪かったかを伝えて引き直す。**
        # 実測（2026-08-16）: 視覚要素を2〜3個に絞る指示に対してモデルは
        # 4個返し、検証で弾かれた。引き直しが無いとカードを1枚も作れず、
        # そのまま SINGLE に降格する（`PostGenerator` と同じ理由で、
        # 同じプロンプトを送り直しても同じ応答が返るためフィードバックが要る）。
        for _attempt in range(_VALIDATION_ATTEMPTS):
            raw = self._complete(prompt)
            try:
                payload = _CardVisualPayload.model_validate_json(raw)
                return CardVisual(**payload.model_dump())
            except (ValidationError, json.JSONDecodeError) as e:
                last_error = e
                prompt = (
                    f"{base_prompt}\n\n"
                    f"直前の応答は検証を通らなかった。次の指摘を直して出し直すこと:\n{e}"
                )

        raise CardVisualGenerationError(
            f"視覚指示が検証を満たしません: {last_error}"
        ) from last_error

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
