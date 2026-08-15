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
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.models.news import NewsArticle
from src.models.social import X_MAX_WEIGHTED_LENGTH, NewPost, PostKind, weighted_length
from src.social.grounding import ungrounded_numbers

# 型ごとの本文の字数予算（日本語の文字数、下限と上限）。
#
# **上限だけでなく下限も持つ。** 上限だけを指示すると下振れし、
# 実測では145字まで縮んで文の断片になった（台本での経験）。
#
# 上限が140より小さいのは、出典表記とハッシュタグを足す余地を残すため。
# weighted length では日本語1字が2カウントなので、140字で上限ぴったり。
BUDGETS: dict[PostKind, tuple[int, int]] = {
    PostKind.SINGLE: (105, 125),
    PostKind.THREAD: (100, 130),  # 1投稿あたり
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


# 各プロンプトが必ず含む禁則事項。定数化して4種類に重複させない。
_COMMON_RULES_JA = """- 各投稿は単独で文として言い切ること（断片にしない）
- 記事本文に無い数値・固有名詞を書かないこと
- 出典表記とハッシュタグはこちらで付けるので書かないこと"""

SYSTEM_PROMPT_SINGLE = f"""<role>
あなたはX（旧Twitter）向けにニュース解説の投稿文を書くライターです。
</role>

<task>
与えられた記事から、単発の投稿を1件、JSON形式で作成してください。
</task>

<content_rules>
{_COMMON_RULES_JA}
- URL を書かないこと（別途システムが必要に応じて付加する）
- body は<<BUDGET>>文字の範囲に収めること（下限を割ると文が断片化する、
  上限を超えると出典表記とハッシュタグを追加する余地が無くなる）
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
- URL を書かないこと（別途システムが必要に応じて付加する）
- posts は{THREAD_MIN_POSTS}〜{THREAD_MAX_POSTS}件。各要素は<<BUDGET>>文字の範囲に収めること
  （下限を割ると文が断片化する、上限を超えると出典表記の余地が無くなる）
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
- URL を書かないこと（別途システムが必要に応じて付加する）
- body は<<BUDGET>>文字の範囲に収めること（下限を割ると文が断片化する、
  上限を超えると出典表記とハッシュタグを追加する余地が無くなる）
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
  上限を超えると出典表記とハッシュタグを追加する余地が無くなる）
- リンクの挿入は別途システムが行うので、本文中にリンクやその案内文言
  （「詳細はこちら」等）を書く必要はない
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

# スキーマ違反や予算超過で引き直す回数。ブロックしすぎないよう1回だけ
# （テストは _complete を定数応答に固定するため、無限に近いループは
# そのままハングする）。
VALIDATION_ATTEMPTS = 2


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
        hashtags: list[str],
        caption: str | None = None,
    ) -> list[NewPost]:
        """記事から投稿の下書きを生成する。

        Args:
            article: 元記事
            kind: 投稿の型
            hashtags: 先頭の投稿に付けるハッシュタグ。モデルには作らせない
                （無関係なタグはスパム判定を受ける）
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
        # グラウンディングの照合対象はタイトル＋本文。見出しだけにある数値も
        # 根拠として認める（記事に実在する情報なので捏造ではない）。
        source_text = article.title + article.content

        last_error: PostGenerationError | None = None
        for _attempt in range(VALIDATION_ATTEMPTS):
            raw = self._complete(system_prompt, user_prompt)
            try:
                bodies, insights = self._parse_payload(kind, raw)
                for body in bodies:
                    self._validate(body, insights, kind, source_text)
            except PostGenerationError as e:
                last_error = e
                continue
            return self._assemble(article, kind, bodies, hashtags)

        assert last_error is not None  # ループを抜けるのは例外時のみ
        raise last_error

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
            prompt += f"\n\n参考（既に確定しているキャプション文）: {caption}"
        return prompt

    @retry(
        retry=retry_if_exception_type(
            (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
        ),
        stop=stop_after_attempt(API_RETRIES),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    def _complete(self, system_prompt: str, user_prompt: str) -> str:
        """1回の補完を呼び、応答の生の JSON 文字列を返す。

        テストが `PostGenerator._complete` を差し替えて検証だけを見るため、
        この名前・このシグネチャで単一のメソッドとして切り出している
        （他の処理を混ぜない）。

        Args:
            system_prompt: システムプロンプト
            user_prompt: ユーザープロンプト

        Returns:
            str: モデルが返した JSON 文字列

        Raises:
            PostGenerationError: モデルが応答本文を返さなかった場合
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise PostGenerationError("モデルが応答を返しませんでした")
        return content

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
    def _assemble(
        article: NewsArticle, kind: PostKind, bodies: list[str], hashtags: list[str]
    ) -> list[NewPost]:
        """検証済みの本文から `NewPost` のリストを組み立てる。

        出典表記とハッシュタグは先頭（position 0）にだけ付ける。
        スレッドの全件に付けると字数を食うだけで、2件目以降は
        1件目からの続きとして読まれるため出典の重複は不要。

        Args:
            article: 元記事
            kind: 投稿の型
            bodies: 検証済みの本文（1件、またはスレッドの複数件）
            hashtags: 先頭の投稿に付けるハッシュタグ

        Returns:
            list[NewPost]: 積む準備ができた投稿
        """
        posts = []
        for position, body in enumerate(bodies):
            final_body = body.strip()
            if position == 0:
                extras = [f"出典: {article.source}"]
                if hashtags:
                    extras.append(" ".join(hashtags))
                final_body = "\n\n".join([final_body, *extras])
            posts.append(
                NewPost(
                    article_id=article.id,
                    article_title=article.title,
                    kind=kind,
                    body=final_body,
                    has_link="http" in final_body,
                    position=position,
                )
            )
        return posts
