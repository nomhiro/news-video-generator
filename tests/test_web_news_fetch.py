"""画面からのニュース取得（`POST /news/fetch`）。

**このルートは長らくテストが無かった。** 実ネットワークを叩くので
書きにくかったのだが、そのせいで `fetch_ai_news_and_store` の引数を
変えたときに気付ける経路が無かった（情報源を Google News の検索クエリから
フィードへ変えたときに実際に書き換えている）。呼び出しの形だけを
フェイクで確かめる。
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.news import NewsArticle, NewsCategory
from src.web import routes
from src.web.dependencies import get_aggregator, get_config


class FakeAggregator:
    """取得の呼び出しだけを記録する。ネットワークは叩かない。"""

    def __init__(self) -> None:
        self.ai_calls: list[dict[str, Any]] = []
        self.category_calls = 0

    async def fetch_and_store(self, limit_per_category: int = 10) -> dict[Any, Any]:
        self.category_calls += 1
        return {}

    async def fetch_ai_news_and_store(
        self, feeds: Any = None, limit_per_feed: int = 3
    ) -> list[NewsArticle]:
        self.ai_calls.append({"feeds": feeds, "limit_per_feed": limit_per_feed})
        return []

    def get_selected_count(self) -> int:
        return 0

    def get_articles_by_category(self, category: NewsCategory) -> list[NewsArticle]:
        return []


class FakeConfig:
    ai_news_limit_per_feed = 7


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_aggregator] = lambda: aggregator
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    return TestClient(app)


aggregator = FakeAggregator()


def test_画面からの取得はフィードの件数設定を渡す(client: TestClient) -> None:
    """`AI_NEWS_LIMIT_PER_FEED` が効かないと、画面からの取得だけ既定値になる。

    キーワード引数で渡していることも含めて見る（`feeds` を第1引数に
    持つシグネチャなので、位置引数で渡すとフィード一覧の位置に
    件数が入る）。
    """
    aggregator.ai_calls.clear()

    response = client.post("/news/fetch")

    assert response.status_code == 200
    assert aggregator.ai_calls == [{"feeds": None, "limit_per_feed": 7}]


def test_画面からの取得は通常カテゴリも取る(client: TestClient) -> None:
    """AI はフィード、それ以外は Google News。画面はどちらも見せる。"""
    before = aggregator.category_calls

    client.post("/news/fetch")

    assert aggregator.category_calls == before + 1
