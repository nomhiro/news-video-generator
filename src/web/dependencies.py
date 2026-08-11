"""FastAPI dependency injection setup."""

import threading
from dataclasses import dataclass

from fastapi import FastAPI

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


# Global instances (set by setup_dependencies)
#
# 設計上の負債: モジュールレベルの可変グローバルを DI として使っているため、
# テスト時に差し替えられず、起動前は None という状態を型で表現するしかない。
# Phase 5 で FastAPI の lifespan + app.state による DI に置き換える。
# それまでは None を許す型にして、getter 側で未初期化を検出する。
_config: Config | None = None
_aggregator: NewsAggregator | None = None
_pipeline: Pipeline | None = None
_generation_state: GenerationState = GenerationState()
_youtube_uploader: YouTubeUploader | None = None
_tiktok_uploader: TikTokUploader | None = None


def setup_dependencies(app: FastAPI, config: Config) -> None:
    """依存関係を設定する。

    Args:
        app: FastAPIアプリケーション
        config: アプリケーション設定
    """
    global _config, _aggregator, _pipeline, _youtube_uploader, _tiktok_uploader

    _config = config

    # Ensure directories exist
    config.ensure_news_dirs()
    config.ensure_output_dirs()

    # Initialize aggregator
    _aggregator = NewsAggregator(config.news_data_dir)

    # Initialize pipeline
    _pipeline = Pipeline(config)

    # Initialize YouTube uploader
    _youtube_uploader = YouTubeUploader(
        client_secrets_file=config.youtube_client_secrets_file,
        token_file=config.youtube_token_file,
    )

    # Initialize TikTok uploader
    _tiktok_uploader = TikTokUploader(
        client_key=config.tiktok_client_key,
        client_secret=config.tiktok_client_secret,
        token_file=config.tiktok_token_file,
        redirect_uri=config.tiktok_redirect_uri,
    )


def get_config() -> Config:
    """設定を取得する。

    Returns:
        Config: アプリケーション設定

    Raises:
        RuntimeError: setup_dependencies が呼ばれていない場合
    """
    if _config is None:
        raise RuntimeError("setup_dependencies() が呼ばれていません")
    return _config


def get_aggregator() -> NewsAggregator:
    """NewsAggregatorを取得する。

    Returns:
        NewsAggregator: ニュース取得・管理インスタンス

    Raises:
        RuntimeError: setup_dependencies が呼ばれていない場合
    """
    if _aggregator is None:
        raise RuntimeError("setup_dependencies() が呼ばれていません")
    return _aggregator


def get_pipeline() -> Pipeline:
    """Pipelineを取得する。

    Returns:
        Pipeline: 動画生成パイプラインインスタンス

    Raises:
        RuntimeError: setup_dependencies が呼ばれていない場合
    """
    if _pipeline is None:
        raise RuntimeError("setup_dependencies() が呼ばれていません")
    return _pipeline


def get_generation_state() -> GenerationState:
    """GenerationStateを取得する。

    Returns:
        GenerationState: 動画生成状態インスタンス
    """
    return _generation_state


def get_youtube_uploader() -> YouTubeUploader:
    """YouTubeUploaderを取得する。

    Returns:
        YouTubeUploader: YouTubeアップローダーインスタンス

    Raises:
        RuntimeError: setup_dependencies が呼ばれていない場合
    """
    if _youtube_uploader is None:
        raise RuntimeError("setup_dependencies() が呼ばれていません")
    return _youtube_uploader


def get_tiktok_uploader() -> TikTokUploader:
    """TikTokUploaderを取得する。

    Returns:
        TikTokUploader: TikTokアップローダーインスタンス

    Raises:
        RuntimeError: setup_dependencies が呼ばれていない場合
    """
    if _tiktok_uploader is None:
        raise RuntimeError("setup_dependencies() が呼ばれていません")
    return _tiktok_uploader
