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
