"""記事の供給。動画と X が同じ1段を通ることを見張る。

**この検査がある理由。** 「選ぶ → 本文を取る」は両方の計画が同じ理由で
必要としているのに、実装が2箇所に分かれていた。結果として
`ARTICLE_OVERFETCH`（取りこぼし対策）が X 側にしか無く、動画側は
必要数ぴったりを選んでいた。同じ対策が要る2箇所に片方だけ入っている状態は、
片方だけ腐る。ここでは共有されていること自体を検査する。
"""

import asyncio
import dataclasses
import inspect
from pathlib import Path

import pytest

from src.jobs import planner, post_planner
from src.jobs.article_supply import ARTICLE_OVERFETCH, supply_articles
from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.news.aggregator import AUTO_SOURCE_CATEGORIES, NewsAggregator


def _article(suffix: str, content: str = "") -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=suffix,
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
        content=content,
    )


class FakeNews:
    def __init__(self, articles: list[NewsArticle], scrapable: set[str] | None = None) -> None:
        self._articles = articles
        self._scrapable = scrapable if scrapable is not None else {a.id for a in articles}
        self.requested: list[int] = []

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        self.requested.append(needed)
        return self._articles[:needed]

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """本文が取れたものだけを返す（実物と同じ契約）。"""
        return [
            NewsArticle(**{**a.__dict__, "content": "本文。" * 40})
            for a in articles
            if a.id in self._scrapable
        ]


@pytest.mark.asyncio
async def test_必要数の3倍を候補にする() -> None:
    """スクレイピングは常に一部落ちる。必要数ぴったりでは足りない。"""
    news = FakeNews([_article(str(i)) for i in range(20)])

    await supply_articles(news, CHANNEL_X, 4)

    assert news.requested == [4 * ARTICLE_OVERFETCH]


@pytest.mark.asyncio
async def test_本文が取れたものと候補すべてを分けて返す() -> None:
    """扱いが計画ごとに違うので、判断は呼び出し元に残す。"""
    news = FakeNews([_article("a"), _article("b"), _article("c")], scrapable={"b"})

    supply = await supply_articles(news, CHANNEL_VIDEO, 1)

    assert [a.id for a in supply.with_content] == ["b"]
    assert [a.id for a in supply.candidates] == ["a", "b", "c"]
    # 本文が取れなかった記事は content が空のまま候補に残る
    assert supply.candidates[0].content == ""


@pytest.mark.asyncio
async def test_候補が無ければ空を返す() -> None:
    supply = await supply_articles(FakeNews([]), CHANNEL_X, 4)

    assert supply.with_content == []
    assert supply.candidates == []


def test_動画とXの両方が共通の供給を通る() -> None:
    """どちらかが `pick_unconsumed` を直接呼ぶ形に戻ったら落とす。

    直接呼ぶ実装に戻ると、そちらだけ `ARTICLE_OVERFETCH` を失う
    （実際にそうなっていた）。
    """
    for module in (planner, post_planner):
        source = inspect.getsource(module)
        assert "supply_articles(" in source, module.__name__
        assert "news.pick_unconsumed(" not in source, (
            f"{module.__name__} が記事の選択を自分で行っている（`supply_articles` を通すこと）"
        )


def test_自動生成はGoogleNews由来のカテゴリを選ばない(tmp_path: Path) -> None:
    """以前は AI が足りないとき technology で補っていた。

    technology は実測で10件中10件が `news.google.com` のリダイレクタ URL で、
    選ばれると投稿のリンクカードに Google News が出る（フィードに変えた
    理由がそのまま戻る）。**AI が枯れたときだけ起きるので気付きにくい。**

    **定数（`AUTO_SOURCE_CATEGORIES`）ではなく挙動を見る。** 定数だけを
    検査した版は、`pick_unconsumed` のループを
    `(NewsCategory.AI, NewsCategory.TECHNOLOGY)` に書き戻しても通った
    （定数が使われなくなるだけなので）。実際に確認して直した。
    """
    aggregator = NewsAggregator(tmp_path)
    try:
        aggregator._save_category(NewsCategory.AI, [_article("ai-1", content="本文")])
        aggregator._save_category(
            NewsCategory.TECHNOLOGY,
            [
                dataclasses.replace(
                    _article("tech-1"),
                    category=NewsCategory.TECHNOLOGY,
                    url="https://news.google.com/rss/articles/CBMi123",
                )
            ],
        )

        # AI が1件しか無いので、フォールバックがあれば technology が入る
        picked = aggregator.pick_unconsumed(CHANNEL_X, 5)
    finally:
        aggregator.close()

    assert [a.id for a in picked] == ["ai-1"]
    assert AUTO_SOURCE_CATEGORIES == (NewsCategory.AI,)


def test_供給は非同期のまま使われる() -> None:
    """`plan_daily_posts` を同期に戻すと `scrape_articles` を呼べない。"""
    assert asyncio.iscoroutinefunction(post_planner.plan_daily_posts)
    assert asyncio.iscoroutinefunction(planner.plan_daily_batch)
