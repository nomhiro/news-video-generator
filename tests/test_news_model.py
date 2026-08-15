"""ニュース記事モデルとファイル名サニタイズの検証。"""

from datetime import datetime

import pytest

from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.pipeline import Pipeline

# --------------------------------------------------------------------------
# NewsCategory
# --------------------------------------------------------------------------


def test_category_str_is_the_value() -> None:
    """StrEnum なので str() が値そのものになること。

    (str, Enum) から StrEnum に変えた際の互換性を固定する。
    テンプレートは category.value を使っているが、
    ここが変わると URL 生成が壊れる可能性がある。
    """
    assert str(NewsCategory.AI) == "ai"
    assert NewsCategory.AI.value == "ai"


def test_category_round_trips_from_string() -> None:
    for category in NewsCategory:
        assert NewsCategory(category.value) is category


def test_every_category_has_a_japanese_display_name() -> None:
    """全カテゴリに日本語表示名があること。UI に enum 名が漏れるのを防ぐ。"""
    for category in NewsCategory:
        assert category.display_name
        assert category.display_name != category.value


# --------------------------------------------------------------------------
# NewsArticle
# --------------------------------------------------------------------------


def test_generate_id_is_stable_for_the_same_url() -> None:
    """同じURLからは常に同じIDが出ること。

    記事の重複排除と選択状態の保持がIDの安定性に依存している。
    """
    url = "https://example.com/news/12345"
    assert NewsArticle.generate_id(url) == NewsArticle.generate_id(url)


def test_generate_id_differs_for_different_urls() -> None:
    a = NewsArticle.generate_id("https://example.com/a")
    b = NewsArticle.generate_id("https://example.com/b")
    assert a != b


def test_generate_id_length() -> None:
    assert len(NewsArticle.generate_id("https://example.com/a")) == 16


def test_article_round_trips_through_dict() -> None:
    article = NewsArticle(
        id="abc123",
        title="テスト記事",
        url="https://example.com/a",
        source="テストソース",
        category=NewsCategory.TECHNOLOGY,
        summary="要約",
        content="本文",
        published_at=datetime(2026, 8, 11, 12, 0, 0),
        is_selected=True,
    )
    restored = NewsArticle.from_dict(article.to_dict())

    assert restored.id == article.id
    assert restored.title == article.title
    assert restored.category is NewsCategory.TECHNOLOGY
    assert restored.published_at == article.published_at
    assert restored.is_selected is True


def test_to_dict_serializes_category_as_string() -> None:
    """カテゴリが JSON で扱える文字列になること。"""
    article = NewsArticle(id="x", title="t", url="u", source="s", category=NewsCategory.AI)
    assert article.to_dict()["category"] == "ai"


def test_from_dict_tolerates_missing_published_at() -> None:
    """published_at が無い記事も読めること（RSS が日付を返さない場合がある）。"""
    article = NewsArticle(id="x", title="t", url="u", source="s", category=NewsCategory.AI)
    data = article.to_dict()
    data["published_at"] = None
    assert NewsArticle.from_dict(data).published_at is None


# --------------------------------------------------------------------------
# ファイル名サニタイズ
#
# 記事タイトルをそのままファイル名に使うため、Windows で使えない文字が
# 入ると生成が失敗する。
# --------------------------------------------------------------------------


@pytest.fixture
def sanitize():
    # Pipeline の生成は API クライアントを作るため、メソッドだけ取り出す
    pipeline = Pipeline.__new__(Pipeline)
    return pipeline._sanitize_filename


@pytest.mark.parametrize("char", ["\\", "/", "*", "?", ":", '"', "<", ">", "|"])
def test_removes_characters_windows_forbids(sanitize, char: str) -> None:
    result = sanitize(f"before{char}after")
    assert char not in result


def test_collapses_consecutive_whitespace(sanitize) -> None:
    assert sanitize("a    b\t\tc") == "a b c"


def test_strips_surrounding_whitespace(sanitize) -> None:
    assert sanitize("   タイトル   ") == "タイトル"


def test_truncates_to_max_length(sanitize) -> None:
    result = sanitize("あ" * 100)
    assert len(result) <= 50


def test_respects_explicit_max_length(sanitize) -> None:
    assert len(sanitize("あ" * 100, max_length=10)) <= 10


def test_falls_back_to_default_name_when_empty(sanitize) -> None:
    """全部除去されても空のファイル名を返さないこと。"""
    assert sanitize("///???") == "video"
    assert sanitize("   ") == "video"
    assert sanitize("") == "video"


def test_keeps_japanese_characters(sanitize) -> None:
    """日本語は保持すること（出力ファイルを人が識別できるようにするため）。"""
    assert sanitize("OpenAIが新モデルを発表") == "OpenAIが新モデルを発表"


# --------------------------------------------------------------------------
# チャネル別消費記録
# --------------------------------------------------------------------------


def test_旧形式の_video_generated_を_consumed_として読む():
    """既存の data/news/*.json を移行スクリプトなしで読めること。

    クラウドの Azure Files 上には旧形式の JSON が既に存在する。
    読めなくなると記事一覧が空になり、生成対象を全て見失う。
    """
    data = {
        "id": "abc123",
        "title": "テスト記事",
        "url": "https://example.com/a",
        "source": "Example",
        "category": "ai",
        "fetched_at": "2026-08-01T10:00:00",
        "video_generated": True,
    }

    article = NewsArticle.from_dict(data)

    assert article.is_consumed_by(CHANNEL_VIDEO) is True
    assert article.video_generated is True
    assert article.is_consumed_by(CHANNEL_X) is False
    assert article.consumed[CHANNEL_VIDEO] == "2026-08-01T10:00:00"


def test_未消費の記事は_どのチャネルでも未消費():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/b", source="s", category=NewsCategory.AI
    )

    assert article.consumed == {}
    assert article.video_generated is False
    assert article.is_consumed_by(CHANNEL_X) is False


def test_mark_consumed_は_他のチャネルを消さない():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/c", source="s", category=NewsCategory.AI
    )

    article.mark_consumed(CHANNEL_VIDEO)
    article.mark_consumed(CHANNEL_X)

    assert article.is_consumed_by(CHANNEL_VIDEO) is True
    assert article.is_consumed_by(CHANNEL_X) is True


def test_to_dict_は_切り戻しのために_video_generated_も出す():
    """旧イメージへの切り戻しで記事が読めなくなるのを防ぐ（1リリース限り）。

    このテストは以前「`video_generated` を出力しない」ことを主張していた。
    権威を1つに保つという意図は正しかったが、**切り戻しを壊す**という
    見落としがあった。CLAUDE.md の切り戻し手順は前のイメージタグへの
    差し替えで、旧コードの `from_dict` は `cls(**data)` なので `consumed`
    を受け取ると `TypeError` になる。`_load_category` は
    JSONDecodeError / KeyError / OSError しか捕まえないため、記事一覧と
    2つの計画がまとめて落ちる。

    権威が2つになる心配は無い: `video_generated` は `consumed` から
    導出した派生値で、新しい `from_dict` は読み込み時に pop する。

    注意: これだけでは切り戻しは成立しない。`consumed` 自体も旧
    `__init__` には未知のキーなので落ちる（`to_dict` のコメント参照）。
    """
    article = NewsArticle(
        id="x", title="t", url="https://example.com/d", source="s", category=NewsCategory.AI
    )
    article.mark_consumed(CHANNEL_VIDEO)

    data = article.to_dict()

    assert data["video_generated"] is True
    assert data["consumed"] == article.consumed


def test_to_dict_の_video_generated_は_consumed_から導かれる():
    """未消費なら False。定数を書いているのではないことの確認。"""
    article = NewsArticle(
        id="y", title="t", url="https://example.com/f", source="s", category=NewsCategory.AI
    )

    assert article.to_dict()["video_generated"] is False

    article.mark_consumed(CHANNEL_X)  # X だけ消費しても動画は False

    assert article.to_dict()["video_generated"] is False


def test_from_dict_は_to_dict_の_出力を_復元できる():
    article = NewsArticle(
        id="x", title="t", url="https://example.com/e", source="s", category=NewsCategory.AI
    )
    article.mark_consumed(CHANNEL_X)

    restored = NewsArticle.from_dict(article.to_dict())

    assert restored.consumed == article.consumed
