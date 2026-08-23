"""実物の PostgreSQL に対する検査（Issue #56 / #3）。

なぜフェイクでは足りないか
--------------------------
既存のリポジトリのテストは SQLite で走る。方言が違うと次の3つが実機でだけ
壊れうる。

1. **マイグレーション。** `migrations/env.py` は `render_as_batch=True` を
   渡し、各リビジョンは `op.batch_alter_table` を使う。SQLite の貧弱な
   ALTER TABLE を回避する仕組みで、PostgreSQL では素通りするはずだが、
   通ることを見ていない
2. **タイムゾーン。** SQLite は `DateTime(timezone=True)` でも tz を保存せず、
   `_as_utc` が naive に UTC を補っている。PostgreSQL は tz を保存するので
   あちらは素通りになる。**`scheduled_at <= now` の比較が落ちる箇所**なので
   実物で見る
3. **claim の排他。** `JobRepository._oldest_queued` は PostgreSQL のときだけ
   `SELECT ... FOR UPDATE SKIP LOCKED` を使う分岐を持つ。SQLite では
   その行が一度も実行されない

そして**共有 DB にした理由そのもの**が排他にある。デプロイ中は旧新2つの
レプリカが1〜2分同時に走る（ACA は activeRevisionsMode = Single なので、
新リビジョンが ready になるまで旧を落とさない）。両者が別々の SQLite を
見ていた間は、同じ行を両方が掴んで**同じ投稿が二度出る**余地があった。
「同時に掴もうとしても1つしか掴めない」ことは、ここで実物に対して確かめる。

走らせ方
--------
既定では skip する（`TEST_POSTGRES_URL` が無ければ）。

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=devpass --name pgtest postgres:17
    TEST_POSTGRES_URL="postgresql+psycopg://postgres:devpass@localhost:5432/postgres" \
      uv run pytest -m slow -k postgres
    docker rm -f pgtest

**パスワード付きの URL を使う。** 実機はパスワードを持たず Entra の
トークンで接続するが、その経路はローカルでは再現できない（`src/storage/db.py`
がパスワードのある URL には注入しない理由がこれ）。トークン注入の配線は
`tests/test_db_engine.py` が見張る。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.models.social import NewPost, PostKind, PostStatus, SocialPost
from src.storage.db import create_db_engine, create_session_factory
from src.storage.jobs import JobRepository
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository

pytestmark = pytest.mark.slow

# 同時に掴ませるワーカーの数。デプロイ中に並ぶのは2つ（旧リビジョンと
# 新リビジョン）だが、余裕を見て多めに走らせる。
CONTENDERS = 4


@pytest.fixture(scope="module")
def sessions() -> Iterator[sessionmaker[Session]]:
    """マイグレーションを当てたセッションファクトリ。

    モジュールに1回で足りる（各テストの冒頭で行を消す）。
    """
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL が無いので実物の PostgreSQL を検査しない")

    # ここが通ることが検査の1つ目。batch_alter_table を含むリビジョンが
    # PostgreSQL でも当たること。
    upgrade_to_head(url)
    engine = create_db_engine(url)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def posts(sessions: sessionmaker[Session]) -> SocialPostRepository:
    _truncate(sessions)
    return SocialPostRepository(sessions)


@pytest.fixture
def jobs(sessions: sessionmaker[Session]) -> JobRepository:
    _truncate(sessions)
    return JobRepository(sessions)


def _truncate(sessions: sessionmaker[Session]) -> None:
    """行だけ消す（alembic_version は残す）。"""
    with sessions() as session:
        session.execute(text("TRUNCATE social_posts, jobs"))
        session.commit()


def _post(position: int = 0, has_link: bool = False) -> NewPost:
    return NewPost(
        article_id=f"a-{uuid.uuid4().hex[:8]}",
        article_title="テスト記事",
        kind=PostKind.SINGLE,
        body="本文",
        has_link=has_link,
        position=position,
    )


def test_マイグレーションが当たり予定時刻がtz付きで戻る(posts: SocialPostRepository) -> None:
    """PostgreSQL は tz を保存するので `_as_utc` は素通りになる。

    naive で戻ると `scheduled_at <= now` の比較が
    `can't compare offset-naive and offset-aware datetimes` で落ちる。
    """
    at = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    posts.enqueue([_post()], {0: at})

    upcoming = posts.list_upcoming()

    assert len(upcoming) == 1
    assert upcoming[0].scheduled_at == at
    assert upcoming[0].scheduled_at is not None
    assert upcoming[0].scheduled_at.tzinfo is not None, "tz が落ちている"
    assert upcoming[0].created_at.tzinfo is not None


def test_同時に掴んでも投稿を掴めるのは1つだけ(posts: SocialPostRepository) -> None:
    """**この検査が共有 DB にした理由そのもの。**

    デプロイ中は旧新2つのレプリカが同時に走る。別々の SQLite を見ていた
    間は両方が同じ行を掴めたので、同じ投稿が二度出る余地があった。
    """
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    posts.enqueue([_post()], {0: now - timedelta(minutes=1)})
    barrier = threading.Barrier(CONTENDERS)

    def claim() -> SocialPost | None:
        barrier.wait(timeout=10)
        return posts.claim_due(now)

    with ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        claimed = [future.result() for future in [pool.submit(claim) for _ in range(CONTENDERS)]]

    winners = [post for post in claimed if post is not None]
    assert len(winners) == 1, f"{len(winners)}人が同じ投稿を掴んだ（二重投稿になる）"
    assert winners[0].status is PostStatus.POSTING


def test_同時に掴んでもジョブを掴めるのは1つだけ(jobs: JobRepository) -> None:
    """`SELECT ... FOR UPDATE SKIP LOCKED` の分岐を通る唯一の経路。

    SQLite ではこの行が一度も実行されない。
    """
    jobs.enqueue_batch([("a1", "テスト記事")], video_format="short")
    barrier = threading.Barrier(CONTENDERS)

    def claim(worker: int) -> object:
        barrier.wait(timeout=10)
        return jobs.claim_next(worker_id=f"w{worker}")

    with ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
        claimed = [
            future.result()
            for future in [pool.submit(claim, worker) for worker in range(CONTENDERS)]
        ]

    winners = [job for job in claimed if job is not None]
    assert len(winners) == 1, f"{len(winners)}人が同じジョブを掴んだ（動画が二重に作られる）"


def test_送信中で残った行はNEEDS_REVIEWに落ちる(posts: SocialPostRepository) -> None:
    """SCHEDULED に戻さないこと（届いていた場合に二度出る）。"""
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    posts.enqueue([_post()], {0: now})
    assert posts.claim_due(now) is not None

    assert posts.recover_stuck_posting("再起動で不明") == 1

    assert [post.status for post in posts.list_needs_review()] == [PostStatus.NEEDS_REVIEW]


def test_遅れすぎた行は捨てる(posts: SocialPostRepository) -> None:
    """復帰直後に連投しないための打ち切り。"""
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    posts.enqueue([_post()], {0: now - timedelta(hours=3)})

    assert posts.discard_stale(now, max_delay_minutes=60) == 1
    assert posts.list_upcoming() == []


def test_予算の元になる月次の件数を数えられる(posts: SocialPostRepository) -> None:
    """予算ガード（`is_over_budget`）の入力。

    以前はこの行がデプロイごとに消えていたので、月の実支出がいくらでも
    ガードがほぼ発火しなかった。**共有 DB にして初めて意味を持つ検査。**
    """
    posted_at = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
    for has_link in (True, True, False):
        posts.enqueue([_post(has_link=has_link)], {0: posted_at})
        claimed = posts.claim_due(posted_at)
        assert claimed is not None
        posts.mark_posted(claimed.id, tweet_id=f"t{claimed.id}", posted_at=posted_at)

    plain, with_link = posts.monthly_post_counts(2026, 8)

    assert (plain, with_link) == (1, 2)
    assert posts.monthly_post_counts(2026, 7) == (0, 0)
