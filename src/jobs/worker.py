"""ジョブを実行するワーカー。

なぜスレッドで回すか
--------------------
`pipeline.run()` は完全に同期で、ネットワークI/O・`subprocess`（ffmpeg）を
含み数分かかる。これをイベントループ上で await すると、生成中に Web
サーバー全体が応答しなくなる（`/status` のポーリングも止まる）。
以前この欠陥を実際に踏んでいる。

`asyncio.to_thread` ではなく素の `threading.Thread` にしているのは、
ワーカーの寿命がリクエストではなく**アプリの寿命**に紐づくため。
lifespan で起動し、終了時に停止させる。

なぜ Celery / Redis を入れないか
--------------------------------
キューは DB のテーブルで足りる。この規模で外部ブローカーを足すと、
運用対象が1つ増えるだけで得るものが少ない。ワーカーを別プロセス
（別コンテナ）に切り出したくなったら、同じ `JobWorker` を
`python -m src.jobs.worker` のように起動すればよい形にしてある。
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from typing import Protocol

from src.models.job import GenerationJob
from src.storage.jobs import DEFAULT_LEASE_SECONDS, JobRepository
from src.utils.logger import log_error, log_step, log_success

# キューが空のときに次を見に行くまでの間隔。
# 生成は分単位の作業なので、数秒の遅れは体感に影響しない。
# 短くすると SQLite への無駄な問い合わせが増える。
POLL_INTERVAL_SEC = 2.0

# リースを延ばす間隔。リース長の 1/3 にして、1回取りこぼしても
# 期限切れにならない余裕を持たせる。
HEARTBEAT_INTERVAL_SEC = DEFAULT_LEASE_SECONDS / 3


class JobRunner(Protocol):
    """ジョブ1件を実行する処理。

    Protocol にしている理由: ワーカーのループ（リース・回収・停止）と
    「実際に動画を作る」処理を分けたい。テストではフェイクを差し込んで、
    Azure を呼ばずにループの挙動だけを検証する。
    """

    def __call__(self, job: GenerationJob) -> str | None:
        """ジョブを実行し、生成した動画の保存先キーを返す。"""
        ...


class JobWorker:
    """ジョブ表をポーリングして実行するワーカー。

    Attributes:
        worker_id: このワーカーの識別子（どのワーカーが握ったか分かるように）
    """

    def __init__(
        self,
        repository: JobRepository,
        runner: JobRunner,
        poll_interval: float = POLL_INTERVAL_SEC,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        """初期化する。

        Args:
            repository: ジョブ表
            runner: ジョブ1件を実行する処理
            poll_interval: キューが空のときの待ち時間（秒）
            lease_seconds: リースの長さ（秒）
        """
        self._repository = repository
        self._runner = runner
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds

        # ホスト名 + PID + 乱数。コンテナを複数動かしても衝突しない。
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 実行中のジョブ。heartbeat の対象を知るために持つ。
        self._current_job_id: int | None = None

    # ----------------------------------------------------------------
    # 起動・停止
    # ----------------------------------------------------------------

    def start(self) -> None:
        """ワーカースレッドを起動する。"""
        if self._thread is not None:
            raise RuntimeError("ワーカーは既に起動しています")

        # daemon=True にしない。生成の途中でプロセスが終わると、
        # 中途半端な生成物とリースの残った RUNNING 行ができる。
        # 停止は stop() で明示的に待つ。
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=False)
        self._thread.start()
        log_step(f"ジョブワーカーを起動しました ({self.worker_id})", "⚙️")

    def stop(self, timeout: float = 30.0) -> None:
        """停止を要求し、スレッドの終了を待つ。

        実行中のジョブは中断しない（ffmpeg の途中で殺すと壊れた動画が
        残る）。ポーリングの待機中なら即座に抜ける。

        Args:
            timeout: 待つ秒数。超えたら諦めてログに残す
        """
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log_error(
                f"ジョブワーカーが {timeout}秒で停止しませんでした"
                "（実行中のジョブが残っています。リースが切れれば他のワーカーが回収します）"
            )
        self._thread = None

    @property
    def is_running(self) -> bool:
        """スレッドが生きているか。"""
        return self._thread is not None and self._thread.is_alive()

    # ----------------------------------------------------------------
    # ループ
    # ----------------------------------------------------------------

    def _loop(self) -> None:
        """停止要求が来るまでジョブを取り続ける。"""
        while not self._stop.is_set():
            try:
                if not self._run_one():
                    # 仕事が無い。ここで初めて待つ。
                    # 待つ前に落ちたワーカーの残骸を回収する。
                    self._recover_stale()
                    self._stop.wait(self._poll_interval)
            except Exception as e:
                # ループ自体は絶対に落とさない。落とすと以降のジョブが
                # 永久に QUEUED のまま残り、再起動しないと動かなくなる。
                log_error(f"ジョブワーカーのループでエラー: {e}")
                self._stop.wait(self._poll_interval)

        log_step(f"ジョブワーカーを停止しました ({self.worker_id})", "⚙️")

    def _run_one(self) -> bool:
        """ジョブを1件取って実行する。

        Returns:
            bool: 実行したなら True、キューが空なら False
        """
        job = self._repository.claim_next(self.worker_id, self._lease_seconds)
        if job is None:
            return False

        self._current_job_id = job.id
        heartbeat = self._start_heartbeat(job.id)
        log_step(
            f"ジョブ {job.id} を開始 ({job.article_title[:30]}, "
            f"{job.video_format}, {job.language}, {job.attempts}回目)",
            "🎬",
        )
        try:
            video_key = self._runner(job)
            self._repository.mark_succeeded(job.id, video_key=video_key)
            log_success(f"ジョブ {job.id} 完了: {job.article_title[:30]}")
        except Exception as e:
            # 失敗の理由を行に残す。UI がこれを表示するので、
            # 例外を握りつぶすと「失敗したが理由が分からない」になる。
            self._repository.mark_failed(job.id, str(e))
            log_error(f"ジョブ {job.id} 失敗: {job.article_title[:30]} - {e}")
        finally:
            heartbeat.set()
            self._current_job_id = None
        return True

    def _start_heartbeat(self, job_id: int) -> threading.Event:
        """リースを延ばし続けるタイマーを起動する。

        長尺の生成はリース（既定15分）を超えうる。延ばさないと、
        実行中のジョブが「落ちたワーカーの残骸」と誤認され、
        別のワーカーが同じ記事を二重に生成してクォータを食う。

        Args:
            job_id: 対象のジョブID

        Returns:
            threading.Event: set すると停止する
        """
        done = threading.Event()

        def beat() -> None:
            while not done.wait(HEARTBEAT_INTERVAL_SEC):
                try:
                    self._repository.heartbeat(job_id, self._lease_seconds)
                except Exception as e:
                    # heartbeat の失敗でジョブを止めない。
                    # 最悪リースが切れて別のワーカーに回収されるだけ。
                    log_error(f"ジョブ {job_id} のリース更新に失敗: {e}")

        # こちらは daemon にする。本体のジョブが終われば不要になるうえ、
        # 待機中に残っていてもプロセスの終了を妨げたくない。
        threading.Thread(target=beat, name=f"job-heartbeat-{job_id}", daemon=True).start()
        return done

    def _recover_stale(self) -> None:
        """リースの切れたジョブを回収する。"""
        recovered = self._repository.requeue_expired()
        if recovered:
            log_step(f"リースの切れたジョブ {recovered}件を回収しました", "♻️")
