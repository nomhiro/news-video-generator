"""FastAPI dependency injection setup."""

from dataclasses import dataclass, field
from typing import List, Optional
from fastapi import FastAPI

from config import Config
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.uploaders.youtube_uploader import YouTubeUploader
from src.uploaders.tiktok_uploader import TikTokUploader


@dataclass
class GenerationState:
    """動画生成の状態を管理するクラス。"""
    is_running: bool = False
    total_count: int = 0
    completed_count: int = 0
    current_article: Optional[str] = None
    error_message: Optional[str] = None
    completed_articles: List[str] = field(default_factory=list)
    failed_articles: List[str] = field(default_factory=list)

    def start(self, total: int) -> None:
        """生成を開始する。"""
        self.is_running = True
        self.total_count = total
        self.completed_count = 0
        self.current_article = None
        self.error_message = None
        self.completed_articles = []
        self.failed_articles = []

    def update(self, article_title: str) -> None:
        """現在処理中の記事を更新する。"""
        self.current_article = article_title

    def complete_one(self, article_title: str, success: bool = True) -> None:
        """1件の処理を完了する。"""
        self.completed_count += 1
        if success:
            self.completed_articles.append(article_title)
        else:
            self.failed_articles.append(article_title)

    def finish(self, error: Optional[str] = None) -> None:
        """生成を終了する。"""
        self.is_running = False
        self.current_article = None
        self.error_message = error

    def get_status(self) -> str:
        """現在のステータスを取得する。"""
        if self.is_running:
            return "running"
        elif self.error_message:
            return "error"
        elif self.completed_count > 0:
            return "success"
        return "idle"


# Global instances (set by setup_dependencies)
_config: Config = None
_aggregator: NewsAggregator = None
_pipeline: Pipeline = None
_generation_state: GenerationState = GenerationState()
_youtube_uploader: YouTubeUploader = None
_tiktok_uploader: TikTokUploader = None


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
    """
    return _config


def get_aggregator() -> NewsAggregator:
    """NewsAggregatorを取得する。

    Returns:
        NewsAggregator: ニュース取得・管理インスタンス
    """
    return _aggregator


def get_pipeline() -> Pipeline:
    """Pipelineを取得する。

    Returns:
        Pipeline: 動画生成パイプラインインスタンス
    """
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
    """
    return _youtube_uploader


def get_tiktok_uploader() -> TikTokUploader:
    """TikTokUploaderを取得する。

    Returns:
        TikTokUploader: TikTokアップローダーインスタンス
    """
    return _tiktok_uploader
