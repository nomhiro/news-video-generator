"""FastAPI dependency injection setup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request

from config import Config
from src.generators.image_generator import ImageGenerator
from src.jobs.planner import fetch_daily_news, plan_daily_batch
from src.jobs.post_planner import plan_daily_posts
from src.jobs.post_worker import PostWorker
from src.jobs.runner import PipelineJobRunner
from src.jobs.scheduler import DailyScheduler
from src.jobs.worker import JobWorker
from src.models.news import CHANNEL_X
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.social.card_visual import CardVisualGenerator
from src.social.cost import PostBudget
from src.social.metrics import collect_metrics
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
from src.utils.logger import log_error, log_step


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
        metrics_scheduler: 指標計測の定期実行（無効なら None）。動画計画・
            X投稿計画とは別の `DailyScheduler` インスタンス。理由は
            `_build_metrics_scheduler` の docstring を参照
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
    metrics_scheduler: DailyScheduler | None

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

        # 画像カードの視覚指示は台本生成と同じ Azure OpenAI リソース・
        # デプロイを使う（`post_generator` と同じ判断。画像そのものは
        # 別リソースなので `pipeline.image_generator` を使い分けている）。
        card_generator = CardVisualGenerator(
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
            # **画像カードを実際に添付する唯一の配線。** これを渡さないと
            # `post_due_once` の media_ids が常に None になり、カードの画像を
            # `gpt-image-2`（クォータはリージョン単位で上限4、動画生成と
            # 共食いする）で作って Blob に上げたうえで、**添付せずに投稿する**。
            # 生成も保存も成功しログにも異常が出ないので、実物の投稿を見るまで
            # 気付けない形で壊れる（実際にそうなっていた）。
            fetch_image=artifact_store.fetch,
            max_post_delay_minutes=config.x_max_post_delay_minutes,
            # 上限判定は計画側だけでは足りない。積んだあとに上限を越えたら、
            # その日の残りが出てしまう（行は SCHEDULED のまま残す）。
            budget=PostBudget(
                monthly_usd=config.x_monthly_budget_usd,
                unit_usd=config.x_cost_per_post_usd,
                unit_with_link_usd=config.x_cost_per_post_with_link_usd,
                unit_read_usd=config.x_cost_per_read_usd,
            ),
        )

        return cls(
            config=config,
            aggregator=aggregator,
            pipeline=pipeline,
            artifact_store=artifact_store,
            jobs=jobs,
            worker=JobWorker(jobs, PipelineJobRunner(pipeline, aggregator)),
            scheduler=_build_scheduler(
                config,
                aggregator,
                jobs,
                posts,
                post_generator,
                x_switch,
                card_generator=card_generator,
                # `Pipeline` が既に画像生成用の Azure クライアントを持っている。
                # 同じ `gpt-image-2` デプロイ（クォータはリージョン単位で
                # 上限4）を叩くだけの2つ目のクライアントを作らない。
                image_generator=pipeline.image_generator,
                artifacts=artifact_store,
            ),
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
            metrics_scheduler=_build_metrics_scheduler(config, posts, token_store, artifact_store),
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
    card_generator: CardVisualGenerator,
    image_generator: ImageGenerator,
    artifacts: ArtifactStore,
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
        jobs: ジョブ表。`plan_daily_posts` に渡し、動画生成が実行中の間は
            画像カードを作らせない（`gpt-image-2` のクォータをリージョン
            単位で動画パイプラインと共有しているため）
        posts: 投稿表
        post_generator: 投稿の下書き生成器
        x_switch: 自動投稿の有効/無効。`plan_daily_posts` に渡し、
            無効なら下書きの生成そのものを止める（送信ステップだけを
            止めるものではない。理由は `plan_daily_posts` の docstring）
        card_generator: 画像カードの視覚指示生成器
        image_generator: 画像カードの画像生成器。`Pipeline` が持つものを
            再利用する（同じクォータ上限を持つ2つ目のクライアントを
            作らないため）
        artifacts: 生成した画像カードの保存先

    Returns:
        DailyScheduler | None: 有効なら組み立てたスケジューラ
    """
    if not config.schedule_enabled:
        return None

    async def task() -> object:
        # **3段の順序: 取得 → X の計画 → 動画の投入。並べ替えないこと。**
        # 2つの理由が別々にこの順序を要求している。片方だけを覚えて直すと、
        # もう片方が守っていたバグが戻る。
        #
        # 理由1: **X の計画は動画の投入より先。** `plan_daily_batch` は動画
        # ジョブをジョブ表に積む。`plan_daily_posts` はカードを作る前に
        # `jobs.has_active_jobs()` を見て、実行待ち・実行中のジョブが1件でも
        # あれば CARD を SINGLE に降格する（`gpt-image-2` のクォータは
        # リージョン単位で上限4で、動画パイプラインと共有しているため）。
        # 動画を先に積むとこの判定が**常に True** になり、画像カードの機能
        # 全体（視覚指示・画像生成・Blob への保存・メディア添付）が本番で
        # 一度も動かない。カードは画像1枚・動画は6枚以上なので、X を先に
        # しても動画側の待ちはほぼ増えず、クォータの譲り合いとしても正しい。
        #
        # 理由2: **取得は両方の計画より先。** 取得は元々
        # `plan_daily_batch` の中にあった。理由1のために X を先に回すと、
        # その位置では X が「このサイクルで取得した記事」を見られなくなり、
        # 前回のサイクルの記事しか選べない（毎日1日古いニュースを投稿する
        # ことになる）。だから取得を `fetch_daily_news` に切り出して先頭で
        # 1回だけ行い、`plan_daily_batch` には `fetch=False` を渡す。
        #
        # 取得の失敗はここで止めない（`fetch_daily_news` が内部でログに
        # 残して帰る）。一時的なネットワーク障害で、既にストアにある記事で
        # できるはずの計画まで落とさないため。**X の計画も巻き込まれない。**
        #
        # `tests/test_scheduler_wiring.py` の
        # `test_動画ジョブを積んでもカードは作られる` が両方を見張っている。
        await fetch_daily_news(
            aggregator,
            ai_limit_per_feed=config.ai_news_limit_per_feed,
        )

        # 動画の計画が失敗しても X の計画は独立して試す（逆も同様）。
        # 1つのトラブルで両方が止まると、原因の切り分けがしづらくなる。
        try:
            post_plan = await plan_daily_posts(
                aggregator,
                posts,
                post_generator,
                enabled=x_switch.is_enabled(),
                times=config.x_post_times,
                posts_per_day=config.x_posts_per_day,
                budget_usd=config.x_monthly_budget_usd,
                unit_usd=config.x_cost_per_post_usd,
                unit_with_link_usd=config.x_cost_per_post_with_link_usd,
                unit_read_usd=config.x_cost_per_read_usd,
                now=datetime.now(UTC),
                timezone=config.schedule_timezone,
                card_generator=card_generator,
                image_generator=image_generator,
                artifacts=artifacts,
                jobs=jobs,
                # 動画パイプラインと同じ output_dir 配下に作る（生成は必ず
                # ローカルで行う、という既存の方針と揃える）。保存先だけが
                # `artifacts` で差し替わる。
                output_dir=config.output_dir / "cards",
            )
            # 積まなかった理由をログに出す。以前は戻り値を捨てていたため、
            # 「予算上限で止まった」「X で未使用の記事が無い」といった
            # 判断が画面にもログにも一切残らず、投稿が出ない日と
            # 「そもそも計画が走っていない」日を区別できなかった。
            if post_plan.skipped_reason:
                log_step(f"X 投稿を積みませんでした: {post_plan.skipped_reason}", "⏭️")
        except Exception as e:
            log_error(f"X 投稿の計画に失敗しました（次回は予定どおり走ります）: {e}")

        return await plan_daily_batch(
            aggregator,
            jobs,
            formats=config.schedule_formats,
            ai_limit_per_feed=config.ai_news_limit_per_feed,
            articles_per_format=config.schedule_articles_per_format,
            # 取得は上で1回だけ済ませてある（理由2）。
            fetch=False,
        )

    return DailyScheduler(
        task,
        run_at=config.schedule_run_at,
        timezone=config.schedule_timezone,
    )


def _build_metrics_scheduler(
    config: Config,
    posts: SocialPostRepository,
    token_store: TokenStore,
    artifact_store: ArtifactStore,
) -> DailyScheduler | None:
    """指標計測の定期実行を組み立てる（無効なら None）。

    **なぜ動画計画・X投稿計画とは別の `DailyScheduler` インスタンスか。**
    `DailyScheduler` は1つのコールバックを1つの時刻で回す作りで、
    2つ目の時刻を持たせる口が無い。選べる形は2つ。

    1. 別インスタンスを1つ増やす（本実装）
    2. 既存のコールバックの中に「今が計測の時刻か」を見る分岐を足す

    2 を選ばなかった理由: 既存のコールバックは `SCHEDULE_TIME` に
    1日1回しか起きない。分岐を追加すると、計測用の時刻に達したかどうかを
    このコールバック自身が毎回チェックする仕組み（もう1つのタイマー）を
    自分で持つ必要があり、結局 `DailyScheduler` を再実装することになる。
    2つ目のインスタンスを作れば、その面倒は既存クラスにそのまま乗れる。

    有効/無効も既存のスケジューラと同じ `SCHEDULE_ENABLED` に乗せる
    （計測だけを個別に切る要求は今のところ無く、設定を増やすと
    「動画計画は動くのに計測だけ動かない」という気付きにくい構成を
    作れてしまう）。

    **プロセスが落ちていた瞬間は測らない（catch-up はしない）。**
    `DailyScheduler` は次回時刻まで単純に待つだけで、過去の未実行分を
    追いかける仕組みを持たない。3日遅れて測った「24時間後の指標」は
    もはや24時間後の指標ではないので、これは正しい振る舞い。

    Args:
        config: アプリケーション設定
        posts: 投稿表
        token_store: OAuth トークンの保存先（X クライアントの構築に使う）
        artifact_store: 指標ファイルの保存先

    Returns:
        DailyScheduler | None: 有効なら組み立てたスケジューラ
    """
    if not config.schedule_enabled:
        return None

    async def task() -> object:
        # `PostWorker._run_one` と同じ規律: クライアントは使うときに作り、
        # 使い終わったら必ず閉じる。ここでの「使うとき」はポーリングの
        # 一周（既定30秒）ではなく、この日次タスクが起きた瞬間そのもの
        # （1日1回）。作りっぱなしで待機する経路が無いので、
        # 30秒ごとに漏れる PostWorker の問題はそもそも起きないが、
        # 閉じる責務をどちらが持つかを揃えるために同じ形にしている。
        try:
            client = _build_x_client(config, token_store)
        except XTokenExpiredError as e:
            # 未認証・失効は珍しい異常ではない。1日測れなくても、
            # 次の実行（翌日）まで待てば良い（このタスク自体が
            # catch-up を持たない設計なので、ここで諦めて構わない）。
            log_error(f"X の認証が必要です。今日の指標計測を見送ります: {e}")
            return None

        try:
            measured = collect_metrics(
                posts,
                client,
                artifact_store,
                config.output_dir / "metrics",
                now=datetime.now(UTC),
            )
        finally:
            client.close()
        return measured

    return DailyScheduler(
        task,
        run_at=config.metrics_run_at,
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


def get_token_store(context: AppContext = Depends(get_context)) -> TokenStore:
    """OAuth トークンの保存先を取得する。

    `/x/status` が X の認証有無（`load_credentials` が None を返すか）を
    判定するのに使う。YouTube/TikTok アップローダはトークン保存先を
    内部に持つが、X はここで直接読む必要があるため getter を分けている。
    """
    return context.token_store


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
    if context.metrics_scheduler is not None:
        context.metrics_scheduler.start()
    try:
        yield
    finally:
        if context.scheduler is not None:
            context.scheduler.stop()
        if context.metrics_scheduler is not None:
            context.metrics_scheduler.stop()
        # 停止を待つ。待たずに落とすと、実行中のジョブが RUNNING のまま
        # 残る（リースが切れれば回収されるが、無駄に15分待つことになる）。
        context.worker.stop()
        context.post_worker.stop()
        context.aggregator.close()
        app.state.context = None
