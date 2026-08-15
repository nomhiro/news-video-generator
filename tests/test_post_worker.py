"""投稿ワーカーの挙動。実際の X は叩かない。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.jobs.post_worker import PostWorker, post_due_once
from src.models.social import NewPost, PostKind, PostStatus
from src.social.x_auth import XTokenExpiredError
from src.social.x_client import XSendUncertainError
from src.storage.db import create_db_engine, create_session_factory
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository


class FakeClient:
    """投稿を記録するだけのクライアント。"""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.posted: list[tuple[str, str | None]] = []
        self.uploaded: list[Path] = []
        self.closed = False
        self._fail_with = fail_with

    def create_post(self, text, reply_to=None, media_ids=None) -> str:
        if self._fail_with is not None:
            raise self._fail_with
        self.posted.append((text, reply_to))
        return f"tw{len(self.posted)}"

    def upload_media(self, path: Path) -> str:
        self.uploaded.append(path)
        return "media1"

    def fetch_metrics(self, tweet_ids):
        return {}

    def close(self) -> None:
        self.closed = True


class CountingFactory:
    """呼ばれた回数を数えるだけの client_factory。

    スイッチが無効なときにクライアントが**作られていない**ことを
    証明するのに使う（作ってしまうと httpx.Client の接続が漏れる）。
    """

    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.calls = 0

    def __call__(self) -> FakeClient:
        self.calls += 1
        return self._client


class FailingFactory:
    """常に `XTokenExpiredError` を投げる client_factory。

    未認証・失効状態を再現する（Task 7 の認証画面ができるまでの
    全デプロイの既定状態でもある）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> FakeClient:
        self.calls += 1
        raise XTokenExpiredError("未認証です")


class EnabledSwitch:
    def is_enabled(self) -> bool:
        return True


class DisabledSwitch:
    def is_enabled(self) -> bool:
        return False


@pytest.fixture
def repository(tmp_path: Path) -> SocialPostRepository:
    url = f"sqlite:///{(tmp_path / 'social.db').as_posix()}"
    upgrade_to_head(url)
    return SocialPostRepository(create_session_factory(create_db_engine(url)))


def _enqueue(repo: SocialPostRepository, when: datetime, count: int = 1) -> str:
    posts = [
        NewPost(
            article_id="a1",
            article_title="記事",
            kind=PostKind.THREAD if count > 1 else PostKind.SINGLE,
            body=f"本文{i}",
            has_link=False,
            position=i,
        )
        for i in range(count)
    ]
    return repo.enqueue(posts, dict.fromkeys(range(count), when))


def test_予定時刻を過ぎた投稿を出す(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient()

    assert post_due_once(repository, client, EnabledSwitch(), now=now) is True

    assert client.posted == [("本文0", None)]
    assert repository.list_upcoming() == []


def test_スイッチが無効なら_出さない(repository: SocialPostRepository) -> None:
    """暴走時に止める手段。行は残して、有効にしたら出せるようにする。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient()

    assert post_due_once(repository, client, DisabledSwitch(), now=now) is False

    assert client.posted == []
    upcoming = repository.list_upcoming()
    assert [p.status for p in upcoming] == [PostStatus.SCHEDULED]


def test_スレッドは_直前の投稿への返信になる(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now, count=3)
    client = FakeClient()

    for _ in range(3):
        post_due_once(repository, client, EnabledSwitch(), now=now)

    assert client.posted == [("本文0", None), ("本文1", "tw1"), ("本文2", "tw2")]


def test_送信結果が不明なら_NEEDS_REVIEW(repository: SocialPostRepository) -> None:
    """再送すると同じ内容が2つ並ぶ。取りこぼしのほうが安全。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient(fail_with=XSendUncertainError("timeout"))

    post_due_once(repository, client, EnabledSwitch(), now=now)

    reviewed = repository.list_needs_review()
    assert len(reviewed) == 1
    assert "timeout" in (reviewed[0].error_message or "")


def test_スレッドの途中で失敗したら_残りも_NEEDS_REVIEW(
    repository: SocialPostRepository,
) -> None:
    """半端なスレッドを、時間をおいて自動で続けると文脈が切れる。

    `post_due_once` は1件を掴む/1件しか出さない関数なので、3件の
    まとまりで「途中で失敗 -> 残りも NEEDS_REVIEW」を確認するには
    3回呼ぶ必要がある。1回目で position 0 を出し、2回目で position 1 が
    送信失敗、3回目で position 2 が「直前 (position 1) の tweet_id が無い」
    分岐に入って NEEDS_REVIEW になる。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now, count=3)

    post_due_once(repository, FakeClient(), EnabledSwitch(), now=now)
    post_due_once(
        repository,
        FakeClient(fail_with=XSendUncertainError("timeout")),
        EnabledSwitch(),
        now=now,
    )
    post_due_once(repository, FakeClient(), EnabledSwitch(), now=now)

    statuses = {p.position: p.status for p in repository.list_needs_review()}
    assert statuses == {1: PostStatus.NEEDS_REVIEW, 2: PostStatus.NEEDS_REVIEW}


def test_画像カードは_メディアを先に上げる(
    repository: SocialPostRepository, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    image = tmp_path / "card.png"
    image.write_bytes(b"png")
    repository.enqueue(
        [
            NewPost(
                article_id="a1",
                article_title="記事",
                kind=PostKind.CARD,
                body="本文",
                has_link=False,
                image_key="social/cards/card.png",
            )
        ],
        {0: now},
    )
    client = FakeClient()

    post_due_once(repository, client, EnabledSwitch(), now=now, fetch_image=lambda key: image)

    assert client.uploaded == [image]


def test_遅れすぎた投稿はワーカーの1周で見送られる(repository: SocialPostRepository) -> None:
    """止まっていたあと復帰した瞬間の連投を防ぐ。

    `claim_due` は予定時刻が最古のものから取るだけで、遅れの大きさを
    見ない。掃く（discard_stale）のを worker 側の責務にしているので、
    ここでは `PostWorker._run_one`（スレッドを起動しない1周ぶん）が
    掃いてから出すことを検証する。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    old = now - timedelta(hours=3)
    _enqueue(repository, old)
    client = FakeClient()
    worker = PostWorker(
        repository,
        client_factory=lambda: client,
        switch=EnabledSwitch(),
        max_post_delay_minutes=60,
    )

    posted = worker._run_one()

    assert posted is False  # 掃いただけで、出すものは無かった
    assert client.posted == []
    assert repository.list_upcoming() == []  # SCHEDULED のまま残っていない


def test_スイッチが無効ならクライアントを作らない(repository: SocialPostRepository) -> None:
    """無効な間、毎ポーリング httpx.Client を作って捨てると接続が漏れる。

    掴む前にスイッチを見て、無効なら `client_factory` に触らないことを
    確認する。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    factory = CountingFactory(FakeClient())
    worker = PostWorker(repository, client_factory=factory, switch=DisabledSwitch())

    posted = worker._run_one()

    assert posted is False
    assert factory.calls == 0


def test_送信できたクライアントは閉じられる(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient()
    worker = PostWorker(repository, client_factory=lambda: client, switch=EnabledSwitch())

    worker._run_one()

    assert client.posted == [("本文0", None)]
    assert client.closed is True


def test_送信が例外で終わってもクライアントは閉じられる(
    repository: SocialPostRepository,
) -> None:
    """finally で閉じないと、送信失敗のたびに接続が漏れる。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    client = FakeClient(fail_with=XSendUncertainError("timeout"))
    worker = PostWorker(repository, client_factory=lambda: client, switch=EnabledSwitch())

    worker._run_one()

    assert client.closed is True


def test_認証エラーは状態が変わるまで繰り返しログしない(
    repository: SocialPostRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未認証は珍しい異常ではない（再認証されるまで続く既定状態）。

    30秒ごとに同じ行をログへ出し続けると、本当に見るべきエラーが
    埋もれる。最初の1回だけ記録し、以降は状態が変わるまで黙る。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    _enqueue(repository, now)
    calls: list[str] = []
    monkeypatch.setattr("src.jobs.post_worker.log_error", lambda message: calls.append(message))
    factory = FailingFactory()
    worker = PostWorker(repository, client_factory=factory, switch=EnabledSwitch())

    for _ in range(3):
        worker._run_one()

    assert factory.calls == 3  # 毎回試すのは正しい（回復を見逃さないため）
    assert len(calls) == 1  # ログは1回だけ
