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

from src.models.job import GenerationJob, JobStatus
from src.models.social import (
    CANCELLABLE_STATUSES,
    InvalidPostTransition,
    PostKind,
    PostStatus,
    SocialPost,
    check_post_transition,
)
from src.web import routes
from src.web.dependencies import (
    get_config,
    get_jobs,
    get_posts,
    get_token_store,
    get_x_switch,
)


def _job(
    job_id: int,
    batch_id: str,
    status: JobStatus,
    created_at: datetime,
    article_title: str = "動画記事",
) -> GenerationJob:
    return GenerationJob(
        id=job_id,
        batch_id=batch_id,
        article_id=f"a-{job_id}",
        article_title=article_title,
        video_format="short",
        language="ja",
        status=status,
        attempts=1,
        error_message=None,
        video_key=None,
        created_at=created_at,
        started_at=created_at,
        finished_at=None,
        worker_id="w",
        lease_expires_at=None,
    )


def _post(
    post_id: int,
    status: PostStatus,
    body: str,
    scheduled_at: datetime | None,
    posted_at: datetime | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
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
        created_at=created_at or datetime(2026, 8, 15, tzinfo=UTC),
    )


class FakeSocialPosts:
    """`SocialPostRepository` の読み取り面だけを差し替える。"""

    def __init__(
        self,
        upcoming: list[SocialPost],
        needs_review: list[SocialPost],
        settled: list[SocialPost] | None = None,
    ) -> None:
        self._upcoming = upcoming
        self._needs_review = needs_review
        # 一覧には出ないが id では引ける行（POSTED / FAILED）。
        # キューは最大30秒古い値を映すため、既に出た行の取り消しボタンが
        # 押されうる。その経路を再現するために持つ。
        self._settled = settled or []
        self.failed: list[tuple[int, str]] = []

    def list_upcoming(self, limit: int = 20) -> list[SocialPost]:
        return self._upcoming[:limit]

    def list_needs_review(self) -> list[SocialPost]:
        return self._needs_review

    def list_recent_failed(self, since: datetime) -> list[SocialPost]:
        return [p for p in self._settled if p.status is PostStatus.FAILED and p.created_at >= since]

    def list_posted_between(self, start: datetime, end: datetime) -> list[SocialPost]:
        return [
            p
            for p in self._upcoming + self._needs_review
            if p.posted_at is not None and start <= p.posted_at < end
        ]

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        return (3, 1)

    def cancel(self, post_id: int, reason: str) -> None:
        """**本物と同じ規律で拒否する。**

        以前このフェイクは状態を見ずに記録するだけで、決して例外を
        投げなかった。そのため「送信中の行を取り消せてしまう」という
        二重投稿の経路がテストを素通りした（フェイクが本物より甘いと、
        テストは実装の安全性を何も保証しない）。判定は本物と同じ
        `CANCELLABLE_STATUSES` / `check_post_transition` を使う。
        """
        found = next(
            (p for p in self._upcoming + self._needs_review + self._settled if p.id == post_id),
            None,
        )
        if found is None:
            return
        if found.status not in CANCELLABLE_STATUSES:
            raise InvalidPostTransition(f"{found.status} の投稿は取り消せません")
        check_post_transition(found.status, PostStatus.FAILED)
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
    """`JobRepository` の読み取り面だけを差し替える。

    帯は `list_jobs_between` で当日の全ジョブを取る（`latest_batch_id` +
    `list_batch` には依存しない）。同日に複数バッチが走っても、
    早いバッチのジョブが見えなくなってはいけない。
    """

    def __init__(self, jobs: list[GenerationJob] | None = None) -> None:
        self._jobs = jobs or []

    def list_jobs_between(self, start: datetime, end: datetime) -> list[GenerationJob]:
        return [j for j in self._jobs if start <= j.created_at < end]


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


def test_予算上限に達していれば停止中と出す(fake_switch: FakeSwitch) -> None:
    """上限に達している間、スイッチが有効でもワーカーは送信しない。

    スイッチの語（稼働中）だけを見せると「稼働中なのに出ない」ことの
    説明が画面から消える。
    """

    class OverBudgetPosts(FakeSocialPosts):
        def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
            return (0, 200)  # 200 * 0.20 = $40 > $20

    fake_switch.set_enabled(True)
    posts = OverBudgetPosts([], [])

    with _client_for(posts, fake_switch) as client:
        panel = client.get("/x/status").text
        header = client.get("/x/status/header").text

    assert "予算上限で停止中" in panel
    assert "予算上限で停止中" in header


def test_ヘッダーは状態と概算コストだけを出す(client: TestClient) -> None:
    """ヘッダーは一日中見えている場所なので、操作と手順の説明を置かない。

    停止ボタンと再認証の案内は本文パネルに置く（設計のワイヤーフレーム）。
    """
    response = client.get("/x/status/header")

    body = response.text
    assert "自動投稿" in body
    assert "概算" in body
    # 操作と手順はヘッダーには無い
    assert "/x/enabled" not in body
    assert "push_tokens" not in body


def test_状態パネルは_id_を持たない(client: TestClient) -> None:
    """同じ id が文書内に2つあると、htmx は最初の一致だけを入れ替える。

    以前はパネル自身が id="x-status" を持ち、本文の呼び出し側にも同じ
    id があった。そのためスイッチを切り替えても本文パネルが古い状態を
    映したまま残った（非常停止のスイッチで最悪の見え方）。
    id は呼び出し側のコンテナ（#x-status-panel）だけが持つ。
    """
    panel = client.get("/x/status").text

    assert 'id="x-status"' not in panel
    assert 'id="x-status-panel"' not in panel


def test_スイッチの切り替えは_ヘッダーも一緒に更新する(client: TestClient) -> None:
    """パネルだけが変わってヘッダーが古いままだと「効いていない」ように見える。

    ヘッダーは60秒ポーリングなので、放置すると最大1分ずれる。
    out-of-band swap で同じ応答の中でヘッダーも書き換える。
    """
    response = client.post("/x/enabled", data={"enabled": "true"})

    assert 'id="header-x-status"' in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "稼働中" in response.text


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


def _client_for(posts: FakeSocialPosts, switch: FakeSwitch) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_posts] = lambda: posts
    app.dependency_overrides[get_x_switch] = lambda: switch
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    app.dependency_overrides[get_jobs] = lambda: FakeJobs()
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore()
    return TestClient(app)


def test_出せなかった投稿が帯とキューに出る(fake_switch: FakeSwitch) -> None:
    """FAILED を返す一覧が1つも無かったため、失敗は画面から見えなかった。

    デプロイでアプリが数時間止まった後は `discard_stale` が4件まとめて
    見送る。運用者は空のキューを見て「今日はニュースが無かった」と
    解釈するしかなく、痕跡はログの1行だけだった。
    """
    now = datetime.now(UTC)
    dropped = _post(
        5,
        PostStatus.FAILED,
        "遅れすぎて見送られた本文です。",
        scheduled_at=now - timedelta(hours=3),
        error_message="予定時刻から60分以上遅れたため投稿しませんでした",
        created_at=now - timedelta(hours=4),
    )
    posts = FakeSocialPosts([], [], settled=[dropped])

    with _client_for(posts, fake_switch) as client:
        band = client.get("/x/band").text
        queue = client.get("/x/queue").text

    assert "出せなかった投稿 1件" in band
    assert "60分以上遅れたため" in band
    assert "出せなかった投稿 1件" in queue
    assert "60分以上遅れたため" in queue


def test_送信中の投稿は取り消せない(fake_switch: FakeSwitch) -> None:
    """取り消せると二重投稿になる。

    `POSTING -> FAILED` は遷移表では許されているので、状態を見ずに
    落とすと成功してしまう。その後ワーカーが `mark_posted` を呼んで
    `FAILED -> POSTED` で例外になり、`on_posted` が走らないので記事が
    消費済みにならない。**投稿は X に出たまま、翌日もう一度公開される。**
    """
    now = datetime.now(UTC)
    posting = _post(3, PostStatus.POSTING, "いま送信中の本文です。", scheduled_at=now)
    posts = FakeSocialPosts([posting], [])

    with _client_for(posts, fake_switch) as client:
        response = client.post("/x/posts/3/cancel")

    assert response.status_code == 200
    assert posts.failed == []  # 落ちていない
    assert "取り消せませんでした" in response.text
    # ボタン自体も出さない
    assert "送信中のため取り消せません" in response.text


def test_送信済みの投稿を取り消しても500にならない(fake_switch: FakeSwitch) -> None:
    """キューは最大30秒古い値を映すので、既に出た行のボタンが押されうる。

    500 を返すと htmx は何も入れ替えず、運用者には「押しても無反応な
    ボタン」に見える。キューを返して理由を伝える。
    """
    now = datetime.now(UTC)
    posted = _post(4, PostStatus.POSTED, "もう出た本文です。", scheduled_at=now, posted_at=now)
    posts = FakeSocialPosts([], [], settled=[posted])

    with _client_for(posts, fake_switch) as client:
        response = client.post("/x/posts/4/cancel")

    assert response.status_code == 200
    assert posts.failed == []
    assert "取り消せませんでした" in response.text


def test_帯には早いバッチの動画も含めて今日の全ジョブが出る(
    fake_posts: FakeSocialPosts, fake_switch: FakeSwitch
) -> None:
    """`latest_batch_id()` + `list_batch()` では直近1バッチしか見えない。

    同じ日に2バッチ走ったとき、早いバッチの動画が帯から消えると、
    「いま画像生成クォータを取り合っている」ことが画面から分からなくなる。
    2バッチぶんのジョブを渡し、両方が帯に出ることを確かめる。
    """
    # 現在時刻の近傍に固定する（日付境界をまたぐと today のフィルタで
    # 落ちてテストが不安定になるため、ずらす量は小さくする）。
    now = datetime.now(UTC)
    older_batch = _job(
        1, "batch-old", JobStatus.SUCCEEDED, now - timedelta(minutes=30), "早いバッチの記事"
    )
    newer_batch = _job(
        2, "batch-new", JobStatus.RUNNING, now - timedelta(minutes=5), "新しいバッチの記事"
    )

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_posts] = lambda: fake_posts
    app.dependency_overrides[get_x_switch] = lambda: fake_switch
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    app.dependency_overrides[get_jobs] = lambda: FakeJobs([older_batch, newer_batch])
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore()

    with TestClient(app) as client:
        body = client.get("/x/band").text

    assert "早いバッチの記事" in body
    assert "新しいバッチの記事" in body
