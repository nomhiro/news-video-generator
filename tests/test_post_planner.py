"""1日ぶんの投稿計画。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.generators.image_generator import ContentFilterError
from src.jobs.post_planner import plan_daily_posts
from src.models.news import CHANNEL_X, NewsArticle, NewsCategory
from src.models.social import NewPost, PostKind
from src.social.card_visual import CardVisual
from src.social.post_generator import PostContentFilterError


class FakeNews:
    def __init__(self, articles: list[NewsArticle]) -> None:
        self._articles = articles
        # 計画が記事を消費済みにしないことを確かめるために記録する。
        # `SupportsArticlePicking` はこのメソッドを要求しないが、実物
        # （`NewsAggregator`）は持っているので、うっかり呼ばれても
        # 型検査では気付けない。
        self.consumed: list[tuple[str, str]] = []
        # コンテンツフィルタに拒否された記事の記録（記事ID, チャネル）。
        self.content_filtered: list[tuple[str, str]] = []
        self.scraped: list[list[str]] = []

    def pick_unconsumed(self, channel: str, needed: int) -> list[NewsArticle]:
        assert channel == CHANNEL_X
        return self._articles[:needed]

    async def scrape_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """本文はすでに入っているものとして返す（実物は HTTP を叩く）。"""
        self.scraped.append([a.id for a in articles])
        return articles

    def mark_consumed(self, article_id: str, channel: str) -> bool:
        self.consumed.append((article_id, channel))
        return True

    def mark_content_filtered(self, article_id: str, channel: str) -> bool:
        self.content_filtered.append((article_id, channel))
        return True


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
    def __init__(
        self, fail_for: set[str] | None = None, filter_for: set[str] | None = None
    ) -> None:
        self._fail_for = fail_for or set()
        # コンテンツフィルタに拒否される記事。一時的な失敗（`fail_for`）と
        # 分けているのは、扱いが違うことを検査するため（あちらは次の候補へ
        # 進むだけ、こちらは記事を対象外にもする）。
        self._filter_for = filter_for or set()
        self.captions: list[str | None] = []

    def generate(self, article, kind, caption: str | None = None) -> list[NewPost]:
        if article.id in self._filter_for:
            raise PostContentFilterError("記事の題材が拒否されました（sexual）")
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
        self.calls: list[tuple[list[str], Path, str | None, bool]] = []

    def generate_batch(
        self,
        prompts: list[str],
        output_dir: Path,
        *,
        size: str | None = None,
        enhance: bool = True,
    ) -> list[Path]:
        if self._error:
            raise self._error
        self.calls.append((prompts, output_dir, size, enhance))
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
        # 本文が空の記事は `plan_daily_posts` が飛ばす（タイトルだけでは
        # 投稿が作れない）。テストの前提として本文を入れておく。
        content="記事の本文。" * 30,
    )


# 2026-08-15 00:00 JST = 2026-08-14 15:00 UTC。全ての枠がまだ先。
MORNING = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)

TIMES = ["08:00", "12:30", "19:00", "21:30"]


def _plan(news, posts, generator, **overrides):
    kwargs: dict[str, Any] = {
        "enabled": True,
        "times": TIMES,
        "posts_per_day": 4,
        "budget_usd": 20.0,
        "unit_usd": 0.015,
        "unit_with_link_usd": 0.20,
        "now": MORNING,
    }
    kwargs.update(overrides)
    return asyncio.run(plan_daily_posts(news, posts, generator, **kwargs))


def test_記事ごとに時刻順で積む() -> None:
    posts = FakePosts()
    plan = _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    assert len(plan.group_ids) == 4
    scheduled = [next(iter(times.values())) for _, times in posts.enqueued]
    assert scheduled == sorted(scheduled)


def test_積んだ時点では記事を消費済みにしない() -> None:
    """消費済みにするのは投稿できた後（`PostWorker.on_posted`）だけ。

    積んだ時点で書くと、出せなかった記事（予定が遅れて見送られた・
    送信が失敗した）を**二度と使えなくなる**。消費記録は Azure Files 上の
    JSON でデプロイでも消えないため、間違って書くと手で直すしかない。
    """
    news = FakeNews([_article(s) for s in "abcd"])
    posts = FakePosts()

    plan = _plan(news, posts, FakeGenerator())

    assert plan.enqueued is True  # 積んではいる
    assert news.consumed == []


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


def test_全件に画像が付く(tmp_path: Path) -> None:
    """Issue #23 の回帰。**画像は型から独立している。**

    以前は `KIND_ROTATION` が4件に1件だけ CARD を割り当て、画像を作るのは
    CARD のときだけだった。投稿枠が3つしか残らない日は割り当てが
    SINGLE, SINGLE, SINGLE になり、画像が1枚も付かなかった
    （2026-08-17 の3件が実際にそうなった）。
    """
    posts = FakePosts()
    image_generator = FakeCardImageGenerator()

    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(),
        card_generator=FakeCardGenerator(),
        image_generator=image_generator,
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=False),
        output_dir=tmp_path,
    )

    first_posts = [batch[0] for batch, _ in posts.enqueued]
    assert len(first_posts) == 4
    # 型は全件 SINGLE。画像を持つかどうかは `image_key` だけが表す
    assert {p.kind for p in first_posts} == {PostKind.SINGLE}
    assert [p.image_key for p in first_posts] == [
        "social/cards/a.png",
        "social/cards/b.png",
        "social/cards/c.png",
        "social/cards/d.png",
    ]
    # **1記事につき1リクエストで、逐次に流れる。** ここが崩れて1回で
    # まとめて投げる形になると、`gpt-image-2` の毎分クォータ
    # （リージョン単位・動画パイプラインと共有）に当たりうる。
    assert len(image_generator.calls) == 4
    assert all(len(prompts) == 1 for prompts, *_ in image_generator.calls)


def test_画像の生成器が無ければ画像を付けずに積む() -> None:
    """card_generator / image_generator / artifacts のいずれかが無ければ
    画像生成そのものを試みず、画像なしで積む（呼び出し元が未設定でも
    壊れない。CLI とテストがこの経路を通る）。
    """
    posts = FakePosts()
    _plan(FakeNews([_article(s) for s in "abcd"]), posts, FakeGenerator())

    first_posts = [batch[0] for batch, _ in posts.enqueued]
    assert {p.kind for p in first_posts} == {PostKind.SINGLE}
    assert all(p.image_key is None for p in first_posts)


def test_画像を生成して保存先へ公開する(tmp_path: Path) -> None:
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

    last_post = posts.enqueued[3][0][0]
    assert last_post.kind == PostKind.SINGLE
    # キーは article_id ベース。同じ記事の再ドラフトは上書きになるので、
    # 孤立したファイルが積み重ならない
    assert last_post.image_key == "social/cards/d.png"
    generated_path = image_generator.calls[3][1] / "card.png"
    assert artifacts.published[3] == (generated_path, "social/cards/d.png")
    # CARD_STYLE_PROMPT は完結済みの指示なので、動画用の装飾
    # （_enhance_prompt）を重ねない（Finding 1 の再発防止）。
    assert image_generator.calls[3][3] is False
    # キャプションが下書き生成に渡っていること。**型に関係なく効く**
    # （`_build_user_prompt` は kind ではなく caption の有無だけを見る）。
    assert generator.captions[-1] == "同じ入力を使い回すことで推論コストが下がる。"


def test_動画生成中は画像を付けない(tmp_path: Path) -> None:
    """gpt-image-2 のクォータはリージョン単位で動画パイプラインと共有して
    いる。動画生成中に画像を作ると動画側を遅くするので諦める（画像は
    諦めるが、その日の投稿は出す）。

    **射程が広がった。** 4件に1件だけが画像を持っていた頃に失うのは1枚
    だけだったが、いまは**その日の全件**が画像なしになる。通常は起きない
    ——日次タスクは「取得 → X の計画 → 動画の投入」の順なので、X の計画時
    にはまだジョブが無い（`tests/test_scheduler_wiring.py` が見張っている）。
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

    first_posts = [batch[0] for batch, _ in posts.enqueued]
    assert {p.kind for p in first_posts} == {PostKind.SINGLE}
    assert all(p.image_key is None for p in first_posts)
    # 諦めたので視覚指示も画像も一切生成していない
    assert card_generator.calls == []
    assert image_generator.calls == []


def test_画像がコンテンツフィルタに拒否されても投稿は積む(tmp_path: Path) -> None:
    """再試行しても結果は変わらない設計（画像生成側）なので、ここでも
    再試行せず即座に諦める。投稿そのものは画像なしで出す。"""
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

    last_post = posts.enqueued[3][0][0]
    assert last_post.kind == PostKind.SINGLE
    assert last_post.image_key is None


def test_視覚指示の生成に失敗しても投稿は積む(tmp_path: Path) -> None:
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

    last_post = posts.enqueued[3][0][0]
    assert last_post.kind == PostKind.SINGLE
    assert last_post.image_key is None


def test_作業ディレクトリが無ければ画像を付けない() -> None:
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

    first_posts = [batch[0] for batch, _ in posts.enqueued]
    assert {p.kind for p in first_posts} == {PostKind.SINGLE}
    assert all(p.image_key is None for p in first_posts)
    assert card_generator.calls == []
    assert image_generator.calls == []


def test_生成に失敗しても後続が枠を繰り上げる() -> None:
    """枠を記事の添字に紐づけると、失敗した記事の時間が誰にも使われず消える。

    実測（2026-08-17）: 3件のうち1件目と3件目の生成が失敗し、成功した2件目だけが
    2番目の枠に積まれた。1番目と3番目の枠は空のまま消え、3件出す予定の日に
    1件しか出なかった。
    """
    posts = FakePosts()
    plan = _plan(
        FakeNews([_article("a"), _article("b"), _article("c")]),
        posts,
        FakeGenerator(fail_for={"a", "c"}),
    )

    assert len(plan.group_ids) == 1
    # 成功した1件は**最初の枠**に入る（2番目ではない）
    scheduled = next(iter(posts.enqueued[0][1].values()))
    first_slot = next(iter(_plan_slots()))
    assert scheduled.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%H:%M") == first_slot


def test_先行が失敗しても残った1件に画像が付く(tmp_path: Path) -> None:
    """画像は枠の順番に依存しない。

    型を循環させていた頃は、失敗した記事のぶん CARD の順番が飛んで
    **画像が1枚も出ない日が偶然できた**。画像を型から切り離した以上、
    成功した件数がいくつでも全件が画像を持つ。
    """
    posts = FakePosts()
    _plan(
        FakeNews([_article(s) for s in "abcd"]),
        posts,
        FakeGenerator(fail_for={"a", "b", "c"}),
        card_generator=FakeCardGenerator(),
        image_generator=FakeCardImageGenerator(),
        artifacts=FakeArtifacts(),
        jobs=FakeJobs(active=False),
        output_dir=tmp_path,
    )

    first_posts = [batch[0] for batch, _ in posts.enqueued]
    assert len(first_posts) == 1
    assert first_posts[0].kind == PostKind.SINGLE
    assert first_posts[0].image_key == "social/cards/d.png"


def _plan_slots() -> list[str]:
    """`_plan` が使う投稿時刻のうち、MORNING の時点で未来のもの。"""
    return TIMES


# --------------------------------------------------------------------------
# コンテンツフィルタに拒否された記事（issue #30 の X 側）
# --------------------------------------------------------------------------


def test_拒否された記事は自動生成の対象外にする() -> None:
    """記事に印を付けて、翌日以降選ばせないこと。

    印が無いと `pick_unconsumed` が翌日も同じ記事を返し、**毎日1回ぶんの
    API 呼び出しを捨て続ける**。当日の投稿は次の候補で埋まるので落ちず、
    そのぶん気付きにくい（動画側は1件で0本になるので目立った）。
    """
    news = FakeNews([_article("a"), _article("b")])
    posts = FakePosts()

    plan = _plan(news, posts, FakeGenerator(filter_for={"a"}))

    assert news.content_filtered == [("a", CHANNEL_X)]
    # 枠は消費せず次の候補で埋める
    assert len(plan.group_ids) == 1
    assert posts.enqueued[0][0][0].article_id == "b"


def test_一時的な失敗では対象外にしない() -> None:
    """引き直しで直りうる失敗を恒久的な拒否と混ぜないこと。

    混ぜると、API が一瞬不調だっただけの記事が二度と使われなくなる。
    """
    news = FakeNews([_article("a"), _article("b")])

    _plan(news, FakePosts(), FakeGenerator(fail_for={"a"}))

    assert news.content_filtered == []


def test_拒否された記事は消費済みにしない() -> None:
    """`consumed` は「もう出した」の権威なので触らないこと。

    消費済みにすると、出せていない記事が「出した」ことになる。対象外の
    記録は別のフィールド（`content_filtered`）に置く。
    """
    news = FakeNews([_article("a"), _article("b")])

    _plan(news, FakePosts(), FakeGenerator(filter_for={"a"}))

    assert ("a", CHANNEL_X) not in news.consumed
