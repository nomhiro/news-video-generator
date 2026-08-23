"""生成ジョブのドメインモデル。

なぜジョブという概念を入れたか
------------------------------
進捗はこれまで `GenerationState`（プロセスメモリ上の可変シングルトン）に
持っていた。3つの問題があった。

1. 再起動で消える。数分かかる生成の途中でプロセスが落ちると、
   何が終わって何が残っているのか誰も知らない状態になる
2. レプリカを増やせない。進捗を持つプロセスと `/status` を返すプロセスが
   別だと、進捗が見えない
3. 失敗した1件をやり直す手段がない。状態が「今この瞬間の集計」しかなく、
   個々の記事の結果を後から参照できない

ジョブを行として永続化すると、いずれも「DB を読む」で解決する。

このモジュールは外部依存を持たない（SQLAlchemy を import しない）。
永続化の詳細は `src/storage/jobs.py` にあり、ここは状態遷移の規則だけを持つ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    """ジョブの状態。

    QUEUED -> RUNNING -> SUCCEEDED / FAILED の一方向。
    RUNNING から QUEUED に戻る経路が1つだけある（リースの期限切れによる回収）。
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# 許可される遷移。
#
# 明示的に持つ理由: 状態を文字列で更新していると、`finish()` の
# 呼び忘れや二重呼び出しが静かに通ってしまう。DB の行は複数の
# プロセスから触られるので、遷移の妥当性はコード側で担保するしかない。
_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    # RUNNING -> QUEUED は、ワーカーが落ちてリースが切れた行の回収。
    # これがないと、落ちたワーカーが握っていた仕事が永久に RUNNING で残る。
    JobStatus.RUNNING: frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),  # 手動での再実行
}

# 終端状態。ここに来た行はワーカーが二度と触らない。
TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})

# 1つのジョブを実行してよい回数の上限。
#
# **2つの経路が同じ上限を見る必要がある。** リースが切れた行の自動回収
# （`requeue_expired`）と、人が画面から押す再実行（`retry`）である。
# 以前は前者の既定引数にしか無く、後者を足すときに数字を書き写せば
# 「自動では打ち切られるのに手動では無限に押せる」形の食い違いになる。
#
# 上限が必要な理由は自動回収と同じ。特定の記事で必ず落ちる場合に、
# 画像生成のクォータ（リージョン単位で上限4、X の画像カードと共食い）を
# 食い潰し続けるのを防ぐ。
MAX_JOB_ATTEMPTS = 3


class InvalidJobTransition(Exception):
    """許可されていない状態遷移。"""


def check_transition(current: JobStatus, new: JobStatus) -> None:
    """状態遷移が許可されているか検証する。

    Args:
        current: 現在の状態
        new: 遷移先

    Raises:
        InvalidJobTransition: 許可されていない遷移
    """
    if new not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidJobTransition(f"{current} -> {new} は許可されていません")


# `JobRecord.origin` に入る値。定期実行が積んだジョブだけが、拒否されたときに
# 別の記事で作り直される（手動は人が選んだ記事を差し替えない）。
# **文字列を書き写さない**——比較する側と入れる側で綴りがずれると、代替が
# 静かに積まれなくなる（症状は「その日の動画が0本」で、直す前と同じに見える）。
ORIGIN_SCHEDULE = "schedule"


@dataclass(frozen=True)
class GenerationJob:
    """生成ジョブ1件の読み取り用の写し。

    DB の行をそのまま渡さない理由: セッションを閉じた後に属性を触ると
    SQLAlchemy が `DetachedInstanceError` を投げる。ルートやワーカーは
    セッションの寿命を意識せずに使いたいので、境界で写しに変換する。

    Attributes:
        id: ジョブID
        batch_id: 「生成開始」1回ぶんのまとまり。`/status` の集計単位
        article_id: 元記事のID
        article_title: 元記事のタイトル（表示用。記事が消えても残す）
        video_format: 動画形式（short / tiktok / long）
        language: 言語コード
        status: 状態
        attempts: 実行を試みた回数
        error_message: 失敗の理由
        video_key: 生成した動画の保存先キー（成功時のみ）
        created_at: 投入時刻（UTC aware）
        started_at: 実行開始時刻
        finished_at: 終了時刻
        worker_id: 実行中のワーカーの識別子
        lease_expires_at: リースの期限。過ぎたら他のワーカーが回収できる
        origin: 積んだ主体（"schedule" は定期実行、None は手動）。
            拒否されたときに代替を積んでよいかの判断に使う
    """

    id: int
    batch_id: str
    article_id: str
    article_title: str
    video_format: str
    language: str
    status: JobStatus
    attempts: int
    error_message: str | None
    video_key: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    worker_id: str | None
    lease_expires_at: datetime | None
    # 既定値を持たせるのは、既存の構築箇所（テストを含む）を壊さないため。
    origin: str | None = None

    @property
    def is_terminal(self) -> bool:
        """もう変化しない状態か。"""
        return self.status in TERMINAL_STATUSES

    @property
    def can_retry(self) -> bool:
        """人が押して再実行できる状態か。

        画面がボタンを出すかどうかの判断に使う。`JobRepository.retry` は
        同じ条件を独立に検査する——ボタンを隠すだけでは、画面を開いたまま
        待っている間に別の経路（自動回収）で試行回数が上限に達しうる。
        """
        return self.status is JobStatus.FAILED and self.attempts < MAX_JOB_ATTEMPTS


@dataclass(frozen=True)
class BatchProgress:
    """バッチ1件の進捗。

    `GenerationState.snapshot()` の置き換え。DB を1回読んで組み立てるので、
    プロセスをまたいでも、再起動をはさんでも同じ値が見える。

    Attributes:
        batch_id: バッチID（1件も無ければ None）
        status: "idle" / "running" / "success" / "error"
        total_count: 対象件数
        completed_count: 終了した件数（成功 + 失敗）
        current_article: いま実行中の記事タイトル
        completed_articles: 成功した記事タイトル
        failed_articles: 失敗した記事タイトル
        error_message: 代表的なエラーメッセージ
    """

    batch_id: str | None
    status: str
    total_count: int
    completed_count: int
    current_article: str | None
    completed_articles: tuple[str, ...]
    failed_articles: tuple[str, ...]
    error_message: str | None

    @property
    def is_running(self) -> bool:
        """まだ動いているか。"""
        return self.status == "running"

    @classmethod
    def idle(cls) -> BatchProgress:
        """まだ何も投入されていない状態。"""
        return cls(
            batch_id=None,
            status="idle",
            total_count=0,
            completed_count=0,
            current_article=None,
            completed_articles=(),
            failed_articles=(),
            error_message=None,
        )

    @classmethod
    def from_jobs(cls, jobs: list[GenerationJob]) -> BatchProgress:
        """同一バッチのジョブから進捗を組み立てる。

        Args:
            jobs: 同じ batch_id を持つジョブ

        Returns:
            BatchProgress: 集計結果（空なら idle）
        """
        if not jobs:
            return cls.idle()

        running = [j for j in jobs if j.status is JobStatus.RUNNING]
        succeeded = [j for j in jobs if j.status is JobStatus.SUCCEEDED]
        failed = [j for j in jobs if j.status is JobStatus.FAILED]
        pending = [j for j in jobs if j.status is JobStatus.QUEUED]

        if running or pending:
            status = "running"
        elif failed:
            # 全件終わっていて失敗がある。1件でも失敗したら error として扱う
            # （UI は失敗した記事名を出すので、成功分も見える）
            status = "error"
        else:
            status = "success"

        return cls(
            batch_id=jobs[0].batch_id,
            status=status,
            total_count=len(jobs),
            completed_count=len(succeeded) + len(failed),
            current_article=running[0].article_title if running else None,
            completed_articles=tuple(j.article_title for j in succeeded),
            failed_articles=tuple(j.article_title for j in failed),
            error_message=next((j.error_message for j in failed if j.error_message), None),
        )
