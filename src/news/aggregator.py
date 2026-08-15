"""News aggregation orchestrator with JSON storage."""

import json
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory
from src.news.sources.google_news import GoogleNewsSource
from src.news.sources.scraper import ArticleScraper
from src.utils.logger import log_error, log_step, log_success


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

        # カテゴリごとの read-modify-write を直列化する。
        #
        # 動画生成は Starlette の threadpool（イベントループ外のスレッド）で
        # 走り、その中で mark_as_generated がファイルを書き換える。一方
        # イベントループ側は同時に toggle_selection などを処理しうる。
        # 排他しないと、読み込んでから書き戻すまでの間に他方の更新が挟まり、
        # その更新が失われる（選択状態や生成済みフラグが消える）。
        #
        # ロックはカテゴリ単位。全カテゴリで1つにすると、
        # 9カテゴリの並行取得が直列化してしまう。
        #
        # RLock（再入可能）である理由: `_load_category` / `_save_category` が
        # それぞれロックを取る一方、read-modify-write の区間は外側で
        # 同じロックを取る。通常の Lock だと入れ子で自分自身を待って
        # デッドロックする。
        self._locks: dict[NewsCategory, threading.RLock] = {
            category: threading.RLock() for category in NewsCategory
        }

        self.google_news = GoogleNewsSource()
        self.scraper = ArticleScraper()

    @contextmanager
    def _category_lock(self, category: NewsCategory) -> Iterator[None]:
        """カテゴリのファイルを排他する。

        read-modify-write の**全体**をこれで囲む必要がある。
        読みだけ、書きだけを個別に守っても失われた更新は防げない。

        Args:
            category: ニュースカテゴリ

        Yields:
            None
        """
        with self._locks[category]:
            yield

    def _get_category_file(self, category: NewsCategory) -> Path:
        """カテゴリのJSONファイルパスを取得する。

        Args:
            category: ニュースカテゴリ

        Returns:
            Path: JSONファイルのパス
        """
        return self.data_dir / f"{category.value}.json"

    def _load_category(self, category: NewsCategory) -> list[NewsArticle]:
        """カテゴリのニュースをJSONから読み込む。

        Args:
            category: ニュースカテゴリ

        Returns:
            List[NewsArticle]: 記事のリスト
        """
        file_path = self._get_category_file(category)

        # 読み取りもロックで守る。
        #
        # 保存は一時ファイルを os.replace で差し替えるが、Windows では
        # 置換の瞬間に開こうとした読み手が PermissionError を受ける
        # （実測で発生した）。ロックを取れば置換と読み取りが重ならない。
        with self._category_lock(category):
            if not file_path.exists():
                return []

            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)

                return [NewsArticle.from_dict(item) for item in data]

            except (json.JSONDecodeError, KeyError, OSError) as e:
                # OSError も捕まえる。ロックで守ってはいるが、
                # 別プロセス（エディタなど）がファイルを掴んでいる場合に
                # 記事一覧の表示ごと 500 にはしたくない。
                log_error(f"ニュースデータの読み込みに失敗 {file_path}: {e}")
                return []

    def _save_category(self, category: NewsCategory, articles: list[NewsArticle]) -> None:
        """カテゴリのニュースをJSONに保存する。

        Args:
            category: ニュースカテゴリ
            articles: 保存する記事のリスト
        """
        file_path = self._get_category_file(category)
        data = [article.to_dict() for article in articles]

        with self._category_lock(category):
            # 一時ファイルへ書いてから置換する。
            # 直接上書きすると、書き込み中にプロセスが落ちた場合に
            # 壊れた JSON が残り、次回の読み込みで全記事を失う。
            # Path.replace（os.replace）は同一ディレクトリ内なら原子的。
            fd, temp_name = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
            temp_path = Path(temp_name)
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                temp_path.replace(file_path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise

    async def fetch_and_store(
        self, limit_per_category: int = 10
    ) -> dict[NewsCategory, list[NewsArticle]]:
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

        result: dict[NewsCategory, list[NewsArticle]] = {}

        for category, new_articles in fetched.items():
            # 読み込み→マージ→保存の全体をロックで囲む。
            # ここも read-modify-write なので、途中で選択状態が
            # 変更されると既存状態の引き継ぎに失敗する。
            with self._category_lock(category):
                existing_by_id = {a.id: a for a in self._load_category(category)}
                merged = self._merge_preserving_state(new_articles, existing_by_id)
                self._save_category(category, merged)
            result[category] = merged

        total = sum(len(articles) for articles in result.values())
        log_success(f"保存完了: {total}件の記事")

        return result

    async def fetch_ai_news_and_store(
        self, search_queries: list[str], limit_per_query: int = 5
    ) -> list[NewsArticle]:
        """AI関連ニュースを取得してJSONに保存する。

        Args:
            search_queries: 検索クエリのリスト
            limit_per_query: クエリごとの取得記事数

        Returns:
            List[NewsArticle]: 取得した記事のリスト
        """
        log_step("AI関連ニュースを取得・保存中...", "🤖")

        # AIニュースを取得
        new_articles = await self.google_news.fetch_ai_news(search_queries, limit_per_query)

        category = NewsCategory.AI
        with self._category_lock(category):
            existing_by_id = {a.id: a for a in self._load_category(category)}
            merged = self._merge_preserving_state(new_articles, existing_by_id)
            self._save_category(category, merged)

        log_success(f"保存完了: {len(merged)}件のAI関連記事")

        return merged

    @staticmethod
    def _merge_preserving_state(
        new_articles: list[NewsArticle], existing_by_id: dict[str, NewsArticle]
    ) -> list[NewsArticle]:
        """取得した記事に、既存の状態を引き継ぐ。

        同じ記事を再取得したときに、ユーザーの選択や生成済みフラグ、
        すでにスクレイピングした本文を失わないようにする。

        Args:
            new_articles: 新しく取得した記事
            existing_by_id: 既存の記事（ID をキーにした辞書）

        Returns:
            list[NewsArticle]: 状態を引き継いだ記事のリスト
        """
        merged = []
        for article in new_articles:
            old = existing_by_id.get(article.id)
            if old is not None:
                article.is_selected = old.is_selected
                article.consumed = dict(old.consumed)
                article.content = old.content or article.content
                article.thumbnail_url = old.thumbnail_url or article.thumbnail_url
            merged.append(article)
        return merged

    def get_articles_by_category(self, category: NewsCategory) -> list[NewsArticle]:
        """カテゴリの記事を取得する。

        Args:
            category: ニュースカテゴリ

        Returns:
            List[NewsArticle]: 記事のリスト（公開日時の降順）
        """
        articles = self._load_category(category)

        # Sort by published_at descending
        return sorted(articles, key=lambda a: a.published_at or datetime.min, reverse=True)

    def get_all_articles(self) -> dict[NewsCategory, list[NewsArticle]]:
        """全カテゴリの記事を取得する。

        Returns:
            Dict[NewsCategory, List[NewsArticle]]: カテゴリ別の記事
        """
        return {category: self.get_articles_by_category(category) for category in NewsCategory}

    def get_selected_articles(self) -> list[NewsArticle]:
        """選択された記事を取得する。

        Returns:
            List[NewsArticle]: 選択済み記事のリスト
        """
        selected: list[NewsArticle] = []

        for category in NewsCategory:
            articles = self._load_category(category)
            selected.extend(a for a in articles if a.is_selected)

        return selected

    def _update_article(
        self, article_id: str, mutate: Callable[[NewsArticle], None]
    ) -> NewsArticle | None:
        """記事を1件見つけて更新し、保存する。

        「全カテゴリを走査して記事を探し、書き換えて保存する」という
        同じ手順が3箇所にあったので、ここに集約した。
        重要なのは、読み込みから保存までをカテゴリのロックで囲むこと。
        囲まないと、他方の更新が間に挟まって失われる。

        Args:
            article_id: 記事ID
            mutate: 見つけた記事を書き換える関数

        Returns:
            NewsArticle | None: 更新後の記事。見つからなければ None
        """
        for category in NewsCategory:
            with self._category_lock(category):
                articles = self._load_category(category)
                for article in articles:
                    if article.id == article_id:
                        mutate(article)
                        self._save_category(category, articles)
                        return article
        return None

    def _replace_article(self, replacement: NewsArticle) -> bool:
        """同じIDの記事を差し替えて保存する。

        Args:
            replacement: 差し替える記事

        Returns:
            bool: 見つかって差し替えたら True
        """
        for category in NewsCategory:
            with self._category_lock(category):
                articles = self._load_category(category)
                for i, existing in enumerate(articles):
                    if existing.id == replacement.id:
                        articles[i] = replacement
                        self._save_category(category, articles)
                        return True
        return False

    def toggle_selection(self, article_id: str) -> bool | None:
        """記事の選択状態を切り替える。

        Args:
            article_id: 記事ID

        Returns:
            Optional[bool]: 新しい選択状態、記事が見つからない場合はNone
        """

        def flip(article: NewsArticle) -> None:
            article.is_selected = not article.is_selected

        updated = self._update_article(article_id, flip)
        return None if updated is None else updated.is_selected

    def clear_selection(self, article_id: str) -> bool:
        """記事の選択を解除する。

        Args:
            article_id: 記事ID

        Returns:
            bool: 成功したかどうか
        """

        def deselect(article: NewsArticle) -> None:
            article.is_selected = False

        return self._update_article(article_id, deselect) is not None

    def mark_consumed(self, article_id: str, channel: str) -> bool:
        """記事をそのチャネルで消費済みとしてマークする。

        動画生成は threadpool のスレッドから、投稿は PostWorker の
        スレッドから呼ばれ、イベントループ側の toggle_selection と
        同時に走りうる。`_update_article` がロックで直列化する。

        Args:
            article_id: 記事ID
            channel: CHANNEL_VIDEO / CHANNEL_X

        Returns:
            bool: 記事が見つかって更新できたか
        """

        def mark(article: NewsArticle) -> None:
            article.mark_consumed(channel)

        return self._update_article(article_id, mark) is not None

    def mark_as_generated(self, article_id: str) -> bool:
        """記事を動画生成済みとしてマークし、選択を外す。

        選択を外すのは動画だけの都合（画面の「選択した記事」から消す）。
        X の投稿では選択状態に触らないため、mark_consumed とは分けている。

        Args:
            article_id: 記事ID

        Returns:
            bool: 成功したかどうか
        """

        def mark(article: NewsArticle) -> None:
            article.mark_consumed(CHANNEL_VIDEO)
            article.is_selected = False

        return self._update_article(article_id, mark) is not None

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を選ぶ。

        AI カテゴリを優先する。このアカウントの主題が AI・技術ニュースで、
        独自解説を載せやすいのがこの分野だから。足りなければ
        technology で補う。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            needed: 必要な件数

        Returns:
            list[NewsArticle]: 選んだ記事（足りなければ少なく返す）
        """
        picked: list[NewsArticle] = []
        seen: set[str] = set()

        for category in (NewsCategory.AI, NewsCategory.TECHNOLOGY):
            for article in self.get_articles_by_category(category):
                if len(picked) >= needed:
                    return picked
                if article.is_consumed_by(channel) or article.id in seen:
                    continue
                seen.add(article.id)
                picked.append(article)

        return picked

    async def scrape_selected_content(self) -> list[NewsArticle]:
        """選択記事の本文をスクレイピングする。

        Returns:
            List[NewsArticle]: スクレイピング済みの記事リスト
        """
        return await self.scrape_articles(self.get_selected_articles())

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """指定した記事の本文をスクレイピングし、結果を保存する。

        選択状態と切り離してある理由: 定期実行は利用者の選択を触らずに
        記事を選ぶ。`is_selected` を書き換えると、画面で選んでいた記事が
        勝手に増減してしまう。

        Args:
            articles: 対象の記事

        Returns:
            list[NewsArticle]: スクレイピング済みの記事（本文が取れなかった
            ものも含む。呼び出し側が判断する）
        """
        if not articles:
            return []

        log_step(f"記事{len(articles)}件をスクレイピング中...", "🔍")

        scraped = await self.scraper.scrape_batch(articles)

        # 取れた本文を書き戻す。ジョブは article_id しか持たないので、
        # ここで保存しておかないと実行時に本文を読めない。
        for article in scraped:
            if article.content:
                self._replace_article(article)

        return scraped

    def get_article_by_id(self, article_id: str) -> NewsArticle | None:
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
