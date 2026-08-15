"""1日ぶんの投稿計画。"""

from datetime import UTC, datetime
from typing import Any

from src.jobs.post_planner import plan_daily_posts
from src.models.news import CHANNEL_X, NewsArticle, NewsCategory
from src.models.social import NewPost, PostKind


class FakeNews:
    def __init__(self, articles: list[NewsArticle]) -> None:
        self._articles = articles

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        assert channel == CHANNEL_X
        return self._articles[:needed]


class FakePosts:
    def __init__(self, plain: int = 0, with_link: int = 0) -> None:
        self.enqueued: list[tuple[list[NewPost], dict[int, datetime]]] = []
        self._counts = (plain, with_link)

    def monthly_post_counts(self, year: int, month: int) -> tuple[int, int]:
        return self._counts

    def enqueue(self, posts, scheduled_at_by_position) -> str:
        self.enqueued.append((posts, scheduled_at_by_position))
        return f"g{len(self.enqueued)}"


class FakeGenerator:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self._fail_for = fail_for or set()

    def generate(self, article, kind, hashtags) -> list[NewPost]:
        if article.id in self._fail_for:
            raise RuntimeError("生成に失敗しました")
        return [
            NewPost(
                article_id=article.id,
                article_title=article.title,
                kind=kind,
                body="本文",
                has_link=False,
            )
        ]


def _article(suffix: str) -> NewsArticle:
    url = f"https://example.com/{suffix}"
    return NewsArticle(
        id=suffix,
        title=f"記事{suffix}",
        url=url,
        source="Example",
        category=NewsCategory.AI,
    )


# 2026-08-15 00:00 JST = 2026-08-14 15:00 UTC。全ての枠がまだ先。
MORNING = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)

TIMES = ["08:00", "12:30", "19:00", "21:30"]


def _plan(news, posts, generator, **overrides):
    kwargs: dict[str, Any] = {
        "times": TIMES,
        "posts_per_day": 4,
        "hashtags": ["#AI"],
        "budget_usd": 20.0,
        "unit_usd": 0.015,
        "unit_with_link_usd": 0.20,
        "now": MORNING,
    }
    kwargs.update(overrides)
    return plan_daily_posts(news, posts, generator, **kwargs)


def test_記事ごとに時刻順で積む() -> None:
    posts = FakePosts()
    plan = _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    assert len(plan.group_ids) == 4
    scheduled = [next(iter(times.values())) for _, times in posts.enqueued]
    assert scheduled == sorted(scheduled)


def test_予算上限を超えていたら何も積まない() -> None:
    """積んでから止めると、月初に古い投稿が一斉に出る。"""
    posts = FakePosts(plain=0, with_link=200)  # 200 * 0.20 = $40
    plan = _plan(FakeNews([_article("a")]), posts, FakeGenerator())

    assert plan.enqueued is False
    assert plan.skipped_reason is not None
    assert "予算上限" in plan.skipped_reason
    assert posts.enqueued == []


def test_生成が1件失敗しても残りを積む() -> None:
    posts = FakePosts()
    plan = _plan(FakeNews([_article("a"), _article("b")]), posts, FakeGenerator(fail_for={"a"}))

    assert len(plan.group_ids) == 1
    assert posts.enqueued[0][0][0].article_id == "b"


def test_過ぎた時刻は使わない() -> None:
    """過去の時刻を入れると discard_stale に即座に捨てられる。"""
    posts = FakePosts()
    # 2026-08-15 20:00 JST。残る枠は 21:30 だけ
    evening = datetime(2026, 8, 15, 11, 0, tzinfo=UTC)

    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator(), now=evening)

    assert len(posts.enqueued) == 1


def test_記事が無ければ理由を返す() -> None:
    plan = _plan(FakeNews([]), FakePosts(), FakeGenerator())

    assert plan.enqueued is False
    assert plan.skipped_reason == "X で未使用の記事がありません"


def test_4件目はカードになる() -> None:
    posts = FakePosts()
    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    kinds = [batch[0].kind for batch, _ in posts.enqueued]
    assert kinds == [PostKind.SINGLE, PostKind.SINGLE, PostKind.SINGLE, PostKind.CARD]
