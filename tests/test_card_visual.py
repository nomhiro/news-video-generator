"""画像カードの視覚指示とプロンプト。"""

import json
from typing import Any

import pytest

from src.generators.image_generator import validate_size
from src.social.card_visual import (
    CARD_IMAGE_SIZE,
    CARD_STYLE_PROMPT,
    CardVisual,
    CardVisualGenerationError,
    CardVisualGenerator,
    build_card_prompt,
)


def _visual(**overrides: Any) -> CardVisual:
    data: dict[str, Any] = {
        "subject": "A cache that reuses previous model inputs to cut cost.",
        "key_details": ["a funnel narrowing", "two arrows returning to a store"],
        "labels": ["CACHE", "REUSED"],
        "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
    }
    data.update(overrides)
    return CardVisual(**data)


def test_固定のスタイル文が先頭に来る():
    """順序は background/scene -> subject -> key details -> constraints。

    OpenAI のガイドがこの順を推奨しており、順序を崩すと
    毎回違う絵が出る。
    """
    prompt = build_card_prompt(_visual())

    assert prompt.startswith(CARD_STYLE_PROMPT)


def test_ラベルは引用符で囲む():
    """ガイドは literal text を引用符か ALL CAPS で示すよう指示している。"""
    prompt = build_card_prompt(_visual())

    assert '"CACHE"' in prompt
    assert '"REUSED"' in prompt


def test_日本語を描かせない指示が入っている():
    """gpt-image-2 の CJK 描画は保証されていない。

    日本語は投稿本文に持たせれば確実に読めるので、画像に賭けない。
    """
    assert "NO Japanese or CJK characters" in CARD_STYLE_PROMPT


def test_caption_ja_はプロンプトに入らない():
    """画像に日本語を入れないという方針と矛盾する。"""
    visual = _visual()
    prompt = build_card_prompt(visual)

    assert visual.caption_ja not in prompt


def test_ラベルが英大文字でなければ弾く():
    with pytest.raises(ValueError):
        _visual(labels=["キャッシュ"])


def test_ラベルは4個まで():
    with pytest.raises(ValueError):
        _visual(labels=["A", "B", "C", "D", "E"])


def test_カードのサイズは_gpt_image_2_の制約を満たす():
    """両辺が16の倍数、総ピクセル数の範囲内。"""
    assert validate_size(CARD_IMAGE_SIZE) == (1024, 1024)


class _FakeCardVisualGenerator(CardVisualGenerator):
    """API 接続を行わず `_complete` だけ差し替える。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def _complete(self, user_prompt: str) -> str:
        return json.dumps(self._payload)


def test_視覚指示生成器は_complete_の応答をCardVisualへ変換する() -> None:
    from src.models.news import NewsArticle, NewsCategory

    article = NewsArticle(
        id="a1",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="TechCrunch",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを削減した。",
    )
    generator = _FakeCardVisualGenerator(
        {
            "subject": "A cache that reuses previous model inputs to cut cost.",
            "key_details": ["a funnel narrowing", "two arrows returning to a store"],
            "labels": ["CACHE"],
            "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
        }
    )

    visual = generator.generate(article)

    assert visual.labels == ["CACHE"]
    assert visual.caption_ja == "同じ入力を使い回すことで推論コストが下がる。"


def test_視覚指示生成器は検証に失敗すると専用の例外を出す() -> None:
    from src.models.news import NewsArticle, NewsCategory

    article = NewsArticle(
        id="a1",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="TechCrunch",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを削減した。",
    )
    # ラベルが日本語（英大文字ルール違反）を返すモデルの応答を模擬する
    generator = _FakeCardVisualGenerator(
        {
            "subject": "A cache that reuses previous model inputs to cut cost.",
            "key_details": ["a funnel narrowing", "two arrows returning to a store"],
            "labels": ["キャッシュ"],
            "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
        }
    )

    with pytest.raises(CardVisualGenerationError):
        generator.generate(article)
