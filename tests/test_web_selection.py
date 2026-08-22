"""記事の選択が押した場所で見えることの検証。

2026-08-22 の実測では、選択ボタンを押してもカードのラベルも背景も変わらず、
唯一の反映である件数のバッジはビューポートから 9,088px 下にあった。
押しても画面上で何も起きない状態だったので、ここで固定する。

ラベルは「選択中」の有無で見る。押した状態の語だけを見れば、未選択の語
（「選択」）が選択中の語に部分一致することに引っかからない。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.news import NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator
from src.storage.publications import PublicationStore
from src.web import routes
from src.web.dependencies import get_aggregator, get_posts, get_publications
from tests.conftest import FakePostQueue


def _article(suffix: str, title: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title=title,
        url=url,
        source="Example",
        category=NewsCategory.AI,
        summary="要約",
    )


@pytest.fixture
def aggregator(tmp_path: Path) -> NewsAggregator:
    store = NewsAggregator(data_dir=tmp_path)
    store._save_category(
        NewsCategory.AI,
        [_article("a", "推論コストが10分の1になった"), _article("b", "混合専門家方式へ")],
    )
    return store


@pytest.fixture
def client(
    aggregator: NewsAggregator, publications: PublicationStore, post_queue: FakePostQueue
) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_aggregator] = lambda: aggregator
    # 記事カードは到達段（公開の記録・X のキュー）も見る。
    app.dependency_overrides[get_publications] = lambda: publications
    app.dependency_overrides[get_posts] = lambda: post_queue
    with TestClient(app) as test_client:
        yield test_client


def _first_id(aggregator: NewsAggregator) -> str:
    return aggregator.get_articles_by_category(NewsCategory.AI)[0].id


def test_カードには自分の_id_が付いている(client: TestClient, aggregator: NewsAggregator) -> None:
    """選択の応答でこのカードだけを差し替えるために必要。"""
    body = client.get("/news/ai").text

    for article in aggregator.get_articles_by_category(NewsCategory.AI):
        assert f'id="article-{article.id}"' in body


def test_選択するとカードが選択済みの見た目で返る(
    client: TestClient, aggregator: NewsAggregator
) -> None:
    article_id = _first_id(aggregator)

    body = client.post(f"/news/{article_id}/toggle").text

    assert f'id="article-{article_id}"' in body
    assert "選択中" in body


def test_選択の応答が件数と選択パネルも運ぶ(client: TestClient, aggregator: NewsAggregator) -> None:
    """押した場所とは別の場所（件数・生成ボタン）を out-of-band で更新する。"""
    body = client.post(f"/news/{_first_id(aggregator)}/toggle").text

    assert 'id="selected-panel" hx-swap-oob="innerHTML"' in body
    assert "1件" in body


def test_件数の更新に_script_を使わない(client: TestClient, aggregator: NewsAggregator) -> None:
    """フラグメントに script を混ぜると、更新の仕組みが隠れる。"""
    body = client.get("/selected").text

    assert "<script>" not in body


def test_もう一度押すと未選択に戻る(client: TestClient, aggregator: NewsAggregator) -> None:
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/toggle")

    body = client.post(f"/news/{article_id}/toggle").text

    assert "選択中" not in body
    assert "0件" in body


def test_選択パネルから解除するとカードも一緒に戻る(
    client: TestClient, aggregator: NewsAggregator
) -> None:
    """戻さないと、解除したのに一覧では「選択中」の見た目が残る。"""
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/toggle")

    body = client.delete(f"/news/{article_id}/remove").text

    assert f'id="article-{article_id}"' in body
    assert 'hx-swap-oob="true"' in body
    assert "選択中" not in body


def test_知らない記事の選択は_404(client: TestClient) -> None:
    """静かに 200 を返すと、押しても何も起きない状態が再現する。"""
    assert client.post("/news/does-not-exist/toggle").status_code == 404


# --------------------------------------------------------------------------
# 一覧の整理（外す / 戻す / すべて解除）
# --------------------------------------------------------------------------


def test_外すと取り消せる行に変わる(client: TestClient, aggregator: NewsAggregator) -> None:
    """消してしまうと押し間違いが戻せず、確認のダイアログが必要になる。"""
    article_id = _first_id(aggregator)

    body = client.post(f"/news/{article_id}/dismiss").text

    assert "一覧から外しました" in body
    assert f'hx-post="/news/{article_id}/restore"' in body


def test_外した記事は一覧に出ない(client: TestClient, aggregator: NewsAggregator) -> None:
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/dismiss")

    assert f'id="article-{article_id}"' not in client.get("/news/ai").text


def test_外すと件数も直る(client: TestClient, aggregator: NewsAggregator) -> None:
    """畳んだ直後に古い件数が残ると、画面の数と実際の数が食い違う。"""
    body = client.post(f"/news/{_first_id(aggregator)}/dismiss").text

    assert 'id="news-count" hx-swap-oob="innerHTML"' in body
    assert "1件" in body


def test_外すと選択も外れる(client: TestClient, aggregator: NewsAggregator) -> None:
    """「使わない」と決めた記事が生成の対象に残っていてはいけない。"""
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/toggle")

    client.post(f"/news/{article_id}/dismiss")

    assert aggregator.get_selected_articles() == []


def test_外した記録は再取得でも残る(client: TestClient, aggregator: NewsAggregator) -> None:
    """引き継がないと、フィードに載っている間は取得のたびに戻ってくる。"""
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/dismiss")

    stored = aggregator._load_category(NewsCategory.AI)
    # フィードが同じ記事を返した状況を作る（取得結果は dismissed を知らない）
    refetched = []
    for article in stored:
        payload = article.to_dict()
        payload["dismissed"] = False
        refetched.append(NewsArticle.from_dict(payload))

    merged = NewsAggregator._merge_preserving_state(refetched, {a.id: a for a in stored})

    assert next(a for a in merged if a.id == article_id).dismissed is True


def test_戻すとカードに戻る(client: TestClient, aggregator: NewsAggregator) -> None:
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/dismiss")

    body = client.post(f"/news/{article_id}/restore").text

    assert f'id="article-{article_id}"' in body
    assert "一覧から外しました" not in body
    assert f'id="article-{article_id}"' in client.get("/news/ai").text


def test_すべて解除で選択が空になる(client: TestClient, aggregator: NewsAggregator) -> None:
    for article in aggregator.get_articles_by_category(NewsCategory.AI):
        client.post(f"/news/{article.id}/toggle")
    assert len(aggregator.get_selected_articles()) == 2

    body = client.post("/selected/clear", data={"category": "ai"}).text

    assert aggregator.get_selected_articles() == []
    assert "0件" in body
    # 一覧を作り直すので、どのカードにも「選択中」は残らない
    assert "選択中" not in body


def test_すべて解除は表示中のカテゴリの一覧を返す(
    client: TestClient, aggregator: NewsAggregator
) -> None:
    """推測すると別のカテゴリの一覧に差し替わる。"""
    tech = _article("t", "テクノロジーの記事")
    tech.category = NewsCategory.TECHNOLOGY
    aggregator._save_category(NewsCategory.TECHNOLOGY, [tech])

    body = client.post("/selected/clear", data={"category": "technology"}).text

    assert "テクノロジーの記事" in body
    assert "推論コストが10分の1になった" not in body


def test_知らない記事を外すと_404(client: TestClient) -> None:
    assert client.post("/news/does-not-exist/dismiss").status_code == 404
    assert client.post("/news/does-not-exist/restore").status_code == 404


# --------------------------------------------------------------------------
# コンテンツフィルタに拒否された記事の表示（issue #30）
# --------------------------------------------------------------------------


def test_拒否された記事は対象外と分かる(client: TestClient, aggregator: NewsAggregator) -> None:
    """自動生成が二度と選ばない理由が画面から読めること。

    印が無いと「新しい記事なのにいつまでも使われない」に見え、原因を
    ログでしか追えない。
    """
    article_id = _first_id(aggregator)
    aggregator.mark_content_filtered(article_id)

    body = client.get("/news/ai").text

    assert "動画対象外" in body


def test_対象外はチャネルごとに出す(client: TestClient, aggregator: NewsAggregator) -> None:
    """動画で拒否された記事に「X対象外」と出さないこと。

    拒否はチャネル別に記録する（動画で使えなくても X ではまだ使える）。
    1つにまとめると、まだ X で使われている記事に「対象外」と出て嘘になる。
    """
    article_id = _first_id(aggregator)
    aggregator.mark_content_filtered(article_id)

    body = client.get("/news/ai").text

    assert "動画対象外" in body
    assert "X対象外" not in body


def test_拒否されていない記事には出さない(client: TestClient, aggregator: NewsAggregator) -> None:
    """印が無い記事に余計な語を出さないこと。"""
    body = client.get("/news/ai").text

    assert "対象外" not in body
