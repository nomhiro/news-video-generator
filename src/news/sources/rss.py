"""任意の RSS / Atom フィードから記事を取得する。

`GoogleNewsSource` と分けてある理由: あちらは Google News 固有の URL の
組み立て（`hl=ja&gl=JP&ceid=JP:ja`、トピック ID、検索クエリのエンコード）を
持っている。こちらは「与えられた URL を読むだけ」で、発信元ごとの知識を
持たない。情報源を足す作業がフィードの URL を1行足すだけになる。

取得元の一覧と、なぜその一覧なのかは `src/news/feeds.py`。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from src.models.news import NewsArticle, NewsCategory
from src.news.feeds import Feed
from src.utils.logger import log_error, log_step

# フィード側が UA を見て弾くことがあるので明示する
# （実測で `codezine.jp` が 403、`levtech.jp` が 429 を返した経験がある）。
USER_AGENT = "Mozilla/5.0 (compatible; news-video-generator/1.0)"


class RssSource:
    """RSS / Atom フィードを読んで `NewsArticle` にするクラス。"""

    def __init__(
        self,
        timeout: float = 20.0,
        concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """初期化する。

        Args:
            timeout: 1本のフィードのタイムアウト秒数
            concurrency: 同時に読むフィードの本数。相手が別ホストなので
                並行にしてよいが、無制限にすると `arxiv` のような大きな
                フィードを同時に何十本も展開してメモリを食う
            transport: httpx のトランスポート。**テストのための注入口**
                （`httpx.MockTransport` を渡す）。`httpx.AsyncClient` を
                monkeypatch で差し替える方法もあるが、クラス変数を
                書き換えるので並行するテストに漏れる
        """
        self.timeout = timeout
        self.concurrency = concurrency
        self.transport = transport

    async def fetch(
        self,
        feeds: tuple[Feed, ...] | list[Feed],
        limit_per_feed: int = 3,
        category: NewsCategory = NewsCategory.AI,
    ) -> list[NewsArticle]:
        """全フィードを読み、重複を除いた記事を返す。

        **1本の失敗で全体を落とさない。** フィードは他人のサーバーで、
        404 / 429 / タイムアウトは日常的に起きる。1本落ちたら
        そのフィードだけ0件にして先に進む（`return_exceptions=True`）。

        Args:
            feeds: 読むフィード
            limit_per_feed: フィードごとの記事数上限（新しい順）
            category: 記事に割り当てるカテゴリ

        Returns:
            list[NewsArticle]: URL で重複を除いた記事
        """
        log_step(f"フィード{len(feeds)}本から記事を取得中...", "📡")

        semaphore = asyncio.Semaphore(self.concurrency)

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=self.transport,
        ) as client:

            async def one(feed: Feed) -> list[NewsArticle]:
                async with semaphore:
                    return await self._fetch_one(client, feed, limit_per_feed, category)

            results = await asyncio.gather(*(one(feed) for feed in feeds), return_exceptions=True)

        articles: list[NewsArticle] = []
        seen: set[str] = set()
        failed = 0
        for feed, result in zip(feeds, results, strict=True):
            if isinstance(result, BaseException):
                failed += 1
                log_error(f"フィードの取得に失敗 {feed.url}: {result}")
                continue
            for article in result:
                if article.url in seen:
                    continue
                seen.add(article.url)
                articles.append(article)

        log_step(
            f"取得完了: {len(articles)}件（フィード{len(feeds) - failed}/{len(feeds)}本）",
            "📡",
        )
        return articles

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        feed: Feed,
        limit: int,
        category: NewsCategory,
    ) -> list[NewsArticle]:
        """1本のフィードを読む。"""
        try:
            response = await client.get(feed.url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            log_error(f"フィードの取得に失敗 {feed.url}: {e}")
            return []

        # `response.text` ではなく `content` を渡す。feedparser は XML 宣言の
        # encoding を自分で見るので、httpx の推測した文字コードでデコード済みの
        # 文字列を渡すと二重解釈で文字化けしうる。
        parsed = feedparser.parse(response.content)

        entries = sorted(
            parsed.entries,
            key=lambda e: self._published_at(e) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

        articles = []
        for entry in entries[:limit]:
            article = self._to_article(entry, feed, category)
            if article:
                articles.append(article)
        return articles

    @staticmethod
    def _published_at(entry: Any) -> datetime | None:
        """公開日時を取り出す。取れなければ None。

        RSS は `pubDate`、Atom は `published` / `updated` を使う。
        feedparser が正規化した `published` 文字列を優先し、
        パースできないものは None にする（並べ替えのキーなので、
        1件のために例外を上げる価値が無い）。
        """
        for key in ("published", "updated", "created"):
            value = entry.get(key)
            if not value:
                continue
            try:
                at = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                continue
            return at if at.tzinfo else at.replace(tzinfo=UTC)
        return None

    @staticmethod
    def _to_article(entry: Any, feed: Feed, category: NewsCategory) -> NewsArticle | None:
        """フィードの1件を `NewsArticle` にする。

        `url` は発信元の実 URL になる（Google News のリダイレクタと違う点）。
        投稿にはこの URL がそのまま載るので、ここが実 URL であることが
        「リンクカードに媒体名が出る」ことの前提。
        """
        url = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not url or not title:
            return None

        return NewsArticle(
            id=NewsArticle.generate_id(url),
            title=title,
            url=url,
            source=feed.source,
            category=category,
            summary=(entry.get("summary") or "").strip(),
            published_at=RssSource._published_at(entry),
        )
