"""FastAPI dependency injection setup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request

from config import Config
from src.jobs.runner import PipelineJobRunner
from src.jobs.worker import JobWorker
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.storage.artifacts import ArtifactStore, build_artifact_store
from src.storage.db import create_db_engine, create_session_factory
from src.storage.jobs import JobRepository
from src.storage.schema import upgrade_to_head
from src.uploaders.tiktok_uploader import TikTokUploader
from src.uploaders.youtube_uploader import YouTubeUploader


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
        artifact_store: 生成物の保存先（ローカル or Blob Storage）
        jobs: ジョブ表（進捗の永続化）
        worker: ジョブを実行するワーカー
        youtube_uploader: YouTube アップローダ
        tiktok_uploader: TikTok アップローダ
    """

    config: Config
    aggregator: NewsAggregator
    pipeline: Pipeline
    artifact_store: ArtifactStore
    jobs: JobRepository
    worker: JobWorker
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

        # 保存先はパイプラインと Web で同じインスタンスを共有する。
        # Blob の場合、クライアントは接続とトークンを内部でキャッシュするため
        # 使い回した方が速い。
        artifact_store = build_artifact_store(
            config.artifact_store,
            local_root=config.output_dir,
            account_url=config.azure_storage_account_url,
            container_name=config.azure_storage_container,
        )

        # スキーマを先に当てる。テーブルが無い状態でワーカーが
        # ポーリングを始めると、意味の分かりにくいエラーが出続ける。
        upgrade_to_head(config.database_url)
        jobs = JobRepository(create_session_factory(create_db_engine(config.database_url)))

        aggregator = NewsAggregator(config.news_data_dir)
        pipeline = Pipeline(config, artifact_store=artifact_store)

        return cls(
            config=config,
            aggregator=aggregator,
            pipeline=pipeline,
            artifact_store=artifact_store,
            jobs=jobs,
            worker=JobWorker(jobs, PipelineJobRunner(pipeline, aggregator)),
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


def get_artifact_store(context: AppContext = Depends(get_context)) -> ArtifactStore:
    """生成物の保存先を取得する。"""
    return context.artifact_store


def get_jobs(context: AppContext = Depends(get_context)) -> JobRepository:
    """ジョブ表を取得する。"""
    return context.jobs


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
    context = AppContext.build(Config.from_env())
    app.state.context = context
    # ワーカーはアプリの寿命に紐づく。リクエストごとの
    # BackgroundTask ではなく、ここで1つ起動して使い回す。
    context.worker.start()
    try:
        yield
    finally:
        # 停止を待つ。待たずに落とすと、実行中のジョブが RUNNING のまま
        # 残る（リースが切れれば回収されるが、無駄に15分待つことになる）。
        context.worker.stop()
        context.aggregator.close()
        app.state.context = None
