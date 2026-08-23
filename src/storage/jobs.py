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
    MAX_JOB_ATTEMPTS,
    BatchProgress,
    GenerationJob,
    InvalidJobTransition,
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
        origin=record.origin,
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

    @staticmethod
    def _queued_record(
        batch_id: str,
        article_id: str,
        article_title: str,
        video_format: str,
        language: str,
        origin: str | None,
    ) -> JobRecord:
        """QUEUED の行を1つ組み立てる。

        `enqueue_batch` と `enqueue_into` で共有する。書き写すと、片方にだけ
        列を足し忘れる形の欠陥が入る（`ARTICLE_OVERFETCH` が X 側にしか
        入っていなかったのと同じ構造）。

        Args:
            batch_id: バッチID
            article_id: 記事ID
            article_title: 記事タイトル（表示用に複製して持つ）
            video_format: 動画形式
            language: 言語コード
            origin: 積んだ主体（"schedule" / None）

        Returns:
            JobRecord: 未保存の行
        """
        return JobRecord(
            batch_id=batch_id,
            article_id=article_id,
            article_title=article_title,
            video_format=video_format,
            language=language,
            origin=origin,
            status=JobStatus.QUEUED,
            attempts=0,
        )

    def enqueue_batch(
        self,
        articles: list[tuple[str, str]],
        video_format: str,
        language: str = "ja",
        origin: str | None = None,
    ) -> str:
        """記事のリストを1つのバッチとして投入する。

        Args:
            articles: (article_id, article_title) の並び
            video_format: 動画形式
            language: 言語コード
            origin: 積んだ主体。定期実行は "schedule"、画面からの手動は
                省略（None）。拒否されたときに代替を積んでよいかを分ける

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
                self._queued_record(batch_id, article_id, title, video_format, language, origin)
                for article_id, title in articles
            )
        return batch_id

    def enqueue_into(
        self,
        batch_id: str,
        article_id: str,
        article_title: str,
        video_format: str,
        language: str,
        origin: str | None = None,
    ) -> None:
        """既にあるバッチに1件足す。

        **新しいバッチを作らないことが要点。** `enqueue_batch` は毎回
        `uuid4()` で batch_id を作るので、代替を別バッチで積むと
        `latest_batch_id()` が代替だけを指し、**拒否された記事が画面から
        消える**。同じバッチに足せば `BatchProgress.from_jobs` が QUEUED を
        見て status を running に戻し、完了後は失敗した記事と成功した記事の
        両方を出す（`BatchProgress` 側の変更は要らない）。

        Args:
            batch_id: 足す先のバッチID
            article_id: 記事ID
            article_title: 記事タイトル
            video_format: 動画形式
            language: 言語コード
            origin: 積んだ主体。元のジョブの値を引き継ぐ
        """
        with session_scope(self._sessions) as session:
            session.add(
                self._queued_record(
                    batch_id, article_id, article_title, video_format, language, origin
                )
            )

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

    def retry(self, job_id: int) -> None:
        """失敗したジョブを人の操作で実行待ちに戻す。

        **`requeue_expired` とは別物。** あちらは「ワーカーが落ちてリースが
        切れた RUNNING」の自動回収で、試行回数を増やし、上限を超えたら
        FAILED にする。こちらは人が画面で押す再実行である。

        `attempts` は**据え置く**。掴むときに `claim_next` が +1 するので、
        ここで戻すと上限が働かず、押すたびに何度でも実行できてしまう
        （取り消せない外向きの操作ではないが、画像生成のクォータを食う）。

        `error_message` は消す。残すと、実行待ちの行に前回の失敗理由が
        付いたままになり、画面で「もう一度落ちた」のか「まだ動いていない」のか
        区別できない。

        Args:
            job_id: 戻すジョブのID

        Raises:
            InvalidJobTransition: 失敗していない行（成功済み・実行中・実行待ち）
                を指した場合、または試行回数が上限に達している場合
        """
        with session_scope(self._sessions) as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise InvalidJobTransition(f"ジョブ {job_id} は見つかりません")
            # 上限の検査を遷移の検査より先に置く。順序を逆にすると、
            # 上限に達した FAILED 行が「遷移は可能」と判定されてから
            # 弾かれるので、理由が2種類の例外に分かれて読みにくくなる。
            if record.attempts >= MAX_JOB_ATTEMPTS:
                raise InvalidJobTransition(
                    f"ジョブ {job_id} は {record.attempts} 回試行しているので"
                    f"再実行できません（上限{MAX_JOB_ATTEMPTS}回）"
                )
            check_transition(JobStatus(record.status), JobStatus.QUEUED)
            record.status = JobStatus.QUEUED
            record.error_message = None
            record.started_at = None
            record.finished_at = None
            # リースは掴むときに入る。終了時に外れているが、念のため揃える。
            record.lease_expires_at = None
            record.worker_id = None

    def list_recent_failed(self, limit: int = 10) -> list[GenerationJob]:
        """失敗したジョブを新しい順に返す。

        **`latest_progress()` では届かない。** あちらは直近1バッチしか見ないので、
        別のバッチが走った時点で古い失敗が画面から消える。#61 のジョブは
        「長く止まっていて後から見つかった」もので、毎朝1バッチ積む運用では
        「気付いた時には別バッチが最新」が通常ケースになる。再実行の導線は
        バッチに依存しない一覧の上に置く必要がある。

        Args:
            limit: 返す最大件数

        Returns:
            list[GenerationJob]: 終了時刻の新しい順（未設定なら id 順）
        """
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(JobRecord)
                .where(JobRecord.status == JobStatus.FAILED)
                .order_by(JobRecord.finished_at.desc(), JobRecord.id.desc())
                .limit(limit)
            ).all()
            return [_to_domain(r) for r in records]

    def requeue_expired(self, max_attempts: int = MAX_JOB_ATTEMPTS) -> int:
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

    def list_jobs_between(self, start: datetime, end: datetime) -> list[GenerationJob]:
        """期間内に投入されたジョブを、投入時刻の範囲で直接返す。

        `latest_batch_id()` + `list_batch()` は「直近1バッチ」しか返さない。
        同じ日に複数バッチが走ると、早いバッチの動画が見えなくなる。
        画面の時間軸は「今日ちょうど走っているものすべて」を見せる必要が
        あるため、バッチではなく `created_at` で直接絞る。
        `SocialPostRepository.list_posted_between` と同じ形にしている。

        Args:
            start: 期間の開始（UTC aware）
            end: 期間の終わり（UTC aware、含まない）

        Returns:
            list[GenerationJob]: 投入時刻順のジョブ
        """
        with session_scope(self._sessions) as session:
            records = session.scalars(
                select(JobRecord)
                .where(JobRecord.created_at >= start, JobRecord.created_at < end)
                .order_by(JobRecord.created_at, JobRecord.id)
            ).all()
            return [_to_domain(r) for r in records]

    def count_by_status(self) -> dict[JobStatus, int]:
        """状態ごとの件数。運用時の確認用。"""
        with session_scope(self._sessions) as session:
            records = session.scalars(select(JobRecord.status)).all()
        counts: dict[JobStatus, int] = {}
        for status in records:
            key = JobStatus(status)
            counts[key] = counts.get(key, 0) + 1
        return counts
