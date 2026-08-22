"""投稿の下書き生成。字数予算と検証の規則を確かめる。"""

import json

import pytest
from openai import BadRequestError

from src.models.news import NewsArticle, NewsCategory
from src.models.social import (
    URL_PATTERN,
    X_MAX_WEIGHTED_LENGTH,
    NewPost,
    PostKind,
    weighted_length,
)
from src.social.post_generator import (
    BUDGETS,
    GroundingError,
    PostContentFilterError,
    PostGenerationError,
    PostGenerator,
    _SinglePayload,
)
from tests.test_content_filter import ISSUE_30_BODY
from tests.test_script_content_filter import (
    FakeClient,
    FakeResponse,
    FakeResponses,
    _bad_request,
)


@pytest.fixture
def article() -> NewsArticle:
    return NewsArticle(
        id="a1",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="TechCrunch",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを 40% 削減したと発表した。"
        "開発者は同じ入力を繰り返す用途で恩恵を受ける。",
    )


@pytest.fixture
def generator(monkeypatch: pytest.MonkeyPatch) -> PostGenerator:
    gen = PostGenerator(
        endpoint="https://example.openai.azure.com",
        api_key="dummy",
        deployment="gpt-5.1",
    )
    return gen


def _reply(gen: PostGenerator, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """LLM の応答を固定する。"""
    monkeypatch.setattr(gen, "_complete", lambda *a, **k: json.dumps(payload))


def test_単発ポストにリンクが付く(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """記事のリンクはコード側が差し込む。

    モデルには URL を渡していない（渡せば捏造する）。**媒体名（`出典: 〜`）は
    書かない**——読み手が元記事に到達するのに必要なのはリンクだけで、
    媒体名は最長22カウントを食う。**リンクは全投稿に付くので `has_link` は
    常に True** で、単価は13倍の階層（$0.20）が全件に効く。
    `x_monthly_budget_usd` の既定はこれを前提に決めてある。
    """
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.SINGLE)

    assert len(posts) == 1
    assert posts[0].body.endswith("\n\nhttps://example.com/openai")
    assert "出典" not in posts[0].body
    assert "TechCrunch" not in posts[0].body
    assert posts[0].has_link is True
    assert posts[0].weighted_length <= X_MAX_WEIGHTED_LENGTH
    assert posts[0].kind is PostKind.SINGLE


def test_記事に無い数値があれば_GroundingError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI が推論コストを85%削減。" + "あ" * 95,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(GroundingError) as excinfo:
        generator.generate(article, PostKind.SINGLE)

    assert "85" in str(excinfo.value)


def test_字数が下限を割ったら_PostGenerationError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限だけを見ると下振れする。実測で145字まで縮んだ事例がある。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "推論コストが40%下がった。",
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE)


def test_practical_use_が短いと_PostGenerationError(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ニュースをなぞるだけの投稿を出さないための必須フィールド。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "便利",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE)


def test_スレッドは_投稿ごとに_position_が付く(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "OpenAI がキャッシュ方式で推論コストを40%削減した理由を説明する。" + "あ" * 70
    _reply(
        generator,
        monkeypatch,
        {
            "posts": [body, body, body],
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.THREAD)

    assert [p.position for p in posts] == [0, 1, 2]
    # リンクは先頭にだけ付ける（毎投稿に付けると字数を食う）
    assert posts[0].body.endswith("\n\nhttps://example.com/openai")
    assert "https://" not in posts[1].body


def test_裸のhttpはURLとして数えない(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """has_link と weighted_length は同じ URL_PATTERN を共有しなければならない。

    記事の元リンクを全投稿に付けるようにしたので、`has_link` はもう
    False にならない（この検査は以前 has_link=False を見ていた）。
    **共有しなければならない不変条件は残っている**: 単純な部分文字列検査で
    URL を数えると、`://` を欠く裸の "http" が1件分（23カウント）として
    数えられ、文字数の予算が実際より 23 少なく見える。
    """
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAIがキャッシュ方式で推論コストを40%削減。詳細はhttp参照。" + "あ" * 70,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.SINGLE)

    # 数えられる URL は、コード側が足した元リンクの1件だけ。
    assert len(URL_PATTERN.findall(posts[0].body)) == 1
    assert posts[0].has_link is True


def test_本文にもURLがあれば2件として数える(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本文中の URL も 23 カウントを消費する（予算計算の前提）。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAIがキャッシュ方式で推論コストを40%削減。"
            "詳細はhttps://example.com/details を参照。" + "あ" * 43,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.SINGLE)

    assert len(URL_PATTERN.findall(posts[0].body)) == 2
    assert posts[0].has_link is True


def test_最終長の検査は_文字数ではなくweighted_lengthで見る() -> None:
    """`_validate` の予算は `len()`、この検査は weighted length。**単位が違う。**

    予算の上限（125字）とリンク（23カウント固定）＋区切り（2）を足しても
    275 なので、いまの `BUDGETS` では `generate()` 経由でこの検査に
    到達できない。**それでも消さない**——`len()` で125字以内でも
    weighted では 280 を超える組み合わせが存在するため（本文に短い URL が
    複数混じると、URL は実際の文字数より多い23カウントで数えられる）。
    プロンプトは URL を書くなと指示しているが、指示は守られないことがある。

    到達できないので `generate()` からではなく検査を直接呼ぶ。
    **`BUDGETS` の上限を上げるときは、この検査が効く境界も一緒に測ること。**
    """
    over = "あ" * 93 + " ".join(["http://a.b"] * 3)  # len=125、weighted=257
    assert len(over) == 125
    assert weighted_length(over) > 125 * 2 - 25

    post = NewPost(
        article_id="a1",
        article_title="記事",
        kind=PostKind.SINGLE,
        body=f"{over}\n\nhttps://example.com/openai",
        has_link=True,
    )

    with pytest.raises(PostGenerationError):
        PostGenerator._validate_final_length([post])


def test_全ての型に予算が定義されている():
    """型を足したときに予算の定義漏れを防ぐ。"""
    assert set(BUDGETS) == set(PostKind)
    for kind, (low, high) in BUDGETS.items():
        assert 0 < low < high, kind


def test_引き直しでは同じプロンプトを送り直さない(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フィードバックが効いている唯一の仕組みなので、消えたら気付けるようにする。

    実測（記事1本で3回ずつ）: 同じプロンプトを送り直す実装では 70 / 80 / 70 字で
    下限105を割り続けた。同じ入力なら同じ長さが返るので、引き直しても結果は
    変わらず、投稿は毎回破棄されてアカウントが沈黙する。
    """
    prompts: list[str] = []

    def record(system_prompt: str, user_prompt: str, schema: object) -> str:
        prompts.append(user_prompt)
        # わざと短い本文を返し続けて、引き直しの経路を通す
        return json.dumps(
            {
                "body": "短すぎる本文。",
                "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
                "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
            }
        )

    monkeypatch.setattr(generator, "_complete", record)

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE)

    assert len(prompts) >= 2, "引き直していない"
    assert prompts[0] != prompts[1], "同じプロンプトを送り直している"
    # 前回の字数と、どちらへ何字動かすかが入っていること
    assert "7字だった" in prompts[1]
    assert "足して" in prompts[1]
    # 中央値を狙わせる（下限95・上限125なので110）。値そのものではなく
    # 「1つの値を示している」ことが要点。範囲の端は当てにくい。
    assert "110字程度" in prompts[1]


def test_長すぎたときは削る指示になる(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限125に対して127字という2字超過で3回とも外れた実測がある。

    何字削るかを伝えないと、範囲の端を狙わせることになって収束しない。
    """
    prompts: list[str] = []

    def record(system_prompt: str, user_prompt: str, schema: object) -> str:
        prompts.append(user_prompt)
        return json.dumps(
            {
                "body": "あ" * 200,
                "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
                "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
            }
        )

    monkeypatch.setattr(generator, "_complete", record)

    with pytest.raises(PostGenerationError):
        generator.generate(article, PostKind.SINGLE)

    assert "200字だった" in prompts[1]
    assert "削って" in prompts[1]


# --------------------------------------------------------------------------
# コンテンツフィルタに拒否されたとき（issue #30 の X 側）
# --------------------------------------------------------------------------
#
# ヘルパーは台本側のテストから借りる。同じ形の stub を書き写すと、
# 応答の形が変わったときに片方だけ直る。


def test_入力が拒否されたら専用の型になる(
    generator: PostGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`BadRequestError(content_filter)` を `PostContentFilterError` にすること。

    素の `PostGenerationError` のままだと `plan_daily_posts` の汎用の
    `except Exception` に入り、記事に印が付かない（＝翌日も同じ記事で
    同じ拒否を踏み、毎日1回ぶんの API 呼び出しを捨てる）。
    """
    responses = FakeResponses(error=_bad_request(ISSUE_30_BODY))
    monkeypatch.setattr(generator, "client", FakeClient(responses))

    with pytest.raises(PostContentFilterError) as caught:
        generator._complete("システム", "ユーザー", _SinglePayload)

    assert "コンテンツフィルタ" in str(caught.value)
    assert "sexual" in str(caught.value)
    # 拒否は引き直しても変わらない。tenacity の許可リストにも入っていない
    assert responses.calls == 1


def test_フィルタ以外の400はそのまま伝播する(
    generator: PostGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直せる失敗を恒久的な拒否と混ぜないこと。

    誤ると、デプロイ名の誤りのような直せる失敗で記事が対象外になる。
    """
    body = {"error": {"code": "DeploymentNotFound", "message": "does not exist"}}
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(error=_bad_request(body))))

    with pytest.raises(BadRequestError):
        generator._complete("システム", "ユーザー", _SinglePayload)


def test_出力が拒否されたら専用の型になる(
    generator: PostGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出力側の扉も閉じていること。

    閉じないと、同じ恒久的な失敗が別の経路から入って毎日の再ドラフトが残る。
    """
    response = FakeResponse(reason="content_filter")
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(response=response)))

    with pytest.raises(PostContentFilterError):
        generator._complete("システム", "ユーザー", _SinglePayload)


def test_打ち切りはコンテンツフィルタとして扱わない(
    generator: PostGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`max_output_tokens` の打ち切りは一時的な失敗として扱うこと。"""
    response = FakeResponse(reason="max_output_tokens")
    monkeypatch.setattr(generator, "client", FakeClient(FakeResponses(response=response)))

    with pytest.raises(PostGenerationError) as caught:
        generator._complete("システム", "ユーザー", _SinglePayload)

    assert not isinstance(caught.value, PostContentFilterError)
