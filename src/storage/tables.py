"""テーブル定義（SQLAlchemy 2.0 の型付き ORM）。

Alembic のマイグレーションはこの `Base.metadata` を見て差分を出すので、
テーブルを増やしたらここに書く。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.models.job import JobStatus


def utcnow() -> datetime:
    """UTC aware な現在時刻。

    naive な `datetime.now()` を使わない理由: DB に入れた時刻を
    後から比較するときに、どのタイムゾーンだったか分からなくなる。
    リースの期限判定はこの比較そのものなので、曖昧だと壊れる。
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """全テーブルの基底。"""


class JobRecord(Base):
    """生成ジョブ1件。

    「動画1本を作る」単位で1行。記事1件から複数言語を作る場合は
    言語ごとに行を作る（片方だけ失敗したときに再実行できる）。
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 「生成開始」1回ぶんのまとまり。/status はこの単位で集計する。
    batch_id: Mapped[str] = mapped_column(String(36), index=True)

    article_id: Mapped[str] = mapped_column(String(64), index=True)
    # 記事タイトルを複製して持つ。記事ストア側で記事が消えても、
    # 進捗表示と履歴は残したいため（正規化より参照の独立性を取る）。
    article_title: Mapped[str] = mapped_column(Text)

    video_format: Mapped[str] = mapped_column(String(16))
    language: Mapped[str] = mapped_column(String(8))

    # 誰がこのジョブを積んだか。"schedule" は定期実行、None は画面からの手動。
    #
    # **代替の投入をこれで分ける。** コンテンツフィルタに拒否されたとき、
    # 定期実行なら別の記事で作り直してよいが、手動なら人が選んだ記事を
    # 勝手に差し替えてはいけない。区別する列が無いと、その判断ができない。
    origin: Mapped[str | None] = mapped_column(String(16), default=None)

    status: Mapped[JobStatus] = mapped_column(String(16), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    # 成功時に生成した動画の保存先キー（`videos/....mp4`）。
    # 一覧やアップロードから引くのに使う。
    video_key: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # 実行中のワーカーを識別する。落ちたワーカーの仕事を見分けるために要る。
    #
    # 64 だと Container Apps では溢れる。実測でレプリカ名だけで58字
    # （`ca-newsvideo-img-mimujd6zyifm6--0000019-7fd4f56486-pwqdv`）あり、
    # `JobWorker.worker_id`（ホスト名 + PID + 乱数6字）を足すと66字になる。
    # SQLite は列長を強制しないので気付かず、PostgreSQL に移した直後
    # （2026-08-23）に `claim_next` の UPDATE が
    # `StringDataRightTruncation` で毎回失敗し、ジョブが QUEUED のまま
    # 無限リトライになって動画が1本も進まなくなった。
    worker_id: Mapped[str | None] = mapped_column(String(128), default=None)
    # リースの期限。過ぎている RUNNING は、ワーカーが落ちたものとして
    # 他のワーカーが QUEUED に戻して回収する。
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # ワーカーの取り合いは「QUEUED を作成順に1件」という検索なので、
        # この2列の複合インデックスが効く。
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<JobRecord id={self.id} status={self.status} title={self.article_title[:20]!r}>"


class SocialPostRecord(Base):
    """X 投稿1件。

    スレッドは `group_id` でまとめ、`position` で順序を持つ。
    1行 = 1テーマ（下書き全体を JSON）にしなかった理由: スレッドの途中で
    失敗したときに「どこまで出せたか」が行として観測できない。
    """

    __tablename__ = "social_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    group_id: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    article_id: Mapped[str] = mapped_column(String(64), index=True)
    article_title: Mapped[str] = mapped_column(Text)

    kind: Mapped[str] = mapped_column(String(16))
    body: Mapped[str] = mapped_column(Text)
    # 生成時に計算した weighted length。画面に「118/140」と出す。
    weighted_length: Mapped[int] = mapped_column(Integer, default=0)
    # URL を含むか。単価が $0.015 と $0.20 で13倍違うため、
    # コスト概算にはこの区別が必須。
    has_link: Mapped[bool] = mapped_column(Boolean, default=False)
    image_key: Mapped[str | None] = mapped_column(Text, default=None)

    status: Mapped[str] = mapped_column(String(16), index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    tweet_id: Mapped[str | None] = mapped_column(String(32), default=None)
    reply_to_tweet_id: Mapped[str | None] = mapped_column(String(32), default=None)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # ワーカーの検索は「SCHEDULED で予定時刻を過ぎた最古の1件」。
        Index("ix_social_posts_status_scheduled_at", "status", "scheduled_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<SocialPostRecord id={self.id} status={self.status} kind={self.kind}>"
