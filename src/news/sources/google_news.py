"""Google News RSS feed parser."""

import asyncio
from email.utils import parsedate_to_datetime
from typing import ClassVar

import feedparser
import httpx

from src.models.news import NewsArticle, NewsCategory
from src.utils.logger import log_error, log_step


class GoogleNewsSource:
    """Google News RSSフィードから日本語ニュースを取得するクラス。

    Google NewsのRSSフィードを解析し、カテゴリ別のニュースを取得します。
    日本語ニュース用に `?hl=ja&gl=JP&ceid=JP:ja` パラメータを使用します。
    """

    BASE_URL = "https://news.google.com/rss"

    # カテゴリをGoogle Newsのトピック名にマッピング
    # headlines/section/topic/ 形式を使用（リダイレクトで正しいトピックIDに解決される）
    # AI カテゴリは検索クエリを使用するため空文字列
    CATEGORY_TOPICS: ClassVar[dict[NewsCategory, str]] = {
        NewsCategory.AI: "",  # 検索クエリで取得
        NewsCategory.GENERAL: "",  # トップニュース
        NewsCategory.POLITICS: "POLITICS",
        NewsCategory.TECHNOLOGY: "TECHNOLOGY",
        NewsCategory.BUSINESS: "BUSINESS",
        NewsCategory.ENTERTAINMENT: "ENTERTAINMENT",
        NewsCategory.SPORTS: "SPORTS",
        NewsCategory.SCIENCE: "SCIENCE",
        NewsCategory.HEALTH: "HEALTH",
    }

    def __init__(self, timeout: float = 30.0):
        """GoogleNewsSourceを初期化する。

        Args:
            timeout: HTTPリクエストのタイムアウト秒数
        """
        self.timeout = timeout

    async def fetch_category(self, category: NewsCategory, limit: int = 10) -> list[NewsArticle]:
        """指定カテゴリのニュース記事を取得する。

        Args:
            category: ニュースカテゴリ
            limit: 取得する記事数の上限

        Returns:
            List[NewsArticle]: 取得した記事のリスト
        """
        topic = self.CATEGORY_TOPICS.get(category, "")

        if topic:
            # headlines/section/topic/ 形式を使用（リダイレクトを追跡）
            url = f"{self.BASE_URL}/headlines/section/topic/{topic}?hl=ja&gl=JP&ceid=JP:ja"
        else:
            url = f"{self.BASE_URL}?hl=ja&gl=JP&ceid=JP:ja"

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()

            feed = feedparser.parse(response.text)

            articles = []
            for entry in feed.entries[:limit]:
                article = self._parse_entry(entry, category)
                if article:
                    articles.append(article)

            return articles

        except httpx.HTTPError as e:
            log_error(f"HTTP error fetching {category.value}: {e}")
            return []
        except Exception as e:
            log_error(f"Error fetching {category.value}: {e}")
            return []

    async def fetch_by_search(
        self, query: str, category: NewsCategory, limit: int = 10
    ) -> list[NewsArticle]:
        """検索クエリでニュース記事を取得する。

        Args:
            query: 検索クエリ（例: "生成AI", "ChatGPT"）
            category: 記事に割り当てるカテゴリ
            limit: 取得する記事数の上限

        Returns:
            List[NewsArticle]: 取得した記事のリスト
        """
        from urllib.parse import quote

        encoded_query = quote(query)
        url = f"{self.BASE_URL}/search?q={encoded_query}&hl=ja&gl=JP&ceid=JP:ja"

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()

            feed = feedparser.parse(response.text)

            articles = []
            for entry in feed.entries[:limit]:
                article = self._parse_entry(entry, category)
                if article:
                    articles.append(article)

            return articles

        except httpx.HTTPError as e:
            log_error(f"HTTP error searching for '{query}': {e}")
            return []
        except Exception as e:
            log_error(f"Error searching for '{query}': {e}")
            return []

    async def fetch_ai_news(
        self, search_queries: list[str], limit_per_query: int = 5
    ) -> list[NewsArticle]:
        """AI関連ニュースを複数の検索クエリから取得する。

        Args:
            search_queries: 検索クエリのリスト
            limit_per_query: クエリごとの記事数上限

        Returns:
            List[NewsArticle]: 取得した記事のリスト（重複除去済み）
        """
        log_step("AI関連ニュースを取得中...", "🤖")

        # AIカテゴリを使用
        category = NewsCategory.AI

        tasks = [self.fetch_by_search(query, category, limit_per_query) for query in search_queries]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 重複を除去しながら収集
        seen_ids = set()
        all_articles = []

        for result in results:
            # asyncio.gather(return_exceptions=True) は BaseException も返す。
            # Exception だけを見ると CancelledError などが素通りして、
            # 例外オブジェクトを反復しようとして TypeError になる。
            if isinstance(result, BaseException):
                log_error(f"Search failed: {result}")
                continue
            for article in result:
                if article.id not in seen_ids:
                    seen_ids.add(article.id)
                    all_articles.append(article)

        log_step(f"取得完了: {len(all_articles)}件のAI関連記事", "✅")
        return all_articles

    async def fetch_all_categories(
        self, limit_per_category: int = 10
    ) -> dict[NewsCategory, list[NewsArticle]]:
        """全カテゴリからニュースを並行取得する。

        Args:
            limit_per_category: カテゴリごとの記事数上限

        Returns:
            Dict[NewsCategory, List[NewsArticle]]: カテゴリ別の記事リスト
        """
        log_step("全カテゴリからニュースを取得中...", "📰")

        # AIカテゴリは検索クエリで取得するため除外
        categories_to_fetch = [cat for cat in NewsCategory if cat != NewsCategory.AI]

        tasks = [self.fetch_category(cat, limit_per_category) for cat in categories_to_fetch]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles_by_category: dict[NewsCategory, list[NewsArticle]] = {}

        # strict=True: asyncio.gather は tasks と同数の結果を返すはずなので、
        # ずれたらカテゴリを黙って取りこぼすのではなく失敗させる
        for category, result in zip(categories_to_fetch, results, strict=True):
            # BaseException で判定する理由は fetch_ai_news と同じ
            if isinstance(result, BaseException):
                log_error(f"Failed to fetch {category.value}: {result}")
                articles_by_category[category] = []
            else:
                articles_by_category[category] = result

        total = sum(len(articles) for articles in articles_by_category.values())
        log_step(f"取得完了: {total}件の記事", "✅")

        return articles_by_category

    def _parse_entry(self, entry: dict, category: NewsCategory) -> NewsArticle | None:
        """RSSエントリをNewsArticleに変換する。

        Args:
            entry: feedparserのエントリ
            category: ニュースカテゴリ

        Returns:
            Optional[NewsArticle]: 変換された記事、または失敗時はNone
        """
        try:
            url = entry.get("link", "")
            if not url:
                return None

            title = entry.get("title", "")

            # Google Newsのタイトル形式: "記事タイトル - ソース名"
            source = "Google News"
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                title = parts[0]
                source = parts[1] if len(parts) > 1 else source

            # 公開日時をパース
            published_at = None
            # feedparser の entry は属性アクセスもできるが、型上は dict なので
            # get() で取り出す（キーが無い場合は None のまま）
            published_raw = entry.get("published")
            if published_raw:
                try:
                    published_at = parsedate_to_datetime(published_raw)
                except (TypeError, ValueError):
                    pass

            # サマリーを取得
            summary = entry.get("summary", "")
            # HTMLタグを簡易的に除去
            if summary:
                import re

                summary = re.sub(r"<[^>]+>", "", summary)

            return NewsArticle(
                id=NewsArticle.generate_id(url),
                title=title,
                url=url,
                source=source,
                category=category,
                summary=summary[:500] if summary else "",
                published_at=published_at,
            )

        except Exception as e:
            log_error(f"Error parsing entry: {e}")
            return None
