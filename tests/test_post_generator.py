"""投稿の下書き生成。字数予算と検証の規則を確かめる。"""

import json

import pytest

from src.models.news import NewsArticle, NewsCategory
from src.models.social import PostKind
from src.social.post_generator import (
    BUDGETS,
    GroundingError,
    PostGenerationError,
    PostGenerator,
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


def test_単発ポストに_出典が付く(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """出典の媒体名はコード側が差し込む（モデルに渡すと捏造する）。"""
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    posts = generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])

    assert len(posts) == 1
    assert "出典: TechCrunch" in posts[0].body
    assert posts[0].has_link is False
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
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])

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
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])


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
        generator.generate(article, PostKind.SINGLE, hashtags=["#AI"])


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

    posts = generator.generate(article, PostKind.THREAD, hashtags=["#AI"])

    assert [p.position for p in posts] == [0, 1, 2]
    # 出典は先頭にだけ付ける（毎投稿に付けると字数を食う）
    assert "出典: TechCrunch" in posts[0].body
    assert "出典" not in posts[1].body


def test_httpだけを含み_を欠く本文は_has_link_がFalse(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """has_link と weighted_length は同じ URL_PATTERN を共有しなければならない。

    has_link はコスト単価（$0.015 と $0.20、13倍差）を選ぶフラグ。
    weighted_length が数えない「裸の http」を has_link が数えると、
    リンク無し扱いで課金されるべき投稿がリンクありの単価になる
    （このケースは逆方向: 単純な部分文字列検査だとリンクあり判定に
    なってしまうが、weighted_length は URL として数えない）。
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

    posts = generator.generate(article, PostKind.SINGLE, hashtags=[])

    assert posts[0].has_link is False


def test_実在するURLを含む本文は_has_link_がTrue(
    generator: PostGenerator, article: NewsArticle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`https://...` の形を持つ本文は has_link=True になること。"""
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

    posts = generator.generate(article, PostKind.SINGLE, hashtags=[])

    assert posts[0].has_link is True


def test_出典とハッシュタグを足すと上限を超えるなら_PostGenerationError(
    generator: PostGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本文だけなら予算内でも、出典名が長く・タグが多いと280を超えうる。

    キューに「健全」に見える行を積んでしまい、投稿予定時刻になって
    初めて X API に拒否される事故を防ぐ。切り詰めては直さない
    （出典や文の断片を作ることになるため）ので、引き直しの対象にする。
    """
    long_source_article = NewsArticle(
        id="a2",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="グローバル・テクノロジー・ニュース・メディア",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを 40% 削減したと発表した。"
        "開発者は同じ入力を繰り返す用途で恩恵を受ける。",
    )
    _reply(
        generator,
        monkeypatch,
        {
            "body": "OpenAI がキャッシュ方式で推論コストを40%削減。" + "あ" * 90,
            "practical_use": "同じ入力を繰り返すバッチ処理を持つ開発者が、推論費用をそのまま下げられる。",
            "why_now": "推論需要が急増し、コスト構造が事業継続の制約として表面化してきたため。",
        },
    )

    with pytest.raises(PostGenerationError):
        generator.generate(
            long_source_article,
            PostKind.SINGLE,
            hashtags=["#人工知能ニュース", "#生成AIウォッチ", "#テクノロジー最前線"],
        )


def test_全ての型に予算が定義されている():
    """型を足したときに予算の定義漏れを防ぐ。"""
    assert set(BUDGETS) == set(PostKind)
    for kind, (low, high) in BUDGETS.items():
        assert 0 < low < high, kind
