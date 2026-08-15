"""X 運用の画面。

以前の画面は「利用者が操作する」ことを前提にしていた（ボタンを押す・
記事を選ぶ）。X の自動投稿が入った今、運用者はこの画面を**操作しない**。
唯一の仕事は「これから出る本文を読んで、おかしければ気付く」こと。
そのため帯とキューは本文を畳まず、状態を色だけでなく語でも示す。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.social import PostKind, PostStatus, SocialPost
from src.web import routes
from src.web.dependencies import (
    get_config,
    get_jobs,
    get_posts,
    get_token_store,
    get_x_switch,
)


def _post(
    post_id: int,
    status: PostStatus,
    body: str,
    scheduled_at: datetime | None,
    posted_at: datetime | None = None,
    error_message: str | None = None,
) -> SocialPost:
    return SocialPost(
        id=post_id,
        group_id="g",
        position=0,
        article_id=f"a-{post_id}",
        article_title=f"記事{post_id}",
        kind=PostKind.SINGLE,
        body=body,
        weighted_length=236,
        has_link=False,
        image_key=None,
        status=status,
        scheduled_at=scheduled_at,
        posted_at=posted_at,
        tweet_id=None,
        reply_to_tweet_id=None,
        attempts=0,
        error_message=error_message,
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
    )


class FakeSocialPosts:
    """`SocialPostRepository` の読み取り面だけを差し替える。"""

    def __init__(self, upcoming: list[SocialPost], needs_review: list[SocialPost]) -> None:
        self._upcoming = upcoming
        self._needs_review = needs_review
        self.failed: list[tuple[int, str]] = []

    def list_upcoming(self, limit: int = 20) -> list[SocialPost]:
        return self._upcoming[:limit]

    def list_needs_review(self) -> list[SocialPost]:
        return self._needs_review

    def list_posted_between(self, start: datetime, end: datetime) -> list[SocialPost]:
        return [
            p
            for p in self._upcoming + self._needs_review
            if p.posted_at is not None and start <= p.posted_at < end
        ]

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        return (3, 1)

    def mark_failed(self, post_id: int, reason: str) -> None:
        self.failed.append((post_id, reason))
        # キューから消えることをテストで確かめるため、以後は空にする
        self._upcoming = [p for p in self._upcoming if p.id != post_id]
        self._needs_review = [p for p in self._needs_review if p.id != post_id]


class FakeSwitch:
    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value


class FakeJobs:
    """帯には今日の動画ジョブも並ぶが、この画面テストでは空でよい。"""

    def latest_batch_id(self) -> str | None:
        return None

    def list_batch(self, batch_id: str) -> list[object]:
        return []


class FakeConfig:
    schedule_timezone = "Asia/Tokyo"
    x_monthly_budget_usd = 20.0
    x_cost_per_post_usd = 0.015
    x_cost_per_post_with_link_usd = 0.20


class FakeTokenStore:
    """未認証（トークンが無い）状態を模す。"""

    def read(self, name: str) -> str | None:
        return None

    def write(self, name: str, payload: str) -> None:  # pragma: no cover - 未使用
        raise NotImplementedError

    def delete(self, name: str) -> None:  # pragma: no cover - 未使用
        raise NotImplementedError

    def exists(self, name: str) -> bool:
        return False


@pytest.fixture
def fake_posts() -> FakeSocialPosts:
    now = datetime.now(UTC)
    # 19:00 JST 予定の投稿（アサーション対象）
    scheduled = now.replace(hour=10, minute=0, second=0, microsecond=0)  # 19:00 JST
    upcoming = [
        _post(
            1,
            PostStatus.SCHEDULED,
            "本文がそのまま全部出ているはずの投稿本文です。",
            scheduled_at=scheduled,
        ),
    ]
    needs_review = [
        _post(
            2,
            PostStatus.NEEDS_REVIEW,
            "送信できたか分からないため確認が必要な投稿です。",
            scheduled_at=now - timedelta(hours=1),
            error_message="送信結果が不明です",
        ),
    ]
    return FakeSocialPosts(upcoming, needs_review)


@pytest.fixture
def fake_switch() -> FakeSwitch:
    return FakeSwitch(enabled=False)


@pytest.fixture
def client(fake_posts: FakeSocialPosts, fake_switch: FakeSwitch) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_posts] = lambda: fake_posts
    app.dependency_overrides[get_x_switch] = lambda: fake_switch
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    app.dependency_overrides[get_jobs] = lambda: FakeJobs()
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore()

    with TestClient(app) as test_client:
        yield test_client


def test_帯に予定と要確認が出る(client: TestClient, fake_posts: FakeSocialPosts) -> None:
    response = client.get("/x/band")

    assert response.status_code == 200
    body = response.text
    assert "19:00" in body
    # 状態は色だけでなく語でも示す
    assert "予約" in body
    assert "要確認" in body


def test_キューに本文が全文出る(client: TestClient, fake_posts: FakeSocialPosts) -> None:
    """畳むと誰も読まない。読んで気付くことが運用者の唯一の仕事。"""
    response = client.get("/x/queue")

    assert "本文がそのまま全部出ているはずの投稿本文です。" in response.text


def test_文字数が出る(client: TestClient, fake_posts: FakeSocialPosts) -> None:
    response = client.get("/x/queue")

    assert "/280" in response.text


def test_スイッチを画面から有効にできる(client: TestClient, fake_switch: FakeSwitch) -> None:
    response = client.post("/x/enabled", data={"enabled": "true"})

    assert response.status_code == 200
    assert fake_switch.is_enabled() is True


def test_概算コストに概算と明示する(client: TestClient, fake_posts: FakeSocialPosts) -> None:
    """実際の課金は X 側の集計なので、一致を保証できない。"""
    response = client.get("/x/status")

    assert "概算" in response.text


def test_未認証なら再認証の手順を案内する(client: TestClient) -> None:
    """画面から OAuth を完結させる経路は無い。ローカル認証とスクリプトの案内を出す。"""
    response = client.get("/x/status")

    assert "push_tokens" in response.text


def test_取り消すと結果が取り消しましたになる(
    client: TestClient, fake_posts: FakeSocialPosts
) -> None:
    """操作名を通す。ボタンが「取り消す」なら結果の文言も「取り消しました」。"""
    response = client.post("/x/posts/1/cancel")

    assert response.status_code == 200
    assert "取り消しました" in response.text
    assert fake_posts.failed == [(1, "取り消しました")]
