"""ニュースストアの並行更新の検証。

守っている欠陥
--------------
記事は `data/news/{category}.json` に保存され、選択状態と生成済みフラグを
書き換える。更新は read-modify-write（読み込み → 変更 → 全件保存）。

動画生成は Starlette の threadpool（イベントループ外のスレッド）で走り、
その中で `mark_as_generated` がファイルを書き換える。同時にイベントループ側は
`toggle_selection` を処理しうる。排他しないと、一方が読み込んでから
書き戻すまでの間にもう一方の更新が挟まり、その更新が失われる。
利用者から見ると「選択したのに消えた」「生成済みにならない」になる。
"""

import threading
from pathlib import Path

import pytest

from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator


@pytest.fixture
def aggregator(tmp_path: Path) -> NewsAggregator:
    return NewsAggregator(tmp_path / "news")


def _seed(aggregator: NewsAggregator, count: int) -> list[NewsArticle]:
    """テスト用の記事を保存する。"""
    articles = [
        NewsArticle(
            id=f"article-{i:03d}",
            title=f"記事{i}",
            url=f"https://example.com/{i}",
            source="テスト",
            category=NewsCategory.AI,
        )
        for i in range(count)
    ]
    aggregator._save_category(NewsCategory.AI, articles)
    return articles


def test_concurrent_updates_do_not_lose_writes(aggregator: NewsAggregator) -> None:
    """並行して別々の記事を更新しても、更新が失われないこと。

    ロックが無いと、後から保存した側が相手の変更を含まない
    古いリストで上書きするため、更新が消える。
    """
    count = 40
    _seed(aggregator, count)

    def select_first_half() -> None:
        for i in range(count // 2):
            aggregator.toggle_selection(f"article-{i:03d}")

    def mark_second_half() -> None:
        for i in range(count // 2, count):
            aggregator.mark_as_generated(f"article-{i:03d}")

    threads = [
        threading.Thread(target=select_first_half),
        threading.Thread(target=mark_second_half),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    articles = {a.id: a for a in aggregator._load_category(NewsCategory.AI)}
    assert len(articles) == count

    lost_selections = [i for i in range(count // 2) if not articles[f"article-{i:03d}"].is_selected]
    lost_marks = [
        i for i in range(count // 2, count) if not articles[f"article-{i:03d}"].video_generated
    ]
    assert not lost_selections, f"選択が失われた記事: {lost_selections}"
    assert not lost_marks, f"生成済みフラグが失われた記事: {lost_marks}"


def test_many_threads_updating_the_same_category(aggregator: NewsAggregator) -> None:
    """多数のスレッドが同じカテゴリを更新しても全件反映されること。"""
    count = 60
    _seed(aggregator, count)

    def worker(start: int, step: int) -> None:
        for i in range(start, count, step):
            aggregator.mark_as_generated(f"article-{i:03d}")

    threads = [threading.Thread(target=worker, args=(n, 4)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    articles = aggregator._load_category(NewsCategory.AI)
    not_marked = [a.id for a in articles if not a.video_generated]
    assert not not_marked, f"生成済みにならなかった記事: {not_marked}"


def test_file_stays_valid_json_under_concurrent_writes(aggregator: NewsAggregator) -> None:
    """並行書き込み中もファイルが常に正しい JSON であること。

    一時ファイルへ書いてから os.replace で置換しているので、
    途中の状態が観測されない。直接上書きだと、読み手が
    書きかけのファイルを読んで全記事を失う。
    """
    count = 30
    _seed(aggregator, count)
    stop = threading.Event()
    corrupted: list[str] = []

    def writer() -> None:
        for i in range(count):
            aggregator.toggle_selection(f"article-{i:03d}")
        stop.set()

    def reader() -> None:
        # アプリと同じ経路（_load_category）で読む。
        # 直接 read_text すると、Windows では置換の瞬間に
        # PermissionError を受けることがある（実測）。
        # _load_category はロックを取るのでそれが起きない。
        while not stop.is_set():
            try:
                articles = aggregator._load_category(NewsCategory.AI)
            except Exception as e:
                corrupted.append(f"{type(e).__name__}: {e}")
                return
            if len(articles) != count:
                corrupted.append(f"記事数が {len(articles)} 件になった（期待 {count}）")
                return

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not corrupted, corrupted[:3]


def test_no_temp_files_are_left_behind(aggregator: NewsAggregator) -> None:
    """原子的書き込みの一時ファイルが残らないこと。"""
    _seed(aggregator, 5)
    aggregator.toggle_selection("article-000")
    leftovers = list(aggregator.data_dir.glob("*.tmp"))
    assert not leftovers, f"一時ファイルが残っている: {leftovers}"


# --------------------------------------------------------------------------
# 更新の意味
# --------------------------------------------------------------------------


def test_toggle_flips_and_returns_the_new_state(aggregator: NewsAggregator) -> None:
    _seed(aggregator, 3)
    assert aggregator.toggle_selection("article-000") is True
    assert aggregator.toggle_selection("article-000") is False


def test_toggle_returns_none_for_unknown_article(aggregator: NewsAggregator) -> None:
    _seed(aggregator, 3)
    assert aggregator.toggle_selection("does-not-exist") is None


def test_clear_selection_deselects(aggregator: NewsAggregator) -> None:
    _seed(aggregator, 3)
    aggregator.toggle_selection("article-001")
    assert aggregator.clear_selection("article-001") is True
    assert aggregator.get_selected_articles() == []


def test_clear_selection_reports_missing_article(aggregator: NewsAggregator) -> None:
    _seed(aggregator, 3)
    assert aggregator.clear_selection("does-not-exist") is False


def test_mark_as_generated_also_deselects(aggregator: NewsAggregator) -> None:
    """生成済みにしたら選択から外すこと。

    外さないと、次の生成でも同じ記事が対象に残る。
    """
    _seed(aggregator, 3)
    aggregator.toggle_selection("article-002")
    assert aggregator.mark_as_generated("article-002") is True

    article = next(a for a in aggregator._load_category(NewsCategory.AI) if a.id == "article-002")
    assert article.video_generated is True
    assert article.is_selected is False
    assert aggregator.get_selected_articles() == []


def test_merge_preserves_user_state(aggregator: NewsAggregator) -> None:
    """再取得で選択状態・生成済みフラグ・本文を失わないこと。"""
    existing = NewsArticle(
        id="article-000",
        title="古いタイトル",
        url="https://example.com/0",
        source="テスト",
        category=NewsCategory.AI,
        content="すでに取得した本文",
        thumbnail_url="https://example.com/thumb.jpg",
        is_selected=True,
    )
    existing.mark_consumed(CHANNEL_VIDEO)
    fetched = NewsArticle(
        id="article-000",
        title="新しいタイトル",
        url="https://example.com/0",
        source="テスト",
        category=NewsCategory.AI,
    )

    merged = NewsAggregator._merge_preserving_state([fetched], {existing.id: existing})

    assert len(merged) == 1
    assert merged[0].title == "新しいタイトル"  # 記事側は更新される
    assert merged[0].is_selected is True
    assert merged[0].video_generated is True
    assert merged[0].content == "すでに取得した本文"
    assert merged[0].thumbnail_url == "https://example.com/thumb.jpg"


def test_merge_leaves_new_articles_untouched(aggregator: NewsAggregator) -> None:
    fetched = NewsArticle(
        id="new-article",
        title="新記事",
        url="https://example.com/new",
        source="テスト",
        category=NewsCategory.AI,
    )
    merged = NewsAggregator._merge_preserving_state([fetched], {})
    assert merged[0].is_selected is False
    assert merged[0].video_generated is False
