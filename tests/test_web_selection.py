"""記事の選択が押した場所で見えることの検証。

2026-08-22 の実測では、選択ボタンを押してもカードのラベル（「+ 選択」）も
背景も変わらず、唯一の反映である件数のバッジはビューポートから 9,088px 下に
あった。押しても画面上で何も起きない状態だったので、ここで固定する。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.news import NewsArticle, NewsCategory
from src.news.aggregator import NewsAggregator
from src.web import routes
from src.web.dependencies import get_aggregator


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
def client(aggregator: NewsAggregator) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_aggregator] = lambda: aggregator
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
    assert "解除" in body
    assert "+ 選択" not in body


def test_選択の応答が件数と選択パネルも運ぶ(client: TestClient, aggregator: NewsAggregator) -> None:
    """押した場所とは別の場所（件数・生成ボタン）を out-of-band で更新する。"""
    body = client.post(f"/news/{_first_id(aggregator)}/toggle").text

    assert 'id="selected-panel" hx-swap-oob="innerHTML"' in body
    assert 'id="selected-badge" hx-swap-oob="innerHTML"' in body
    assert "1件" in body


def test_件数の更新に_script_を使わない(client: TestClient, aggregator: NewsAggregator) -> None:
    """フラグメントに script を混ぜると、更新の仕組みが隠れる。"""
    body = client.get("/selected").text

    assert "<script>" not in body


def test_もう一度押すと未選択に戻る(client: TestClient, aggregator: NewsAggregator) -> None:
    article_id = _first_id(aggregator)
    client.post(f"/news/{article_id}/toggle")

    body = client.post(f"/news/{article_id}/toggle").text

    assert "+ 選択" in body
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
    assert "+ 選択" in body


def test_知らない記事の選択は_404(client: TestClient) -> None:
    """静かに 200 を返すと、押しても何も起きない状態が再現する。"""
    assert client.post("/news/does-not-exist/toggle").status_code == 404
