"""記事がどこまで届いたか、記事プールが共通の在庫であることが画面から読めること。

**`動画済` と `X済` は同じ形で並んでいたが、意味が違った。** `X済` は投稿が
成功した後に立つので「公開した」だが、`動画済` は動画ファイルを作った時点で
立ち、YouTube に出したかはどこにも記録が無かった（Issue #46）。ここでは
到達段が段として読めること、そして**カードを描くどの応答でもバッジが出る**
ことを固定する。後者が要るのは、集合を渡し忘れた経路だけバッジが消えるという
形の欠陥がテンプレート側で例外にならない（`default([])`）ため。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.news import CHANNEL_VIDEO, CHANNEL_X, NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator
from src.storage.publications import CHANNEL_YOUTUBE, PublicationStore
from src.web import routes
from src.web.dependencies import (
    get_aggregator,
    get_config,
    get_posts,
    get_publications,
    get_x_switch,
)
from tests.conftest import FakePostQueue

# 在庫の日数が「設定から導出されている」ことを見るための値。
# 動画は1日1件、X は1日4件。記事8件なら動画は8日ぶん・X は2日ぶんで、
# 先に枯れる X が日数を決める。
ARTICLE_COUNT = 8


class FakeConfig:
    """在庫の計算に使う設定だけを持つ。

    クラス属性にしているのは、テストの中から値を書き換えて「日数が設定から
    導出されている」ことを確かめるため（`test_設定を変えると日数が変わる`）。
    """

    schedule_articles_per_format: ClassVar[int] = 1
    schedule_formats: ClassVar[list[str]] = ["short"]
    x_posts_per_day: ClassVar[int] = 4
    schedule_timezone: ClassVar[str] = "Asia/Tokyo"


class FakeSwitch:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title=f"記事 {suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
        summary="要約",
    )


@pytest.fixture
def articles() -> list[NewsArticle]:
    return [_article(str(i)) for i in range(ARTICLE_COUNT)]


@pytest.fixture
def aggregator(tmp_path: Path, articles: list[NewsArticle]) -> NewsAggregator:
    store = NewsAggregator(data_dir=tmp_path)
    store._save_category(NewsCategory.AI, articles)
    return store


@pytest.fixture
def switch() -> FakeSwitch:
    return FakeSwitch()


@pytest.fixture
def client(
    aggregator: NewsAggregator,
    publications: PublicationStore,
    post_queue: FakePostQueue,
    switch: FakeSwitch,
) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_aggregator] = lambda: aggregator
    app.dependency_overrides[get_publications] = lambda: publications
    app.dependency_overrides[get_posts] = lambda: post_queue
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    app.dependency_overrides[get_x_switch] = lambda: switch
    with TestClient(app) as test_client:
        yield test_client


def _card(body: str, article_id: str) -> str:
    """その記事のカードの断片だけを取り出す。

    一覧には他の記事のカードも並ぶので、本文全体で `in` を見ると別の記事の
    バッジを自分のものと誤認する。
    """
    start = body.index(f'id="article-{article_id}"')
    end = body.find('id="article-', start + 10)
    return body[start:] if end == -1 else body[start:end]


# --------------------------------------------------------------------------
# 到達段
# --------------------------------------------------------------------------


def test_作っただけの動画は生成と出る(
    client: TestClient, aggregator: NewsAggregator, articles: list[NewsArticle]
) -> None:
    """`consumed[video]` は「作った」でしかない。**公開したとは言わせない。**"""
    aggregator.mark_as_generated(articles[0].id)

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "動画:生成" in card
    assert "動画:公開" not in card


def test_公開まで届いた動画は公開と出る(
    client: TestClient,
    aggregator: NewsAggregator,
    publications: PublicationStore,
    articles: list[NewsArticle],
) -> None:
    aggregator.mark_as_generated(articles[0].id)
    publications.record(
        "videos/a_ja.mp4",
        CHANNEL_YOUTUBE,
        external_id="abc",
        url="https://www.youtube.com/watch?v=abc",
        article_id=articles[0].id,
    )

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "動画:公開" in card
    # **最も進んだ段だけを出す。** 2つ並べるとカードが高くなり、
    # 一度に見える件数が減る。
    assert "動画:生成" not in card


def test_公開の記録は他の記事に漏れない(
    client: TestClient,
    aggregator: NewsAggregator,
    publications: PublicationStore,
    articles: list[NewsArticle],
) -> None:
    publications.record(
        "videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="abc", url="u", article_id=articles[0].id
    )
    aggregator.mark_as_generated(articles[1].id)

    body = client.get("/news/ai").text

    assert "動画:公開" in _card(body, articles[0].id)
    assert "動画:生成" in _card(body, articles[1].id)


def test_Xに投稿した記事は投稿と出る(
    client: TestClient, aggregator: NewsAggregator, articles: list[NewsArticle]
) -> None:
    aggregator.mark_consumed(articles[0].id, CHANNEL_X)

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "X:投稿" in card


def test_キューにある記事は予定と出る(
    client: TestClient, post_queue: FakePostQueue, articles: list[NewsArticle]
) -> None:
    """下書きは記事データではなく投稿表にある。"""
    post_queue.article_ids = [articles[0].id]

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "X:予定" in card
    assert "X:投稿" not in card


def test_投稿済みならキューに残っていても投稿と出る(
    client: TestClient,
    aggregator: NewsAggregator,
    post_queue: FakePostQueue,
    articles: list[NewsArticle],
) -> None:
    """スレッドの途中が残っている場合など、両方立つ状態は起こりうる。"""
    aggregator.mark_consumed(articles[0].id, CHANNEL_X)
    post_queue.article_ids = [articles[0].id]

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "X:投稿" in card
    assert "X:予定" not in card


def test_何も届いていない記事にはバッジが出ない(
    client: TestClient, articles: list[NewsArticle]
) -> None:
    card = _card(client.get("/news/ai").text, articles[0].id)

    for word in ("動画:生成", "動画:公開", "X:予定", "X:投稿"):
        assert word not in card


# --------------------------------------------------------------------------
# カードを描くどの応答でもバッジが出る（渡し忘れの検出）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path_template", ["/news/{id}/toggle", "/news/{id}/restore"])
def test_カード単体の応答でもバッジが出る(
    client: TestClient,
    aggregator: NewsAggregator,
    publications: PublicationStore,
    articles: list[NewsArticle],
    path_template: str,
) -> None:
    """**この検査が本題。** 集合を渡し忘れた経路だけバッジが消える。

    テンプレートは `default([])` で落ちないので、渡し忘れても 500 にならず、
    その経路を通ったときだけ静かにバッジが消える（押した場所が変わらない
    UI と同じ形の欠陥）。
    """
    publications.record(
        "videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="abc", url="u", article_id=articles[0].id
    )

    body = client.post(path_template.format(id=articles[0].id)).text

    assert "動画:公開" in body


def test_一覧を作り直す応答でもバッジが出る(
    client: TestClient, publications: PublicationStore, articles: list[NewsArticle]
) -> None:
    """「すべて解除」は一覧を作り直す経路。"""
    publications.record(
        "videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="abc", url="u", article_id=articles[0].id
    )

    body = client.post("/selected/clear", data={"category": "ai"}).text

    assert "動画:公開" in _card(body, articles[0].id)


def test_選択パネルから解除した応答でもバッジが出る(
    client: TestClient,
    aggregator: NewsAggregator,
    publications: PublicationStore,
    articles: list[NewsArticle],
) -> None:
    aggregator.toggle_selection(articles[0].id)
    publications.record(
        "videos/a_ja.mp4", CHANNEL_YOUTUBE, external_id="abc", url="u", article_id=articles[0].id
    )

    body = client.delete(f"/news/{articles[0].id}/remove").text

    assert "動画:公開" in body


# --------------------------------------------------------------------------
# 記事プールが共通の在庫であること
# --------------------------------------------------------------------------


def test_共通の在庫であることが書かれている(client: TestClient) -> None:
    body = client.get("/").text

    assert "動画と X の共通の在庫" in body


def test_在庫の日数は設定から導出される(client: TestClient) -> None:
    """**画面に数字を焼き付けない。** 設定を変えたら日数も変わる。

    記事8件・動画1日1件・X 1日4件なら、先に枯れる X が日数を決めて2日。
    """
    body = client.get("/").text

    assert "約2日" in body
    assert f"{ARTICLE_COUNT}</span>件" in body


def test_設定を変えると日数が変わる(client: TestClient) -> None:
    """導出であることを、値を動かして確かめる（定数を読むだけでは足りない）。"""
    FakeConfig.x_posts_per_day = 2
    try:
        body = client.get("/").text
        assert "約4日" in body
    finally:
        FakeConfig.x_posts_per_day = 4


def test_Xが止まっていれば日数に入らない(client: TestClient, switch: FakeSwitch) -> None:
    """止まっている間は `plan_daily_posts` が下書きを作らず記事も減らない。

    入れると「あと2日」と出ているのに実際は動画だけ何日も回る嘘になる。
    """
    switch.enabled = False

    body = client.get("/").text

    assert "約8日" in body
    assert "X は停止中" in body


def test_消費済みの記事は在庫に数えない(
    client: TestClient, aggregator: NewsAggregator, articles: list[NewsArticle]
) -> None:
    """在庫は `pick_unconsumed` と同じ条件で数える（`count_unconsumed`）。"""
    for article in articles[:4]:
        aggregator.mark_consumed(article.id, CHANNEL_X)

    body = client.get("/").text

    # X の未消費は4件になり、1日4件なので1日ぶん。
    assert "約1日" in body


def test_外した記事は在庫に数えない(
    client: TestClient, aggregator: NewsAggregator, articles: list[NewsArticle]
) -> None:
    """人が外した記事は自動生成の候補に入らない（＝在庫でもない）。

    ここが食い違うと「在庫はあると出ているのに毎朝の生成が記事を
    見つけられない」という、画面から原因の分からない状態になる。
    """
    for article in articles[:6]:
        aggregator.set_dismissed(article.id, True)

    body = client.get("/").text

    # 残りは2件。X は1日4件なので0日ぶん。
    assert "約0日" in body


def test_自動生成の対象カテゴリに印が付く(client: TestClient) -> None:
    """タブが9つあるのに自動生成は AI だけ、という状態を画面から読めるようにする。"""
    body = client.get("/").text

    assert "自動生成（毎朝の動画と X の投稿）が記事を選ぶのはこのカテゴリだけ" in body
    # 印は1つだけ（`AUTO_SOURCE_CATEGORIES` は AI のみ）。
    assert body.count("自動生成（毎朝の動画と X の投稿）が記事を選ぶのはこのカテゴリだけ") == 1


def test_消費の記録はチャネルごとに独立している(
    client: TestClient, aggregator: NewsAggregator, articles: list[NewsArticle]
) -> None:
    """同じ記事を動画で1本・X で1投稿、それぞれ1回ずつ使える。

    在庫の内訳を2つ出しているのはこれが理由（1つの数字にまとめると、
    片方だけ枯れている状態を表せない）。
    """
    aggregator.mark_consumed(articles[0].id, CHANNEL_VIDEO)

    card = _card(client.get("/news/ai").text, articles[0].id)

    assert "動画:生成" in card
    assert "X:投稿" not in card
