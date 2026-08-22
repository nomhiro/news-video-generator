"""News aggregation orchestrator with JSON storage."""

import json
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory
from src.news.feeds import AI_FEEDS, Feed
from src.news.sources.google_news import GoogleNewsSource
from src.news.sources.rss import RssSource
from src.news.sources.scraper import ArticleScraper
from src.utils.logger import log_error, log_step, log_success

# 自動生成（動画・X）が記事を選ぶカテゴリ。**Google News 由来を含めない。**
#
# 2つの情報源が同じストアに同居している。
#
# - `AI` は発信元のフィード（`src/news/feeds.py`）。`link` は媒体の実 URL
# - それ以外のカテゴリは Google News の RSS。**画面でブラウズするための一覧**で、
#   `link` は `news.google.com/rss/articles/...` というリダイレクタ
#
# 以前は「AI を優先し、足りなければ technology で補う」形だった。AI が
# 検索クエリ由来で薄かった頃の保険だが、**フィードに変えたあとは害しかない**。
# technology は実測で 10件中10件が Google News のリダイレクタ URL で、
# 選ばれると (a) 投稿のリンクカードに Google News が出る、
# (b) 芸能・PR 転載を一次情報と区別できない——フィードに変えた理由が
# そのまま戻る。しかも AI が枯れたときだけ起きるので気付きにくい。
#
# フィードで埋めるカテゴリを増やすなら、ここに足す。
AUTO_SOURCE_CATEGORIES: tuple[NewsCategory, ...] = (NewsCategory.AI,)

# 消費済み（投稿した・動画にした）記事を、フィードから流れたあとも
# どれだけ保持するか。
#
# `consumed` は「もう出した」の権威なので、失うと同じ記事で投稿が作り直される。
# 一方で永久に残すと記事プールが単調増加する（毎日5件前後 = 年1,800件）。
# フィードが運ぶのは数日ぶんの項目なので、それを大きく超えたものが
# もう一度取得されることは実質的に無い。
CONSUMED_RETENTION = timedelta(days=90)


def _sort_key(article: NewsArticle) -> datetime:
    """公開日時の降順に並べるためのキー。**naive を UTC に読み替える。**

    `published_at` は情報源によって tz 付きと naive が混ざる。RSS / Atom は
    `pubDate` にオフセットを持つので `RssSource` は aware を返すが、
    `GoogleNewsSource` 経由や JSON から復元した古い記事は naive になりうる。
    混ざったまま `sorted` に渡すと
    `can't compare offset-naive and offset-aware datetimes` で落ちる
    （実際に踏んだ。フィードから記事を入れた直後の `pick_unconsumed` で、
    記事一覧・動画の計画・投稿の計画がすべて落ちる）。

    **`datetime.min` を既定値に使わない**のも同じ理由（naive なので、
    aware な記事が1件あるだけで比較不能になる）。

    Args:
        article: 対象の記事

    Returns:
        datetime: tz 付きの比較用キー（未設定なら最小値）
    """
    at = article.published_at
    if at is None:
        return datetime.min.replace(tzinfo=UTC)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


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
        self.rss = RssSource()
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
        self,
        feeds: tuple[Feed, ...] | list[Feed] = AI_FEEDS,
        limit_per_feed: int = 3,
    ) -> list[NewsArticle]:
        """AI関連の記事を発信元のフィードから取得してJSONに保存する。

        **Google News の検索クエリは使わない。** 語が一致するだけの記事
        （AI が話題に出た芸能ニュース、PR 転載）を一次情報と区別できず、
        `link` も Google News のリダイレクタになる。理由の詳細は
        `src/news/feeds.py` の冒頭。

        Args:
            feeds: 読むフィード。既定は `AI_FEEDS`
            limit_per_feed: フィードごとの取得記事数（新しい順）

        Returns:
            List[NewsArticle]: 取得した記事のリスト
        """
        log_step("AI関連の記事を取得・保存中...", "🤖")

        new_articles = await self.rss.fetch(feeds, limit_per_feed, NewsCategory.AI)

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

        引き継ぎは2方向ある。**片方だけだと状態が消える。**

        - 再取得できた記事: 選択・消費の記録・本文・サムネイルを引き継ぐ
        - 取得できなくなった記事: 残すべきものだけ残す
          （判断は `_must_survive_refetch`）

        後者が無かった間、ここは取得できた記事だけを返し、それを
        `_save_category` がストアに上書きしていた。フィードは新しい順に
        数件しか返さないので、**選択中の記事と `consumed` が黙って消えていた**。

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
                # 外した記録も引き継ぐ。落とすと、まだフィードに載っている
                # 記事が取得のたびに戻ってきて「外す」が効かなくなる。
                article.dismissed = old.dismissed
                # 拒否の記録も引き継ぐ。**落とすと再取得のたびに初期値へ戻り、
                # 拒否された記事が毎日選ばれ直す**（この機能そのものが効かない）。
                article.content_filtered = dict(old.content_filtered)
                article.content = old.content or article.content
                article.thumbnail_url = old.thumbnail_url or article.thumbnail_url
            merged.append(article)

        fetched_ids = {article.id for article in new_articles}
        merged.extend(
            old
            for article_id, old in existing_by_id.items()
            if article_id not in fetched_ids and NewsAggregator._must_survive_refetch(old)
        )
        return merged

    @staticmethod
    def _must_survive_refetch(article: NewsArticle) -> bool:
        """取得結果に無くなった既存記事を残すべきか。

        フィードは新しい順に数件しか返さないので、記事は数時間で入れ替わる。
        取得できたものだけを保存すると、**選択中の記事も消費の記録も消える**。

        - 選択中の記事が消える: 記事を選んで「最新ニュースを取得」を押すと
          選択が黙って減る。画面には何の説明も出ない
        - `consumed` が消える: これは「もう投稿した」の権威で、SQLite では
          なく記事データに置いてあるのがこの設計の要点
          （CLAUDE.md「もう投稿したの権威は Azure Files 上の記事データ」）。
          失うと、同じ記事の投稿が作り直されて二重投稿になりうる

        **無条件に全件残してはいけない。** 単にフィードから流れていっただけの
        記事まで残すと一覧が単調増加し、記事プールが読めなくなる
        （実測で AI カテゴリは53件で 8,633px の高さになっていた）。

        消費済みには保持期間を置く。フィードが運べるのは数日ぶんの項目なので、
        それを大きく超えて経ったものは「もう一度取得されて再投稿される」
        経路が実質的に無い。選択中のものは期間で切らない——人が意図して
        選んだものが日付で消えるのは、この画面で最も分かりにくい壊れ方になる。

        Args:
            article: 取得結果に含まれなかった既存の記事

        Returns:
            bool: 残すなら True
        """
        if article.is_selected:
            return True

        # 消費済みと「コンテンツフィルタに拒否された」を同じ扱いにする。
        # どちらも失うと同じ記事が選び直され、前者は二重投稿、後者は毎日
        # 同じ理由で失敗する生成に化ける。キーが衝突しうる（どちらも
        # "video" を持つ）ので dict をマージせず値だけを並べる。
        recorded = [*article.consumed.values(), *article.content_filtered.values()]
        if not recorded:
            return False

        # 時刻が読めないなら残す。判断できないときに落とす方向へ倒すと、
        # 二重投稿という取り返しのつかない方の失敗に寄る。
        stamps = []
        for value in recorded:
            try:
                at = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return True
            stamps.append(at if at.tzinfo else at.replace(tzinfo=UTC))
        if not stamps:
            return True

        return datetime.now(UTC) - max(stamps) < CONSUMED_RETENTION

    def get_articles_by_category(
        self, category: NewsCategory, include_dismissed: bool = False
    ) -> list[NewsArticle]:
        """カテゴリの記事を取得する。

        外した記事（`dismissed`）は既定で返さない。畳めない一覧では、
        題材の合わない記事を毎回読み飛ばすことになる。

        **自動生成の記事選択（`pick_unconsumed`）もここを通る。**
        つまり外した記事は自動生成の候補にも入らない。これは意図した挙動で、
        人が「題材が合わない」と判断した記事から毎朝の動画が作られるのは
        筋が違う。副産物として、コンテンツフィルタに拒否されるような記事を
        **人が手で止められる逃げ道**になっている（`set_dismissed` の
        「使わないと決めた記事が生成の対象に残っていると、押した操作と
        画面の状態が食い違う」と整合する）。

        以前ここには「`pick_unconsumed` はここを通らない。あちらは
        `_load_category` を直接読む」と書いてあったが、**それは書かれた時点
        から誤り**だった（`pick_unconsumed` は導入時から一貫してこのメソッド
        経由）。確信を持って書かれた誤ったコメントの実例として残しておく。

        引き換えに、記事を外しすぎると自動生成の候補が枯れる。ただし
        `_must_survive_refetch` は `dismissed` だけの記事を保持しないので、
        フィードから流れれば記事プールから抜け、詰まり続けることはない。

        Args:
            category: ニュースカテゴリ
            include_dismissed: 外した記事も含めるか

        Returns:
            List[NewsArticle]: 記事のリスト（公開日時の降順）
        """
        articles = self._load_category(category)
        if not include_dismissed:
            articles = [a for a in articles if not a.dismissed]
        return sorted(articles, key=_sort_key, reverse=True)

    def set_dismissed(self, article_id: str, dismissed: bool) -> NewsArticle | None:
        """記事を一覧から外す / 戻す。

        外すときは選択も外す。「使わない」と決めた記事が生成の対象に
        残っていると、押した操作と画面の状態が食い違う。

        Args:
            article_id: 記事ID
            dismissed: 外すなら True、戻すなら False

        Returns:
            NewsArticle | None: 更新後の記事。見つからなければ None
        """

        def apply(article: NewsArticle) -> None:
            article.dismissed = dismissed
            if dismissed:
                article.is_selected = False

        return self._update_article(article_id, apply)

    def clear_all_selections(self) -> int:
        """選択をすべて外す。

        1件ずつ解除するしかない状態だと、選び直すたびに選択の数だけ操作が
        要る。カテゴリごとに read-modify-write を1回で済ませる
        （`clear_selection` を件数ぶん呼ぶと保存も件数ぶん走る）。

        Returns:
            int: 解除した件数
        """
        cleared = 0
        for category in NewsCategory:
            with self._category_lock(category):
                articles = self._load_category(category)
                selected = [a for a in articles if a.is_selected]
                if not selected:
                    continue
                for article in selected:
                    article.is_selected = False
                cleared += len(selected)
                self._save_category(category, articles)
        return cleared

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

    def mark_content_filtered(self, article_id: str, channel: str = CHANNEL_VIDEO) -> bool:
        """記事をそのチャネルで恒久的に使えないとマークする。

        Azure OpenAI のコンテンツフィルタが記事の題材を拒否したときに呼ぶ。
        以降 `pick_unconsumed` はこの記事を返さないので、毎日同じ記事で
        同じ理由の失敗を繰り返さなくなる。

        **選択は外さない**（`mark_as_generated` との違い）。人が選んだ記事が
        拒否されたなら、その事実は画面に残っている方がよい。

        既定を CHANNEL_VIDEO にしてあるのは、`PipelineJobRunner` が要求する
        Protocol を `mark_content_filtered(article_id)` の形に保つため。
        X へ広げるときは呼び出し側が channel を渡すだけでよい。

        Args:
            article_id: 記事ID
            channel: CHANNEL_VIDEO / CHANNEL_X

        Returns:
            bool: 記事が見つかって更新できたか
        """

        def mark(article: NewsArticle) -> None:
            article.mark_content_filtered(channel)

        return self._update_article(article_id, mark) is not None

    def _iter_unconsumed(self, channel: str) -> Iterator[NewsArticle]:
        """そのチャネルでまだ使える記事を、新しい順に返す。

        **見るのは `AUTO_SOURCE_CATEGORIES` だけ**（動画と X で共通）。
        外した記事（`dismissed`）は `get_articles_by_category` が既定で
        落とすので、ここにも出てこない。

        絞り込みをこのメソッド1つに置いている理由は、**画面に出す在庫の
        件数（`count_unconsumed`）と、実際に選ばれる記事（`pick_unconsumed`）が
        同じ条件で決まらなければならない**こと。条件を写すと、片方だけが
        変わったときに「在庫はあると出ているのに毎朝の生成が記事を見つけ
        られない」という、画面からは原因の分からない食い違いになる。
        """
        seen: set[str] = set()

        for category in AUTO_SOURCE_CATEGORIES:
            for article in self.get_articles_by_category(category):
                if (
                    article.is_consumed_by(channel)
                    # コンテンツフィルタに拒否された記事を毎日選び直さない。
                    # 拒否は記事の題材が原因の恒久的な失敗なので、翌日
                    # 同じ記事で同じ理由の失敗を繰り返すだけになる。
                    or article.is_content_filtered_for(channel)
                    or article.id in seen
                ):
                    continue
                seen.add(article.id)
                yield article

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        """そのチャネルでまだ使っていない記事を選ぶ。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X
            needed: 必要な件数

        Returns:
            list[NewsArticle]: 選んだ記事（足りなければ少なく返す）
        """
        picked: list[NewsArticle] = []
        for article in self._iter_unconsumed(channel):
            if len(picked) >= needed:
                break
            picked.append(article)
        return picked

    def count_unconsumed(self, channel: str) -> int:
        """そのチャネルでまだ使える記事の件数を返す。

        画面に「在庫が何日ぶんあるか」を出すために要る。**枯渇に気付く
        手段がこれしかない**——外した記事も消費済みの記事も一覧には残るので、
        件数を見ないと「記事は並んでいるのに自動生成が記事を見つけられない」
        状態に気付けない。

        Args:
            channel: CHANNEL_VIDEO / CHANNEL_X

        Returns:
            int: まだ使える記事の件数
        """
        return sum(1 for _ in self._iter_unconsumed(channel))

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
