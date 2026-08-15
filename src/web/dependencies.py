"""FastAPI dependency injection setup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request

from config import Config
from src.jobs.planner import plan_daily_batch
from src.jobs.post_planner import plan_daily_posts
from src.jobs.post_worker import PostWorker
from src.jobs.runner import PipelineJobRunner
from src.jobs.scheduler import DailyScheduler
from src.jobs.worker import JobWorker
from src.models.news import CHANNEL_X
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.social.post_generator import PostGenerator
from src.social.switch import PostingSwitch
from src.social.x_auth import HttpTokenExchange, XTokenExpiredError, ensure_fresh, load_credentials
from src.social.x_client import HttpXClient, XClient
from src.storage.artifacts import ArtifactStore, build_artifact_store
from src.storage.db import create_db_engine, create_session_factory
from src.storage.jobs import JobRepository
from src.storage.schema import upgrade_to_head
from src.storage.social import SocialPostRepository
from src.storage.tokens import TokenStore, build_token_store
from src.uploaders.tiktok_uploader import TikTokUploader
from src.uploaders.youtube_uploader import YouTubeUploader
from src.utils.logger import log_error


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
        scheduler: 定期実行（無効なら None）
        youtube_uploader: YouTube アップローダ
        tiktok_uploader: TikTok アップローダ
        token_store: OAuth トークンの保存先。以前は `build` のローカル変数
            だったが、X 投稿の client_factory がアクセストークンの更新の
            たびに読み書きする必要があり、`get_youtube_uploader` などと
            同じ形で外から取れるようにフィールドへ昇格した
        posts: 投稿表（social_posts、進捗の永続化）
        x_switch: 自動投稿の有効/無効
        post_worker: 投稿を実行するワーカー
    """

    config: Config
    aggregator: NewsAggregator
    pipeline: Pipeline
    artifact_store: ArtifactStore
    jobs: JobRepository
    worker: JobWorker
    scheduler: DailyScheduler | None
    youtube_uploader: YouTubeUploader
    tiktok_uploader: TikTokUploader
    token_store: TokenStore
    posts: SocialPostRepository
    x_switch: PostingSwitch
    post_worker: PostWorker

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

        # OAuth トークンの保存先。ローカルはファイル、コンテナでは Blob。
        # コンテナのファイルシステムは再起動で消えるため、ローカル固定だと
        # 毎回ブラウザ認証が必要になり、そもそも完了できない。
        token_store = build_token_store(
            config.token_store,
            local_paths=config.token_paths,
            account_url=config.azure_storage_account_url,
            container_name=config.azure_token_container,
        )

        # スキーマを先に当てる。テーブルが無い状態でワーカーが
        # ポーリングを始めると、意味の分かりにくいエラーが出続ける。
        upgrade_to_head(config.database_url, config.sqlite_journal_mode)
        # ジョブ表と投稿表は同じ DB の別テーブルなので、engine/session_factory
        # を1つ作って両方のリポジトリで使い回す。
        session_factory = create_session_factory(
            create_db_engine(config.database_url, config.sqlite_journal_mode)
        )
        jobs = JobRepository(session_factory)
        posts = SocialPostRepository(session_factory)

        aggregator = NewsAggregator(config.news_data_dir)
        pipeline = Pipeline(config, artifact_store=artifact_store)

        # 台本生成と同じ Azure OpenAI リソース・デプロイを使う。
        # 投稿の下書きは台本と違う字数制約を持つだけで、生成基盤は共有できる。
        post_generator = PostGenerator(
            config.azure_openai_endpoint,
            config.azure_openai_api_key.get_secret_value(),
            config.azure_openai_deployment,
        )

        # スイッチの実体は Azure Files 上を想定するファイル
        # （ジョブ表の SQLite と違い、リビジョン更新で消えない場所）。
        # X_POSTING_ENABLED はファイルが無いときの初期値でしかない。
        x_switch = PostingSwitch(
            config.x_posting_switch_path, default_enabled=config.x_posting_enabled
        )

        post_worker = PostWorker(
            posts,
            client_factory=lambda: _build_x_client(config, token_store),
            switch=x_switch,
            # 投稿できた記事はここで消費済みにする。積んだ時点では書かない
            # （出せなかった記事を二度と使えなくなるため）。
            on_posted=lambda article_id: _mark_posted_consumed(aggregator, article_id),
            max_post_delay_minutes=config.x_max_post_delay_minutes,
        )

        return cls(
            config=config,
            aggregator=aggregator,
            pipeline=pipeline,
            artifact_store=artifact_store,
            jobs=jobs,
            worker=JobWorker(jobs, PipelineJobRunner(pipeline, aggregator)),
            scheduler=_build_scheduler(config, aggregator, jobs, posts, post_generator, x_switch),
            youtube_uploader=YouTubeUploader(token_store=token_store),
            tiktok_uploader=TikTokUploader(
                client_key=config.tiktok_client_key.get_secret_value(),
                client_secret=config.tiktok_client_secret.get_secret_value(),
                token_store=token_store,
                redirect_uri=config.tiktok_redirect_uri,
            ),
            token_store=token_store,
            posts=posts,
            x_switch=x_switch,
            post_worker=post_worker,
        )


def _mark_posted_consumed(aggregator: NewsAggregator, article_id: str) -> None:
    """投稿できた記事を X チャネルで消費済みにする（戻り値は捨てる）。

    `mark_consumed` は「記事が見つかって更新できたか」の bool を返す。
    `PostWorker.on_posted` のコールバック型は `None` を返す想定なので
    ここで戻り値を捨てる（薄いラッパにするのは、記事が既に消えていた
    場合でも投稿の完了自体は失敗にしないため）。
    """
    aggregator.mark_consumed(article_id, CHANNEL_X)


def _build_x_client(config: Config, token_store: TokenStore) -> XClient:
    """X クライアントを作る（呼び出しごとに1つ）。

    起動時に1つ作って使い回さない理由: アクセストークンは
    `ensure_fresh` が期限に応じて更新する。使い回すと更新後も
    古いトークンで `Authorization` ヘッダーを送り続ける。

    Args:
        config: アプリケーション設定
        token_store: OAuth トークンの保存先

    Returns:
        XClient: 有効なアクセストークンを持つクライアント

    Raises:
        XTokenExpiredError: 未認証、または更新に失敗した
            （呼び出し元の `PostWorker` がループを止めずにログへ残す）
    """
    credentials = load_credentials(token_store)
    if credentials is None:
        raise XTokenExpiredError("X が未認証です。設定画面から認証してください")
    credentials = ensure_fresh(
        token_store,
        credentials,
        HttpTokenExchange(config.x_client_id, config.x_client_secret.get_secret_value()),
    )
    return HttpXClient(credentials.access_token)


def _build_scheduler(
    config: Config,
    aggregator: NewsAggregator,
    jobs: JobRepository,
    posts: SocialPostRepository,
    post_generator: PostGenerator,
    x_switch: PostingSwitch,
) -> DailyScheduler | None:
    """定期実行を組み立てる（無効なら None）。

    既定で無効にしている理由: ローカルで開発しているだけのときに、
    毎朝ニュースを取得して動画を作り始めると課金が発生する。

    **DailyScheduler は1つだけ。** 動画の計画と X 投稿の計画を
    別スケジューラにすると、「1日1回」という前提が二重になり、
    タイミングのずれや停止漏れの温床になる。同じ日次実行の中で
    両方を計画する。

    Args:
        config: アプリケーション設定
        aggregator: ニュースストア
        jobs: ジョブ表
        posts: 投稿表
        post_generator: 投稿の下書き生成器
        x_switch: 自動投稿の有効/無効。`plan_daily_posts` に渡し、
            無効なら下書きの生成そのものを止める（送信ステップだけを
            止めるものではない。理由は `plan_daily_posts` の docstring）

    Returns:
        DailyScheduler | None: 有効なら組み立てたスケジューラ
    """
    if not config.schedule_enabled:
        return None

    async def task() -> object:
        video_plan = await plan_daily_batch(
            aggregator,
            jobs,
            formats=config.schedule_formats,
            search_queries=config.ai_search_queries,
            ai_limit_per_query=config.ai_news_limit_per_query,
            articles_per_format=config.schedule_articles_per_format,
        )
        # 動画の計画が失敗しても X の計画は独立して試す（逆も同様）。
        # 1つの記事取得トラブルで両方が止まると、原因の切り分けが
        # しづらくなる。
        try:
            plan_daily_posts(
                aggregator,
                posts,
                post_generator,
                enabled=x_switch.is_enabled(),
                times=config.x_post_times,
                posts_per_day=config.x_posts_per_day,
                hashtags=config.x_hashtags,
                budget_usd=config.x_monthly_budget_usd,
                unit_usd=config.x_cost_per_post_usd,
                unit_with_link_usd=config.x_cost_per_post_with_link_usd,
                now=datetime.now(UTC),
                timezone=config.schedule_timezone,
            )
        except Exception as e:
            log_error(f"X 投稿の計画に失敗しました（次回は予定どおり走ります）: {e}")
        return video_plan

    return DailyScheduler(
        task,
        run_at=config.schedule_run_at,
        timezone=config.schedule_timezone,
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


def get_posts(context: AppContext = Depends(get_context)) -> SocialPostRepository:
    """投稿表を取得する。"""
    return context.posts


def get_x_switch(context: AppContext = Depends(get_context)) -> PostingSwitch:
    """自動投稿の有効/無効のスイッチを取得する。"""
    return context.x_switch


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
    #
    # `generate_videos_task`（src/web/routes.py）と同じ理由で、
    # PostWorker もスレッドで回す。async def の中で同期の投稿処理を
    # await すると Web サーバー全体が応答しなくなる。
    context.worker.start()
    context.post_worker.start()
    if context.scheduler is not None:
        context.scheduler.start()
    try:
        yield
    finally:
        if context.scheduler is not None:
            context.scheduler.stop()
        # 停止を待つ。待たずに落とすと、実行中のジョブが RUNNING のまま
        # 残る（リースが切れれば回収されるが、無駄に15分待つことになる）。
        context.worker.stop()
        context.post_worker.stop()
        context.aggregator.close()
        app.state.context = None
