"""Article content scraper using newspaper3k."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from src.models.news import NewsArticle
from src.utils.logger import log_step, log_error, log_warning


class ArticleScraper:
    """記事本文をスクレイピングするクラス。

    newspaper3kを使用して、URLから記事の本文とサムネイル画像を抽出します。
    """

    def __init__(self, max_workers: int = 5, timeout: int = 30):
        """ArticleScraperを初期化する。

        Args:
            max_workers: 並行スクレイピングの最大ワーカー数
            timeout: スクレイピングタイムアウト秒数
        """
        self.max_workers = max_workers
        self.timeout = timeout
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def _resolve_google_news_url(self, url: str) -> str:
        """Google NewsのリダイレクトURLを実際の記事URLに解決する。

        googlenewsdecoderを使用してGoogle NewsのURLをデコードします。

        Args:
            url: Google NewsのURL

        Returns:
            str: 実際の記事URL、または解決できない場合は元のURL
        """
        if "news.google.com" not in url:
            return url

        try:
            from googlenewsdecoder import new_decoderv1

            result = new_decoderv1(url)
            if result.get("status") and result.get("decoded_url"):
                return result["decoded_url"]
            return url
        except Exception as e:
            log_warning(f"Failed to resolve Google News URL: {e}")
            return url

    def _extract_content(self, url: str) -> Tuple[str, Optional[str]]:
        """URLから記事本文とサムネイルを抽出する（同期処理）。

        Args:
            url: 記事のURL

        Returns:
            Tuple[str, Optional[str]]: (本文, サムネイルURL)
        """
        try:
            from newspaper import Article, Config

            # Resolve Google News redirect URLs to actual article URLs
            actual_url = self._resolve_google_news_url(url)

            # Configure newspaper3k for Japanese content
            config = Config()
            config.language = "ja"
            config.request_timeout = self.timeout
            config.browser_user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )

            article = Article(actual_url, config=config)
            article.download()
            article.parse()

            content = article.text or ""
            thumbnail = article.top_image if article.top_image else None

            return content, thumbnail

        except Exception as e:
            log_warning(f"Scraping failed for {url}: {e}")
            return "", None

    async def scrape_content(self, article: NewsArticle) -> NewsArticle:
        """記事の本文をスクレイピングする。

        Args:
            article: スクレイピング対象の記事

        Returns:
            NewsArticle: 本文が追加された記事
        """
        if article.content:
            # Already has content
            return article

        try:
            loop = asyncio.get_event_loop()
            content, thumbnail = await loop.run_in_executor(
                self._executor,
                self._extract_content,
                article.url
            )

            article.content = content
            if thumbnail and not article.thumbnail_url:
                article.thumbnail_url = thumbnail

        except Exception as e:
            log_error(f"Error scraping {article.url}: {e}")

        return article

    async def scrape_batch(
        self, articles: List[NewsArticle], show_progress: bool = True
    ) -> List[NewsArticle]:
        """複数記事を並行スクレイピングする。

        Args:
            articles: スクレイピング対象の記事リスト
            show_progress: 進捗表示するか

        Returns:
            List[NewsArticle]: スクレイピング済みの記事リスト
        """
        if not articles:
            return []

        if show_progress:
            log_step(f"{len(articles)}件の記事をスクレイピング中...", "🔍")

        # Filter articles that need scraping
        need_scraping = [a for a in articles if not a.content]
        already_scraped = [a for a in articles if a.content]

        if not need_scraping:
            if show_progress:
                log_step("全記事がスクレイピング済みです", "✅")
            return articles

        tasks = [self.scrape_content(article) for article in need_scraping]
        scraped = await asyncio.gather(*tasks)

        # Combine results
        result = already_scraped + list(scraped)

        # Count successes
        success_count = sum(1 for a in scraped if a.content)
        if show_progress:
            log_step(
                f"スクレイピング完了: {success_count}/{len(need_scraping)}件成功",
                "✅"
            )

        return result

    def close(self):
        """リソースを解放する。"""
        self._executor.shutdown(wait=False)
