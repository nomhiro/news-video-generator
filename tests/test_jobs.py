"""ジョブ表とワーカーの検証。

ここで守りたい性質
------------------
- 1つのジョブを2つのワーカーが同時に実行しない（リース方式）
- ワーカーが落ちても仕事が永久に RUNNING で残らない（期限切れの回収）
- 何度も落ちるジョブが無限に再実行されない（試行回数の上限）
- 進捗の集計が実際の行と一致する
"""

import threading
from datetime import timedelta
from pathlib import Path

import pytest

from src.jobs.runner import ArticleUnavailable, PipelineJobRunner
from src.jobs.worker import JobWorker
from src.models.job import (
    BatchProgress,
    GenerationJob,
    InvalidJobTransition,
    JobStatus,
    check_transition,
)
from src.storage.db import create_db_engine, create_session_factory, session_scope
from src.storage.jobs import JobRepository
from src.storage.schema import upgrade_to_head
from src.storage.tables import JobRecord, utcnow


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    upgrade_to_head(url)
    return url


@pytest.fixture
def repository(db_url: str) -> JobRepository:
    return JobRepository(create_session_factory(create_db_engine(db_url)))


ARTICLES = [("a-1", "記事1"), ("a-2", "記事2"), ("a-3", "記事3")]


# --------------------------------------------------------------------------
# 状態遷移の規則
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (JobStatus.QUEUED, JobStatus.RUNNING),
        (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.FAILED),
        # ワーカーが落ちた行の回収
        (JobStatus.RUNNING, JobStatus.QUEUED),
        # 手動での再実行
        (JobStatus.FAILED, JobStatus.QUEUED),
    ],
)
def test_allowed_transitions(current: JobStatus, new: JobStatus) -> None:
    check_transition(current, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        # 実行せずに成功にはできない
        (JobStatus.QUEUED, JobStatus.SUCCEEDED),
        (JobStatus.QUEUED, JobStatus.FAILED),
        # 成功は終端。二重完了を弾く
        (JobStatus.SUCCEEDED, JobStatus.RUNNING),
        (JobStatus.SUCCEEDED, JobStatus.FAILED),
    ],
)
def test_rejected_transitions(current: JobStatus, new: JobStatus) -> None:
    with pytest.raises(InvalidJobTransition):
        check_transition(current, new)


def test_finishing_twice_is_rejected(repository: JobRepository) -> None:
    """終わったジョブをもう一度終わらせられないこと。

    ワーカーが二重に走ったときに、後から来た方が結果を
    上書きしてしまうのを防ぐ。
    """
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    job = repository.claim_next("worker-1")
    assert job is not None
    repository.mark_succeeded(job.id)

    with pytest.raises(InvalidJobTransition):
        repository.mark_failed(job.id, "あとから来た失敗")

    assert repository.get(job.id) is not None
    assert repository.get(job.id).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# 投入と取得
# --------------------------------------------------------------------------


def test_enqueue_creates_one_row_per_article(repository: JobRepository) -> None:
    batch_id = repository.enqueue_batch(ARTICLES, video_format="tiktok", language="ja")
    jobs = repository.list_batch(batch_id)
    assert [j.article_id for j in jobs] == ["a-1", "a-2", "a-3"]
    assert all(j.status is JobStatus.QUEUED for j in jobs)
    assert all(j.video_format == "tiktok" for j in jobs)


def test_enqueue_rejects_an_empty_batch(repository: JobRepository) -> None:
    with pytest.raises(ValueError):
        repository.enqueue_batch([], video_format="short")


def test_claim_takes_the_oldest_first(repository: JobRepository) -> None:
    """投入順に処理すること（選んだ順に出来上がる方が分かりやすい）。"""
    repository.enqueue_batch(ARTICLES, video_format="short")
    claimed = [repository.claim_next("w") for _ in range(3)]
    assert [j.article_id for j in claimed if j] == ["a-1", "a-2", "a-3"]


def test_claim_marks_running_with_a_lease(repository: JobRepository) -> None:
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    job = repository.claim_next("worker-1", lease_seconds=60)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.worker_id == "worker-1"
    assert job.attempts == 1
    assert job.started_at is not None
    assert job.lease_expires_at is not None
    assert job.lease_expires_at > utcnow()


def test_claim_returns_none_when_empty(repository: JobRepository) -> None:
    """キューが空なら None（エラーではない）。"""
    assert repository.claim_next("worker-1") is None


def test_a_job_is_claimed_by_only_one_worker(repository: JobRepository) -> None:
    """同じジョブを2人が掴まないこと。

    掴めた回数が投入数と一致し、それ以上にならないことを見る。
    ここが壊れると同じ記事を二重に生成し、画像生成のクォータを倍使う。
    """
    repository.enqueue_batch(ARTICLES, video_format="short")

    claimed: list[GenerationJob] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            job = repository.claim_next(name)
            if job is None:
                return
            with lock:
                claimed.append(job)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(claimed) == len(ARTICLES)
    assert len({j.id for j in claimed}) == len(ARTICLES), "同じジョブが複数回掴まれた"


# --------------------------------------------------------------------------
# リースの回収
# --------------------------------------------------------------------------


def _expire_lease(db_url: str, job_id: int) -> None:
    """リースを過去に書き換える（ワーカーが落ちた状況を作る）。"""
    factory = create_session_factory(create_db_engine(db_url))
    with session_scope(factory) as session:
        record = session.get(JobRecord, job_id)
        assert record is not None
        record.lease_expires_at = utcnow() - timedelta(minutes=1)


def test_expired_lease_is_requeued(repository: JobRepository, db_url: str) -> None:
    """期限切れの RUNNING が QUEUED に戻ること。

    これが無いと、落ちたワーカーが握っていた仕事が永久に
    「実行中」のまま残り、進捗が止まったように見える。
    """
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    job = repository.claim_next("dead-worker")
    assert job is not None
    _expire_lease(db_url, job.id)

    assert repository.requeue_expired() == 1

    recovered = repository.get(job.id)
    assert recovered is not None
    assert recovered.status is JobStatus.QUEUED
    assert recovered.worker_id is None
    # 別のワーカーが掴めること
    retaken = repository.claim_next("live-worker")
    assert retaken is not None
    assert retaken.id == job.id
    assert retaken.attempts == 2


def test_a_live_lease_is_not_requeued(repository: JobRepository) -> None:
    """期限内の RUNNING を奪わないこと。

    奪うと、生きているワーカーの仕事が二重に走る。
    """
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    assert repository.claim_next("worker-1", lease_seconds=600) is not None
    assert repository.requeue_expired() == 0


def test_too_many_attempts_becomes_failed(repository: JobRepository, db_url: str) -> None:
    """試行を繰り返しても終わらないジョブは打ち切ること。

    特定の記事で必ず落ちる場合に、無限に再実行して
    画像生成のクォータを食い潰すのを防ぐ。
    """
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    job_id: int | None = None
    for _ in range(3):
        job = repository.claim_next("dying-worker")
        assert job is not None
        job_id = job.id
        _expire_lease(db_url, job.id)
        repository.requeue_expired(max_attempts=3)

    assert job_id is not None
    final = repository.get(job_id)
    assert final is not None
    assert final.status is JobStatus.FAILED
    assert final.error_message is not None
    assert "3回" in final.error_message


def test_heartbeat_extends_the_lease(repository: JobRepository) -> None:
    """リースを延ばせること（長尺はリースより長くなりうる）。"""
    repository.enqueue_batch(ARTICLES[:1], video_format="long")
    job = repository.claim_next("worker-1", lease_seconds=60)
    assert job is not None
    first = job.lease_expires_at
    assert first is not None

    repository.heartbeat(job.id, lease_seconds=600)

    updated = repository.get(job.id)
    assert updated is not None
    assert updated.lease_expires_at is not None
    assert updated.lease_expires_at > first


# --------------------------------------------------------------------------
# 進捗の集計
# --------------------------------------------------------------------------


def test_progress_is_idle_with_no_jobs(repository: JobRepository) -> None:
    progress = repository.latest_progress()
    assert progress.status == "idle"
    assert progress.total_count == 0
    assert progress.is_running is False


def test_progress_reports_the_running_article(repository: JobRepository) -> None:
    repository.enqueue_batch(ARTICLES, video_format="short")
    repository.claim_next("worker-1")

    progress = repository.latest_progress()
    assert progress.status == "running"
    assert progress.current_article == "記事1"
    assert progress.total_count == 3
    assert progress.completed_count == 0


def test_progress_counts_successes_and_failures(repository: JobRepository) -> None:
    repository.enqueue_batch(ARTICLES, video_format="short")
    first = repository.claim_next("w")
    second = repository.claim_next("w")
    third = repository.claim_next("w")
    assert first and second and third
    repository.mark_succeeded(first.id, video_key="videos/1.mp4")
    repository.mark_failed(second.id, "画像生成に失敗しました")
    repository.mark_succeeded(third.id)

    progress = repository.latest_progress()
    assert progress.completed_count == 3
    assert progress.completed_articles == ("記事1", "記事3")
    assert progress.failed_articles == ("記事2",)
    # 1件でも失敗したら error として出す（成功分も UI に出る）
    assert progress.status == "error"
    assert progress.error_message == "画像生成に失敗しました"


def test_progress_is_success_when_all_succeed(repository: JobRepository) -> None:
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    job = repository.claim_next("w")
    assert job is not None
    repository.mark_succeeded(job.id)
    assert repository.latest_progress().status == "success"


def test_progress_prefers_the_active_batch(repository: JobRepository) -> None:
    """実行中のバッチがあればそれを出すこと。

    完了済みの新しいバッチで実行中の進捗が隠れると、
    UI が「終わった」と表示してしまう。
    """
    old = repository.enqueue_batch(ARTICLES[:1], video_format="short")
    new = repository.enqueue_batch(ARTICLES[1:2], video_format="short")
    # 新しい方を先に終わらせる
    for _ in range(2):
        job = repository.claim_next("w")
        assert job is not None
        if job.batch_id == new:
            repository.mark_succeeded(job.id)

    assert repository.latest_progress().batch_id == old


def test_has_active_jobs(repository: JobRepository) -> None:
    assert repository.has_active_jobs() is False
    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    assert repository.has_active_jobs() is True
    job = repository.claim_next("w")
    assert job is not None
    assert repository.has_active_jobs() is True
    repository.mark_succeeded(job.id)
    assert repository.has_active_jobs() is False


def test_batch_progress_from_empty_jobs() -> None:
    assert BatchProgress.from_jobs([]).status == "idle"


# --------------------------------------------------------------------------
# ワーカーのループ
# --------------------------------------------------------------------------


def test_worker_runs_queued_jobs(repository: JobRepository) -> None:
    """投入したジョブをワーカーが実行し、成功として記録すること。"""
    executed: list[str] = []
    done = threading.Event()

    def runner(job: GenerationJob) -> str:
        executed.append(job.article_id)
        if len(executed) == len(ARTICLES):
            done.set()
        return f"videos/{job.article_id}.mp4"

    repository.enqueue_batch(ARTICLES, video_format="short")
    worker = JobWorker(repository, runner, poll_interval=0.05)
    worker.start()
    try:
        assert done.wait(timeout=20), f"全件実行されなかった: {executed}"
    finally:
        worker.stop(timeout=10)

    assert sorted(executed) == ["a-1", "a-2", "a-3"]
    progress = repository.latest_progress()
    assert progress.status == "success"
    assert progress.completed_count == 3
    # 成功時は動画のキーを行に残す
    jobs = repository.list_batch(progress.batch_id or "")
    assert all(j.video_key == f"videos/{j.article_id}.mp4" for j in jobs)


def test_worker_records_the_failure_reason(repository: JobRepository) -> None:
    """失敗の理由を行に残すこと（UI がこれを表示する）。"""
    done = threading.Event()

    def failing_runner(job: GenerationJob) -> str:
        try:
            raise RuntimeError("画像生成のクォータが足りません")
        finally:
            done.set()

    repository.enqueue_batch(ARTICLES[:1], video_format="short")
    worker = JobWorker(repository, failing_runner, poll_interval=0.05)
    worker.start()
    try:
        assert done.wait(timeout=20)
        # mark_failed が走るのを待つ
        deadline = threading.Event()
        for _ in range(100):
            if repository.latest_progress().status == "error":
                break
            deadline.wait(0.05)
    finally:
        worker.stop(timeout=10)

    progress = repository.latest_progress()
    assert progress.status == "error"
    assert progress.error_message == "画像生成のクォータが足りません"


def test_worker_keeps_going_after_a_failure(repository: JobRepository) -> None:
    """1件失敗しても残りを実行すること。

    ループが例外で止まると、以降のジョブが永久に QUEUED で残る。
    """
    seen: list[str] = []
    done = threading.Event()

    def flaky_runner(job: GenerationJob) -> str:
        seen.append(job.article_id)
        if len(seen) == len(ARTICLES):
            done.set()
        if job.article_id == "a-1":
            raise RuntimeError("この1件だけ失敗")
        return "videos/x.mp4"

    repository.enqueue_batch(ARTICLES, video_format="short")
    worker = JobWorker(repository, flaky_runner, poll_interval=0.05)
    worker.start()
    try:
        assert done.wait(timeout=20), f"残りが実行されなかった: {seen}"
    finally:
        worker.stop(timeout=10)

    assert sorted(seen) == ["a-1", "a-2", "a-3"]


def test_worker_stop_is_idempotent(repository: JobRepository) -> None:
    worker = JobWorker(repository, lambda job: None, poll_interval=0.05)
    worker.stop()  # 起動前でも落ちない
    worker.start()
    worker.stop(timeout=10)
    assert worker.is_running is False
    worker.stop()


# --------------------------------------------------------------------------
# ジョブ -> パイプライン
# --------------------------------------------------------------------------


class FakeArticle:
    def __init__(
        self,
        article_id: str,
        title: str,
        content: str,
        url: str = "https://example.com/a-1",
    ):
        self.id = article_id
        self.title = title
        self.content = content
        self.url = url


class FakeArticleStore:
    def __init__(self, articles: dict[str, FakeArticle]):
        self._articles = articles
        self.generated: list[str] = []

    def get_article_by_id(self, article_id: str) -> FakeArticle | None:
        return self._articles.get(article_id)

    def mark_as_generated(self, article_id: str) -> bool:
        self.generated.append(article_id)
        return True


class RecordingPipeline:
    def __init__(self, result: dict[str, object] | None = None):
        self.calls: list[dict[str, object]] = []
        self._result = result or {
            "artifact_keys": {"videos": {"ja": "videos/20260814_000000_ja.mp4"}}
        }

    def run(
        self,
        news_topic: str,
        languages: list[str] | None = None,
        output_name: str | None = None,
        video_format: str = "short",
        source_url: str = "",
    ) -> dict[str, object]:
        self.calls.append(
            {
                "topic": news_topic,
                "languages": languages,
                "output_name": output_name,
                "video_format": video_format,
                "source_url": source_url,
            }
        )
        return self._result


def _job(article_id: str = "a-1", language: str = "ja") -> GenerationJob:
    now = utcnow()
    return GenerationJob(
        id=1,
        batch_id="b-1",
        article_id=article_id,
        article_title="記事1",
        video_format="short",
        language=language,
        status=JobStatus.RUNNING,
        attempts=1,
        error_message=None,
        video_key=None,
        created_at=now,
        started_at=now,
        finished_at=None,
        worker_id="w",
        lease_expires_at=None,
    )


def test_runner_reads_the_article_from_the_store() -> None:
    """本文はジョブ行ではなくニュースストアから読むこと。

    以前はスクレイピング済みの記事オブジェクトを background task の
    引数としてメモリで渡していたため、再起動で本文ごと消えた。
    """
    store = FakeArticleStore({"a-1": FakeArticle("a-1", "記事1", "本文" * 50)})
    pipeline = RecordingPipeline()

    key = PipelineJobRunner(pipeline, store)(_job())

    assert key == "videos/20260814_000000_ja.mp4"
    assert pipeline.calls[0]["languages"] == ["ja"]
    assert pipeline.calls[0]["video_format"] == "short"
    assert "記事1" in str(pipeline.calls[0]["topic"])
    assert store.generated == ["a-1"]


def test_runner_passes_the_source_url_out_of_band() -> None:
    """出典 URL は引数で渡し、トピック（プロンプト入力）には入れないこと。

    説明文への出典記載は再利用コンテンツ対策で必須だが、モデルに URL を
    渡すと本文に書き込もうとする。追記はコード側で行う。
    """
    store = FakeArticleStore(
        {"a-1": FakeArticle("a-1", "記事1", "本文", url="https://example.com/orig")}
    )
    pipeline = RecordingPipeline()

    PipelineJobRunner(pipeline, store)(_job())

    assert pipeline.calls[0]["source_url"] == "https://example.com/orig"
    assert "https://example.com/orig" not in str(pipeline.calls[0]["topic"])


def test_runner_truncates_long_content() -> None:
    """本文を切り詰めること（トークン上限に当たるため）。"""
    store = FakeArticleStore({"a-1": FakeArticle("a-1", "記事1", "あ" * 5000)})
    pipeline = RecordingPipeline()
    PipelineJobRunner(pipeline, store)(_job())
    assert str(pipeline.calls[0]["topic"]).count("あ") == 2000


@pytest.mark.parametrize(
    "articles",
    [
        {},  # 記事が消えている
        {"a-1": FakeArticle("a-1", "記事1", "")},  # 本文が取れていない
    ],
)
def test_runner_fails_clearly_without_content(articles: dict[str, FakeArticle]) -> None:
    """記事や本文が無いときは、理由の分かる失敗にすること。

    パイプラインに空の本文を渡すと、台本生成のあたりで
    原因の分かりにくいエラーになる。
    """
    pipeline = RecordingPipeline()
    with pytest.raises(ArticleUnavailable):
        PipelineJobRunner(pipeline, FakeArticleStore(articles))(_job())
    assert pipeline.calls == [], "パイプラインを呼んではいけない"


def test_runner_tolerates_a_result_without_keys() -> None:
    """キーを持たない結果でもジョブを失敗にしないこと。"""
    store = FakeArticleStore({"a-1": FakeArticle("a-1", "記事1", "本文")})
    runner = PipelineJobRunner(RecordingPipeline(result={"status": "success"}), store)
    assert runner(_job()) is None
