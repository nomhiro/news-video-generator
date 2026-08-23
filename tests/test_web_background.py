"""生成中も Web サーバーが応答することの検証。

守っている欠陥
--------------
生成（`pipeline.run()`）は完全同期で、ネットワークI/O・ffmpeg の
subprocess を含み数分かかる。これがイベントループ上で走ると、
生成中は Web サーバー全体が応答しなくなる。`/status` すら返らないので
UI は固まったままになる。**これは実際に起きていた欠陥**（当時は
`generate_videos_task` が `async def` で、Starlette の BackgroundTask が
非同期関数をイベントループ上で直接 await するため）。

現在の構造では、生成はジョブ表を経由してワーカースレッドが実行する。
`/generate` は行を作るだけ、`/status` は行を読むだけ。したがって
守るべき性質は次の2つになった。

1. `/generate` がリクエスト処理の中で生成を始めない（投入だけ）
2. ワーカーが実際にジョブを実行している最中でも `/status` が即座に返る

2 は TestClient では検証できない（TestClient はリクエスト処理の中で
完結してしまい、「別スレッドで生成が進行中」の状況を作れない）。
uvicorn を実際に起動して本物の HTTP で確かめる。
"""

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from src.jobs.worker import JobWorker
from src.models.job import GenerationJob, JobStatus
from src.storage.db import create_db_engine, create_session_factory
from src.storage.jobs import JobRepository
from src.storage.schema import upgrade_to_head
from src.web import routes
from src.web.dependencies import get_aggregator, get_jobs


@pytest.fixture
def repository(tmp_path: Path) -> JobRepository:
    """一時ファイルの SQLite にジョブ表を作る。

    :memory: を使わない理由: ワーカースレッドと HTTP ハンドラが別々の
    接続で同じ DB を見る必要があり、in-memory は接続ごとに別の DB になる。
    """
    url = f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    upgrade_to_head(url)
    return JobRepository(create_session_factory(create_db_engine(url)))


def _free_port() -> int:
    """空いている TCP ポートを取得する。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeAggregator:
    """スクレイピングも保存もしない差し替え。"""

    def __init__(self, articles: list[object]):
        self._articles = articles

    async def scrape_selected_content(self) -> list[object]:
        return self._articles

    def get_selected_count(self) -> int:
        return len(self._articles)


class FakeArticle:
    """`scrape_selected_content` の戻り値として最小限の形。"""

    def __init__(
        self,
        article_id: str,
        title: str,
        content: str = "本文" * 60,
        url: str = "https://example.com/a",
    ):
        self.id = article_id
        self.title = title
        self.content = content
        # 出典 URL は台本の説明文に載せるため PipelineJobRunner が読む
        self.url = url


@pytest.fixture
def app(repository: JobRepository) -> Iterator[tuple[FastAPI, JobRepository]]:
    application = FastAPI()
    application.include_router(routes.router)
    application.dependency_overrides[get_jobs] = lambda: repository
    application.dependency_overrides[get_aggregator] = lambda: FakeAggregator(
        [FakeArticle("a-1", "テスト記事1"), FakeArticle("a-2", "テスト記事2")]
    )
    yield application, repository


def test_generate_only_enqueues(app: tuple[FastAPI, JobRepository]) -> None:
    """`/generate` は行を作るだけで、生成を始めないこと。

    リクエスト処理の中で生成を始めると、それが同期なら
    イベントループを、非同期ならワーカーの意味を壊す。
    """
    from fastapi.testclient import TestClient

    application, repository = app
    with TestClient(application) as client:
        response = client.post("/generate", data={"video_format": "short"})

    assert response.status_code == 200
    jobs = repository.list_batch(repository.latest_batch_id() or "")
    assert [j.status for j in jobs] == [JobStatus.QUEUED, JobStatus.QUEUED]
    # まだ誰も掴んでいない
    assert all(j.worker_id is None and j.started_at is None for j in jobs)


def test_articles_without_content_are_not_dropped(repository: JobRepository) -> None:
    """本文が取れなかった記事も投入されること。

    以前はここで黙って捨てていた。選択したのにジョブが作られないため、
    「3件選んだのに2件しか出来ていない」理由を利用者が説明できなかった。
    投入しておけば、理由付きの失敗として `/status` に並ぶ。
    """
    from fastapi.testclient import TestClient

    application = FastAPI()
    application.include_router(routes.router)
    application.dependency_overrides[get_jobs] = lambda: repository
    application.dependency_overrides[get_aggregator] = lambda: FakeAggregator(
        [
            FakeArticle("ok", "本文あり"),
            FakeArticle("ng", "本文なし", content=""),
        ]
    )

    with TestClient(application) as client:
        assert client.post("/generate", data={"video_format": "short"}).status_code == 200

    titles = {j.article_title for j in repository.list_batch(repository.latest_batch_id() or "")}
    assert titles == {"本文あり", "本文なし"}


def test_a_missing_body_fails_with_a_reason(repository: JobRepository) -> None:
    """本文の無いジョブが、理由の分かる失敗になること。

    画像生成に到達する前に落ちるので、クォータは使わない。
    """
    from src.jobs.runner import PipelineJobRunner

    class Store:
        def get_article_by_id(self, article_id: str) -> FakeArticle | None:
            return FakeArticle(article_id, "本文なし", content="")

        def mark_as_generated(self, article_id: str) -> bool:  # pragma: no cover
            raise AssertionError("生成していないのに印を付けてはいけない")

        def mark_content_filtered(self, article_id: str) -> bool:  # pragma: no cover
            raise AssertionError("本文が無いだけの失敗を拒否として記録してはいけない")

    class ExplodingPipeline:
        def run(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("本文が無いのにパイプラインを呼んではいけない")

    repository.enqueue_batch([("ng", "本文なし")], video_format="short")
    worker = JobWorker(
        repository, PipelineJobRunner(ExplodingPipeline(), Store()), poll_interval=0.05
    )
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if repository.latest_progress().status == "error":
                break
            time.sleep(0.05)
    finally:
        worker.stop(timeout=10)

    progress = repository.latest_progress()
    assert progress.failed_articles == ("本文なし",)
    assert progress.error_message is not None
    assert "本文を取得できませんでした" in progress.error_message


def test_generate_does_not_stack_while_active(app: tuple[FastAPI, JobRepository]) -> None:
    """実行待ちがあるうちは積み増さないこと。

    同じ記事のジョブが二重に入ると、画像生成のクォータを無駄に使う。
    """
    from fastapi.testclient import TestClient

    application, repository = app
    with TestClient(application) as client:
        client.post("/generate", data={"video_format": "short"})
        client.post("/generate", data={"video_format": "short"})

    assert sum(repository.count_by_status().values()) == 2


def test_real_server_answers_while_the_worker_generates(
    app: tuple[FastAPI, JobRepository],
) -> None:
    """ワーカーが生成している最中に /status が即座に返ること。

    **TestClient では検証できない。** リクエスト処理の中で完結するため、
    「別スレッドで生成が進行中」という状況をそもそも作れない。
    uvicorn を別スレッドで実際に起動して本物の HTTP で確かめる。

    生成をイベントループ上で走らせる実装に戻すと、ここが
    タイムアウトで落ちる。
    """
    application, repository = app

    generation_started = threading.Event()
    release_generation = threading.Event()

    def blocking_runner(job: GenerationJob) -> str | None:
        """解放されるまで戻らない。pipeline.run() の代わり。"""
        generation_started.set()
        release_generation.wait(timeout=20)
        return "videos/fake.mp4"

    worker = JobWorker(repository, blocking_runner, poll_interval=0.05)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    worker.start()

    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.05)
        assert server.started, "uvicorn が起動しなかった"

        with httpx.Client(base_url=base, timeout=10.0) as client:
            assert client.post("/generate", data={"video_format": "short"}).status_code == 200
            assert generation_started.wait(timeout=10), "ワーカーが生成を開始しなかった"

            # 生成が進行中。ここで /status が素早く返らなければ
            # イベントループが占有されている。
            began = time.monotonic()
            response = client.get("/status", timeout=5.0)
            elapsed = time.monotonic() - began

            assert response.status_code == 200
            assert elapsed < 3.0, f"/status の応答に {elapsed:.1f}秒かかった（ブロックされている）"
            assert repository.latest_progress().is_running is True
    finally:
        release_generation.set()
        worker.stop(timeout=15)
        server.should_exit = True
        thread.join(timeout=15)


def test_progress_survives_a_new_process(repository: JobRepository, tmp_path: Path) -> None:
    """別のプロセス（別のリポジトリ実体）から同じ進捗が見えること。

    進捗をプロセスメモリに持っていた頃は、再起動で消え、
    レプリカを増やしても共有されなかった。行にしたので、
    同じ DB を指す別の実体から同じ値が読める。
    """
    repository.enqueue_batch([("a-1", "記事1"), ("a-2", "記事2")], video_format="short")
    claimed = repository.claim_next("worker-1")
    assert claimed is not None
    repository.mark_succeeded(claimed.id, video_key="videos/a.mp4")

    # 再起動を模して、同じ DB を指す新しい実体を作る
    url = f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    reopened = JobRepository(create_session_factory(create_db_engine(url)))

    progress = reopened.latest_progress()
    assert progress.total_count == 2
    assert progress.completed_count == 1
    assert progress.completed_articles == ("記事1",)
    assert progress.status == "running"  # 残り1件が QUEUED


def test_status_shows_successes_alongside_failures(repository: JobRepository) -> None:
    """一部失敗のときに、成功した記事も画面に出ること。

    1件でも失敗すると status は error になる。error の表示が
    メッセージだけだと「3件のうち2件は出来ている」ことが
    画面から読み取れない。
    """
    from fastapi.testclient import TestClient

    repository.enqueue_batch([("ok", "できた記事"), ("ng", "だめだった記事")], video_format="short")
    first = repository.claim_next("w")
    second = repository.claim_next("w")
    assert first and second
    repository.mark_succeeded(first.id, video_key="videos/ok.mp4")
    repository.mark_failed(second.id, "本文を取得できませんでした")

    application = FastAPI()
    application.include_router(routes.router)
    application.dependency_overrides[get_jobs] = lambda: repository

    with TestClient(application) as client:
        body = client.get("/status").text

    assert "できた記事" in body
    assert "だめだった記事" in body
    assert "本文を取得できませんでした" in body
    assert "一部が失敗しました" in body


def test_status_explains_a_content_filter_rejection(repository: JobRepository) -> None:
    """拒否の理由が画面で読めること（issue #30 の④）。

    直す前は「パイプライン実行に失敗しました: 台本生成に失敗しました:
    Error code: 400 - {...}」という3段ラップの生 JSON が出るだけで、
    **記事の題材が原因だと読み取れなかった**。

    文言は手で書かず、実際に本番で作られるものを使う（`_input_filter_message`）。
    手書きにすると、実装の文言を変えたときにテストだけが古い文言を守る。
    """
    from fastapi.testclient import TestClient

    from src.generators.script_generator import _input_filter_message
    from tests.test_content_filter import ISSUE_30_BODY, FakeBadRequest

    message = _input_filter_message(FakeBadRequest(body=ISSUE_30_BODY))

    repository.enqueue_batch([("ng", "成人向けサービスの発表記事")], video_format="short")
    job = repository.claim_next("w")
    assert job
    repository.mark_failed(job.id, message)

    application = FastAPI()
    application.include_router(routes.router)
    application.dependency_overrides[get_jobs] = lambda: repository

    with TestClient(application) as client:
        body = client.get("/status").text

    assert "成人向けサービスの発表記事" in body
    assert "コンテンツフィルタに拒否されました" in body
    # 拒否されたカテゴリは手掛かりとして出す
    assert "sexual" in body
    # 生の応答の断片は出さない
    assert "content_filter_offsets" not in body
    assert "end_offset" not in body


# --------------------------------------------------------------------------
# 失敗したジョブの再実行（#4）
# --------------------------------------------------------------------------


def _failed(repository: JobRepository, reason: str = "台本の検証に失敗しました") -> GenerationJob:
    """1回試行して失敗した行を作る。"""
    repository.enqueue_batch([("a-1", "落ちた記事")], video_format="short")
    job = repository.claim_next("worker-1")
    assert job is not None
    repository.mark_failed(job.id, reason)
    failed = repository.get(job.id)
    assert failed is not None
    return failed


def test_failed_jobs_are_listed_with_a_retry_button(app: tuple[FastAPI, JobRepository]) -> None:
    """一覧に記事名・理由・試行回数と再実行ボタンが出ること。"""
    from fastapi.testclient import TestClient

    application, repository = app
    _failed(repository)

    with TestClient(application) as client:
        response = client.get("/jobs/failed")

    assert response.status_code == 200
    body = response.text
    assert "落ちた記事" in body
    assert "台本の検証に失敗しました" in body
    assert "1/3回" in body
    assert "/retry" in body


def test_retry_queues_the_job_again(app: tuple[FastAPI, JobRepository]) -> None:
    """押すと実行待ちに戻り、一覧から消えること（押した場所が変わること）。"""
    from fastapi.testclient import TestClient

    application, repository = app
    failed = _failed(repository)

    with TestClient(application) as client:
        response = client.post(f"/jobs/{failed.id}/retry")

    assert response.status_code == 200
    back = repository.get(failed.id)
    assert back is not None
    assert back.status is JobStatus.QUEUED
    assert "落ちた記事" not in response.text
    # 生成状況も同じ応答で直す（out-of-band）。実行待ちがあるので「生成中」
    assert 'id="generation-status"' in response.text
    assert 'hx-swap-oob="innerHTML"' in response.text
    assert "生成中" in response.text


def test_retry_switches_the_status_panel_to_the_older_batch(
    app: tuple[FastAPI, JobRepository],
) -> None:
    """**古いバッチのジョブを戻しても生成状況がそちらに切り替わること。**

    ここが `/status` のパネルに再実行を置けない理由の裏返しでもある。
    あのパネルは直近1バッチしか見ないので、後から別のバッチが走ると
    古い失敗は見えなくなる。戻したときは `latest_batch_id()` が
    「実行待ちのあるバッチ」を優先するので、そちらに切り替わる。
    """
    from fastapi.testclient import TestClient

    application, repository = app
    failed = _failed(repository)
    # 後から別のバッチを積んで終わらせる（こちらが最新になる）
    repository.enqueue_batch([("a-2", "あとの記事")], video_format="short")
    later = repository.claim_next("worker-2")
    assert later is not None
    repository.mark_succeeded(later.id, video_key="videos/later.mp4")

    with TestClient(application) as client:
        before = client.get("/status").text
        assert "あとの記事" in before

        response = client.post(f"/jobs/{failed.id}/retry")

    # out-of-band 側が「あとのバッチ」から離れて、戻したバッチを映していること
    # （戻した行は QUEUED なので記事名はまだ出ない。出るのは「生成中」）
    oob = response.text.split('id="generation-status"')[1]
    assert "あとの記事" not in oob
    assert "生成中" in oob


def test_retry_of_a_succeeded_job_explains_itself(app: tuple[FastAPI, JobRepository]) -> None:
    """成功済み・上限到達を 500 にせず、読める理由で返すこと。

    htmx はエラー応答で対象を差し替えない。4xx/5xx にすると画面が黙って
    古いまま残り、押しても何も起きないように見える。
    """
    from fastapi.testclient import TestClient

    application, repository = app
    repository.enqueue_batch([("a-1", "成功した記事")], video_format="short")
    job = repository.claim_next("worker-1")
    assert job is not None
    repository.mark_succeeded(job.id, video_key="videos/ok.mp4")

    with TestClient(application) as client:
        response = client.post(f"/jobs/{job.id}/retry")

    assert response.status_code == 200
    assert "許可されていません" in response.text


def test_a_job_at_the_attempt_limit_has_no_button(app: tuple[FastAPI, JobRepository]) -> None:
    """上限に達した行にはボタンを出さず、語で示すこと。"""
    from fastapi.testclient import TestClient

    application, repository = app
    failed = _failed(repository)
    repository.retry(failed.id)
    for worker in ("w-2", "w-3"):
        job = repository.claim_next(worker)
        assert job is not None
        repository.mark_failed(job.id, "また落ちた")
        if worker == "w-2":
            repository.retry(job.id)

    with TestClient(application) as client:
        response = client.get("/jobs/failed")

    assert "3/3回" in response.text
    assert "/retry" not in response.text
    assert "上限" in response.text
