"""ジョブ1件をパイプラインに繋ぐ層（`PipelineJobRunner`）。

ここは「記事の題材が拒否された」ことを**記事データ側の印に変える**唯一の
場所。印が付かないと `pick_unconsumed` が翌日も同じ記事を返し、毎日同じ
理由で失敗し続ける（issue #30 の②）。
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from src.generators.script_generator import ScriptContentFilterError
from src.jobs.runner import ArticleRejected, ArticleUnavailable, PipelineJobRunner
from src.models.job import GenerationJob, JobStatus
from src.pipeline import PipelineError

REJECTION_MESSAGE = "記事の題材がコンテンツフィルタに拒否されました（sexual）。"


class FakeArticle:
    """`ArticleLike` が要求する4つの属性だけ持つ記事。"""

    def __init__(self, article_id: str, title: str, content: str = "本文", url: str = "u"):
        self.id = article_id
        self.title = title
        self.content = content
        self.url = url


class FakeStore:
    """記事ストアの差し替え。どの印が付いたかを記録する。"""

    def __init__(self, article: FakeArticle | None, mark_raises: bool = False):
        self._article = article
        self._mark_raises = mark_raises
        self.generated: list[str] = []
        self.filtered: list[str] = []

    def get_article_by_id(self, article_id: str) -> FakeArticle | None:
        return self._article

    def mark_as_generated(self, article_id: str) -> bool:
        self.generated.append(article_id)
        return True

    def mark_content_filtered(self, article_id: str) -> bool:
        if self._mark_raises:
            raise OSError("記事データを書けませんでした")
        self.filtered.append(article_id)
        return True


class RejectingPipeline:
    """台本生成の拒否がそのまま上がってくる状態を作る。"""

    def run(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise ScriptContentFilterError(REJECTION_MESSAGE)


class FailingPipeline:
    """拒否ではない普通の失敗。"""

    def run(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise PipelineError("ffmpeg が異常終了しました")


class OkPipeline:
    """成功する。"""

    def run(self, *args: object, **kwargs: object) -> dict[str, Any]:
        return {"artifact_keys": {"videos": {"ja": "videos/20260822_000000_ja.mp4"}}}


def _job(article_id: str = "a1") -> GenerationJob:
    return GenerationJob(
        id=1,
        batch_id="b1",
        article_id=article_id,
        article_title="タイトル",
        video_format="short",
        language="ja",
        status=JobStatus.RUNNING,
        attempts=1,
        error_message=None,
        video_key=None,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
        worker_id="w1",
        lease_expires_at=None,
    )


def test_拒否されたら記事に印を付けてArticleRejectedにする() -> None:
    """印を付けることと、専用の型で伝えることの両方をやること。

    印はこの後の「代替の記事を選ぶ」処理より先に付いている必要がある
    （`pick_unconsumed` が同じ記事を返さないため）。
    """
    store = FakeStore(FakeArticle("a1", "タイトル"))
    runner = PipelineJobRunner(RejectingPipeline(), store)

    with pytest.raises(ArticleRejected) as caught:
        runner(_job())

    assert store.filtered == ["a1"]
    # 動画は1本も出来ていないので、生成済みの印は付けてはいけない
    assert store.generated == []
    # 画面に出る文言がそのまま乗る
    assert REJECTION_MESSAGE in str(caught.value)


def test_印を付けられなくても拒否は伝える() -> None:
    """記事データの書き込みが失敗しても `ArticleRejected` を投げること。

    ここで例外を漏らすと、画面は生の 400 JSON に戻り（読める理由が消える）、
    代替の投入も起きない。印が無いぶん代替の選択で同じ記事が候補に戻りうるが、
    そちら側が `job.article_id` を除いて選ぶので二重には踏まない。
    """
    store = FakeStore(FakeArticle("a1", "タイトル"), mark_raises=True)
    runner = PipelineJobRunner(RejectingPipeline(), store)

    with pytest.raises(ArticleRejected):
        runner(_job())


def test_拒否以外の失敗では印を付けない() -> None:
    """一時的な失敗を恒久的な拒否と混ぜないこと。

    混ぜると、ffmpeg が落ちただけの記事が二度と使われなくなる。
    """
    store = FakeStore(FakeArticle("a1", "タイトル"))
    runner = PipelineJobRunner(FailingPipeline(), store)

    with pytest.raises(PipelineError):
        runner(_job())

    assert store.filtered == []


def test_本文が無い記事は拒否ではなく取得できない扱い() -> None:
    """`ArticleUnavailable` と `ArticleRejected` を混ぜないこと。

    本文が取れないのはサイト側の都合で、再取得すれば直りうる。
    """
    store = FakeStore(FakeArticle("a1", "タイトル", content=""))
    runner = PipelineJobRunner(OkPipeline(), store)

    with pytest.raises(ArticleUnavailable):
        runner(_job())

    assert store.filtered == []


def test_成功したら生成済みの印を付ける() -> None:
    """既存の振る舞いを壊していないこと。"""
    store = FakeStore(FakeArticle("a1", "タイトル"))
    runner = PipelineJobRunner(OkPipeline(), store)

    key = runner(_job())

    assert key == "videos/20260822_000000_ja.mp4"
    assert store.generated == ["a1"]
    assert store.filtered == []
