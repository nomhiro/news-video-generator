"""定期実行（スケジューラと計画）の検証。

守りたい性質
------------
- 再起動のたびに生成が始まらない（次回時刻まで待つ）
- 実行中のジョブがあるときに積み増さない
- 利用者の選択状態（`is_selected`）を触らない
- 1回失敗してもスケジューラが死なない
- ニュース取得が落ちても、既存の記事で続行する
"""

import threading
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.jobs.planner import fetch_daily_news, plan_daily_batch
from src.jobs.scheduler import DailyScheduler, next_run_at
from src.models.job import ORIGIN_SCHEDULE
from src.models.news import CHANNEL_VIDEO, NewsArticle, NewsCategory

JST = ZoneInfo("Asia/Tokyo")


# --------------------------------------------------------------------------
# 次回時刻の計算
# --------------------------------------------------------------------------


def test_runs_later_today_when_the_time_has_not_passed() -> None:
    now = datetime(2026, 8, 14, 3, 0, tzinfo=JST)
    assert next_run_at(now, time(6, 30), JST) == datetime(2026, 8, 14, 6, 30, tzinfo=JST)


def test_runs_tomorrow_when_the_time_has_passed() -> None:
    """過ぎていたら翌日にする（即実行しない）。

    即実行にすると、デプロイでリビジョンが入れ替わるたびに生成が始まり、
    リビジョン更新を繰り返した日に何本も作ってしまう。
    """
    now = datetime(2026, 8, 14, 9, 0, tzinfo=JST)
    assert next_run_at(now, time(6, 30), JST) == datetime(2026, 8, 15, 6, 30, tzinfo=JST)


def test_exactly_on_time_waits_for_tomorrow() -> None:
    """同時刻なら翌日（同じ回を二度走らせない）。"""
    now = datetime(2026, 8, 14, 6, 30, tzinfo=JST)
    assert next_run_at(now, time(6, 30), JST) == datetime(2026, 8, 15, 6, 30, tzinfo=JST)


def test_utc_input_is_converted_to_local() -> None:
    """UTC で渡しても、指定はローカル時刻として扱うこと。

    コンテナの時刻は UTC。JST の 06:30 に走らせたいので、
    ここを間違えると9時間ずれる。
    """
    now = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)  # = 8/14 05:00 JST
    assert next_run_at(now, time(6, 30), JST) == datetime(2026, 8, 14, 6, 30, tzinfo=JST)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        next_run_at(datetime(2026, 8, 14, 3, 0), time(6, 30), JST)


# --------------------------------------------------------------------------
# スケジューラのスレッド
# --------------------------------------------------------------------------


def test_scheduler_does_not_run_on_start() -> None:
    """起動しただけでは実行しないこと。"""
    ran = threading.Event()

    async def task() -> None:
        ran.set()

    # 次回は明日になる時刻を渡す
    now_local = datetime.now(JST)
    scheduler = DailyScheduler(task, run_at=time(now_local.hour, now_local.minute))
    scheduler.start()
    try:
        assert not ran.wait(1.0), "起動直後に実行された"
    finally:
        scheduler.stop(timeout=5)
    assert scheduler.is_running is False


def test_scheduler_stops_promptly_while_waiting() -> None:
    """待機中でも停止要求に即座に反応すること。

    sleep で待つと、デプロイ時のシャットダウンが最大1日待ちになる。
    """

    async def task() -> None:  # pragma: no cover - 実行されない
        raise AssertionError("実行されてはいけない")

    scheduler = DailyScheduler(task, run_at=time(4, 0))
    scheduler.start()
    began = datetime.now(UTC)
    scheduler.stop(timeout=5)
    elapsed = (datetime.now(UTC) - began).total_seconds()
    assert elapsed < 3.0, f"停止に {elapsed:.1f}秒かかった"


def test_scheduler_survives_a_failing_task() -> None:
    """タスクが例外を投げてもスレッドが死なないこと。

    死ぬと翌日以降も走らなくなり、気付くのが遅れる。
    """
    calls: list[int] = []
    done = threading.Event()

    async def task() -> None:
        calls.append(1)
        done.set()
        raise RuntimeError("失敗した")

    scheduler = DailyScheduler(task, run_at=time(0, 0))
    # 内部の待機を潰して即実行させる（次回時刻の計算は別のテストで見ている）
    scheduler._stop = threading.Event()
    original_wait = scheduler._stop.wait

    def instant_wait(timeout: float | None = None) -> bool:
        # 1回目だけ即座に抜け、以降は通常の待機に戻す
        scheduler._stop.wait = original_wait  # type: ignore[method-assign]
        return False

    scheduler._stop.wait = instant_wait  # type: ignore[method-assign]
    scheduler.start()
    try:
        assert done.wait(5.0), "タスクが実行されなかった"
        assert scheduler.is_running is True, "例外でスレッドが死んだ"
    finally:
        scheduler.stop(timeout=5)
    assert calls == [1]


# --------------------------------------------------------------------------
# 計画
# --------------------------------------------------------------------------


def _article(article_id: str, title: str, generated: bool = False) -> NewsArticle:
    article = NewsArticle(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source="テスト",
        category=NewsCategory.AI,
        content="本文" * 60,
    )
    if generated:
        article.mark_consumed(CHANNEL_VIDEO)
    return article


class FakeNews:
    """ニュースストアの差し替え。"""

    def __init__(self, articles: list[NewsArticle], fetch_fails: bool = False):
        self.articles = articles
        self.fetch_fails = fetch_fails
        self.fetch_calls = 0
        self.scraped: list[str] = []
        self.selection_touched = False

    async def fetch_and_store(self, limit_per_category: int = 10) -> dict:
        self.fetch_calls += 1
        if self.fetch_fails:
            raise RuntimeError("取得に失敗")
        return {}

    async def fetch_ai_news_and_store(
        self, feeds: object = (), limit_per_feed: int = 3
    ) -> list[NewsArticle]:
        if self.fetch_fails:
            raise RuntimeError("取得に失敗")
        return self.articles

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        self.scraped.extend(a.id for a in articles)
        return articles

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        picked = [a for a in self.articles if not a.is_consumed_by(channel)]
        return picked[:needed]

    # 定期実行が触ってはいけないもの
    def toggle_selection(self, article_id: str) -> bool | None:  # pragma: no cover
        self.selection_touched = True
        return True


class FakeJobs:
    """ジョブ表の差し替え。"""

    def __init__(self, busy: bool = False):
        self.busy = busy
        self.batches: list[tuple[str, list[tuple[str, str]]]] = []
        # 定期実行が渡した origin。拒否されたときに代替を積んでよいかを
        # 決める値なので、渡っていることを検査できるように残す。
        self.origins: list[str | None] = []

    def has_active_jobs(self) -> bool:
        return self.busy

    def enqueue_batch(
        self,
        articles: list[tuple[str, str]],
        video_format: str,
        language: str = "ja",
        origin: str | None = None,
    ) -> str:
        self.batches.append((video_format, articles))
        self.origins.append(origin)
        return f"batch-{video_format}"


async def test_enqueues_one_batch_per_format() -> None:
    news = FakeNews([_article("a1", "記事1"), _article("a2", "記事2")])
    jobs = FakeJobs()

    plan = await plan_daily_batch(news, jobs, formats=["short", "long"])

    assert plan.enqueued is True
    assert set(plan.batch_ids) == {"short", "long"}
    assert [f for f, _ in jobs.batches] == ["short", "long"]
    # 形式ごとに別の記事を割り当てる（同じ記事で2本作らない）
    assert jobs.batches[0][1] != jobs.batches[1][1]


async def test_skips_when_jobs_are_active() -> None:
    """実行中のジョブがあるときは積み増さないこと。

    積み増すと画像生成のクォータを食い合い、どちらも遅くなる。
    """
    news = FakeNews([_article("a1", "記事1")])
    jobs = FakeJobs(busy=True)

    plan = await plan_daily_batch(news, jobs, formats=["short"])

    assert plan.enqueued is False
    assert plan.skipped_reason is not None
    assert jobs.batches == []
    assert news.fetch_calls == 0, "見送るなら取得もしない"


async def test_skips_already_generated_articles() -> None:
    news = FakeNews(
        [
            _article("done", "生成済み", generated=True),
            _article("fresh", "未生成"),
        ]
    )
    jobs = FakeJobs()

    await plan_daily_batch(news, jobs, formats=["short"])

    assert jobs.batches[0][1] == [("fresh", "未生成")]


async def test_does_not_touch_the_users_selection() -> None:
    """選択状態を触らないこと。

    触ると、画面で選んでいた記事が定期実行で勝手に増減する。
    """
    news = FakeNews([_article("a1", "記事1")])
    jobs = FakeJobs()

    await plan_daily_batch(news, jobs, formats=["short"])

    assert news.selection_touched is False


async def test_continues_when_fetching_fails() -> None:
    """取得が落ちても既存の記事で続行すること。

    ここで諦めると、一時的なネットワーク障害で丸一日ぶんが飛ぶ。
    """
    news = FakeNews([_article("a1", "記事1")], fetch_fails=True)
    jobs = FakeJobs()

    plan = await plan_daily_batch(news, jobs, formats=["short"])

    assert plan.enqueued is True
    assert jobs.batches[0][1] == [("a1", "記事1")]


async def test_plan_daily_batch_fetches_by_default() -> None:
    """取得を切り出しても、`plan_daily_batch` 単体では取得すること。

    取得は `fetch_daily_news` に切り出して Web の日次タスクが先に呼ぶ
    ようにしたが、既定は `fetch=True` のまま。この検査が無いと、
    抽出のときに動画側の取得を黙って止めてしまえる。
    """
    news = FakeNews([_article("a1", "記事1")])
    jobs = FakeJobs()

    await plan_daily_batch(news, jobs, formats=["short"])

    assert news.fetch_calls == 1


async def test_plan_daily_batch_skips_fetching_when_told_to() -> None:
    """日次タスクは取得を先に1回だけ済ませてから `fetch=False` で呼ぶ。

    取得が動画の計画の中に埋まっていると、X の計画を先に回した瞬間に
    X が「このサイクルで取得した記事」を見られなくなる。
    """
    news = FakeNews([_article("a1", "記事1")])
    jobs = FakeJobs()

    plan = await plan_daily_batch(news, jobs, formats=["short"], fetch=False)

    assert news.fetch_calls == 0
    # 取得しないだけで、計画そのものは通常どおり成立する
    assert plan.enqueued is True
    assert jobs.batches[0][1] == [("a1", "記事1")]


async def test_fetch_daily_news_never_raises() -> None:
    """取得の失敗を例外にしない。

    日次タスクは取得を両方の計画より先に呼ぶので、ここで例外が外へ
    出ると **X の計画も動画の計画も巻き込んで止まる**。一時的な
    ネットワーク障害で丸一日ぶんが飛ぶ。
    """
    news = FakeNews([_article("a1", "記事1")], fetch_fails=True)

    await fetch_daily_news(news)

    assert news.fetch_calls == 1  # 試みてはいる


async def test_reports_when_there_is_nothing_to_build() -> None:
    news = FakeNews([_article("done", "生成済み", generated=True)])
    jobs = FakeJobs()

    plan = await plan_daily_batch(news, jobs, formats=["short"])

    assert plan.enqueued is False
    assert plan.skipped_reason == "未生成の記事がありません"


async def test_builds_what_it_can_when_articles_run_short() -> None:
    """記事が足りないときは、作れる分だけ作ること。

    全部やめてしまうと、記事が1件しか無い日に何も作られない。
    """
    news = FakeNews([_article("only", "1件だけ")])
    jobs = FakeJobs()

    plan = await plan_daily_batch(news, jobs, formats=["short", "long"])

    assert list(plan.batch_ids) == ["short"]
    assert jobs.batches[0][1] == [("only", "1件だけ")]


async def test_scrapes_the_chosen_articles() -> None:
    """選んだ記事の本文を取ること。

    ジョブは article_id しか持たないので、実行時に本文が
    ストアに無いと理由付きで失敗する。
    """
    news = FakeNews([_article("a1", "記事1"), _article("a2", "記事2")])
    jobs = FakeJobs()

    await plan_daily_batch(news, jobs, formats=["short", "long"])

    assert news.scraped == ["a1", "a2"]


async def test_定期実行のジョブには_schedule_の印が付く() -> None:
    """`origin=ORIGIN_SCHEDULE` を渡していること（issue #30）。

    この印が無いと、記事の題材がコンテンツフィルタに拒否されたときに
    代替が積まれず、**その日の動画が0本になる**（手動投入と区別できないので、
    差し替えてよいか判断できない）。定数を見るのではなく実際に渡る値を見る。
    """
    news = FakeNews([_article("a1", "記事1")])
    jobs = FakeJobs()

    await plan_daily_batch(news, jobs, formats=["short"])

    assert jobs.origins == [ORIGIN_SCHEDULE]
