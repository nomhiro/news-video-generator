"""記事のチャネル別消費記録の読み書き。"""

from pathlib import Path

import pytest

from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator


@pytest.fixture
def aggregator(tmp_path: Path) -> NewsAggregator:
    return NewsAggregator(data_dir=tmp_path)


def _store(aggregator: NewsAggregator, *articles: NewsArticle) -> None:
    aggregator._save_category(NewsCategory.AI, list(articles))


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


def test_mark_consumed_は_保存される(aggregator: NewsAggregator) -> None:
    article = _article("a")
    _store(aggregator, article)

    assert aggregator.mark_consumed(article.id, CHANNEL_X) is True

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.is_consumed_by(CHANNEL_X) is True
    assert reloaded.is_consumed_by(CHANNEL_VIDEO) is False


def test_mark_as_generated_は_video_チャネルを記録する(aggregator: NewsAggregator) -> None:
    """既存の呼び出し元（PipelineJobRunner）を壊さないこと。"""
    article = _article("b")
    _store(aggregator, article)

    assert aggregator.mark_as_generated(article.id) is True

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.video_generated is True
    assert reloaded.is_selected is False


def test_pick_unconsumed_は_そのチャネルで未使用の記事だけ返す(
    aggregator: NewsAggregator,
) -> None:
    used_by_x, used_by_video, fresh = _article("c"), _article("d"), _article("e")
    used_by_x.mark_consumed(CHANNEL_X)
    used_by_video.mark_consumed(CHANNEL_VIDEO)
    _store(aggregator, used_by_x, used_by_video, fresh)

    picked = aggregator.pick_unconsumed(CHANNEL_X, needed=10)

    ids = {a.id for a in picked}
    assert used_by_x.id not in ids
    # 動画で使った記事は X には出せる（両チャネルで使う運用）
    assert used_by_video.id in ids
    assert fresh.id in ids


def test_pick_unconsumed_は_needed_件で打ち切る(aggregator: NewsAggregator) -> None:
    _store(aggregator, _article("f"), _article("g"), _article("h"))

    assert len(aggregator.pick_unconsumed(CHANNEL_X, needed=2)) == 2


# --------------------------------------------------------------------------
# コンテンツフィルタに拒否された記事（issue #30）
# --------------------------------------------------------------------------


def test_mark_content_filtered_は保存される(aggregator: NewsAggregator) -> None:
    """拒否の記録が記事データに残ること。

    権威を SQLite ではなく記事データ（Azure Files）に置くのは `consumed` と
    同じ理由——ジョブ表はリビジョン更新で消えるので、そこに置くとデプロイ
    直後に同じ記事が選び直される。
    """
    article = _article("filtered")
    _store(aggregator, article)

    assert aggregator.mark_content_filtered(article.id) is True

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.is_content_filtered_for(CHANNEL_VIDEO) is True
    # チャネルは分ける。X は記事単位で次の候補へ進むので当日の投稿は落ちない
    assert reloaded.is_content_filtered_for(CHANNEL_X) is False


def test_拒否は生成済みとは別の記録(aggregator: NewsAggregator) -> None:
    """`consumed` を流用していないこと。

    流用すると `video_generated` が真になり、画面に「動画を作り終えた」と
    嘘が出る（動画は1本も出来ていない）。
    """
    article = _article("not-generated")
    _store(aggregator, article)

    aggregator.mark_content_filtered(article.id)

    reloaded = aggregator.get_article_by_id(article.id)
    assert reloaded is not None
    assert reloaded.video_generated is False
    assert reloaded.is_consumed_by(CHANNEL_VIDEO) is False


def test_拒否された記事は自動生成に選ばれない(aggregator: NewsAggregator) -> None:
    """`pick_unconsumed` が拒否済みを返さないこと（issue #30 の②）。

    ここが効かないと、記事がフィードから流れていくまで毎日同じ記事で
    同じ理由の失敗を繰り返す。
    """
    rejected = _article("rejected")
    healthy = _article("healthy")
    _store(aggregator, rejected, healthy)
    aggregator.mark_content_filtered(rejected.id)

    picked = aggregator.pick_unconsumed(CHANNEL_VIDEO, 10)

    assert [a.id for a in picked] == [healthy.id]


def test_動画で拒否されてもXの候補には残る(aggregator: NewsAggregator) -> None:
    """チャネルごとに独立していること。

    X は下書きの生成に失敗しても次の候補へ進むので、その日の投稿は落ちない。
    動画側の判断で X の候補まで削るのは行き過ぎ。
    """
    article = _article("video-only")
    _store(aggregator, article)
    aggregator.mark_content_filtered(article.id)

    assert aggregator.pick_unconsumed(CHANNEL_VIDEO, 10) == []
    assert [a.id for a in aggregator.pick_unconsumed(CHANNEL_X, 10)] == [article.id]


def test_外した記事は自動生成にも選ばれない(aggregator: NewsAggregator) -> None:
    """`dismissed` が `pick_unconsumed` にも効くことを固定する。

    **これは意図した挙動。** 以前 `get_articles_by_category` の docstring と
    CLAUDE.md は「`pick_unconsumed` はここを通らないので外した記事も候補に
    入る」と書いていたが、`pick_unconsumed` は導入時から一貫して
    `get_articles_by_category` 経由で、記述は書かれた時点から誤りだった。
    人が「題材が合わない」と決めた記事から毎朝の動画が作られるのは筋が違い、
    コンテンツフィルタに拒否されるような記事を手で止める逃げ道にもなる。
    """
    dismissed = _article("dismissed")
    healthy = _article("kept")
    _store(aggregator, dismissed, healthy)
    aggregator.set_dismissed(dismissed.id, True)

    picked = aggregator.pick_unconsumed(CHANNEL_VIDEO, 10)

    assert [a.id for a in picked] == [healthy.id]
