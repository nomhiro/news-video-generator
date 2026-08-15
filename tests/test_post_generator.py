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


def test_全ての型に予算が定義されている():
    """型を足したときに予算の定義漏れを防ぐ。"""
    assert set(BUDGETS) == set(PostKind)
    for kind, (low, high) in BUDGETS.items():
        assert 0 < low < high, kind
