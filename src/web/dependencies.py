"""FastAPI dependency injection setup."""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from config import Config
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.uploaders.tiktok_uploader import TikTokUploader
from src.uploaders.youtube_uploader import YouTubeUploader


@dataclass(frozen=True)
class GenerationSnapshot:
    """ある時点の生成状態の写し。

    `/status` はこの写しを読む。写しを取る理由は、生成が別スレッドで
    走っており、複数フィールドを個別に読むと「completed_count は更新済みだが
    completed_articles はまだ」という中途半端な組み合わせを観測しうるため。

    Attributes:
        status: "idle" / "running" / "success" / "error"
        is_running: 生成中かどうか
        total_count: 対象件数
        completed_count: 完了件数
        current_article: いま処理中の記事タイトル
        error_message: エラーメッセージ
        completed_articles: 成功した記事タイトル
        failed_articles: 失敗した記事タイトル
    """

    status: str
    is_running: bool
    total_count: int
    completed_count: int
    current_article: str | None
    error_message: str | None
    completed_articles: tuple[str, ...]
    failed_articles: tuple[str, ...]


class GenerationState:
    """動画生成の状態を管理するクラス。

    生成は Starlette の threadpool（イベントループ外のスレッド）で走り、
    `/status` はイベントループ側から読む。両者が同時に触るため、
    更新と読み取りをロックで守る。

    設計上の制約: 状態はプロセスメモリにしかないので、再起動で消え、
    レプリカを増やすと共有されない。Phase 4 でジョブテーブルに移す。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._is_running = False
        self._total_count = 0
        self._completed_count = 0
        self._current_article: str | None = None
        self._error_message: str | None = None
        self._completed_articles: list[str] = []
        self._failed_articles: list[str] = []

    @property
    def is_running(self) -> bool:
        """生成中かどうか。"""
        with self._lock:
            return self._is_running

    def start(self, total: int) -> None:
        """生成を開始する。

        Args:
            total: 対象件数
        """
        with self._lock:
            self._is_running = True
            self._total_count = total
            self._completed_count = 0
            self._current_article = None
            self._error_message = None
            self._completed_articles = []
            self._failed_articles = []

    def update(self, article_title: str) -> None:
        """現在処理中の記事を更新する。

        Args:
            article_title: 記事タイトル
        """
        with self._lock:
            self._current_article = article_title

    def complete_one(self, article_title: str, success: bool = True) -> None:
        """1件の処理を完了する。

        Args:
            article_title: 記事タイトル
            success: 成功したかどうか
        """
        with self._lock:
            self._completed_count += 1
            if success:
                self._completed_articles.append(article_title)
            else:
                self._failed_articles.append(article_title)

    def finish(self, error: str | None = None) -> None:
        """生成を終了する。

        Args:
            error: エラーメッセージ（正常終了なら None）
        """
        with self._lock:
            self._is_running = False
            self._current_article = None
            self._error_message = error

    def snapshot(self) -> GenerationSnapshot:
        """一貫した状態の写しを返す。

        Returns:
            GenerationSnapshot: ロック内で一度に読み取った状態
        """
        with self._lock:
            return GenerationSnapshot(
                status=self._status_locked(),
                is_running=self._is_running,
                total_count=self._total_count,
                completed_count=self._completed_count,
                current_article=self._current_article,
                error_message=self._error_message,
                completed_articles=tuple(self._completed_articles),
                failed_articles=tuple(self._failed_articles),
            )

    def _status_locked(self) -> str:
        """ステータス文字列を返す。呼び出し側がロックを保持していること。"""
        if self._is_running:
            return "running"
        if self._error_message:
            return "error"
        if self._completed_count > 0:
            return "success"
        return "idle"

    def get_status(self) -> str:
        """現在のステータスを取得する。

        Returns:
            str: "idle" / "running" / "success" / "error"
        """
        with self._lock:
            return self._status_locked()


@dataclass
class AppContext:
    """アプリの寿命に紐づく依存の集まり。

    以前はモジュールレベルの可変グローバルに入れていた。差し替えた理由:

    - テストで差し替えられなかった。`monkeypatch.setattr` でモジュール属性を
      書き換える必要があり、FastAPI が用意している
      `app.dependency_overrides` が使えなかった
    - 起動前は全てが None という状態を型で表現するしかなく、
      getter ごとに未初期化チェックが必要だった
    - 複数のアプリインスタンスを同時に持てなかった（テストで実際に困る）

    今は `lifespan` で組み立てて `app.state` に置く。寿命がアプリと
    一致し、テストは `dependency_overrides` で差し替えられる。

    Attributes:
        config: アプリケーション設定
        aggregator: ニュース取得・管理
        pipeline: 動画生成パイプライン
        generation_state: 生成の進捗
        youtube_uploader: YouTube アップローダ
        tiktok_uploader: TikTok アップローダ
    """

    config: Config
    aggregator: NewsAggregator
    pipeline: Pipeline
    generation_state: GenerationState
    youtube_uploader: YouTubeUploader
    tiktok_uploader: TikTokUploader

    @classmethod
    def build(cls, config: Config) -> "AppContext":
        """設定から依存を組み立てる。

        Args:
            config: アプリケーション設定

        Returns:
            AppContext: 組み立て済みの依存
        """
        config.ensure_news_dirs()
        config.ensure_output_dirs()

        return cls(
            config=config,
            aggregator=NewsAggregator(config.news_data_dir),
            pipeline=Pipeline(config),
            generation_state=GenerationState(),
            youtube_uploader=YouTubeUploader(
                client_secrets_file=config.youtube_client_secrets_file,
                token_file=config.youtube_token_file,
            ),
            tiktok_uploader=TikTokUploader(
                client_key=config.tiktok_client_key.get_secret_value(),
                client_secret=config.tiktok_client_secret.get_secret_value(),
                token_file=config.tiktok_token_file,
                redirect_uri=config.tiktok_redirect_uri,
            ),
        )


def get_context(request: Request) -> AppContext:
    """リクエストからアプリの依存を取り出す。

    Args:
        request: FastAPI リクエスト

    Returns:
        AppContext: lifespan が組み立てた依存

    Raises:
        RuntimeError: lifespan を通らずにアプリが動いている場合
    """
    context: AppContext | None = getattr(request.app.state, "context", None)
    if context is None:
        # TestClient を `with` 無しで使うと lifespan が走らない
        raise RuntimeError(
            "AppContext が未初期化です。lifespan が実行されていません"
            "（TestClient は `with TestClient(app) as client:` で使う）"
        )
    return context


# 個別の依存を取り出す関数。
# ルート側は `Depends(get_aggregator)` のように書けるので、
# 何に依存しているかがシグネチャから読める。


def get_config(context: AppContext = Depends(get_context)) -> Config:
    """設定を取得する。"""
    return context.config


def get_aggregator(context: AppContext = Depends(get_context)) -> NewsAggregator:
    """NewsAggregatorを取得する。"""
    return context.aggregator


def get_pipeline(context: AppContext = Depends(get_context)) -> Pipeline:
    """Pipelineを取得する。"""
    return context.pipeline


def get_generation_state(context: AppContext = Depends(get_context)) -> GenerationState:
    """GenerationStateを取得する。"""
    return context.generation_state


def get_youtube_uploader(context: AppContext = Depends(get_context)) -> YouTubeUploader:
    """YouTubeUploaderを取得する。"""
    return context.youtube_uploader


def get_tiktok_uploader(context: AppContext = Depends(get_context)) -> TikTokUploader:
    """TikTokUploaderを取得する。"""
    return context.tiktok_uploader


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """アプリの起動時に依存を組み立て、終了時に片付ける。

    Args:
        app: FastAPIアプリケーション

    Yields:
        None
    """
    app.state.context = AppContext.build(Config.from_env())
    try:
        yield
    finally:
        app.state.context.aggregator.close()
        app.state.context = None
