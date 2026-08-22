"""social_posts の読み書き。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.social import InvalidPostTransition, NewPost, PostKind, PostStatus
from src.storage.db import create_db_engine, create_session_factory
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository


@pytest.fixture
def repository(tmp_path: Path) -> SocialPostRepository:
    """既存 tests/test_jobs.py と同じ作り方。

    `create_all` ではなく `upgrade_to_head` を使う。マイグレーションを
    通しておかないと、Alembic の当て漏れをテストが検出できない。
    """
    url = f"sqlite:///{(tmp_path / 'social.db').as_posix()}"
    upgrade_to_head(url)
    return SocialPostRepository(create_session_factory(create_db_engine(url)))


def _post(position: int = 0, has_link: bool = False) -> NewPost:
    return NewPost(
        article_id="a1",
        article_title="テスト記事",
        kind=PostKind.SINGLE,
        body="本文",
        has_link=has_link,
        position=position,
    )


def test_claim_due_は_予定時刻を過ぎた行だけ返す(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=1)})
    repository.enqueue([_post()], {0: now + timedelta(hours=1)})

    claimed = repository.claim_due(now)

    assert claimed is not None
    assert claimed.status is PostStatus.POSTING
    # 2件目はまだ来ていない
    assert repository.claim_due(now) is None


def test_claim_due_は_同じ行を二度返さない(repository: SocialPostRepository) -> None:
    """POSTING にした行を再び掴むと、同じ内容が2回公開される。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})

    assert repository.claim_due(now) is not None
    assert repository.claim_due(now) is None


def test_recover_stuck_posting_は_NEEDS_REVIEW_にする(
    repository: SocialPostRepository,
) -> None:
    """これがこの計画で最も重要な回帰テスト。

    POSTING で残った行を SCHEDULED に戻すと、送信が届いていた場合に
    同じ投稿が2つ並ぶ。自動では再送しない。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})
    claimed = repository.claim_due(now)
    assert claimed is not None

    recovered = repository.recover_stuck_posting("プロセスが再起動しました")

    assert recovered == 1
    reviewed = repository.list_needs_review()
    assert [p.id for p in reviewed] == [claimed.id]
    # 掴み直せないこと（再送されない）
    assert repository.claim_due(now) is None


def test_discard_stale_は_遅れすぎた行を捨てる(repository: SocialPostRepository) -> None:
    """復帰した瞬間に溜まった投稿が連投されるとスパムに見える。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=90)})
    repository.enqueue([_post()], {0: now - timedelta(minutes=10)})

    discarded = repository.discard_stale(now, max_delay_minutes=60)

    assert discarded == 1
    claimed = repository.claim_due(now)
    assert claimed is not None
    assert repository.claim_due(now) is None


def test_list_recent_failed_は_見送られた行を返す(repository: SocialPostRepository) -> None:
    """FAILED を返す一覧が無いと、見送られた投稿が画面に一切出ない。

    `discard_stale` が4件まとめて捨てた日を、運用者は空のキューから
    「ニュースが無かった」と読み違える。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=90)})
    repository.enqueue([_post()], {0: now + timedelta(hours=1)})  # 生き残る
    repository.discard_stale(now, max_delay_minutes=60)

    failed = repository.list_recent_failed(now - timedelta(hours=24))

    assert len(failed) == 1
    assert failed[0].status is PostStatus.FAILED
    assert "60分以上遅れた" in (failed[0].error_message or "")


def test_list_recent_failed_は_古い行を返さない(repository: SocialPostRepository) -> None:
    """since より前に作られた行は運用者の問い（今日の投稿）に関係しない。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now - timedelta(minutes=90)})
    repository.discard_stale(now, max_delay_minutes=60)

    # created_at は行の作成時刻（現在時刻）なので、未来を since にすれば
    # 「範囲外」を再現できる。
    assert repository.list_recent_failed(datetime.now(UTC) + timedelta(hours=1)) == []


def test_cancel_は_送信中の行を拒否する(repository: SocialPostRepository) -> None:
    """`POSTING -> FAILED` は遷移表では許されているので、状態を見ないと通る。

    通してしまうと、投稿は X に出たのに行は FAILED、ワーカーの
    `mark_posted` は例外、記事は未消費 —— 翌日もう一度公開される。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})
    claimed = repository.claim_due(now)
    assert claimed is not None

    with pytest.raises(InvalidPostTransition):
        repository.cancel(claimed.id, "取り消しました")

    # 行は POSTING のまま。ワーカーは mark_posted まで進める
    repository.mark_posted(claimed.id, tweet_id="tw1", posted_at=now)


def test_cancel_は_送信済みの行を拒否する(repository: SocialPostRepository) -> None:
    """キューは最大30秒古い値を映すので、既に出た行のボタンが押されうる。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})
    claimed = repository.claim_due(now)
    assert claimed is not None
    repository.mark_posted(claimed.id, tweet_id="tw1", posted_at=now)

    with pytest.raises(InvalidPostTransition):
        repository.cancel(claimed.id, "取り消しました")


def test_cancel_は_予約済みの行を落とす(repository: SocialPostRepository) -> None:
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post()], {0: now})
    scheduled = repository.list_upcoming(limit=1)[0]

    repository.cancel(scheduled.id, "取り消しました")

    assert repository.list_upcoming() == []
    assert repository.claim_due(now) is None


def test_monthly_post_counts_は_リンク有無で分ける(repository: SocialPostRepository) -> None:
    """単価が13倍違うので、混ぜて数えるとコスト概算が意味を失う。"""
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    for has_link in (False, False, True):
        repository.enqueue([_post(has_link=has_link)], {0: now})
        claimed = repository.claim_due(now)
        assert claimed is not None
        repository.mark_posted(claimed.id, tweet_id="1", posted_at=now)

    plain, with_link = repository.monthly_post_counts(2026, 8)

    assert (plain, with_link) == (2, 1)


def test_スレッドは_group_id_でまとまる(repository: SocialPostRepository) -> None:
    group_id = repository.enqueue(
        [_post(position=0), _post(position=1)],
        {0: datetime(2026, 8, 15, 12, 0, tzinfo=UTC), 1: datetime(2026, 8, 15, 12, 0, tzinfo=UTC)},
    )

    upcoming = repository.list_upcoming(limit=10)

    assert {p.group_id for p in upcoming} == {group_id}
    assert sorted(p.position for p in upcoming) == [0, 1]


def test_list_thread_は_スレッド全体を位置の順に返す(repository: SocialPostRepository) -> None:
    """プレビューは1行だけでは足りない。

    リンクと画像を背負うのは先頭の1件だけなので、2件目の id で引いた
    ときにも先頭が返らなければ「リンクが無い」ように見える。
    """
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    repository.enqueue([_post(position=0), _post(position=1)], {0: now, 1: now})
    # 別スレッド。混ざらないことを確かめるために入れる
    repository.enqueue([_post(position=0)], {0: now})

    upcoming = repository.list_upcoming()
    second = next(p for p in upcoming if p.position == 1)

    thread = repository.list_thread(second.id)

    assert [p.position for p in thread] == [0, 1]
    assert {p.group_id for p in thread} == {second.group_id}


def test_list_thread_は_存在しない_id_で空を返す(repository: SocialPostRepository) -> None:
    assert repository.list_thread(999) == []
