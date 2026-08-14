"""ジョブ表の読み書き。

ここが担う一番難しいこと
------------------------
「1つのジョブを2つのワーカーが同時に実行しない」こと。

そのために **リース方式** を採る。ワーカーは QUEUED の行を1件
RUNNING に更新し、`lease_expires_at` に期限を入れる。実行中は
`heartbeat()` で期限を延ばす。ワーカーが落ちれば期限が切れ、
別のワーカーが QUEUED に戻して回収できる。

「RUNNING のまま放置された行を人が直す」運用を避けたいのが理由。
数分かかるジョブなので、落ちる機会はそれなりにある。

排他の実装は接続先で変える。

- PostgreSQL: `SELECT ... FOR UPDATE SKIP LOCKED`。複数ワーカーが
  同時に取りに来ても、互いに別の行を掴む
- SQLite: 上記は使えない（行ロックが無い）。代わりに
  「更新対象を id で絞った UPDATE の影響行数が 1 か」で判定する。
  同じ行を2つのワーカーが狙ったら、片方の UPDATE が 0 行になり、
  取得に失敗したと分かる
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session, sessionmaker

from src.models.job import (
    BatchProgress,
    GenerationJob,
    JobStatus,
    check_transition,
)
from src.storage.db import session_scope
from src.storage.tables import JobRecord, utcnow

# ワーカーがジョブを掴んでいられる時間。
# ショート1本は1分程度、長尺は数分かかる。余裕を持って 15 分にし、
# 実行中は heartbeat で延ばす。短すぎると、生きているワーカーの
# 仕事を別のワーカーが二重に始めてしまう。
DEFAULT_LEASE_SECONDS = 900


def _as_utc(value: datetime | None) -> datetime | None:
    """naive な datetime に UTC を補う。

    SQLite は列に `DateTime(timezone=True)` を指定してもタイムゾーンを
    保存しない（オフセットを落として書き、naive で読み返す）。
    値そのものは UTC で書いているので、読み出し側で付け直す。

    これをしないと、Python 側で `lease_expires_at > now` のように
    比較したときに `can't compare offset-naive and offset-aware
    datetimes` で落ちる。DB 内での比較（回収処理の WHERE 句）は
    すべて UTC の壁時計同士なので一貫している。

    Args:
        value: DB から読んだ値

    Returns:
        datetime | None: UTC aware な値
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _to_domain(record: JobRecord) -> GenerationJob:
    """行をドメインの写しに変換する。

    セッションを閉じた後でも安全に読めるようにするための境界。
    時刻はここで UTC aware に揃える。
    """
    created_at = _as_utc(record.created_at)
    assert created_at is not None  # created_at は NOT NULL
    return GenerationJob(
        id=record.id,
        batch_id=record.batch_id,
        article_id=record.article_id,
        article_title=record.article_title,
        video_format=record.video_format,
        language=record.language,
        status=JobStatus(record.status),
        attempts=record.attempts,
        error_message=record.error_message,
        video_key=record.video_key,
        created_at=created_at,
        started_at=_as_utc(record.started_at),
        finished_at=_as_utc(record.finished_at),
        worker_id=record.worker_id,
        lease_expires_at=_as_utc(record.lease_expires_at),
    )


class JobRepository:
    """ジョブ表への操作。

    セッションは呼び出しごとに開いて閉じる。ワーカースレッドと
    イベントループが同じリポジトリを共有するため、セッションを
    インスタンスに持たせるとスレッド間で共有されてしまう
    （SQLAlchemy の Session はスレッドセーフでない）。
    """

    def __init__(self, session_factory: sessionmaker[Session]):
        """初期化する。

        Args:
            session_factory: セッションファクトリ
        """
        self._sessions = session_factory

    # ----------------------------------------------------------------
    # 投入
    # ----------------------------------------------------------------

    def enqueue_batch(
        self,
        articles: list[tuple[str, str]],
        video_format: str,
        language: str = "ja",
    ) -> str:
        """記事のリストを1つのバッチとして投入する。

        Args:
            articles: (article_id, article_title) の並び
            video_format: 動画形式
            language: 言語コード

        Returns:
            str: バッチID

        Raises:
            ValueError: 記事が空の場合
        """
        if not articles:
            raise ValueError("投入する記事がありません")

        batch_id = str(uuid.uuid4())
        with session_scope(self._sessions) as session:
            session.add_all(
                JobRecord(
                    batch_id=batch_id,
                    article_id=article_id,
                    article_title=title,
                    video_format=video_format,
                    language=language,
                    status=JobStatus.QUEUED,
                    attempts=0,
                )
                for article_id, title in articles
            )
        return batch_id

    # ----------------------------------------------------------------
    # ワーカー向け
    # ----------------------------------------------------------------

    def claim_next(
        self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> GenerationJob | None:
        """QUEUED のジョブを1件掴んで RUNNING にする。

        掴めなければ None を返す（キューが空、または他のワーカーに
        先を越された）。呼び出し側は None をエラーではなく
        「今は仕事が無い」として扱う。

        Args:
            worker_id: 掴むワーカーの識別子
            lease_seconds: リースの長さ（秒）

        Returns:
            GenerationJob | None: 掴んだジョブ
        """
        now = utcnow()
        with session_scope(self._sessions) as session:
            candidate = self._oldest_queued(session)
            if candidate is None:
                return None

            # 掴む操作を「status が QUEUED のままである」ことを条件にした
            # UPDATE で行う。他のワーカーが先に掴んでいれば 0 行になる。
            # SQLite には SKIP LOCKED が無いため、この方式で競合を検出する。
            claimed_rows = session.execute(
                update(JobRecord)
                .where(JobRecord.id == candidate.id, JobRecord.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    worker_id=worker_id,
                    started_at=now,
                    attempts=candidate.attempts + 1,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
            ).rowcount  # type: ignore[attr-defined]  # UPDATE は CursorResult を返す
            if claimed_rows != 1:
                # 競合した。次のポーリングで別の行を狙う
                return None

            session.flush()
            claimed = session.get(JobRecord, candidate.id)
            assert claimed is not None  # 直前に UPDATE が成功している
            return _to_domain(claimed)

    @staticmethod
    def _oldest_queued(session: Session) -> JobRecord | None:
        """最も古い QUEUED の行を取る。

        PostgreSQL では行ロックを取り、他のワーカーには見えなくする。
        SQLite は行ロックを持たないので素の SELECT になり、
        競合は claim_next 側の UPDATE 影響行数で検出する。
        """
        statement: Select[tuple[JobRecord]] = (
            select(JobRecord)
            .where(JobRecord.status == JobStatus.QUEUED)
            .order_by(JobRecord.created_at, JobRecord.id)
            .limit(1)
        )
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        return session.scalars(statement).first()

    def heartbeat(self, job_id: int, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        """リースの期限を延ばす。

        長尺の生成はリースより長くなりうる。延ばさないと、実行中の
        ジョブが「落ちたワーカーの残骸」と誤認されて二重に実行される。

        Args:
            job_id: ジョブID
            lease_seconds: 新しいリースの長さ（秒）
        """
        with session_scope(self._sessions) as session:
            session.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.status == JobStatus.RUNNING)
                .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
            )

    def mark_succeeded(self, job_id: int, video_key: str | None = None) -> None:
        """成功として終了させる。

        Args:
            job_id: ジョブID
            video_key: 生成した動画の保存先キー
        """
        self._finish(job_id, JobStatus.SUCCEEDED, video_key=video_key)

    def mark_failed(self, job_id: int, error_message: str) -> None:
        """失敗として終了させる。

        Args:
            job_id: ジョブID
            error_message: 失敗の理由（UI に出す）
        """
        self._finish(job_id, JobStatus.FAILED, error_message=error_message)

    def _finish(
        self,
        job_id: int,
        new_status: JobStatus,
        error_message: str | None = None,
        video_key: str | None = None,
    ) -> None:
        """終端状態へ遷移させる。

        Raises:
            InvalidJobTransition: 許可されない遷移（二重完了など）
        """
        with session_scope(self._sessions) as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return
            check_transition(JobStatus(record.status), new_status)
            record.status = new_status
            record.finished_at = utcnow()
            record.error_message = error_message
            record.video_key = video_key
            # リースを外す。残っていると、回収処理が終わった行を
            # 期限切れとして数えてしまう
            record.lease_expires_at = None
            record.worker_id = None

    def requeue_expired(self, max_attempts: int = 3) -> int:
        """リースが切れた RUNNING を回収する。

        ワーカーが落ちた（コンテナの再起動・OOM）ときに、握られたままの
        ジョブを他のワーカーが実行できるようにする。これが無いと
        RUNNING の行が永久に残り、進捗が「実行中」で止まる。

        試行回数の上限を超えた行は FAILED にする。落ちる原因が
        ジョブ自身にある場合（特定の記事で必ず落ちる等）、無限に
        再実行してクォータを食い潰すのを防ぐ。

        Args:
            max_attempts: この回数を超えたら失敗として打ち切る

        Returns:
            int: 回収した件数
        """
        now = utcnow()
        recovered = 0
        with session_scope(self._sessions) as session:
            stale = session.scalars(
                select(JobRecord).where(
                    JobRecord.status == JobStatus.RUNNING,
                    JobRecord.lease_expires_at.is_not(None),
                    JobRecord.lease_expires_at < now,
                )
            ).all()
            for record in stale:
                if record.attempts >= max_attempts:
                    record.status = JobStatus.FAILED
                    record.finished_at = now
                    record.error_message = (
                        f"{record.attempts}回試行しても完了しませんでした"
                        "（ワーカーが落ちた可能性があります）"
                    )
                else:
                    record.status = JobStatus.QUEUED
                    record.started_at = None
                record.worker_id = None
                record.lease_expires_at = None
                recovered += 1
        return recovered

    # ----------------------------------------------------------------
    # 読み取り
    # ----------------------------------------------------------------

    def get(self, job_id: int) -> GenerationJob | None:
        """1件取得する。"""
        with session_scope(self._sessions) as session:
            record = session.get(JobRecord, job_id)
            return _to_domain(record) if record else None

    def list_batch(self, batch_id: str) -> list[GenerationJob]:
        """バッチ内のジョブを投入順に返す。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(JobRecord)
                .where(JobRecord.batch_id == batch_id)
                .order_by(JobRecord.created_at, JobRecord.id)
            ).all()
            return [_to_domain(r) for r in records]

    def latest_batch_id(self) -> str | None:
        """最後に投入されたバッチのID。

        `/status` は「直近の生成」の進捗を出すため、どのバッチを見るかを
        決める必要がある。実行中のバッチがあればそれを優先する
        （新しい投入が完了済みバッチより後に来る場合を除き同じ結果になるが、
        実行中を隠さないことを優先する）。
        """
        with session_scope(self._sessions) as session:
            running = session.scalars(
                select(JobRecord.batch_id)
                .where(JobRecord.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
                .order_by(JobRecord.created_at.desc())
                .limit(1)
            ).first()
            if running:
                return str(running)

            latest = session.scalars(
                select(JobRecord.batch_id)
                .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
                .limit(1)
            ).first()
            return str(latest) if latest else None

    def latest_progress(self) -> BatchProgress:
        """直近のバッチの進捗を返す。

        `GenerationState.snapshot()` の置き換え。プロセスをまたいでも
        再起動をはさんでも同じ値が見える。

        Returns:
            BatchProgress: 進捗（1件も無ければ idle）
        """
        batch_id = self.latest_batch_id()
        if batch_id is None:
            return BatchProgress.idle()
        return BatchProgress.from_jobs(self.list_batch(batch_id))

    def has_active_jobs(self) -> bool:
        """実行待ち・実行中のジョブがあるか。

        二重投入を防ぐために使う。生成中に「生成開始」を押しても、
        同じ記事のジョブが積み増されないようにする。
        """
        with session_scope(self._sessions) as session:
            found = session.scalars(
                select(JobRecord.id)
                .where(JobRecord.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
                .limit(1)
            ).first()
            return found is not None

    def count_by_status(self) -> dict[JobStatus, int]:
        """状態ごとの件数。運用時の確認用。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(select(JobRecord.status)).all()
        counts: dict[JobStatus, int] = {}
        for status in records:
            key = JobStatus(status)
            counts[key] = counts.get(key, 0) + 1
        return counts
