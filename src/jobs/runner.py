"""ジョブ1件を実際の動画生成に繋ぐ処理。

ワーカーのループ（リース・回収・停止）とは分けてある。分ける理由は
テストで、ループの挙動を Azure を呼ばずに確かめられるようにするため。

記事本文をジョブ行に持たせていない理由
--------------------------------------
本文は数千文字あり、ジョブ表を太らせる。それに、本文は既に
ニュースストアに保存されている（`scrape_selected_content` が
スクレイピング結果を書き戻す）。ジョブは `article_id` だけを持ち、
実行時に読み直す。

副作用として、再起動をはさんでもジョブが実行できる。以前は
スクレイピング済みの記事オブジェクトを background task の引数として
メモリで渡していたので、プロセスが落ちると本文ごと消えていた。
"""

from __future__ import annotations

from typing import Any, Protocol

from src.models.job import GenerationJob

# 台本生成に渡す本文の長さ。
# 長すぎるとトークン上限に当たる。ニュース記事は冒頭に要点が来るので、
# 先頭を切り取れば十分（元の実装からの引き継ぎ）。
MAX_CONTENT_CHARS = 2000


class SupportsGeneration(Protocol):
    """動画生成パイプラインの必要な部分だけ。"""

    def run(
        self,
        news_topic: str,
        languages: list[str] | None = ...,
        output_name: str | None = ...,
        video_format: str = ...,
        source_url: str = ...,
    ) -> dict[str, Any]:
        """1本生成する。"""
        ...


class ArticleLike(Protocol):
    """台本生成に必要な記事の項目だけ。

    `NewsArticle` そのものを要求しない理由: このモジュールが使うのは
    4つの属性だけで、ニュースモデル全体に依存する必要がない
    （テストで差し替えるときも軽くなる）。
    """

    @property
    def id(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def content(self) -> str | None: ...

    @property
    def url(self) -> str: ...


class SupportsArticleLookup(Protocol):
    """記事ストアの必要な部分だけ。"""

    def get_article_by_id(self, article_id: str) -> ArticleLike | None:
        """IDで記事を取得する。"""
        ...

    def mark_as_generated(self, article_id: str) -> bool:
        """生成済みとして記録する。"""
        ...


class ArticleUnavailable(Exception):
    """記事が見つからない、または本文が無い。

    再実行しても直らない種類の失敗なので、ワーカーはこれを
    そのまま失敗として記録する（リトライしても同じ結果になる）。
    """


class PipelineJobRunner:
    """ジョブを受け取り、パイプラインを1回走らせる。"""

    def __init__(self, pipeline: SupportsGeneration, articles: SupportsArticleLookup):
        """初期化する。

        Args:
            pipeline: 動画生成パイプライン
            articles: 記事ストア
        """
        self._pipeline = pipeline
        self._articles = articles

    def __call__(self, job: GenerationJob) -> str | None:
        """ジョブを実行し、生成した動画の保存先キーを返す。

        Args:
            job: 実行するジョブ

        Returns:
            str | None: 動画の保存先キー（取得できなければ None）

        Raises:
            ArticleUnavailable: 記事または本文が無い場合
        """
        article = self._articles.get_article_by_id(job.article_id)
        if article is None:
            raise ArticleUnavailable(f"記事が見つかりません: {job.article_id}")
        if not article.content:
            # 失敗の理由はそのまま UI に出る。何をすれば直るかを書く。
            raise ArticleUnavailable(
                "本文を取得できませんでした（サイト側が取得を拒否した、"
                "または記事ページではない可能性があります）。"
                "ニュースを再取得してから、もう一度お試しください"
            )

        # URL はトピック（プロンプト入力）に含めない。モデルに URL を扱わせると
        # 台本本文に書き込もうとするので、出典は引数で渡してコード側が
        # 説明文に追記する（src/models/script.py の _with_source）。
        topic = f"{article.title}\n\n{article.content[:MAX_CONTENT_CHARS]}"
        result = self._pipeline.run(
            topic,
            languages=[job.language],
            output_name=article.title,
            video_format=job.video_format,
            source_url=article.url,
        )

        # 生成に成功したのでニュース側にも印を付ける。
        # ここで失敗しても動画は出来ているので、例外にはしない。
        self._articles.mark_as_generated(job.article_id)

        return _video_key(result, job.language)


def _video_key(result: dict[str, Any], language: str) -> str | None:
    """パイプラインの結果から動画の保存先キーを取り出す。

    キーを持たない結果（差し替えたフェイク等）でも落とさない。
    ジョブ自体は成功しているので、キーが無いことで失敗にはしない。

    Args:
        result: `Pipeline.run` の戻り値
        language: 言語コード

    Returns:
        str | None: 保存先キー
    """
    keys = result.get("artifact_keys")
    if not isinstance(keys, dict):
        return None
    videos = keys.get("videos")
    if not isinstance(videos, dict):
        return None
    value = videos.get(language)
    return str(value) if value else None
