"""記事から X 投稿の下書きを作る。

なぜ ScriptGenerator に相乗りしないか
------------------------------------
台本は「音声のタイミング同期」という制約を背負っている
（segment_narrations と image_prompts と text_overlays の要素数が
一致しなければ動画合成が失敗する）。投稿にその制約は無く、代わりに
weighted length という別の制約がある。混ぜると両方のスキーマに
相手の都合が入る。

独自解説を必須フィールドで強制する理由
--------------------------------------
ニュースをなぞるだけの投稿は伸びず、引用元の価値を横取りするだけになる。
Structured Outputs では必須フィールドをモデルが省略できないので、
プロンプトでお願いするより保証が強い（台本の technical_insight /
practical_impact と同じ理屈）。
"""

from __future__ import annotations

import json

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models.news import NewsArticle
from src.models.social import (
    URL_PATTERN,
    X_MAX_WEIGHTED_LENGTH,
    NewPost,
    PostKind,
    weighted_length,
)
from src.utils.content_filter import category_suffix, is_content_filter_error
from src.utils.grounding import ungrounded_numbers

# 型ごとの本文の字数予算（日本語の文字数、下限と上限）。
#
# **上限だけでなく下限も持つ。** 上限だけを指示すると下振れし、
# 実測では145字まで縮んで文の断片になった（台本での経験）。
#
# 上限が140より小さいのは、出典表記とハッシュタグを足す余地を残すため。
# weighted length では日本語1字が2カウントなので、140字で上限ぴったり。
#
# **下限は実測で決めた**（2026-08-17）。当初 105 にしていたが根拠が無く、
# 弾きたいもの（見出しの言い換え: 69 / 70 / 79 / 80字）と通したいもの
# （実質のある本文: 96 / 99 / 109 / 112 / 118 / 124字）の間に無かった。
# 105 のままだと 96・99 を弾き、実際に1日3件の予定が1件になった
# （引き直しを3回しても収束しない。帯が狭いほど当てにくい）。
# 95 なら見出しの言い換えは余裕を持って除外できる。
#
# **上限は記事の元リンクを足したあとの実測で決めた**（2026-08-22）。
# **上限 125 は「本文 + リンクだけ」を前提にした値。**
# 先頭の投稿は 本文 / 空行 / URL の形で、本文以外は 25 カウント固定
# （URL は t.co 短縮で23カウント、区切りが2）。本文125字（250カウント）で
# 275 なので5カウント余る。**媒体名を書いていた頃は 110 が限界だった**
# （`出典: {媒体名}` が最長22カウントで、予約が 54 に膨らんでいた）。
# 媒体名を投稿から外したぶんが本文に戻っている。書き戻すなら上限を
# 110 に下げる必要がある。

BUDGETS: dict[PostKind, tuple[int, int]] = {
    PostKind.SINGLE: (95, 125),
    # THREAD も先頭の投稿だけがリンクを背負うので、上限は SINGLE と同じ
    # 制約を受ける。130 のままでは先頭が 285 で必ず溢れる
    # （未配線なので露見していなかった。`KIND_ROTATION` を参照）。
    PostKind.THREAD: (100, 125),  # 1投稿あたり
    PostKind.CARD: (60, 90),
    PostKind.PROMO: (70, 100),
}

# スレッドの投稿数。
THREAD_MIN_POSTS = 3
THREAD_MAX_POSTS = 5

# 独自解説の最低文字数。言語非依存にしている
# （スキーマが language を持たないため、言語別の閾値を選べない）。
MIN_INSIGHT_CHARS = 30

# プロンプト内で予算の指示を差し込む位置。
BUDGET_TOKEN = "<<BUDGET>>"


class PostGenerationError(Exception):
    """下書きの生成または検証に失敗した。"""


class GroundingError(PostGenerationError):
    """記事本文に根拠の無い数値が含まれていた。"""


class PostContentFilterError(PostGenerationError):
    """Azure のコンテンツフィルタが記事の題材を拒否した。

    **引き直しても結果は変わらない。** 拒否されるのは入力（記事のタイトルと
    本文）なので、プロンプトを直しても同じ記事なら同じ 400 が返る。
    呼び出し元（`plan_daily_posts`）はこの型を見て記事を自動生成の対象外に
    し、次の候補へ進む。台本側の `ScriptContentFilterError` と同じ役割。
    """


class _SinglePayload(BaseModel):
    """SINGLE / CARD / PROMO の出力スキーマ。

    Structured Outputs にこのモデルをそのまま渡すことで、
    `practical_use` / `why_now` をモデルが省略できなくなる
    （JSON モードではキー自体を返さない選択肢が残ってしまい、
    「プロンプトでお願いする」の域を出ない）。
    """

    body: str
    practical_use: str
    why_now: str


class _ThreadPayload(BaseModel):
    """THREAD の出力スキーマ。本文が複数件になる点だけが単発と異なる。"""

    posts: list[str]
    practical_use: str
    why_now: str


# 各プロンプトが必ず含む禁則事項。定数化して4種類に重複させない。
#
# 「何を含めるか」で書いているのは実測の結果。字数だけを指示した版は
# 記事タイトルの言い換え1文（70〜80字）で返ってきて、下限105字を割り続けた。
# 文数（「2文以上」など）で縛る書き方も試したが、カードの予算（60〜90字）とは
# 衝突する。含めるべき要素を挙げるのが、型ごとの予算と喧嘩しない書き方だった。
_COMMON_RULES_JA = """- **投稿は必ず日本語で書くこと。** 記事が英語（海外メディア、企業の
  公式ブログ、arXiv の論文）でも、投稿は日本語にする。読み手は日本語話者で、
  情報源の半分以上が英語のフィードになっている（`src/news/feeds.py`）
- 各投稿は単独で文として言い切ること（断片にしない）
- 本文には「何が起きたか」と「誰がどの作業でどう使えるか」の両方を含めること。
  記事タイトルの言い換えで終わらせないこと（引用元の要約をなぞるだけの投稿は
  伸びないうえ、元記事の価値を横取りするだけになる）
- 記事本文に無い数値・固有名詞を書かないこと
- 記事のリンクは本文に書かないこと（こちらで付ける）
- **ハッシュタグを書かないこと。** タグは1つも付けない"""


SYSTEM_PROMPT_SINGLE = f"""<role>
あなたはX（旧Twitter）向けにニュース解説の投稿文を書くライターです。
</role>

<task>
与えられた記事から、単発の投稿を1件、JSON形式で作成してください。
</task>

<content_rules>
{_COMMON_RULES_JA}
- URL を書かないこと（記事の元リンクはこちらで末尾に付ける）
- body は<<BUDGET>>文字の範囲に収めること（下限を割ると文が断片化する、
  上限を超えるとリンクを追加する余地が無くなる）
- practical_use: 実務でどう役立つかの独自解説（{MIN_INSIGHT_CHARS}文字以上）
- why_now: なぜ今このニュースが重要かの独自解説（{MIN_INSIGHT_CHARS}文字以上）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{{
    "body": "投稿本文",
    "practical_use": "実務での使い道の解説",
    "why_now": "なぜ今重要かの解説"
}}
</output_format>"""

SYSTEM_PROMPT_THREAD = f"""<role>
あなたはX（旧Twitter）向けにニュース解説のスレッド（連続投稿）を書くライターです。
</role>

<task>
与えられた記事から、{THREAD_MIN_POSTS}〜{THREAD_MAX_POSTS}件の連続する投稿をJSON形式で作成してください。
</task>

<content_rules>
{_COMMON_RULES_JA}
- URL を書かないこと（記事の元リンクはこちらで末尾に付ける）
- posts は{THREAD_MIN_POSTS}〜{THREAD_MAX_POSTS}件。各要素は<<BUDGET>>文字の範囲に収めること
  （下限を割ると文が断片化する、上限を超えるとリンクの余地が無くなる）
- 1件目で結論、2件目以降で仕組みや根拠を展開する構成にすること
- practical_use: 実務でどう役立つかの独自解説（{MIN_INSIGHT_CHARS}文字以上）
- why_now: なぜ今このニュースが重要かの独自解説（{MIN_INSIGHT_CHARS}文字以上）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{{
    "posts": ["1件目の本文", "2件目の本文", "3件目の本文"],
    "practical_use": "実務での使い道の解説",
    "why_now": "なぜ今重要かの解説"
}}
</output_format>"""

SYSTEM_PROMPT_CARD = f"""<role>
あなたはX（旧Twitter）向けに、画像カードに添える短い投稿文を書くライターです。
</role>

<task>
与えられた記事から、画像カードに添える短い投稿を1件、JSON形式で作成してください。
画像側に詳細を語らせるので、本文はキャプション相当に短くします。
</task>

<content_rules>
{_COMMON_RULES_JA}
- URL を書かないこと（記事の元リンクはこちらで末尾に付ける）
- body は<<BUDGET>>文字の範囲に収めること（下限を割ると文が断片化する、
  上限を超えるとリンクを追加する余地が無くなる）
- practical_use: 実務でどう役立つかの独自解説（{MIN_INSIGHT_CHARS}文字以上）
- why_now: なぜ今このニュースが重要かの独自解説（{MIN_INSIGHT_CHARS}文字以上）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{{
    "body": "画像カードに添える短い本文",
    "practical_use": "実務での使い道の解説",
    "why_now": "なぜ今重要かの解説"
}}
</output_format>"""

SYSTEM_PROMPT_PROMO = f"""<role>
あなたはX（旧Twitter）向けに、動画への誘導を目的とした投稿文を書くライターです。
</role>

<task>
与えられた記事から、動画への誘導を意図した投稿を1件、JSON形式で作成してください。
</task>

<content_rules>
{_COMMON_RULES_JA}
- body は<<BUDGET>>文字の範囲に収めること（下限を割ると文が断片化する、
  上限を超えるとリンクを追加する余地が無くなる）
- URL を書かないこと（記事の元リンクはこちらで末尾に付ける）。
  「詳細はこちら」等の案内文言も不要
- practical_use: 実務でどう役立つかの独自解説（{MIN_INSIGHT_CHARS}文字以上）
- why_now: なぜ今このニュースが重要かの独自解説（{MIN_INSIGHT_CHARS}文字以上）
</content_rules>

<output_format>
以下のJSON形式のみを出力してください。JSON以外のテキストは含めないでください。

{{
    "body": "動画への誘導を意図した投稿本文",
    "practical_use": "実務での使い道の解説",
    "why_now": "なぜ今重要かの解説"
}}
</output_format>"""

_SYSTEM_PROMPTS: dict[PostKind, str] = {
    PostKind.SINGLE: SYSTEM_PROMPT_SINGLE,
    PostKind.THREAD: SYSTEM_PROMPT_THREAD,
    PostKind.CARD: SYSTEM_PROMPT_CARD,
    PostKind.PROMO: SYSTEM_PROMPT_PROMO,
}

# API 呼び出しの通信エラー・レートリミット・5xx に対する試行回数。
API_RETRIES = 4

# スキーマ違反や予算超過で引き直す回数。
#
# 3回にしている理由は実測。同じプロンプトを送り直す実装では
# 70 / 80 / 70 字（下限105）と外れ続け、投稿が毎回破棄されていた。
# 再生成時に前回の字数と直す方向を伝えると1回で予算内に入ったが、
# 1回目で必ず外れる型（構成の指示が効いて長めに出る）もあるため、
# 「初回 + フィードバック2回」の余裕を持たせる。
#
# 無限にはしない。テストは `_complete` を定数応答に固定するので、
# 上限が無いとそのままハングする。
VALIDATION_ATTEMPTS = 3


class PostGenerator:
    """Azure OpenAI で X 投稿の下書きを生成するクラス。

    Attributes:
        client: OpenAI APIクライアント
        model: 使用するモデル（デプロイメント名）
    """

    def __init__(self, endpoint: str, api_key: str, deployment: str):
        """PostGeneratorを初期化する。

        Args:
            endpoint: Azure OpenAI endpoint URL
            api_key: Azure OpenAI API key
            deployment: 投稿生成モデルのデプロイ名

        Raises:
            ValueError: endpoint / api_key / deployment が空の場合
        """
        if not endpoint:
            raise ValueError("Azure OpenAI endpoint が指定されていません")
        if not api_key:
            raise ValueError("Azure OpenAI API key が指定されていません")
        if not deployment:
            raise ValueError("投稿生成モデルのデプロイ名が指定されていません")

        # Azure OpenAI v1 エンドポイント形式（ScriptGenerator と同じ組み立て）
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
        article: NewsArticle,
        kind: PostKind,
        caption: str | None = None,
    ) -> list[NewPost]:
        """記事から投稿の下書きを生成する。

        Args:
            article: 元記事
            kind: 投稿の型
            caption: 画像カード用に別途生成済みの日本語キャプション文。
                指定があればユーザープロンプトのヒントに追加する
                （画像カード生成タスクが本文生成へ渡す用途。この引数自体は
                このタスクでは未使用でも、後続タスクの変更範囲を広げない
                ためにここで受け取る）

        Returns:
            list[NewPost]: 積む準備ができた投稿。単発は1件、スレッドは複数件

        Raises:
            GroundingError: 記事本文に無い数値が含まれていた
            PostGenerationError: 字数または独自解説が要件を満たさなかった
        """
        system_prompt = self._build_system_prompt(kind)
        user_prompt = self._build_user_prompt(article, caption)
        schema = _ThreadPayload if kind is PostKind.THREAD else _SinglePayload
        # グラウンディングの照合対象はタイトル＋本文。見出しだけにある数値も
        # 根拠として認める（記事に実在する情報なので捏造ではない）。
        # **区切りを挟む。** 単純連結だと、タイトル末尾の数字と本文冒頭の
        # 数字が1つのトークンに融合しうる。グラウンディングは捏造対策の
        # 最後の防衛線なので、実在しない数値が「融合した数字」として
        # 根拠ありと誤判定される事故を防ぐ。
        source_text = f"{article.title}\n{article.content}"

        last_error: PostGenerationError | None = None
        attempt_prompt = user_prompt
        for _attempt in range(VALIDATION_ATTEMPTS):
            raw = self._complete(system_prompt, attempt_prompt, schema)
            try:
                bodies, insights = self._parse_payload(kind, raw)
                for body in bodies:
                    self._validate(body, insights, kind, source_text)
                posts = self._assemble(article, kind, bodies)
                self._validate_final_length(posts)
            except PostGenerationError as e:
                last_error = e
                # **同じプロンプトを送り直さない。** 実測（記事1本で3回ずつ）:
                # 現状のプロンプトは 70 / 80 / 70 字で下限105を割り続け、
                # 構成の指示を足すと 162 / 163 / 140 字で今度は上限を超えた。
                # どちらも同じ入力なら同じ長さが返るので、再生成しても
                # 結果は変わらず、投稿は毎回破棄されてアカウントが沈黙する。
                # 前回の実測値とどちらへ直すかを伝えると 118 / 109 / 112 字で
                # 3回とも予算内に入った。効いているのはこのフィードバック。
                attempt_prompt = self._with_length_feedback(user_prompt, bodies, kind)
                continue
            return posts

        assert last_error is not None  # ループを抜けるのは例外時のみ
        raise last_error

    def _with_length_feedback(self, user_prompt: str, bodies: list[str], kind: PostKind) -> str:
        """前回の本文の長さと直す方向をユーザープロンプトに足す。

        文字数そのものを守らせるのは LLM に不得手な仕事なので、
        「何字だったか」と「増やすのか減らすのか」を渡して寄せていく。

        Args:
            user_prompt: 元のユーザープロンプト
            bodies: 前回生成された本文（解析に失敗していれば空）
            kind: 投稿の型

        Returns:
            str: フィードバックを足したプロンプト。長さが分からなければ元のまま
        """
        if not bodies:
            return user_prompt

        low, high = BUDGETS[kind]
        # スレッドは投稿ごとに予算を見るので、外れているものを代表に使う。
        length = next((len(b) for b in bodies if not low <= len(b) <= high), len(bodies[0]))

        # **不足・超過の字数と、狙う1つの値まで伝える。**
        # 「長すぎる。削る」だけを返した版では、上限125に対して127字という
        # 2字超過で3回とも外れた。範囲の端を狙わせるのは難しいので、
        # 何字動かすかと中央値を渡す。
        target = (low + high) // 2
        if length < low:
            direction = f"あと{low - length}字以上足して、{target}字程度を狙う"
        else:
            direction = f"あと{length - high}字以上削って、{target}字程度に収める"
        return (
            f"{user_prompt}\n\n"
            f"直前の生成では本文が{length}字だった。{low}〜{high}字に収める必要がある。"
            f"{direction}こと。"
        )

    def _build_system_prompt(self, kind: PostKind) -> str:
        """型に応じたシステムプロンプトを組む（予算を文字列で埋め込む）。

        Args:
            kind: 投稿の型

        Returns:
            str: システムプロンプト
        """
        low, high = BUDGETS[kind]
        return _SYSTEM_PROMPTS[kind].replace(BUDGET_TOKEN, f"{low}〜{high}")

    @staticmethod
    def _build_user_prompt(article: NewsArticle, caption: str | None) -> str:
        """ユーザープロンプトを組む。

        記事の title と content だけを渡す。`url` と `source` は渡さない
        （モデルは URL を知らないので渡せば捏造する。出典名はコード側が
        `_assemble` で追記する）。これは ScriptGenerator と同じ判断。

        Args:
            article: 元記事
            caption: 画像カード用の日本語キャプション（あれば追加のヒント）

        Returns:
            str: ユーザープロンプト
        """
        prompt = f"タイトル: {article.title}\n本文: {article.content}"
        if caption:
            # **同じことを繰り返させない。** この文は既に画像の中に描かれている。
            # 「参考」として渡すだけだと本文が言い換えただけの内容になり、
            # 画像と本文で同じ主張が2回出る（読み手には情報が増えない）。
            prompt += (
                f"\n\n添える画像には次の文がすでに描かれている: {caption}\n"
                "本文でこの文を繰り返さないこと。画像が言っていないこと"
                "（誰がどの作業で使えるか、なぜ今か）を本文で補うこと。"
            )
        return prompt

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        ),
        stop=stop_after_attempt(API_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _complete(self, system_prompt: str, user_prompt: str, schema: type[BaseModel]) -> str:
        """1回の補完を Structured Outputs で呼び、JSON 文字列を返す。

        `response_format={"type": "json_object"}`（JSON モード）は使わない。
        JSON モードはパースできる JSON であることしか保証せず、スキーマの
        強制が無いため `practical_use` / `why_now` をモデルが省略できてしまう。
        それでは「必須フィールドで独自解説を強制する」という設計の前提が崩れ、
        プロンプトでお願いするのと変わらなくなる（ScriptGenerator が
        `responses.parse` を使う理由と同じ）。

        **戻り値を文字列にしているのはテストの都合。** `_complete` は
        テストが差し替える差し込み点で、`_parse_payload` が JSON 文字列を
        受け取る形を前提にしている。ここを Pydantic オブジェクトのまま
        返す方が自然に見えるが、そうすると呼び出し側のテストが
        `json.dumps(payload)` を返す差し替えと噛み合わなくなる。
        「簡潔にする」つもりでオブジェクトを返す変更をすると
        `tests/test_post_generator.py` が壊れるので、変えないこと。

        Args:
            system_prompt: システムプロンプト
            user_prompt: ユーザープロンプト
            schema: 出力スキーマ（`_SinglePayload` または `_ThreadPayload`）

        Returns:
            str: 検証済みオブジェクトを JSON 文字列化したもの

        Raises:
            PostContentFilterError: コンテンツフィルタに拒否された場合
                （入力・出力のどちらでも）
            PostGenerationError: モデルが出力を拒否した場合
        """
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=system_prompt,
                input=user_prompt,
                text_format=schema,
            )
        except BadRequestError as e:
            # **入力側の扉。** 記事のタイトルと本文が拒否されると 400 で返る。
            # tenacity の retry は許可リスト方式で BadRequestError を含まない
            # ので、ここに来る時点で再試行は済んでいない（してもいけない）。
            if is_content_filter_error(e):
                raise PostContentFilterError(
                    f"記事の題材が Azure OpenAI のコンテンツフィルタに"
                    f"拒否されました{category_suffix(e)}"
                ) from e
            raise

        parsed = response.output_parsed
        if parsed is None:
            # **出力側の扉。** 台本側と同じ理由で、理由がコンテンツフィルタなら
            # 恒久的な失敗として扱う（閉じないと同じ記事が毎日再ドラフトされる）。
            reason = getattr(response.incomplete_details, "reason", None)
            if reason == "content_filter":
                raise PostContentFilterError(
                    "生成された投稿が Azure OpenAI のコンテンツフィルタに拒否されました"
                )
            raise PostGenerationError(
                f"モデルが投稿を出力しませんでした "
                f"(status={response.status!r}, incomplete={response.incomplete_details!r})"
            )
        return parsed.model_dump_json()

    @staticmethod
    def _parse_payload(kind: PostKind, raw: str) -> tuple[list[str], dict[str, str]]:
        """モデルの応答 JSON を、本文のリストと独自解説に分解する。

        単発の型（SINGLE/CARD/PROMO）は "body" キー、THREAD は
        要素数が可変の "posts" キーという2つの形を持つ。

        Args:
            kind: 投稿の型
            raw: モデルが返した JSON 文字列

        Returns:
            tuple[list[str], dict[str, str]]: (本文のリスト, 独自解説)

        Raises:
            PostGenerationError: JSON が解釈できない、または必須キーが無い場合
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PostGenerationError(f"モデルの応答が JSON として解釈できません: {e}") from e

        insights = {
            "practical_use": str(data.get("practical_use", "")),
            "why_now": str(data.get("why_now", "")),
        }

        if kind is PostKind.THREAD:
            posts = data.get("posts")
            count = len(posts) if isinstance(posts, list) else 0
            if not isinstance(posts, list) or not (THREAD_MIN_POSTS <= count <= THREAD_MAX_POSTS):
                raise PostGenerationError(
                    f"スレッドの投稿数が範囲外です（{count}件、"
                    f"期待 {THREAD_MIN_POSTS}〜{THREAD_MAX_POSTS}件）"
                )
            bodies = [str(p) for p in posts]
        else:
            body = data.get("body")
            if not body:
                raise PostGenerationError("モデルの応答に body がありません")
            bodies = [str(body)]

        return bodies, insights

    def _validate(
        self, body: str, insights: dict[str, str], kind: PostKind, source_text: str
    ) -> None:
        """字数・独自解説・数値の根拠を検証する。

        Raises:
            GroundingError: 記事本文に無い数値があった
            PostGenerationError: 字数または独自解説が要件を満たさない
        """
        low, high = BUDGETS[kind]
        length = len(body.strip())
        if not low <= length <= high:
            raise PostGenerationError(
                f"{kind} の本文が予算外です（{length}字、期待 {low}〜{high}字）"
            )

        weighted = weighted_length(body)
        if weighted > X_MAX_WEIGHTED_LENGTH:
            raise PostGenerationError(
                f"weighted length が上限を超えています（{weighted}/{X_MAX_WEIGHTED_LENGTH}）"
            )

        for name in ("practical_use", "why_now"):
            value = insights.get(name, "").strip()
            if len(value) < MIN_INSIGHT_CHARS:
                raise PostGenerationError(
                    f"{name} が短すぎます（{len(value)}字、最低 {MIN_INSIGHT_CHARS}字）"
                )

        ungrounded = ungrounded_numbers(body, source_text)
        if ungrounded:
            raise GroundingError(f"記事本文に根拠の無い数値が含まれています: {sorted(ungrounded)}")

    @staticmethod
    def _assemble(article: NewsArticle, kind: PostKind, bodies: list[str]) -> list[NewPost]:
        """検証済みの本文から `NewPost` のリストを組み立てる。

        記事のリンクは先頭（position 0）にだけ付ける。スレッドの全件に
        付けると字数を食うだけで、2件目以降は1件目からの続きとして
        読まれるため重複は不要。

        **`出典: {媒体名}` は書かない。** 以前は媒体名とリンクを並べていたが、
        媒体名は最長22カウントを食う一方、読み手が元記事に到達するのに
        必要なのはリンクだけ。書き戻すなら `BUDGETS` の上限を 110 に下げる。

        **ハッシュタグは付けない**（このモジュールに付ける経路が無い）。
        理由は CLAUDE.md「ハッシュタグは付けない」を参照。

        Args:
            article: 元記事
            kind: 投稿の型
            bodies: 検証済みの本文（1件、またはスレッドの複数件）

        Returns:
            list[NewPost]: 積む準備ができた投稿
        """
        posts = []
        for position, body in enumerate(bodies):
            final_body = body.strip()
            if position == 0:
                # `article.url` はモデルに渡していない（渡せば捏造する）。
                # ここがリンクの唯一の出所。
                final_body = "\n\n".join([final_body, article.url])
            posts.append(
                NewPost(
                    article_id=article.id,
                    article_title=article.title,
                    kind=kind,
                    body=final_body,
                    # `weighted_length`（src/models/social.py）と同じ
                    # `URL_PATTERN` を使う。`has_link` はコスト単価
                    # （$0.015 と $0.20、13倍差）を選ぶフラグなので、
                    # ここでの「リンクを含む」の定義が weighted_length の
                    # 定義とずれると、文字数の予算計算と実際の課金階層が
                    # 食い違う。単純な "http" in ... の部分文字列検査では、
                    # "://" を欠く裸の "http" がリンク扱いされてしまう。
                    has_link=URL_PATTERN.search(final_body) is not None,
                    position=position,
                )
            )
        return posts

    @staticmethod
    def _validate_final_length(posts: list[NewPost]) -> None:
        """リンク付加後の最終本文が上限を超えていないか検証する。

        `_validate` は生成された本文だけを見る（リンクを付ける前）。
        リンクは t.co 短縮で23カウント固定なので、`BUDGETS` の上限を
        守れていれば理論上ここは通る（125字 + 区切り2 + URL23 = 275）。

        **それでも残してある。単位が違う。** 予算の検査は `len()`
        （Python の文字数）、こちらは weighted length（CJK が2カウント）。
        本文に全角記号が混じると `len()` は125以内なのに weighted は
        250を超えうる。ここで検出しないと、キューには「健全」に見える行が
        積まれ、投稿予定時刻になって初めて X API に拒否される。

        切り詰めては直さない。本文を途中で削れば文の断片ができる
        （このプロジェクトが台本で既に「断片は不可」と決めている）。
        検出したら引き直す。

        Args:
            posts: `_assemble` が組み立てた投稿

        Raises:
            PostGenerationError: いずれかの投稿が上限を超えている場合
        """
        for post in posts:
            weighted = post.weighted_length
            if weighted > X_MAX_WEIGHTED_LENGTH:
                raise PostGenerationError(
                    f"リンクを含めた最終本文が weighted length の"
                    f"上限を超えています（{weighted}/{X_MAX_WEIGHTED_LENGTH}）"
                )
