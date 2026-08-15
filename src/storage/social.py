"""social_posts の読み書き。

ジョブ表（src/storage/jobs.py）との違い
--------------------------------------
リースと heartbeat を持たない。投稿は数秒で終わるので、15分のリースを
延ばし続ける仕組みは意味を持たない。

代わりに `recover_stuck_posting()` を持つ。POSTING で残った行を
**SCHEDULED に戻さず NEEDS_REVIEW にする**のがジョブ表との決定的な違い。
X API に冪等キーが無く、送信が届いたか分からない行を再送すると
同じ内容が2回公開される。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from src.models.social import (
    CANCELLABLE_STATUSES,
    InvalidPostTransition,
    NewPost,
    PostKind,
    PostStatus,
    SocialPost,
    check_post_transition,
)
from src.storage.db import session_scope
from src.storage.tables import SocialPostRecord, utcnow


def _as_utc(value: datetime | None) -> datetime | None:
    """naive な datetime に UTC を補う。

    SQLite は `DateTime(timezone=True)` でもタイムゾーンを保存しない。
    付け直さないと `scheduled_at <= now` の比較が
    `can't compare offset-naive and offset-aware datetimes` で落ちる。
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_domain(record: SocialPostRecord) -> SocialPost:
    """行をドメインの写しに変換する。"""
    created_at = _as_utc(record.created_at)
    assert created_at is not None  # NOT NULL
    return SocialPost(
        id=record.id,
        group_id=record.group_id,
        position=record.position,
        article_id=record.article_id,
        article_title=record.article_title,
        kind=PostKind(record.kind),
        body=record.body,
        weighted_length=record.weighted_length,
        has_link=record.has_link,
        image_key=record.image_key,
        status=PostStatus(record.status),
        scheduled_at=_as_utc(record.scheduled_at),
        posted_at=_as_utc(record.posted_at),
        tweet_id=record.tweet_id,
        reply_to_tweet_id=record.reply_to_tweet_id,
        attempts=record.attempts,
        error_message=record.error_message,
        created_at=created_at,
    )


class SocialPostRepository:
    """social_posts への操作。

    セッションは呼び出しごとに開いて閉じる（SQLAlchemy の Session は
    スレッドセーフでなく、ワーカースレッドとイベントループが
    同じリポジトリを共有する）。
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def enqueue(self, posts: list[NewPost], scheduled_at_by_position: dict[int, datetime]) -> str:
        """投稿を1つのまとまりとして積む。

        Args:
            posts: 積む投稿
            scheduled_at_by_position: position -> 予定時刻

        Returns:
            str: group_id

        Raises:
            ValueError: 投稿が空、または予定時刻の無い position がある
        """
        if not posts:
            raise ValueError("積む投稿がありません")
        missing = [p.position for p in posts if p.position not in scheduled_at_by_position]
        if missing:
            raise ValueError(f"予定時刻の無い position があります: {missing}")

        group_id = str(uuid.uuid4())
        with session_scope(self._sessions) as session:
            session.add_all(
                SocialPostRecord(
                    group_id=group_id,
                    position=post.position,
                    article_id=post.article_id,
                    article_title=post.article_title,
                    kind=post.kind,
                    body=post.body,
                    weighted_length=post.weighted_length,
                    has_link=post.has_link,
                    image_key=post.image_key,
                    status=PostStatus.SCHEDULED,
                    scheduled_at=scheduled_at_by_position[post.position],
                )
                for post in posts
            )
        return group_id

    def claim_due(self, now: datetime) -> SocialPost | None:
        """予定時刻を過ぎた SCHEDULED を1件掴んで POSTING にする。

        スレッドは position 順に出す必要があるため、
        `(scheduled_at, group_id, position)` の順で最古を取る。

        Args:
            now: 現在時刻（UTC aware）

        Returns:
            SocialPost | None: 掴んだ投稿。無ければ None
        """
        with session_scope(self._sessions) as session:
            candidate = session.scalars(
                select(SocialPostRecord)
                .where(
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                    SocialPostRecord.scheduled_at.is_not(None),
                    SocialPostRecord.scheduled_at <= now,
                )
                .order_by(
                    SocialPostRecord.scheduled_at,
                    SocialPostRecord.group_id,
                    SocialPostRecord.position,
                )
                .limit(1)
            ).first()
            if candidate is None:
                return None

            # 「status が SCHEDULED のままである」ことを条件にした UPDATE。
            # SQLite には SKIP LOCKED が無いため、影響行数で競合を検出する。
            claimed_rows = session.execute(
                update(SocialPostRecord)
                .where(
                    SocialPostRecord.id == candidate.id,
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                )
                .values(status=PostStatus.POSTING, attempts=candidate.attempts + 1)
            ).rowcount  # type: ignore[attr-defined]
            if claimed_rows != 1:
                return None

            session.flush()
            claimed = session.get(SocialPostRecord, candidate.id)
            assert claimed is not None
            return _to_domain(claimed)

    def mark_posted(self, post_id: int, tweet_id: str, posted_at: datetime | None = None) -> None:
        """投稿できたと記録する。"""
        self._transition(
            post_id, PostStatus.POSTED, tweet_id=tweet_id, posted_at=posted_at or utcnow()
        )

    def mark_failed(self, post_id: int, reason: str) -> None:
        """送信前に失敗したと記録する（再実行できる）。"""
        self._transition(post_id, PostStatus.FAILED, error_message=reason)

    def cancel(self, post_id: int, reason: str) -> None:
        """画面からの取り消し。**送信中・送信済みは拒否する。**

        `mark_failed` と分けている理由: `POSTING -> FAILED` は遷移表では
        許可されている（送信前に失敗が確定した場合のため）。そのため
        `mark_failed` をそのまま画面に繋ぐと、送信中の行を取り消せてしまい、
        投稿は X に出たのに行は FAILED、記事は未消費のまま残って
        **翌日もう一度公開される**。許可の判断は遷移表では表現できない
        （同じ遷移が、誰が起こしたかで許否が変わる）ので、
        `CANCELLABLE_STATUSES` を別に持って先に検査する。

        Args:
            post_id: 対象の投稿
            reason: 画面に出す理由

        Raises:
            InvalidPostTransition: 取り消せない状態だった
        """
        with session_scope(self._sessions) as session:
            record = session.get(SocialPostRecord, post_id)
            if record is None:
                return
            current = PostStatus(record.status)
            if current not in CANCELLABLE_STATUSES:
                raise InvalidPostTransition(f"{current} の投稿は取り消せません")
            check_post_transition(current, PostStatus.FAILED)
            record.status = PostStatus.FAILED
            record.error_message = reason

    def mark_needs_review(self, post_id: int, reason: str) -> None:
        """人が見るまで触らない状態にする。"""
        self._transition(post_id, PostStatus.NEEDS_REVIEW, error_message=reason)

    def _transition(
        self,
        post_id: int,
        new_status: PostStatus,
        error_message: str | None = None,
        tweet_id: str | None = None,
        posted_at: datetime | None = None,
    ) -> None:
        with session_scope(self._sessions) as session:
            record = session.get(SocialPostRecord, post_id)
            if record is None:
                return
            check_post_transition(PostStatus(record.status), new_status)
            record.status = new_status
            if error_message is not None:
                record.error_message = error_message
            if tweet_id is not None:
                record.tweet_id = tweet_id
            if posted_at is not None:
                record.posted_at = posted_at

    def recover_stuck_posting(self, reason: str) -> int:
        """POSTING で残った行を NEEDS_REVIEW にする。

        **SCHEDULED に戻さない。** 送信が届いたか分からない行なので、
        再送すると同じ内容が2つ並ぶ。取りこぼしのほうが安全。

        起動時に1回呼ぶ（前回のプロセスが送信中に落ちた分を拾う）。

        Args:
            reason: 画面に出す理由

        Returns:
            int: NEEDS_REVIEW にした件数
        """
        with session_scope(self._sessions) as session:
            stuck = session.scalars(
                select(SocialPostRecord).where(SocialPostRecord.status == PostStatus.POSTING)
            ).all()
            for record in stuck:
                record.status = PostStatus.NEEDS_REVIEW
                record.error_message = reason
            return len(stuck)

    def discard_stale(self, now: datetime, max_delay_minutes: int) -> int:
        """予定時刻から遅れすぎた行を捨てる。

        デプロイやプロセス停止で数時間止まったあと、復帰した瞬間に
        4件が連投されるとスパムに見える。

        Args:
            now: 現在時刻
            max_delay_minutes: これ以上遅れたら捨てる

        Returns:
            int: 捨てた件数
        """
        limit = now - timedelta(minutes=max_delay_minutes)
        with session_scope(self._sessions) as session:
            stale = session.scalars(
                select(SocialPostRecord).where(
                    SocialPostRecord.status == PostStatus.SCHEDULED,
                    SocialPostRecord.scheduled_at.is_not(None),
                    SocialPostRecord.scheduled_at < limit,
                )
            ).all()
            for record in stale:
                record.status = PostStatus.FAILED
                record.error_message = (
                    f"予定時刻から{max_delay_minutes}分以上遅れたため投稿しませんでした"
                )
            return len(stale)

    def list_upcoming(self, limit: int = 20) -> list[SocialPost]:
        """これから出る投稿を予定時刻順に返す。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord)
                .where(
                    SocialPostRecord.status.in_(
                        [PostStatus.DRAFTED, PostStatus.SCHEDULED, PostStatus.POSTING]
                    )
                )
                .order_by(SocialPostRecord.scheduled_at, SocialPostRecord.position)
                .limit(limit)
            ).all()
            return [_to_domain(r) for r in records]

    def list_needs_review(self) -> list[SocialPost]:
        """人が見る必要のある投稿。画面の上の帯にも出す。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord)
                .where(SocialPostRecord.status == PostStatus.NEEDS_REVIEW)
                .order_by(SocialPostRecord.scheduled_at)
            ).all()
            return [_to_domain(r) for r in records]

    def list_recent_failed(self, since: datetime) -> list[SocialPost]:
        """`since` 以降に作られた FAILED の行を新しい順に返す。

        **これが無い間、FAILED はどの一覧にも出てこなかった。**
        FAILED になる経路は3つあり、どれも黙って消える:

        1. `discard_stale`（遅れすぎた予定の見送り）
        2. 画面からの取り消し
        3. 送信前の失敗（画像カードのメディアアップロード失敗を含む）

        デプロイでアプリが数時間止まった後は 1 が4件まとめて起き、
        運用者は空のキューを見て「今日はニュースが無かった」と解釈する。
        痕跡がログの1行しか無いのが問題だった。

        絞り込みに `created_at` を使う（`scheduled_at` は理屈上 NULL に
        なりうるが `created_at` は NOT NULL）。下書きは予定と同じ日に
        作られるので、運用者の問い（「今日の投稿は落ちていないか」）には
        これで答えられる。

        Args:
            since: この時刻以降に作られた行だけを見る

        Returns:
            list[SocialPost]: FAILED の行（新しい順）
        """
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord)
                .where(
                    SocialPostRecord.status == PostStatus.FAILED,
                    SocialPostRecord.created_at >= since,
                )
                .order_by(SocialPostRecord.created_at.desc())
            ).all()
            return [_to_domain(r) for r in records]

    def list_posted_between(self, start: datetime, end: datetime) -> list[SocialPost]:
        """期間内に投稿できたものを返す（計測の対象を選ぶのに使う）。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(SocialPostRecord).where(
                    SocialPostRecord.status == PostStatus.POSTED,
                    SocialPostRecord.posted_at.is_not(None),
                    SocialPostRecord.posted_at >= start,
                    SocialPostRecord.posted_at < end,
                )
            ).all()
            return [_to_domain(r) for r in records]

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        """当月の投稿数を（リンク無し, リンク有り）で返す。

        単価が $0.015 と $0.20 で13倍違うため、混ぜて数えると
        コスト概算が意味を失う。

        Returns:
            tuple[int, int]: (リンク無しの件数, リンク有りの件数)
        """
        start = datetime(year, month, 1, tzinfo=UTC)
        end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=UTC)
        posts = self.list_posted_between(start, end)
        with_link = sum(1 for p in posts if p.has_link)
        return len(posts) - with_link, with_link

    def group_posted_tweet_id(self, group_id: str, position: int) -> str | None:
        """同じまとまりの指定 position の tweet_id を返す。

        スレッドの返信先（`reply_to_tweet_id`）を決めるのに使う。
        """
        with session_scope(self._sessions) as session:
            return session.scalars(
                select(SocialPostRecord.tweet_id).where(
                    SocialPostRecord.group_id == group_id,
                    SocialPostRecord.position == position,
                )
            ).first()
