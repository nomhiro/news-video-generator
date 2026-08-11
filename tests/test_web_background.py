"""生成中も Web サーバーが応答することの検証。

守っている欠陥
--------------
`generate_videos_task` は当初 `async def` だった。Starlette の
BackgroundTask は非同期関数をイベントループ上で直接 await するため
（starlette/background.py）、内部で呼ばれる完全同期の `pipeline.run()`
（ネットワークI/O、time.sleep、ffmpeg の subprocess）がイベントループを
数分間占有し、**生成中は Web サーバー全体が応答しなくなっていた**。
進捗を取るための `/status` すら返らないので、UI は固まったままになる。

修正は「関数を同期にする」こと。そうすると Starlette が threadpool に
回してくれる。うっかり `async def` に戻すと欠陥が復活するため、
ここで実際に走らせて確かめる。
"""

import inspect
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from src.web import routes
from src.web.dependencies import GenerationState


def test_background_task_is_synchronous() -> None:
    """タスク関数が同期であること。

    `async def` に戻すと Starlette がイベントループ上で実行し、
    生成中サーバー全体が止まる。
    """
    assert not inspect.iscoroutinefunction(routes.generate_videos_task), (
        "generate_videos_task は同期関数でなければならない。"
        "async def にすると Starlette がイベントループ上で実行し、"
        "生成中にサーバー全体が応答しなくなる。"
    )


def _free_port() -> int:
    """空いている TCP ポートを取得する。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_real_server_answers_while_generation_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際のサーバーで、生成中に /status が応答すること。

    **TestClient では検証できない。** TestClient はバックグラウンドタスクを
    リクエストの処理内で完了させてしまうため、「生成が進行中に別の
    リクエストを受ける」状況をそもそも再現できない。
    そこで uvicorn を別スレッドで実際に起動し、本物の HTTP で確かめる。

    generate_videos_task を `async def` に戻すと、/status がブロックされて
    このテストはタイムアウトで落ちる。
    """
    from fastapi import FastAPI

    from src.models.news import NewsArticle, NewsCategory
    from src.web import dependencies

    article = NewsArticle(
        id="test-1",
        title="テスト記事",
        url="https://example.com/a",
        source="テスト",
        category=NewsCategory.AI,
        content="本文" * 50,
        is_selected=True,
    )

    generation_started = threading.Event()
    release_generation = threading.Event()

    class FakeAggregator:
        """スクレイピングも保存もしない差し替え。"""

        async def scrape_selected_content(self) -> list[NewsArticle]:
            return [article]

        def mark_as_generated(self, article_id: str) -> bool:
            return True

        def get_selected_count(self) -> int:
            return 1

    class BlockingPipeline:
        """pipeline.run() のように、解放されるまで戻らない差し替え。"""

        def run(self, *args: object, **kwargs: object) -> dict[str, object]:
            generation_started.set()
            release_generation.wait(timeout=20)
            return {"status": "success"}

    state = GenerationState()
    monkeypatch.setattr(dependencies, "_aggregator", FakeAggregator())
    monkeypatch.setattr(dependencies, "_pipeline", BlockingPipeline())
    monkeypatch.setattr(dependencies, "_generation_state", state)

    app = FastAPI()
    app.include_router(routes.router)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        # 起動待ち
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not server.started:
            time.sleep(0.05)
        assert server.started, "uvicorn が起動しなかった"

        with httpx.Client(base_url=base, timeout=10.0) as client:
            assert client.post("/generate", data={"video_format": "short"}).status_code == 200
            assert generation_started.wait(timeout=10), "生成が開始されなかった"

            # 生成が進行中。ここで /status が素早く返らなければ
            # イベントループが占有されている。
            began = time.monotonic()
            response = client.get("/status", timeout=5.0)
            elapsed = time.monotonic() - began

            assert response.status_code == 200
            assert elapsed < 3.0, f"/status の応答に {elapsed:.1f}秒かかった（ブロックされている）"
            assert state.snapshot().is_running is True, "生成が進行中でない"
    finally:
        release_generation.set()
        server.should_exit = True
        thread.join(timeout=15)


# --------------------------------------------------------------------------
# GenerationState のスレッド安全性
#
# 生成は threadpool のスレッドで状態を更新し、/status はイベントループ側から
# 読む。両者が同時に触るためロックで守っている。
# --------------------------------------------------------------------------


def test_snapshot_is_internally_consistent_under_concurrent_updates() -> None:
    """並行更新中に取った写しが矛盾しないこと。

    completed_count と completed_articles を別々に読むと
    「カウントは増えたがリストにはまだ入っていない」状態を
    観測しうる。写しはロック内で一度に取るので一致する。
    """
    state = GenerationState()
    total = 200
    state.start(total)
    stop = threading.Event()
    inconsistencies: list[str] = []

    def writer() -> None:
        for i in range(total):
            state.update(f"記事{i}")
            state.complete_one(f"記事{i}", success=i % 5 != 0)
        stop.set()

    def reader() -> None:
        while not stop.is_set():
            snap = state.snapshot()
            counted = len(snap.completed_articles) + len(snap.failed_articles)
            if counted != snap.completed_count:
                inconsistencies.append(
                    f"completed_count={snap.completed_count} だが リストの合計は {counted}"
                )

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not inconsistencies, inconsistencies[:5]
    final = state.snapshot()
    assert final.completed_count == total
    assert len(final.completed_articles) + len(final.failed_articles) == total


def test_counts_are_not_lost_under_concurrent_writers() -> None:
    """複数スレッドからの更新でカウントが失われないこと。"""
    state = GenerationState()
    state.start(0)
    per_thread = 100
    thread_count = 4

    def writer(offset: int) -> None:
        for i in range(per_thread):
            state.complete_one(f"記事{offset}-{i}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    snap = state.snapshot()
    assert snap.completed_count == per_thread * thread_count
    assert len(snap.completed_articles) == per_thread * thread_count


# --------------------------------------------------------------------------
# 状態遷移
# --------------------------------------------------------------------------


def test_initial_status_is_idle() -> None:
    assert GenerationState().snapshot().status == "idle"


def test_status_is_running_after_start() -> None:
    state = GenerationState()
    state.start(3)
    snap = state.snapshot()
    assert snap.status == "running"
    assert snap.is_running is True
    assert snap.total_count == 3


def test_status_is_success_after_finishing_work() -> None:
    state = GenerationState()
    state.start(1)
    state.complete_one("記事")
    state.finish()
    assert state.snapshot().status == "success"


def test_status_is_error_when_finished_with_a_message() -> None:
    state = GenerationState()
    state.start(1)
    state.finish(error="失敗しました")
    snap = state.snapshot()
    assert snap.status == "error"
    assert snap.error_message == "失敗しました"


def test_start_clears_previous_results() -> None:
    """再実行時に前回の結果が残らないこと。"""
    state = GenerationState()
    state.start(1)
    state.complete_one("前回の記事", success=False)
    state.finish(error="前回のエラー")

    state.start(2)
    snap = state.snapshot()
    assert snap.completed_count == 0
    assert snap.completed_articles == ()
    assert snap.failed_articles == ()
    assert snap.error_message is None
    assert snap.total_count == 2


def test_finish_clears_the_current_article() -> None:
    state = GenerationState()
    state.start(1)
    state.update("処理中の記事")
    assert state.snapshot().current_article == "処理中の記事"
    state.finish()
    assert state.snapshot().current_article is None


def test_snapshot_lists_are_immutable() -> None:
    """写しのリストを呼び出し側が書き換えられないこと。

    可変のまま返すと、テンプレート側の操作が内部状態に漏れる。
    """
    state = GenerationState()
    state.start(1)
    state.complete_one("記事")
    snap = state.snapshot()
    assert isinstance(snap.completed_articles, tuple)
    with pytest.raises(AttributeError):
        snap.completed_articles.append("追加")  # type: ignore[attr-defined]
