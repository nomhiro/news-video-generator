"""記事本文の抽出。

trafilatura を使う理由: 以前は newspaper3k を使っていたが、最終リリース
0.2.8 が 2018年9月で開発が止まっている。`lxml.html.clean` が別パッケージへ
分離された際も追随せず、`lxml_html_clean` を明示的に入れる回避が必要だった。
また nltk / jieba3k / tinysegmenter / feedfinder2 といった重い依存を
引きずっていた（置換で12パッケージ減った）。

trafilatura は活発に保守され、テキスト密度と HTML 構造から本文を判定するため
サイトごとのセレクタを持たない。多言語の抽出精度ベンチでも上位。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import trafilatura

from src.models.news import NewsArticle
from src.utils.logger import log_error, log_step, log_warning

# 一般的なブラウザを装う。装わないと 403 を返すニュースサイトがある。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# これ未満の抽出結果は本文として扱わない。
#
# 本文が無いページ（記事一覧やリダイレクト先の空ページ）でも、
# trafilatura はナビゲーションの断片を返すことがある（実際に
# "a | b" のような数文字が返った）。呼び出し側は `if article.content`
# で真偽を見るだけなので、こういう断片が「記事本文」として
# そのまま台本生成の材料に渡ってしまう。
#
# 日本語のニュース記事は最短でも数百文字あるため、100文字を下限にする。
MIN_CONTENT_CHARS = 100


class ArticleScraper:
    """記事本文とサムネイルを抽出するクラス。"""

    def __init__(self, max_workers: int = 5, timeout: int = 30):
        """ArticleScraperを初期化する。

        Args:
            max_workers: 並行スクレイピングの最大ワーカー数
            timeout: HTTP タイムアウト秒数
        """
        self.max_workers = max_workers
        self.timeout = timeout
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def _resolve_google_news_url(self, url: str) -> str:
        """Google NewsのリダイレクトURLを実際の記事URLに解決する。

        Google News RSS の URL は元記事を base64 風に包んだものなので、
        そのまま取得しても本文が得られない。

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
            decoded = result.get("decoded_url")
            if result.get("status") and isinstance(decoded, str) and decoded:
                return decoded
            return url
        except Exception as e:
            log_warning(f"Google News URL の解決に失敗: {e}")
            return url

    def _extract_content(self, url: str) -> tuple[str, str | None]:
        """URLから記事本文とサムネイルを抽出する（同期処理）。

        取得と抽出を分けている理由: trafilatura の `fetch_url` は
        User-Agent もタイムアウトも制御しづらい。httpx で取得してから
        `extract` に渡す方が、403 への対処もタイムアウトも扱いやすい。

        Args:
            url: 記事のURL

        Returns:
            tuple[str, str | None]: (本文, サムネイルURL)。
            失敗時は ("", None)
        """
        actual_url = self._resolve_google_news_url(url)

        try:
            response = httpx.get(
                actual_url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            log_warning(f"取得に失敗 {actual_url}: {e}")
            return "", None

        try:
            content = trafilatura.extract(
                response.text,
                url=actual_url,
                # コメント欄とテーブルは本文として不要
                include_comments=False,
                include_tables=False,
                # 本文らしさの判定を緩めない。台本の材料にするので
                # ナビゲーションや広告文が混ざる方が害が大きい。
                favor_precision=True,
            )
            thumbnail = self._extract_thumbnail(response.text, actual_url)
        except Exception as e:
            log_warning(f"本文抽出に失敗 {actual_url}: {e}")
            return "", None

        if not content:
            log_warning(f"本文が見つかりませんでした {actual_url}")
            return "", None

        stripped = content.strip()
        if len(stripped) < MIN_CONTENT_CHARS:
            # 記事ページではない（一覧・エラーページなど）と判断する
            log_warning(
                f"本文が短すぎるため破棄します（{len(stripped)}文字 < "
                f"{MIN_CONTENT_CHARS}文字）: {actual_url}"
            )
            return "", None

        return stripped, thumbnail

    @staticmethod
    def _extract_thumbnail(html: str, url: str) -> str | None:
        """メタデータからサムネイル画像のURLを取り出す。

        Args:
            html: 取得した HTML
            url: 記事のURL（相対URLの解決に使う）

        Returns:
            str | None: 画像URL。見つからなければ None
        """
        try:
            metadata = trafilatura.extract_metadata(html, default_url=url)
        except Exception:
            return None
        if metadata is None:
            return None
        image = getattr(metadata, "image", None)
        return image if isinstance(image, str) and image else None

    async def scrape_content(self, article: NewsArticle) -> NewsArticle:
        """記事の本文をスクレイピングする。

        抽出は同期処理なので、イベントループを塞がないよう
        threadpool に逃がす。

        Args:
            article: スクレイピング対象の記事

        Returns:
            NewsArticle: 本文が追加された記事
        """
        if article.content:
            # Already has content
            return article

        try:
            loop = asyncio.get_running_loop()
            content, thumbnail = await loop.run_in_executor(
                self._executor, self._extract_content, article.url
            )

            article.content = content
            if thumbnail and not article.thumbnail_url:
                article.thumbnail_url = thumbnail

        except Exception as e:
            log_error(f"スクレイピング中のエラー {article.url}: {e}")

        return article

    async def scrape_batch(
        self, articles: list[NewsArticle], show_progress: bool = True
    ) -> list[NewsArticle]:
        """複数記事を並行スクレイピングする。

        Args:
            articles: スクレイピング対象の記事リスト
            show_progress: 進捗表示するか

        Returns:
            list[NewsArticle]: スクレイピング済みの記事リスト
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
            log_step(f"スクレイピング完了: {success_count}/{len(need_scraping)}件成功", "✅")

        return result

    def close(self):
        """リソースを解放する。"""
        self._executor.shutdown(wait=False)
