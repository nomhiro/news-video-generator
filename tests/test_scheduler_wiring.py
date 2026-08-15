"""定期実行が画像カードの5点セットを実際に渡すことの検証（Ruling R20）。

Task 6 の当初実装は `plan_daily_posts` に `card_generator` /
`image_generator` / `artifacts` / `jobs` / `output_dir` を受け取れるように
しただけで、呼び出し元（`_build_scheduler`）がそれを渡していなかった。
その状態では `CARD` は永久に画像無しのフォールバックに留まり、
テストも実行中のアプリも「繋がっていない」ことを教えてくれない
（Dead code that looks live）。ここでは `AppContext.build` から
`plan_daily_posts` まで、実際のオブジェクトが渡ることを確認する。

実 Azure / 実 DB への接続は避ける。`Config` はローカルの SQLite / ローカルの
ファイルストアだけで完結する値に絞り、`plan_daily_posts` と
`plan_daily_batch`（動画側の計画）はフェイクに差し替えてネットワークを
一切叩かない。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import src.web.dependencies as dependencies
from config import Config
from src.jobs.post_planner import DailyPostPlan
from src.models.news import CHANNEL_X, NewsArticle, NewsCategory
from src.social.card_visual import CardVisualGenerator
from tests.test_config import REQUIRED_VALUES


def _config(tmp_path: Path) -> Config:
    """実 Azure / 実ネットワークに触れないローカル設定を作る。"""
    values: dict[str, object] = {
        **REQUIRED_VALUES,
        "output_dir": tmp_path / "output",
        "news_data_dir": tmp_path / "news",
        "database_url": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        "x_posting_switch_path": tmp_path / "x_posting.json",
        "schedule_enabled": True,
    }
    # test_config.py の `_config` と同じ理由で型検査を抑制する
    # （pydantic-settings のキーワード引数は生成された __init__ の型と
    # 静的には合わない）。
    return Config(_env_file=None, **values)  # type: ignore[arg-type,call-arg]


@pytest.fixture
def captured_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`plan_daily_posts` の呼び出しキーワード引数を記録する。

    `plan_daily_batch`（動画の計画）もフェイクにする。実装からニュース
    取得やジョブ投入を行わせず、X 投稿側の配線だけを見るため。
    """
    calls: dict[str, Any] = {}

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> DailyPostPlan:
        calls.update(kwargs)
        # 実物と同じ型を返す。呼び出し元は `skipped_reason` を読んで
        # ログに出すので、None を返すフェイクだと本物では起きない
        # AttributeError が `except Exception` に飲まれてしまう。
        return DailyPostPlan(group_ids=[])

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)
    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    return calls


def test_定期実行はplan_daily_postsに画像カードの5点セットを渡す(
    tmp_path: Path, captured_kwargs: dict[str, Any]
) -> None:
    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None  # schedule_enabled=True にしてある

        asyncio.run(context.scheduler._task())

        # 5点すべてが None ではなく、しかも「別の何か」ではなく
        # AppContext が実際に持っているインスタンスと同一であること。
        assert captured_kwargs["card_generator"] is not None
        assert isinstance(captured_kwargs["card_generator"], CardVisualGenerator)
        # Pipeline が既に持つ ImageGenerator を再利用している
        # （同じ gpt-image-2 クォータに対して2つ目のクライアントを
        # 作っていないことの確認）。
        assert captured_kwargs["image_generator"] is context.pipeline.image_generator
        assert captured_kwargs["artifacts"] is context.artifact_store
        assert captured_kwargs["jobs"] is context.jobs
        assert captured_kwargs["output_dir"] == context.config.output_dir / "cards"
    finally:
        context.aggregator.close()


def test_投稿ワーカーの_on_posted_が記事ストアに繋がっている(tmp_path: Path) -> None:
    """`on_posted=` の配線を消してもテストが緑のままだった（I4）。

    `post_due_once` 側のコールバック呼び出しは
    `tests/test_post_worker.py` が見張っているが、それは
    「渡されたものを呼ぶ」ことしか確認していない。`AppContext.build` が
    渡すのを忘れると、記事は永久に未消費のまま**全記事が毎日再投稿される**。
    ここでは実際のコールバックを呼んで、記事ストアに書かれることを見る。
    """
    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        article = NewsArticle(
            id=NewsArticle.generate_id("https://example.com/a"),
            title="記事",
            url="https://example.com/a",
            source="Example",
            category=NewsCategory.AI,
        )
        context.aggregator._save_category(NewsCategory.AI, [article])

        callback = context.post_worker._on_posted
        assert callback is not None, "on_posted が PostWorker に渡っていない"
        callback(article.id)

        reloaded = context.aggregator.get_article_by_id(article.id)
        assert reloaded is not None
        assert reloaded.is_consumed_by(CHANNEL_X) is True
    finally:
        context.aggregator.close()


def test_動画ジョブを積んでもカードは作られる(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定期実行は X の計画を動画より**先に**立てる（C1 の回帰）。

    `plan_daily_posts` はカードを作る前に `jobs.has_active_jobs()` を見て、
    実行待ち・実行中の動画ジョブがあれば CARD を SINGLE に降格する。
    動画を先に積むとこの判定が**常に True** になり、画像カードの機能全体が
    本番で一度も動かない。

    上のテストが `plan_daily_batch` を no-op のフェイクにしていたため、
    この不具合はテストからは見えなかった。ここでは
    **実際にジョブ表へ行を積むフェイク**を使い、その状態でも
    `plan_daily_posts` が CARD を作れることを確認する。
    """
    enqueued_before_posts: list[bool] = []

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        # 実物と同じことをする: ジョブ表に QUEUED の行を積む。
        # no-op にするとこの回帰は再現しない。
        context.jobs.enqueue_batch([("a1", "記事")], video_format="short")

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> DailyPostPlan:
        jobs = kwargs["jobs"]
        enqueued_before_posts.append(jobs.has_active_jobs())
        return DailyPostPlan(group_ids=[])

    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)

    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None

        asyncio.run(context.scheduler._task())

        # 投稿計画の時点でジョブ表が空であること。True だと
        # `_build_card_image` が必ず SINGLE に降格する。
        assert enqueued_before_posts == [False]
        # 動画側も走っていること（順序を直しただけで、片方を
        # 止めてしまっていないことの確認）。
        assert context.jobs.has_active_jobs() is True
    finally:
        context.aggregator.close()
