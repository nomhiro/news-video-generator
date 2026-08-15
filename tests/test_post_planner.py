"""1日ぶんの投稿計画。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.generators.image_generator import ContentFilterError
from src.jobs.post_planner import plan_daily_posts
from src.models.news import CHANNEL_X, NewsArticle, NewsCategory
from src.models.social import NewPost, PostKind
from src.social.card_visual import CardVisual


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
        self.captions: list[str | None] = []

    def generate(self, article, kind, hashtags, caption: str | None = None) -> list[NewPost]:
        if article.id in self._fail_for:
            raise RuntimeError("生成に失敗しました")
        self.captions.append(caption)
        return [
            NewPost(
                article_id=article.id,
                article_title=article.title,
                kind=kind,
                body="本文",
                has_link=False,
            )
        ]


class FakeJobs:
    """`JobRepository.has_active_jobs()` のフェイク。"""

    def __init__(self, active: bool = False) -> None:
        self.active = active

    def has_active_jobs(self) -> bool:
        return self.active


def _card_visual(**overrides) -> CardVisual:
    data: dict[str, Any] = {
        "subject": "A cache that reuses previous model inputs to cut cost.",
        "key_details": ["a funnel narrowing", "two arrows returning to a store"],
        "labels": ["CACHE"],
        "caption_ja": "同じ入力を使い回すことで推論コストが下がる。",
    }
    data.update(overrides)
    return CardVisual(**data)


class FakeCardGenerator:
    """`CardVisualGenerator.generate` のフェイク。"""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[NewsArticle] = []

    def generate(self, article: NewsArticle) -> CardVisual:
        self.calls.append(article)
        if self._error:
            raise self._error
        return _card_visual()


class FakeCardImageGenerator:
    """`ImageGenerator.generate_batch` のフェイク。実 API を呼ばない。"""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[tuple[list[str], Path, str | None]] = []

    def generate_batch(
        self, prompts: list[str], output_dir: Path, *, size: str | None = None
    ) -> list[Path]:
        if self._error:
            raise self._error
        self.calls.append((prompts, output_dir, size))
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "card.png"
        path.write_bytes(b"fake-png")
        return [path]


class FakeArtifacts:
    """`ArtifactStore.publish` のフェイク。"""

    def __init__(self) -> None:
        self.published: list[tuple[Path, str]] = []

    def publish(self, local_path: Path, key: str) -> str:
        self.published.append((local_path, key))
        return f"local://{key}"


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
        "enabled": True,
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


def test_スイッチが無効なら何も積まない() -> None:
    """switch off はスイッチであって発信計画ではない。

    無効なのに下書きだけ作ると、discard_stale に黙って捨てられて
    誰にも見えない・記事が消費済みにならず翌日も再ドラフトされる・
    Azure OpenAI（Task 6 以降は画像クォータも）を無駄に使う。
    """
    posts = FakePosts()
    plan = _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator(), enabled=False)

    assert plan.enqueued is False
    assert plan.skipped_reason is not None
    assert "スイッチ" in plan.skipped_reason
    assert posts.enqueued == []


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


def test_カード生成器が無ければ画像無しのカードのまま() -> None:
    """既存の動作（Task 6 以前）を変えていないことの確認。

    card_generator / image_generator / artifacts のいずれかが無ければ
    画像生成そのものを試みず、CARD のまま積む。
    """
    posts = FakePosts()
    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.CARD
    assert card_post.image_key is None


def test_カードは画像を生成してキーを添付する(tmp_path: Path) -> None:
    posts = FakePosts()
    generator = FakeGenerator()
    card_generator = FakeCardGenerator()
    image_generator = FakeCardImageGenerator()
    artifacts = FakeArtifacts()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        generator,
        card_generator=card_generator,
        image_generator=image_generator,
        artifacts=artifacts,
        jobs=FakeJobs(active=False),
        output_dir=tmp_path,
    )

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.CARD
    assert card_post.image_key == "social/cards/d.png"
    # 1回だけ生成し、そのまま公開している（キーは article_id ベース）
    assert len(image_generator.calls) == 1
    generated_path = image_generator.calls[0][1] / "card.png"
    assert artifacts.published == [(generated_path, "social/cards/d.png")]
    # キャプションが下書き生成に渡っていること
    assert generator.captions[-1] == "同じ入力を使い回すことで推論コストが下がる。"


def test_動画生成中はカードをSINGLEに降格する(tmp_path: Path) -> None:
    """gpt-image-2 のクォータはリージョン単位で上限4で、動画パイプラインと
    共有している。動画生成中にカードを作ると動画側を遅くするので、
    その日は SINGLE に降格する（カード1枚は諦めるが、その日の投稿は出す）。
    """
    posts = FakePosts()
    card_generator = FakeCardGenerator()
    image_generator = FakeCardImageGenerator()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(),
        card_generator=card_generator,
        image_generator=image_generator,
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=True),
        output_dir=tmp_path,
    )

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.SINGLE
    assert card_post.image_key is None
    # 降格したので視覚指示も画像も一切生成していない
    assert card_generator.calls == []
    assert image_generator.calls == []


def test_コンテンツフィルタに拒否されたらSINGLEに降格する(tmp_path: Path) -> None:
    """再試行しても結果は変わらない設計（画像生成側）なので、ここでも
    再試行せず即座に降格する。"""
    posts = FakePosts()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(),
        card_generator=FakeCardGenerator(),
        image_generator=FakeCardImageGenerator(error=ContentFilterError("拒否されました")),
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=False),
        output_dir=tmp_path,
    )

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.SINGLE
    assert card_post.image_key is None


def test_視覚指示の生成に失敗したらSINGLEに降格する(tmp_path: Path) -> None:
    posts = FakePosts()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(),
        card_generator=FakeCardGenerator(error=RuntimeError("視覚指示の生成に失敗")),
        image_generator=FakeCardImageGenerator(),
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=False),
        output_dir=tmp_path,
    )

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.SINGLE
    assert card_post.image_key is None


def test_作業ディレクトリが無ければSINGLEに降格する() -> None:
    """output_dir が無いと画像を生成する場所が無い。"""
    posts = FakePosts()
    card_generator = FakeCardGenerator()
    image_generator = FakeCardImageGenerator()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(),
        card_generator=card_generator,
        image_generator=image_generator,
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=False),
        output_dir=None,
    )

    card_post = posts.enqueued[3][0][0]
    assert card_post.kind == PostKind.SINGLE
    assert card_generator.calls == []
    assert image_generator.calls == []
