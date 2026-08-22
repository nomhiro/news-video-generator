"""フィードからの記事取得。HTTP は叩かず、応答を差し替える。"""

from collections.abc import Mapping
from datetime import UTC, datetime

import httpx
import pytest

from src.models.news import NewsArticle, NewsCategory
from src.news.aggregator import _sort_key
from src.news.feeds import AI_FEEDS, Feed
from src.news.sources.rss import RssSource


def _rss(items: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>{items}</channel></rss>""".encode()


def _item(title: str, link: str, date: str) -> str:
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate></item>"


def _transport(bodies: Mapping[str, bytes | int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


async def _fetch(
    feeds: list[Feed], bodies: Mapping[str, bytes | int], limit: int = 3
) -> list[NewsArticle]:
    """応答を差し替えて `RssSource.fetch` を呼ぶ。"""
    source = RssSource(timeout=1.0, transport=_transport(bodies))
    return await source.fetch(feeds, limit)


@pytest.mark.asyncio
async def test_リンクは発信元の実URLになる() -> None:
    """Google News のリダイレクタと違い、フィードの link は媒体の実 URL。

    投稿にはこの URL がそのまま載るので、ここが実 URL であることが
    「リンクカードに媒体名とタイトルが出る」ことの前提。
    """
    feeds = [Feed("Zenn", "https://zenn.dev/topics/ai/feed")]
    bodies = {
        feeds[0].url: _rss(
            _item("記事A", "https://zenn.dev/u/articles/a", "Fri, 22 Aug 2026 09:00:00 +0900")
        )
    }

    articles = await _fetch(feeds, bodies)

    assert [a.url for a in articles] == ["https://zenn.dev/u/articles/a"]
    assert articles[0].source == "Zenn"
    assert articles[0].category is NewsCategory.AI


@pytest.mark.asyncio
async def test_1本の失敗で全体を落とさない() -> None:
    """フィードは他人のサーバー。404 / 429 は日常的に起きる。

    実測（2026-08-22）で 31本中3本が 404 / 401 だった。ここで例外を
    上げると、生きている27本ぶんの記事も取れなくなる。
    """
    ok = Feed("Zenn", "https://zenn.dev/topics/ai/feed")
    dead = Feed("Anthropic", "https://www.anthropic.com/news/rss.xml")
    bodies: dict[str, bytes | int] = {
        ok.url: _rss(_item("記事A", "https://zenn.dev/a", "Fri, 22 Aug 2026 09:00:00 +0900")),
        dead.url: 404,
    }

    articles = await _fetch([ok, dead], bodies)

    assert [a.title for a in articles] == ["記事A"]


@pytest.mark.asyncio
async def test_同じURLは1件にまとめる() -> None:
    """Zenn のトピックは重なる（`ai` と `llm` に同じ記事が載る）。"""
    a = Feed("Zenn", "https://zenn.dev/topics/ai/feed")
    b = Feed("Zenn", "https://zenn.dev/topics/llm/feed")
    same = _item("記事A", "https://zenn.dev/a", "Fri, 22 Aug 2026 09:00:00 +0900")
    bodies: dict[str, bytes | int] = {a.url: _rss(same), b.url: _rss(same)}

    articles = await _fetch([a, b], bodies)

    assert len(articles) == 1


@pytest.mark.asyncio
async def test_新しい順に上から取る() -> None:
    """arXiv は1日で200件超を返す。フィードの並び順を当てにしない。"""
    feed = Feed("arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI")
    bodies: dict[str, bytes | int] = {
        feed.url: _rss(
            _item("古い", "https://arxiv.org/abs/1", "Wed, 20 Aug 2026 09:00:00 +0000")
            + _item("新しい", "https://arxiv.org/abs/2", "Fri, 22 Aug 2026 09:00:00 +0000")
            + _item("中間", "https://arxiv.org/abs/3", "Thu, 21 Aug 2026 09:00:00 +0000")
        )
    }

    articles = await _fetch([feed], bodies, limit=2)

    assert [a.title for a in articles] == ["新しい", "中間"]


def test_公開日時のnaiveとawareが混ざっても並べ替えられる() -> None:
    """`RssSource` は tz 付きを返し、古い記事や他の情報源は naive を持つ。

    混ざったまま `sorted` に渡すと
    `can't compare offset-naive and offset-aware datetimes` で落ちる。
    **実際に踏んだ**（フィードから記事を入れた直後の `pick_unconsumed` で、
    記事一覧・動画の計画・投稿の計画がすべて落ちた）。
    """

    def article(published: datetime | None) -> NewsArticle:
        return NewsArticle(
            id="x",
            title="t",
            url="https://example.com/x",
            source="s",
            category=NewsCategory.AI,
            published_at=published,
        )

    mixed = [
        article(datetime(2026, 8, 22, 9, 0, tzinfo=UTC)),
        article(datetime(2026, 8, 21, 9, 0)),  # naive
        article(None),
    ]

    ordered = sorted(mixed, key=_sort_key, reverse=True)

    assert [a.published_at for a in ordered][2] is None


def test_フィードの一覧に重複したURLが無い() -> None:
    """同じ URL を2行書いても実害は無いが、取得が無駄に1本増える。"""
    urls = [f.url for f in AI_FEEDS]
    assert len(urls) == len(set(urls))


def test_フィードのURLはhttpsで始まる() -> None:
    for feed in AI_FEEDS:
        assert feed.url.startswith("https://"), feed
        assert feed.source, feed
