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
        "labels": ["キャッシュ", "再利用"],
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

    assert '"キャッシュ"' in prompt
    assert '"再利用"' in prompt


def test_画像内の文字を日本語で描かせる指示が入っている():
    """当初は逆（CJK を描かせない）だった。

    `gpt-image-2` が日本語を崩すという理解で英語に限っていたが、
    2026-08-16 に実画像で確かめたところ字形は正確だった。読み手が
    日本語話者なので、英語ラベルは「読めるが分からない」だけになる。
    """
    assert "every word in this image MUST be Japanese" in CARD_STYLE_PROMPT
    assert "NO Japanese or CJK characters" not in CARD_STYLE_PROMPT


def test_caption_ja_は画像に描かれる():
    """図だけでは「何が言いたい絵か」が伝わらない。

    この1行が絵の意味を決めるので、投稿本文ではなく画像に描く。
    """
    visual = _visual()
    prompt = build_card_prompt(visual)

    assert visual.caption_ja in prompt
    assert "single line across the bottom" in prompt


def test_ラベルが長すぎると弾く():
    """長い文をラベルに入れると図が文字で埋まり、縮小表示で読めなくなる。

    説明は caption_ja の1行に寄せ、ラベルは名札の役割に留める。
    """
    with pytest.raises(ValueError):
        _visual(labels=["コスト削減と体験向上のトレードオフ"])


def test_空のラベルは弾く():
    """空文字が通ると、画像に意味のない名札の枠だけが描かれる。"""
    with pytest.raises(ValueError):
        _visual(labels=["  "])


def test_短い日本語ラベルは通る():
    """日本語そのものは正当な入力になった（実画像で確認済み）。"""
    visual = _visual(labels=["コスト削減", "体験向上"])

    assert visual.labels == ["コスト削減", "体験向上"]


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
    # ラベルが長すぎる（名札に収まらない）応答を模擬する
    generator = _FakeCardVisualGenerator(
        {
            "subject": "A cache that reuses previous model inputs to cut cost.",
            "key_details": ["a funnel narrowing", "two arrows returning to a store"],
            "labels": ["コスト削減と体験向上のトレードオフ"],
            "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
        }
    )

    with pytest.raises(CardVisualGenerationError):
        generator.generate(article)


def test_視覚要素が長すぎると弾く():
    """上限が無いと、モデルは1項目にパネル1枚ぶんの記述を書く。

    実測（2026-08-16）: 40〜60語の項目が4つ集まってコマ割りの図になり、
    スタイル文の "One idea only — no comic panels" が無視された。
    スマホでは小さい文字が読めない密度になる。
    """
    panel = (
        "a split page layout: on the left a PCG GAME box with happy players and "
        "thumbs-up icons, on the right an AI-GENERATED GAME box with mixed faces"
    )
    with pytest.raises(ValueError):
        _visual(key_details=["a funnel narrowing", panel])


def test_視覚要素はちょうど2個():
    """範囲を与えると上限まで使われ、図がグループに割れる。

    実測で最も明快だったのは「2要素 + 名札 + 要点1行」の構図
    （output/cards/card-sample-ja-labels-only.png）。3個許すと3グループになった。
    """
    with pytest.raises(ValueError):
        _visual(key_details=["one", "two", "three"])
    with pytest.raises(ValueError):
        _visual(key_details=["one"])


def test_検証に失敗したら理由を伝えて引き直す() -> None:
    """引き直しが無いと、1回の逸脱でカードを作れず SINGLE に降格する。

    実測（2026-08-16）: 「視覚要素は2〜3個」の指示に対してモデルは4個返した。
    同じプロンプトを送り直しても同じ応答が返るので、何が悪かったかを渡す。
    """
    from src.models.news import NewsArticle, NewsCategory

    article = NewsArticle(
        id="a1",
        title="OpenAI が推論コストを40%削減",
        url="https://example.com/openai",
        source="TechCrunch",
        category=NewsCategory.AI,
        content="OpenAI は新しいキャッシュ方式で推論コストを削減した。",
    )
    prompts: list[str] = []
    # 1回目は視覚要素が4個（規則違反）、2回目は正しい件数を返す
    replies = [
        {
            "subject": "A cache that reuses previous model inputs to cut cost.",
            "key_details": ["one", "two", "three", "four"],
            "labels": ["キャッシュ"],
            "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
        },
        {
            "subject": "A cache that reuses previous model inputs to cut cost.",
            "key_details": ["a funnel narrowing", "two arrows returning to a store"],
            "labels": ["キャッシュ"],
            "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
        },
    ]

    generator = CardVisualGenerator("https://example.openai.azure.com", "key", "gpt-5.1")

    def fake_complete(user_prompt: str) -> str:
        prompts.append(user_prompt)
        return json.dumps(replies[min(len(prompts) - 1, len(replies) - 1)])

    generator._complete = fake_complete  # type: ignore[method-assign]

    visual = generator.generate(article)

    assert len(visual.key_details) == 2
    assert len(prompts) == 2, "引き直していない"
    assert prompts[0] != prompts[1], "同じプロンプトを送り直している"
    assert "検証を通らなかった" in prompts[1]
