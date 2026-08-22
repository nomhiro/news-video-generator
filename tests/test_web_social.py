"""X 運用の画面。

以前の画面は「利用者が操作する」ことを前提にしていた（ボタンを押す・
記事を選ぶ）。X の自動投稿が入った今、運用者はこの画面を**操作しない**。
唯一の仕事は「これから出る本文を読んで、おかしければ気付く」こと。
そのため帯とキューは本文を畳まず、状態を色だけでなく語でも示す。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.models.job import GenerationJob, JobStatus
from src.models.social import (
    CANCELLABLE_STATUSES,
    InvalidPostTransition,
    PostKind,
    PostStatus,
    SocialPost,
    check_post_transition,
)
from src.storage.tokens import X_TOKEN
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
    group_id: str = "g",
    position: int = 0,
    kind: PostKind = PostKind.SINGLE,
    image_key: str | None = None,
) -> SocialPost:
    return SocialPost(
        id=post_id,
        group_id=group_id,
        position=position,
        article_id=f"a-{post_id}",
        article_title=f"記事{post_id}",
        kind=kind,
        body=body,
        weighted_length=236,
        has_link=False,
        image_key=image_key,
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

    def list_thread(self, post_id: int) -> list[SocialPost]:
        rows = self._upcoming + self._needs_review + self._settled
        found = next((p for p in rows if p.id == post_id), None)
        if found is None:
            return []
        return sorted((p for p in rows if p.group_id == found.group_id), key=lambda p: p.position)

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
    x_cost_per_read_usd = 0.005
    # 既定は「設定済み」。未設定を検証するテストだけが差し替える
    x_client_id = "client-id"
    x_client_secret = SecretStr("client-secret")


class UnconfiguredConfig(FakeConfig):
    """クライアント資格情報が Container App に渡っていない状態。

    実際に起きた（issue #28）。トークンだけ入れても更新（refresh）が
    Basic 認証で client_id / client_secret を要求するので投稿できない。
    """

    x_client_id = ""
    x_client_secret = SecretStr("")


class AuthenticatedTokenStore:
    """トークンが保存されている状態を模す。"""

    def read(self, name: str) -> str | None:
        if name != X_TOKEN:
            return None
        return json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            }
        )

    def write(self, name: str, payload: str) -> None:  # pragma: no cover - 未使用
        raise NotImplementedError

    def delete(self, name: str) -> None:  # pragma: no cover - 未使用
        raise NotImplementedError

    def exists(self, name: str) -> bool:
        return name == X_TOKEN


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


def test_キューの予定時刻は運用者のタイムゾーンで出る(
    client: TestClient, fake_posts: FakeSocialPosts
) -> None:
    """行は UTC を持っているので、変換しないと帯と9時間ずれた時刻が並ぶ。

    「何がいつ出るか」を読む場所で時刻が2種類あるのは、表示の粗さではなく
    誤読の原因になる（実際にローカルで確認して見つけた）。
    """
    utc_noon = datetime(2026, 8, 16, 3, 42, tzinfo=UTC)
    fake_posts._upcoming = [
        _post(1, PostStatus.SCHEDULED, "本文", scheduled_at=utc_noon),
    ]

    response = client.get("/x/queue")

    # Asia/Tokyo は UTC+9。03:42 UTC は 12:42 JST
    assert "12:42" in response.text
    assert "03:42" not in response.text


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


# --------------------------------------------------------------------------
# クライアント資格情報の未設定は「未認証」と別に見せる
#
# Container App に X_CLIENT_ID / X_CLIENT_SECRET を渡していない状態が実際に
# あった（issue #28）。画面は「未認証」としか言わず、案内は push_tokens
# だけだったので、**その手順を踏んでも直らない**ことが分からなかった。
# --------------------------------------------------------------------------


def test_キーが未設定なら不足している環境変数名を出す(fake_switch: FakeSwitch) -> None:
    """名前を出すのは、それがそのまま `azd env set` の引数になるから。"""
    with _client_for(FakeSocialPosts([], []), fake_switch, config=UnconfiguredConfig()) as client:
        panel = client.get("/x/status").text

    assert "X_CLIENT_ID" in panel
    assert "X_CLIENT_SECRET" in panel
    assert "azd provision" in panel
    # 入れ忘れるとイメージが巻き戻る。手順から落とさない
    assert "SERVICE_WEB_IMAGE_NAME" in panel


def test_片方だけ未設定ならその名前だけを出す(fake_switch: FakeSwitch) -> None:
    """揃っているものを直せと言うと、どこを直すのか分からなくなる。"""

    class OnlySecretMissing(FakeConfig):
        x_client_secret = SecretStr("")

    with _client_for(FakeSocialPosts([], []), fake_switch, config=OnlySecretMissing()) as client:
        panel = client.get("/x/status").text

    assert "X_CLIENT_SECRET" in panel
    assert "X_CLIENT_ID" not in panel


def test_未設定でもシークレットの値は画面に出ない(fake_switch: FakeSwitch) -> None:
    """真偽の判定にしか使わない。値を渡すと SecretStr の意味が無くなる。"""

    class LeakyConfig(FakeConfig):
        x_client_id = ""
        x_client_secret = SecretStr("super-secret-value")

    with _client_for(FakeSocialPosts([], []), fake_switch, config=LeakyConfig()) as client:
        panel = client.get("/x/status").text
        header = client.get("/x/status/header").text

    assert "super-secret-value" not in panel
    assert "super-secret-value" not in header


def test_トークンがあってもキーが無ければ認証済みと出さない(fake_switch: FakeSwitch) -> None:
    """issue #28 をトークンだけ直した状態。

    更新（refresh）は Basic 認証で client_id / client_secret を要求するので、
    トークンがあっても投稿は「要確認」に落ちる。ここで緑の「認証済み」を
    出すと、問題が画面から消える。
    """
    with _client_for(
        FakeSocialPosts([], []),
        fake_switch,
        config=UnconfiguredConfig(),
        tokens=AuthenticatedTokenStore(),
    ) as client:
        panel = client.get("/x/status").text
        header = client.get("/x/status/header").text

    assert "認証済み" not in panel
    assert "要確認" in panel
    assert "未設定" in header


def test_キーが揃っていれば未設定の表示は出ない(client: TestClient) -> None:
    """常に警告が出ている状態にすると、警告として働かなくなる。"""
    panel = client.get("/x/status").text
    header = client.get("/x/status/header").text

    assert "未設定" not in panel
    assert "未設定" not in header


def test_ヘッダーにキー未設定の手順は書かない(fake_switch: FakeSwitch) -> None:
    """ヘッダーは「いま動いているか」「いくら使ったか」に絞る（設計の方針）。"""
    with _client_for(FakeSocialPosts([], []), fake_switch, config=UnconfiguredConfig()) as client:
        header = client.get("/x/status/header").text

    assert "未設定" in header
    assert "azd" not in header


def test_取り消すと結果が取り消しましたになる(
    client: TestClient, fake_posts: FakeSocialPosts
) -> None:
    """操作名を通す。ボタンが「取り消す」なら結果の文言も「取り消しました」。"""
    response = client.post("/x/posts/1/cancel")

    assert response.status_code == 200
    assert "取り消しました" in response.text
    assert fake_posts.failed == [(1, "取り消しました")]


def _client_for(
    posts: FakeSocialPosts,
    switch: FakeSwitch,
    config: object | None = None,
    tokens: object | None = None,
) -> TestClient:
    resolved_config = config or FakeConfig()
    resolved_tokens = tokens or FakeTokenStore()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_posts] = lambda: posts
    app.dependency_overrides[get_x_switch] = lambda: switch
    app.dependency_overrides[get_config] = lambda: resolved_config
    app.dependency_overrides[get_jobs] = lambda: FakeJobs()
    app.dependency_overrides[get_token_store] = lambda: resolved_tokens
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


# --------------------------------------------------------------------------
# 投稿プレビュー
# --------------------------------------------------------------------------
#
# 「画像を作って Blob に上げたうえで添付せずに投稿する」事故は、実物の
# 投稿を見るまで気付けなかった（`fetch_image` を渡し忘れていた期間が
# 実際にある）。出る前に画面で見られることが、その気付きを前に倒す。


def _preview_client(posts: FakeSocialPosts) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_posts] = lambda: posts
    app.dependency_overrides[get_x_switch] = lambda: FakeSwitch()
    app.dependency_overrides[get_config] = lambda: FakeConfig()
    app.dependency_overrides[get_jobs] = lambda: FakeJobs()
    app.dependency_overrides[get_token_store] = lambda: FakeTokenStore()
    return TestClient(app)


def test_プレビューはスレッドを位置の順に並べる() -> None:
    scheduled = datetime.now(UTC)
    thread = [
        _post(11, PostStatus.SCHEDULED, "先頭の投稿", scheduled, group_id="t", position=0),
        _post(12, PostStatus.SCHEDULED, "続きの投稿", scheduled, group_id="t", position=1),
    ]
    # 位置の逆順で持たせる。並べ替えを実装が行っていることを確かめるため
    with _preview_client(FakeSocialPosts(list(reversed(thread)), [])) as client:
        body = client.get("/x/posts/12/preview").text
    assert body.index("先頭の投稿") < body.index("続きの投稿")


def test_プレビューは画像カードを画面に出す() -> None:
    """`image_key` があれば実物の画像を出すこと。

    キーの有無だけを文字で出しても「添付されるはず」の確認にしかならない。
    絵が出ることが、画像生成のクォータを使った結果の唯一の確認手段。
    """
    post = _post(
        13,
        PostStatus.SCHEDULED,
        "画像付きの投稿",
        datetime.now(UTC),
        image_key="social/cards/a-13.png",
    )
    with _preview_client(FakeSocialPosts([post], [])) as client:
        body = client.get("/x/posts/13/preview").text
    assert 'src="/artifacts/social/cards/a-13.png"' in body


def test_プレビューは本文のURLをリンクとカードにする() -> None:
    post = _post(
        14,
        PostStatus.SCHEDULED,
        "本文の後にリンクが付く https://www.example.com/news/1",
        datetime.now(UTC),
    )
    with _preview_client(FakeSocialPosts([post], [])) as client:
        body = client.get("/x/posts/14/preview").text
    assert 'href="https://www.example.com/news/1"' in body
    # X のリンクカードに出るのは媒体のドメイン。OG 情報は取りに行かない
    assert "example.com" in body


def test_プレビューは本文のHTMLをエスケープする() -> None:
    """本文は LLM の出力で、記事タイトルに `<` が実際に混じる。"""
    post = _post(15, PostStatus.SCHEDULED, "<script>alert(1)</script>", datetime.now(UTC))
    with _preview_client(FakeSocialPosts([post], [])) as client:
        body = client.get("/x/posts/15/preview").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_存在しない投稿のプレビューは画面に理由を出す() -> None:
    """404 にしない。

    htmx はエラー応答で対象を差し替えないので、モーダルには前の投稿の
    内容が残ったままになる。「別の投稿が出ている」のが最悪の見え方。
    """
    with _preview_client(FakeSocialPosts([], [])) as client:
        response = client.get("/x/posts/999/preview")
    assert response.status_code == 200
    assert "見つかりません" in response.text


def test_キューから投稿プレビューを開ける() -> None:
    post = _post(16, PostStatus.SCHEDULED, "予定されている投稿", datetime.now(UTC))
    with _preview_client(FakeSocialPosts([post], [])) as client:
        body = client.get("/x/queue").text
    assert "/x/posts/16/preview" in body


def test_キューは画像の有無を絵で示す() -> None:
    """一覧で画像の有無が分かること。

    キーが入っているのに添付されない事故が実際にあった。一覧に絵が
    出ていれば「作ったのに出ていない」を毎日見る場所で気付ける。
    """
    post = _post(
        17,
        PostStatus.SCHEDULED,
        "画像付きの投稿",
        datetime.now(UTC),
        image_key="social/cards/a-17.png",
    )
    with _preview_client(FakeSocialPosts([post], [])) as client:
        body = client.get("/x/queue").text
    assert 'src="/artifacts/social/cards/a-17.png"' in body
