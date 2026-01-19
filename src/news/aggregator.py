"""News aggregation orchestrator with JSON storage."""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.models.news import NewsArticle, NewsCategory
from src.news.sources.google_news import GoogleNewsSource
from src.news.sources.scraper import ArticleScraper
from src.utils.logger import log_step, log_success, log_error


class NewsAggregator:
    """ニュース取得・管理を統括するクラス。

    Google News RSSからニュースを取得し、JSONファイルで永続化します。
    選択状態の管理とコンテンツスクレイピングも行います。

    Attributes:
        data_dir: ニュースデータの保存ディレクトリ
        google_news: Google Newsソース
        scraper: 記事スクレイパー
    """

    def __init__(self, data_dir: Path):
        """NewsAggregatorを初期化する。

        Args:
            data_dir: ニュースデータの保存ディレクトリ
        """
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.google_news = GoogleNewsSource()
        self.scraper = ArticleScraper()

    def _get_category_file(self, category: NewsCategory) -> Path:
        """カテゴリのJSONファイルパスを取得する。

        Args:
            category: ニュースカテゴリ

        Returns:
            Path: JSONファイルのパス
        """
        return self.data_dir / f"{category.value}.json"

    def _load_category(self, category: NewsCategory) -> List[NewsArticle]:
        """カテゴリのニュースをJSONから読み込む。

        Args:
            category: ニュースカテゴリ

        Returns:
            List[NewsArticle]: 記事のリスト
        """
        file_path = self._get_category_file(category)

        if not file_path.exists():
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return [NewsArticle.from_dict(item) for item in data]

        except (json.JSONDecodeError, KeyError) as e:
            log_error(f"Error loading {file_path}: {e}")
            return []

    def _save_category(
        self, category: NewsCategory, articles: List[NewsArticle]
    ) -> None:
        """カテゴリのニュースをJSONに保存する。

        Args:
            category: ニュースカテゴリ
            articles: 保存する記事のリスト
        """
        file_path = self._get_category_file(category)

        data = [article.to_dict() for article in articles]

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def fetch_and_store(
        self, limit_per_category: int = 10
    ) -> Dict[NewsCategory, List[NewsArticle]]:
        """ニュースを取得してJSONに保存する。

        既存の記事は選択状態を保持しながらマージします。

        Args:
            limit_per_category: カテゴリごとの取得記事数

        Returns:
            Dict[NewsCategory, List[NewsArticle]]: カテゴリ別の記事
        """
        log_step("ニュースを取得・保存中...", "📥")

        # Fetch from Google News
        fetched = await self.google_news.fetch_all_categories(limit_per_category)

        result: Dict[NewsCategory, List[NewsArticle]] = {}

        for category, new_articles in fetched.items():
            # Load existing articles to preserve selection state
            existing = self._load_category(category)
            existing_by_id = {a.id: a for a in existing}

            # Merge: preserve selection state from existing
            merged = []
            for article in new_articles:
                if article.id in existing_by_id:
                    old = existing_by_id[article.id]
                    article.is_selected = old.is_selected
                    article.video_generated = old.video_generated
                    article.content = old.content or article.content
                    article.thumbnail_url = old.thumbnail_url or article.thumbnail_url
                merged.append(article)

            # Save to JSON
            self._save_category(category, merged)
            result[category] = merged

        total = sum(len(articles) for articles in result.values())
        log_success(f"保存完了: {total}件の記事")

        return result

    async def fetch_ai_news_and_store(
        self, search_queries: List[str], limit_per_query: int = 5
    ) -> List[NewsArticle]:
        """AI関連ニュースを取得してJSONに保存する。

        Args:
            search_queries: 検索クエリのリスト
            limit_per_query: クエリごとの取得記事数

        Returns:
            List[NewsArticle]: 取得した記事のリスト
        """
        log_step("AI関連ニュースを取得・保存中...", "🤖")

        # AIニュースを取得
        new_articles = await self.google_news.fetch_ai_news(
            search_queries, limit_per_query
        )

        # AIカテゴリ用のJSON保存
        category = NewsCategory.AI

        # 既存の記事を読み込んで状態を保持
        existing = self._load_category(category)
        existing_by_id = {a.id: a for a in existing}

        # マージ: 既存の選択状態を保持
        merged = []
        for article in new_articles:
            if article.id in existing_by_id:
                old = existing_by_id[article.id]
                article.is_selected = old.is_selected
                article.video_generated = old.video_generated
                article.content = old.content or article.content
                article.thumbnail_url = old.thumbnail_url or article.thumbnail_url
            merged.append(article)

        # JSONに保存
        self._save_category(category, merged)
        log_success(f"保存完了: {len(merged)}件のAI関連記事")

        return merged

    def get_articles_by_category(
        self, category: NewsCategory
    ) -> List[NewsArticle]:
        """カテゴリの記事を取得する。

        Args:
            category: ニュースカテゴリ

        Returns:
            List[NewsArticle]: 記事のリスト（公開日時の降順）
        """
        articles = self._load_category(category)

        # Sort by published_at descending
        return sorted(
            articles,
            key=lambda a: a.published_at or datetime.min,
            reverse=True
        )

    def get_all_articles(self) -> Dict[NewsCategory, List[NewsArticle]]:
        """全カテゴリの記事を取得する。

        Returns:
            Dict[NewsCategory, List[NewsArticle]]: カテゴリ別の記事
        """
        return {
            category: self.get_articles_by_category(category)
            for category in NewsCategory
        }

    def get_selected_articles(self) -> List[NewsArticle]:
        """選択された記事を取得する。

        Returns:
            List[NewsArticle]: 選択済み記事のリスト
        """
        selected = []

        for category in NewsCategory:
            articles = self._load_category(category)
            selected.extend(a for a in articles if a.is_selected)

        return selected

    def toggle_selection(self, article_id: str) -> Optional[bool]:
        """記事の選択状態を切り替える。

        Args:
            article_id: 記事ID

        Returns:
            Optional[bool]: 新しい選択状態、記事が見つからない場合はNone
        """
        for category in NewsCategory:
            articles = self._load_category(category)

            for article in articles:
                if article.id == article_id:
                    article.is_selected = not article.is_selected
                    self._save_category(category, articles)
                    return article.is_selected

        return None

    def clear_selection(self, article_id: str) -> bool:
        """記事の選択を解除する。

        Args:
            article_id: 記事ID

        Returns:
            bool: 成功したかどうか
        """
        for category in NewsCategory:
            articles = self._load_category(category)

            for article in articles:
                if article.id == article_id:
                    article.is_selected = False
                    self._save_category(category, articles)
                    return True

        return False

    def mark_as_generated(self, article_id: str) -> bool:
        """記事を動画生成済みとしてマークする。

        Args:
            article_id: 記事ID

        Returns:
            bool: 成功したかどうか
        """
        for category in NewsCategory:
            articles = self._load_category(category)

            for article in articles:
                if article.id == article_id:
                    article.video_generated = True
                    article.is_selected = False
                    self._save_category(category, articles)
                    return True

        return False

    async def scrape_selected_content(self) -> List[NewsArticle]:
        """選択記事の本文をスクレイピングする。

        Returns:
            List[NewsArticle]: スクレイピング済みの記事リスト
        """
        selected = self.get_selected_articles()

        if not selected:
            return []

        log_step(f"選択記事{len(selected)}件をスクレイピング中...", "🔍")

        # Scrape content
        scraped = await self.scraper.scrape_batch(selected)

        # Update JSON with scraped content
        for article in scraped:
            if article.content:
                for category in NewsCategory:
                    articles = self._load_category(category)
                    for i, a in enumerate(articles):
                        if a.id == article.id:
                            articles[i] = article
                            self._save_category(category, articles)
                            break

        return scraped

    def get_article_by_id(self, article_id: str) -> Optional[NewsArticle]:
        """IDで記事を取得する。

        Args:
            article_id: 記事ID

        Returns:
            Optional[NewsArticle]: 記事、見つからない場合はNone
        """
        for category in NewsCategory:
            articles = self._load_category(category)
            for article in articles:
                if article.id == article_id:
                    return article
        return None

    def get_selected_count(self) -> int:
        """選択済み記事数を取得する。

        Returns:
            int: 選択済み記事の数
        """
        return len(self.get_selected_articles())

    def close(self):
        """リソースを解放する。"""
        self.scraper.close()
