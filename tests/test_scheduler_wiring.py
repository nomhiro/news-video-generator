"""定期実行が画像カードの5点セットを実際に渡すことの検証（Ruling R20）。

Task 6 の当初実装は `plan_daily_posts` に `card_generator` /
`image_generator` / `artifacts` / `jobs` / `output_dir` を受け取れるように
しただけで、呼び出し元（`_build_scheduler`）がそれを渡していなかった。
その状態では `CARD` は永久に画像無しのフォールバックに留まり、
テストも実行中のアプリも「繋がっていない」ことを教えてくれない
（Dead code that looks live）。ここでは `AppContext.build` から
`plan_daily_posts` まで、実際のオブジェクトが渡ることを確認する。

実 Azure / 実 DB への接続は避ける。`Config` はローカルの SQLite / ローカルの
ファイルストアだけで完結する値に絞り、`plan_daily_posts` /
`plan_daily_batch`（動画側の計画）/ `fetch_daily_news`（ニュース取得）を
フェイクに差し替えてネットワークを一切叩かない。

**取得のフェイクを忘れないこと。** 取得は `plan_daily_batch` の中から
切り出して日次タスクの先頭に移したので、計画2つだけをフェイクにしても
実ネットワークを叩く。
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


def _fetched_article() -> NewsArticle:
    """「このサイクルの取得で入ってきた記事」を表す1件。"""
    url = "https://example.com/fetched-now"
    return NewsArticle(
        id=NewsArticle.generate_id(url),
        title="今回のサイクルで取得した記事",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


@pytest.fixture
def captured_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`plan_daily_posts` の呼び出しキーワード引数を記録する。

    `plan_daily_batch`（動画の計画）と `fetch_daily_news`（取得）も
    フェイクにする。実装からニュース取得やジョブ投入を行わせず、
    X 投稿側の配線だけを見るため。

    **取得のフェイクは必須。** 取得は `plan_daily_batch` の中から
    切り出して日次タスクの先頭に移したので、`plan_daily_batch` だけを
    フェイクにしても実ネットワークを叩いてしまう。
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

    async def fake_fetch_daily_news(news: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)
    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    monkeypatch.setattr(dependencies, "fetch_daily_news", fake_fetch_daily_news)
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


def test_積まなかった理由をログに出す(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`DailyPostPlan.skipped_reason` を呼び出し元が捨てていた（I10）。

    捨てていた間、「予算上限で止まった」「X で未使用の記事が無い」と
    いった判断がログにも画面にも残らず、投稿が出ない日と「そもそも計画が
    走っていない」日を区別できなかった。
    """
    logged: list[str] = []

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_fetch_daily_news(news: Any, **kwargs: Any) -> None:
        return None

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> DailyPostPlan:
        return DailyPostPlan(group_ids=[], skipped_reason="予算上限（概算 $40.00）")

    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)
    monkeypatch.setattr(dependencies, "fetch_daily_news", fake_fetch_daily_news)
    monkeypatch.setattr(
        dependencies, "log_step", lambda message, *args, **kwargs: logged.append(message)
    )

    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None
        asyncio.run(context.scheduler._task())
    finally:
        context.aggregator.close()

    assert any("予算上限（概算 $40.00）" in message for message in logged)


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
    """定期実行は「取得 → X の計画 → 動画の投入」の順で走る（C1 の回帰）。

    2つのことを同時に見張っている。片方だけを直すともう片方が壊れる。

    1. **X の計画は動画の投入より先。** `plan_daily_posts` はカードを作る前に
       `jobs.has_active_jobs()` を見て、実行待ち・実行中の動画ジョブがあれば
       CARD を SINGLE に降格する。動画を先に積むとこの判定が**常に True**
       になり、画像カードの機能全体が本番で一度も動かない。
    2. **取得は両方の計画より先。** 取得は元々 `plan_daily_batch` の中に
       あった。1 のために X を先に回すと、その位置では X が「このサイクルで
       取得した記事」を見られず、毎日1日古いニュースを投稿することになる。

    上のテストが `plan_daily_batch` を no-op のフェイクにしていたため、
    1 はテストからは見えなかった。ここでは**実際にジョブ表へ行を積む
    フェイク**を使う。
    """
    enqueued_before_posts: list[bool] = []
    # X の計画が「このサイクルで取得した記事」を見られたか
    seen_by_posts: list[list[str]] = []

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        # 実物と同じことをする: ジョブ表に QUEUED の行を積む。
        # no-op にするとこの回帰は再現しない。
        context.jobs.enqueue_batch([("a1", "記事")], video_format="short")

    async def fake_fetch_daily_news(news: Any, **kwargs: Any) -> None:
        # 実物と同じことをする: このサイクルの記事をストアに入れる。
        # no-op にすると「X が今回の取得を見られるか」を検証できない。
        news._save_category(NewsCategory.AI, [_fetched_article()])

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> DailyPostPlan:
        jobs = kwargs["jobs"]
        enqueued_before_posts.append(jobs.has_active_jobs())
        news = args[0]
        seen_by_posts.append([a.title for a in news.pick_unconsumed(CHANNEL_X, 4)])
        return DailyPostPlan(group_ids=[])

    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)
    monkeypatch.setattr(dependencies, "fetch_daily_news", fake_fetch_daily_news)

    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None

        asyncio.run(context.scheduler._task())

        # 投稿計画の時点でジョブ表が空であること。True だと
        # `_build_card_image` が必ず SINGLE に降格する。
        assert enqueued_before_posts == [False]
        # 投稿計画が今回取得した記事を見られていること。取得が
        # `plan_daily_batch` の中に戻ると、ここは空になる。
        assert seen_by_posts == [["今回のサイクルで取得した記事"]]
        # 動画側も走っていること（順序を直しただけで、片方を
        # 止めてしまっていないことの確認）。
        assert context.jobs.has_active_jobs() is True
    finally:
        context.aggregator.close()


def test_日次タスクは取得を先に1回だけ呼ぶ(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取得は3段の先頭で1回だけ。`plan_daily_batch` には fetch=False を渡す。

    渡し忘れると同じサイクルで2回取得することになる（無駄な HTTP と、
    「取得を切り出した意味が半分無くなっている」ことに誰も気付けない）。
    """
    order: list[str] = []

    async def fake_fetch_daily_news(news: Any, **kwargs: Any) -> None:
        order.append("fetch")

    async def fake_plan_daily_batch(*args: Any, **kwargs: Any) -> None:
        order.append(f"video(fetch={kwargs['fetch']})")

    def fake_plan_daily_posts(*args: Any, **kwargs: Any) -> DailyPostPlan:
        order.append("posts")
        return DailyPostPlan(group_ids=[])

    monkeypatch.setattr(dependencies, "fetch_daily_news", fake_fetch_daily_news)
    monkeypatch.setattr(dependencies, "plan_daily_batch", fake_plan_daily_batch)
    monkeypatch.setattr(dependencies, "plan_daily_posts", fake_plan_daily_posts)

    context = dependencies.AppContext.build(_config(tmp_path))
    try:
        assert context.scheduler is not None
        asyncio.run(context.scheduler._task())
    finally:
        context.aggregator.close()

    assert order == ["fetch", "posts", "video(fetch=False)"]
