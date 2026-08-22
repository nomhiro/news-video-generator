"""FastAPI routes for HTMX web interface."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import Config
from src.models.job import BatchProgress, GenerationJob, JobStatus
from src.models.news import NewsCategory
from src.models.social import (
    CANCELLABLE_STATUSES,
    X_MAX_WEIGHTED_LENGTH,
    InvalidPostTransition,
    PostStatus,
    SocialPost,
)
from src.news.aggregator import NewsAggregator
from src.social.cost import estimate_month_cost, is_over_budget
from src.social.switch import PostingSwitch
from src.social.x_auth import load_credentials
from src.storage.artifacts import ArtifactStore, ArtifactStoreError
from src.storage.jobs import JobRepository
from src.storage.social import SocialPostRepository
from src.storage.tokens import TokenStore
from src.uploaders.tiktok_uploader import TikTokUploader, parse_privacy_level
from src.uploaders.youtube_uploader import YouTubeUploader
from src.utils.logger import log_error, log_step, log_success
from src.web.dependencies import (
    get_aggregator,
    get_artifact_store,
    get_config,
    get_jobs,
    get_posts,
    get_tiktok_uploader,
    get_token_store,
    get_x_switch,
    get_youtube_uploader,
)

# Setup router and templates
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, aggregator: NewsAggregator = Depends(get_aggregator)):
    """メインページを表示する。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: メインページHTML
    """
    categories = list(NewsCategory)
    selected_count = aggregator.get_selected_count()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "categories": categories,
            "selected_count": selected_count,
            "active_category": NewsCategory.AI,  # AIカテゴリをデフォルトに
        },
    )


@router.get("/news/{category}", response_class=HTMLResponse)
async def get_news_by_category(
    request: Request, category: str, aggregator: NewsAggregator = Depends(get_aggregator)
):
    """カテゴリ別ニュース一覧を取得する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        category: カテゴリ名
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: ニュース一覧パーシャルHTML
    """
    try:
        cat = NewsCategory(category)
    except ValueError:
        cat = NewsCategory.GENERAL

    articles = aggregator.get_articles_by_category(cat)

    return templates.TemplateResponse(
        request,
        "partials/news_list.html",
        {
            "articles": articles,
            "category": cat,
        },
    )


@router.post("/news/fetch", response_class=HTMLResponse)
async def fetch_news(
    request: Request,
    aggregator: NewsAggregator = Depends(get_aggregator),
    config: Config = Depends(get_config),
):
    """最新ニュースを取得する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: カテゴリタブパーシャルHTML
    """
    # 通常カテゴリのニュースを取得
    await aggregator.fetch_and_store()

    # AI関連の記事も取得（発信元のフィードから。理由は src/news/feeds.py）
    await aggregator.fetch_ai_news_and_store(limit_per_feed=config.ai_news_limit_per_feed)

    categories = list(NewsCategory)
    selected_count = aggregator.get_selected_count()

    return templates.TemplateResponse(
        request,
        "partials/category_tabs.html",
        {
            "categories": categories,
            "active_category": NewsCategory.AI,  # AIカテゴリをデフォルトに
            "selected_count": selected_count,
            "fetch_success": True,
        },
    )


@router.post("/news/{article_id}/toggle", response_class=HTMLResponse)
async def toggle_article_selection(
    request: Request, article_id: str, aggregator: NewsAggregator = Depends(get_aggregator)
):
    """記事の選択状態を切り替える（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        article_id: 記事ID
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: 選択パネルパーシャルHTML
    """
    aggregator.toggle_selection(article_id)

    selected_articles = aggregator.get_selected_articles()
    selected_count = len(selected_articles)

    return templates.TemplateResponse(
        request,
        "partials/selected_panel.html",
        {
            "selected_articles": selected_articles,
            "selected_count": selected_count,
        },
    )


@router.get("/selected", response_class=HTMLResponse)
async def get_selected(request: Request, aggregator: NewsAggregator = Depends(get_aggregator)):
    """選択済み記事パネルを取得する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: 選択パネルパーシャルHTML
    """
    selected_articles = aggregator.get_selected_articles()
    selected_count = len(selected_articles)

    return templates.TemplateResponse(
        request,
        "partials/selected_panel.html",
        {
            "selected_articles": selected_articles,
            "selected_count": selected_count,
        },
    )


@router.delete("/news/{article_id}/remove", response_class=HTMLResponse)
async def remove_from_selection(
    request: Request, article_id: str, aggregator: NewsAggregator = Depends(get_aggregator)
):
    """記事を選択から削除する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        article_id: 記事ID
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: 選択パネルパーシャルHTML
    """
    aggregator.clear_selection(article_id)

    selected_articles = aggregator.get_selected_articles()
    selected_count = len(selected_articles)

    return templates.TemplateResponse(
        request,
        "partials/selected_panel.html",
        {
            "selected_articles": selected_articles,
            "selected_count": selected_count,
        },
    )


@router.post("/generate", response_class=HTMLResponse)
async def generate_videos(
    request: Request,
    video_format: str = Form("short"),
    aggregator: NewsAggregator = Depends(get_aggregator),
    jobs: JobRepository = Depends(get_jobs),
):
    """選択記事の生成ジョブを投入する。

    **このルートは生成しない。** ジョブ表に行を作って即座に返り、
    実行はワーカースレッドが担う。

    以前はここから `BackgroundTask` で生成を回していた。進捗が
    プロセスメモリにしか無かったため、再起動で消え、レプリカを
    増やせなかった。投入と実行を分けると、`/status` は DB を読むだけに
    なり、どのプロセスからでも同じ進捗が見える。

    Args:
        request: FastAPIリクエスト
        video_format: 動画形式 ("short" / "tiktok" / "long")
        aggregator: ニュース取得インスタンス
        jobs: ジョブ表

    Returns:
        HTMLResponse: 生成ステータスパーシャルHTML
    """
    # 実行中に押されたら積み増さない。同じ記事のジョブが二重に入ると
    # 画像生成のクォータを無駄に使う。
    if jobs.has_active_jobs():
        return _status_response(request, jobs.latest_progress())

    # 本文を先に取る。ジョブは article_id しか持たないので、
    # 実行時にニュースストアから読み直せる状態にしておく必要がある
    # （scrape_selected_content がスクレイピング結果を書き戻す）。
    articles = await aggregator.scrape_selected_content()

    if not articles:
        return templates.TemplateResponse(
            request,
            "partials/generation_status.html",
            {
                "status": "error",
                "message": "記事が選択されていません",
            },
        )

    # 本文が取れなかった記事も投入する。
    #
    # 以前はここで捨てていた（`if a.content` で絞っていた）。選択したのに
    # ジョブが作られず、どれが落ちたのかも分からないため、「3件選んだのに
    # 2件しか出来ていない」という状態を利用者が説明できなかった。
    # 全件を投入すれば、本文の無い記事は `ArticleUnavailable` で
    # **理由付きの失敗として** `/status` に並ぶ。画像生成に到達する前に
    # 落ちるのでクォータも使わず、再取得後に再実行もできる。
    without_content = [a.title for a in articles if not a.content]

    batch_id = jobs.enqueue_batch(
        [(a.id, a.title) for a in articles],
        video_format=video_format,
    )
    log_step(
        f"生成ジョブを投入しました: {len(articles)}件 ({video_format}, batch={batch_id[:8]})",
        "📥",
    )
    if without_content:
        log_error(
            f"本文が取得できていない記事が {len(without_content)}件あります"
            f"（失敗として記録されます）: {', '.join(t[:20] for t in without_content)}"
        )

    return _status_response(request, jobs.latest_progress())


@router.get("/status", response_class=HTMLResponse)
async def get_status(
    request: Request,
    jobs: JobRepository = Depends(get_jobs),
):
    """生成ステータスを取得する（ポーリング用）。

    ジョブ表を読むだけなので、生成中でも即座に返る。プロセスを
    再起動しても進捗が消えない。

    Args:
        request: FastAPIリクエスト
        jobs: ジョブ表

    Returns:
        HTMLResponse: ステータスパーシャルHTML
    """
    return _status_response(request, jobs.latest_progress())


def _status_response(request: Request, progress: BatchProgress) -> HTMLResponse:
    """進捗をステータスのパーシャル HTML にする。

    投入直後とポーリングで同じ表示になるよう、変換を1箇所に置く。

    Args:
        request: FastAPIリクエスト
        progress: 直近バッチの進捗

    Returns:
        HTMLResponse: ステータスパーシャルHTML
    """
    return templates.TemplateResponse(
        request,
        "partials/generation_status.html",
        {
            "status": progress.status,
            "total_count": progress.total_count,
            "completed_count": progress.completed_count,
            "current_article": progress.current_article,
            "completed_articles": progress.completed_articles,
            "failed_articles": progress.failed_articles,
            "error_message": progress.error_message,
        },
    )


# ============================================================
# Legal Pages (for TikTok URL Verification)
# ============================================================


@router.get("/terms", response_class=HTMLResponse)
async def terms_of_service(request: Request):
    """利用規約ページを表示する。

    TikTok Developer PortalのURL検証に必要。

    Returns:
        HTMLResponse: 利用規約ページHTML
    """
    return templates.TemplateResponse(request, "legal/terms.html", {})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request):
    """プライバシーポリシーページを表示する。

    TikTok Developer PortalのURL検証に必要。

    Returns:
        HTMLResponse: プライバシーポリシーページHTML
    """
    return templates.TemplateResponse(request, "legal/privacy.html", {})


# ============================================================
# YouTube Upload Routes
# ============================================================


@router.get("/youtube/status", response_class=HTMLResponse)
async def youtube_auth_status(
    request: Request, uploader: YouTubeUploader = Depends(get_youtube_uploader)
):
    """YouTube認証状態を取得する。

    Args:
        request: FastAPIリクエスト
        uploader: YouTubeアップローダー

    Returns:
        HTMLResponse: 認証状態パーシャルHTML
    """
    is_authenticated = uploader.is_authenticated()

    return templates.TemplateResponse(
        request,
        "partials/youtube_status.html",
        {
            "is_authenticated": is_authenticated,
        },
    )


@router.post("/youtube/auth", response_class=HTMLResponse)
async def youtube_authenticate(
    request: Request, uploader: YouTubeUploader = Depends(get_youtube_uploader)
):
    """YouTube認証を実行する。

    Args:
        request: FastAPIリクエスト
        uploader: YouTubeアップローダー

    Returns:
        HTMLResponse: 認証結果パーシャルHTML
    """
    try:
        uploader.authenticate()
        return templates.TemplateResponse(
            request,
            "partials/youtube_status.html",
            {
                "is_authenticated": True,
                "auth_message": "YouTube認証に成功しました",
            },
        )
    except Exception as e:
        log_error(f"YouTube認証エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/youtube_status.html",
            {
                "is_authenticated": False,
                "auth_message": str(e),
            },
        )


@router.get("/videos", response_class=HTMLResponse)
async def list_videos(
    request: Request,
    artifact_store: ArtifactStore = Depends(get_artifact_store),
):
    """生成済み動画一覧を表示する。

    保存先（ローカル or Blob Storage）に問い合わせる。以前は
    `output_dir/videos` を glob していたが、それだとコンテナで動かしたときに
    Blob 上の動画が一覧に出ない。

    Args:
        request: FastAPIリクエスト
        artifact_store: 生成物の保存先

    Returns:
        HTMLResponse: 動画一覧パーシャルHTML
    """
    try:
        found = artifact_store.list("videos/")
    except ArtifactStoreError as e:
        log_error(f"動画一覧を取得できません: {e}")
        found = []

    # 新しい20件だけを見る。台本 JSON の取得は Blob だと1件ごとに
    # ダウンロードが走るため、表示しない分は読まない。
    recent = [a for a in found if a.key.endswith(".mp4")][:20]

    videos = [
        {
            "filename": artifact.name,
            "key": artifact.key,
            "language": _language_from_key(artifact.key),
            "size_mb": round(artifact.size_bytes / (1024 * 1024), 2),
            "created": artifact.modified_at.timestamp(),
            **_script_metadata(artifact_store, artifact.key),
        }
        for artifact in recent
    ]

    return templates.TemplateResponse(
        request,
        "partials/video_list.html",
        {
            "videos": videos,
        },
    )


def _language_from_key(key: str) -> str:
    """動画のキーから言語コードを取り出す。

    キーの形は `videos/YYYYMMDD_HHMMSS_タイトル_<lang>.mp4`。

    Args:
        key: 動画のキー

    Returns:
        str: 言語コード（判別できなければ "unknown"）
    """
    stem = PurePosixPath(key).stem
    parts = stem.rsplit("_", 1)
    return parts[-1] if len(parts) > 1 else "unknown"


def _script_metadata(artifact_store: ArtifactStore, video_key: str) -> dict[str, str]:
    """動画に対応する台本 JSON からタイトルと説明を読む。

    台本が無い動画（手動で置いた等）でも一覧は出したいので、
    取得できなければ空文字を返す。

    Args:
        artifact_store: 生成物の保存先
        video_key: 動画のキー

    Returns:
        dict[str, str]: title と description
    """
    script_key = f"scripts/{PurePosixPath(video_key).stem}.json"
    try:
        with artifact_store.fetch(script_key) as path:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (ArtifactStoreError, OSError, json.JSONDecodeError):
        return {"title": "", "description": ""}
    return {
        "title": str(data.get("title", "")),
        "description": str(data.get("description", "")),
    }


@router.post("/youtube/upload", response_class=HTMLResponse)
async def youtube_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    video_key: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    uploader: YouTubeUploader = Depends(get_youtube_uploader),
    config: Config = Depends(get_config),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
):
    """動画をYouTubeにアップロードする。

    Args:
        request: FastAPIリクエスト
        background_tasks: バックグラウンドタスク
        video_key: 生成物のキー（`videos/....mp4`）
        title: 動画タイトル
        description: 動画説明
        uploader: YouTubeアップローダー
        artifact_store: 生成物の保存先

    Returns:
        HTMLResponse: アップロード結果パーシャルHTML
    """
    if not artifact_store.exists(video_key):
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "success": False,
                "error_message": f"動画が見つかりません: {video_key}",
            },
        )

    # Check authentication
    if not uploader.is_authenticated():
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "success": False,
                "error_message": "YouTubeにログインしてください",
                "need_auth": True,
            },
        )

    log_step(f"YouTubeアップロード開始: {title}", "📤")

    # アップローダはローカルパスを要求するため、保存先から借りる。
    # Blob 保存なら一時ファイルに落ち、この with を抜けたら消える。
    with artifact_store.fetch(video_key) as video_file:
        result = uploader.upload(
            video_path=str(video_file),
            title=title,
            description=description,
            tags=["Shorts", "ニュース", "AI生成"],
            privacy_status=config.youtube_default_privacy,
        )

    if result.success:
        log_success(f"YouTubeアップロード完了: {result.video_url}")
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "success": True,
                "video_id": result.video_id,
                "video_url": result.video_url,
                "title": title,
            },
        )
    else:
        log_error(f"YouTubeアップロード失敗: {result.error_message}")
        return templates.TemplateResponse(
            request,
            "partials/upload_result.html",
            {
                "success": False,
                "error_message": result.error_message,
            },
        )


# ============================================================
# TikTok Upload Routes
# ============================================================


@router.get("/tiktok/status", response_class=HTMLResponse)
async def tiktok_auth_status(
    request: Request,
    uploader: TikTokUploader = Depends(get_tiktok_uploader),
    config: Config = Depends(get_config),
):
    """TikTok認証状態を取得する。

    Args:
        request: FastAPIリクエスト
        uploader: TikTokアップローダー

    Returns:
        HTMLResponse: 認証状態パーシャルHTML
    """
    is_configured = config.is_tiktok_configured()
    is_authenticated = uploader.is_authenticated() if is_configured else False

    return templates.TemplateResponse(
        request,
        "partials/tiktok_status.html",
        {
            "is_configured": is_configured,
            "is_authenticated": is_authenticated,
        },
    )


@router.post("/tiktok/auth", response_class=HTMLResponse)
async def tiktok_authenticate(
    request: Request,
    uploader: TikTokUploader = Depends(get_tiktok_uploader),
    config: Config = Depends(get_config),
):
    """TikTok認証を実行する。

    Args:
        request: FastAPIリクエスト
        uploader: TikTokアップローダー

    Returns:
        HTMLResponse: 認証結果パーシャルHTML
    """
    if not config.is_tiktok_configured():
        return templates.TemplateResponse(
            request,
            "partials/tiktok_status.html",
            {
                "is_configured": False,
                "is_authenticated": False,
                "auth_message": "TikTokのAPIキーが設定されていません。.envファイルにTIKTOK_CLIENT_KEYとTIKTOK_CLIENT_SECRETを設定してください。",
            },
        )

    try:
        uploader.authenticate()
        return templates.TemplateResponse(
            request,
            "partials/tiktok_status.html",
            {
                "is_configured": True,
                "is_authenticated": True,
                "auth_message": "TikTok認証に成功しました",
            },
        )
    except Exception as e:
        log_error(f"TikTok認証エラー: {e}")
        return templates.TemplateResponse(
            request,
            "partials/tiktok_status.html",
            {
                "is_configured": True,
                "is_authenticated": False,
                "auth_message": str(e),
            },
        )


@router.post("/tiktok/upload", response_class=HTMLResponse)
async def tiktok_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    video_key: str = Form(...),
    title: str = Form(...),
    uploader: TikTokUploader = Depends(get_tiktok_uploader),
    config: Config = Depends(get_config),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
):
    """動画をTikTokにアップロードする。

    Args:
        request: FastAPIリクエスト
        background_tasks: バックグラウンドタスク
        video_key: 生成物のキー（`videos/....mp4`）
        title: 動画タイトル（キャプション）
        uploader: TikTokアップローダー
        artifact_store: 生成物の保存先

    Returns:
        HTMLResponse: アップロード結果パーシャルHTML
    """
    # Verify TikTok is configured
    if not config.is_tiktok_configured():
        return templates.TemplateResponse(
            request,
            "partials/tiktok_upload_result.html",
            {
                "success": False,
                "error_message": "TikTokのAPIキーが設定されていません",
            },
        )

    if not artifact_store.exists(video_key):
        return templates.TemplateResponse(
            request,
            "partials/tiktok_upload_result.html",
            {
                "success": False,
                "error_message": f"動画が見つかりません: {video_key}",
            },
        )

    # Check authentication
    if not uploader.is_authenticated():
        return templates.TemplateResponse(
            request,
            "partials/tiktok_upload_result.html",
            {
                "success": False,
                "error_message": "TikTokにログインしてください",
                "need_auth": True,
            },
        )

    log_step(f"TikTokアップロード開始: {title}", "📤")

    # 保存先からローカルパスを借りる（Blob なら一時ファイル）
    with artifact_store.fetch(video_key) as video_file:
        result = uploader.upload(
            video_path=str(video_file),
            title=title,
            # 設定から来た文字列をここで検証する。不正な値が TikTok API まで
            # 到達すると、原因の分かりにくいエラーで失敗する。
            privacy_level=parse_privacy_level(config.tiktok_default_privacy),
        )

    if result.success:
        log_success("TikTokアップロード完了")
        return templates.TemplateResponse(
            request,
            "partials/tiktok_upload_result.html",
            {
                "success": True,
                "publish_id": result.publish_id,
                "video_url": result.video_url,
                "title": title,
            },
        )
    else:
        log_error(f"TikTokアップロード失敗: {result.error_message}")
        return templates.TemplateResponse(
            request,
            "partials/tiktok_upload_result.html",
            {
                "success": False,
                "error_message": result.error_message,
            },
        )


# ============================================================
# X 運用の画面
#
# 以前の画面は「利用者が操作する」ことを前提にしていた。X の自動投稿が
# 入った今、運用者はこの画面を見ているだけで、実際の操作（下書き生成・
# 送信）はスケジューラとワーカーが行う。運用者の仕事は「これから出る
# ものを読んで、おかしければ気付く」ことだけなので、ルートも
# 「畳まず全文を出す」「状態を色と語の両方で示す」ことを優先する。
# ============================================================

# 投稿の状態 -> 表示語。色だけでは伝わらない（色覚・モノクロ印刷等）ので
# 必ず語も出す。
_POST_STATUS_LABELS: dict[PostStatus, str] = {
    PostStatus.DRAFTED: "下書き",
    PostStatus.SCHEDULED: "予約",
    PostStatus.POSTING: "投稿中",
    PostStatus.POSTED: "投稿済",
    PostStatus.FAILED: "失敗",
    PostStatus.NEEDS_REVIEW: "要確認",
}

# 動画ジョブの状態 -> 表示語。X の投稿と同じ帯に並べるための対応表。
_JOB_STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.QUEUED: "待機",
    JobStatus.RUNNING: "生成中",
    JobStatus.SUCCEEDED: "完成",
    JobStatus.FAILED: "失敗",
}


def _slot_time(post: SocialPost) -> datetime:
    """帯に置く時刻を決める。

    投稿済みは実際に出た時刻（`posted_at`）、それ以外は予定時刻を使う。
    どちらも無い（起こらないはずだが、下書きのまま予定が付いていない等）
    場合は投入時刻に落とす。
    """
    if post.status == PostStatus.POSTED and post.posted_at is not None:
        return post.posted_at
    return post.scheduled_at or post.created_at


def _to_post_slot(post: SocialPost, zone: ZoneInfo) -> dict[str, object]:
    """`SocialPost` を帯の1枠に変換する。"""
    return {
        "at": _slot_time(post).astimezone(zone),
        "kind": "x",
        "status": post.status.value,
        "status_label": _POST_STATUS_LABELS[post.status],
        "label": post.article_title,
    }


def _to_job_slot(job: GenerationJob, zone: ZoneInfo) -> dict[str, object]:
    """`GenerationJob` を帯の1枠に変換する。

    動画と X 投稿を同じ軸に並べる理由: どちらも `gpt-image-2` の
    リージョン単位クォータ（上限4）を共有しているため、同時に走っている
    ことが帯を見た瞬間に分かる必要がある。
    """
    return {
        "at": job.created_at.astimezone(zone),
        "kind": "video",
        "status": job.status.value,
        "status_label": _JOB_STATUS_LABELS[job.status],
        "label": job.article_title,
    }


def _slot_sort_key(slot: dict[str, object]) -> datetime:
    """帯の並び替え用キー。

    `dict[str, object]` のままだと `sorted` の `key` が `object` を返す形に
    推論され、比較可能性を型で保証できない。時刻だけを取り出して
    `datetime` として返す。
    """
    at = slot["at"]
    assert isinstance(at, datetime)
    return at


@router.get("/x/band", response_class=HTMLResponse)
async def x_band(
    request: Request,
    posts: SocialPostRepository = Depends(get_posts),
    config: Config = Depends(get_config),
    jobs: JobRepository = Depends(get_jobs),
) -> HTMLResponse:
    """今日の時間割。過ぎた枠・いま・これから出るものを1本の軸に並べる。

    このプロダクトの本質は「決めた時刻に、見ていない間に出る」こと。
    数字のタイルではなく時間軸を主役にしているのはそのため。
    """
    zone = ZoneInfo(config.schedule_timezone)
    local_now = datetime.now(UTC).astimezone(zone)
    day_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_local.astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)

    # 失敗した行も帯に載せる。載せていなかった間、`discard_stale` が
    # 見送った投稿はどの一覧にも出ず、痕跡がログの1行だけだった
    # （運用者は空のキューを見て「今日はニュースが無かった」と解釈する）。
    failed = posts.list_recent_failed(day_start_utc)
    post_slots = [
        _to_post_slot(post, zone)
        for post in posts.list_upcoming(limit=40)
        + posts.list_posted_between(day_start_utc, day_end_utc)
        + failed
    ]
    # `latest_batch_id()` + `list_batch()` は直近1バッチしか見ない。
    # 同じ日に複数バッチが走ると早い方の動画が帯から消え、進行中の
    # クォータ衝突が見えなくなる。投入時刻の範囲で直接絞る。
    job_slots = [
        _to_job_slot(job, zone) for job in jobs.list_jobs_between(day_start_utc, day_end_utc)
    ]

    return templates.TemplateResponse(
        request,
        "partials/day_band.html",
        {
            "slots": sorted(post_slots + job_slots, key=_slot_sort_key),
            "now": local_now,
            # 1日を 00:00〜24:00 の帯として描くので、いまの位置は割合で渡す
            "now_ratio": (local_now.hour * 60 + local_now.minute) / (24 * 60),
            "needs_review": posts.list_needs_review(),
            "failed": failed,
        },
    )


@router.get("/x/queue", response_class=HTMLResponse)
async def x_queue(
    request: Request,
    posts: SocialPostRepository = Depends(get_posts),
    config: Config = Depends(get_config),
    message: str | None = None,
) -> HTMLResponse:
    """投稿キュー。要確認を先に、次にこれから出るものを並べる。

    本文は畳まない。自動投稿なので運用者の唯一の仕事が「読んで気付く」
    ことで、開かないと読めない UI では誰も読まない。

    Args:
        message: 直前の操作結果（例:「取り消しました」）。`aria-live` に出す
    """
    # 失敗した行も出す。出なかった投稿があることを、空のキューだけから
    # 読み取ることはできない。
    #
    # 帯（`/x/band`）は1日の時間軸を描くので日付境界で切るが、キューは
    # 時間軸ではないので直近24時間で切る。日付境界だと 00:05 に見たとき
    # 「昨夜 21:30 の投稿が落ちた」ことが消える。
    since = datetime.now(UTC) - timedelta(hours=24)

    return templates.TemplateResponse(
        request,
        "partials/post_queue.html",
        {
            "needs_review": posts.list_needs_review(),
            "failed": posts.list_recent_failed(since),
            "upcoming": posts.list_upcoming(limit=20),
            "max_weighted": X_MAX_WEIGHTED_LENGTH,
            # 予定時刻は運用者のタイムゾーンで出す。行が持っているのは UTC で、
            # そのまま strftime すると帯（`/x/band` は astimezone している）と
            # 9時間ずれた時刻が並ぶ。「何がいつ出るか」を読む場所で時刻が
            # 2種類あるのは、単なる表示の粗さではなく誤読の原因になる。
            "zone": ZoneInfo(config.schedule_timezone),
            "message": message,
            # `list_upcoming` は POSTING（送信中）も含める。送信中の行に
            # 取り消しボタンを出すと、押した結果が二重投稿になりうるので
            # 状態で出し分ける（拒否はサーバー側にもあるが、押せない
            # ボタンを見せない方が運用者を迷わせない）。
            "cancellable": CANCELLABLE_STATUSES,
        },
    )


def _x_status_context(
    posts: SocialPostRepository,
    switch: PostingSwitch,
    config: Config,
    tokens: TokenStore,
) -> dict[str, object]:
    """ヘッダーと本文パネルが共有する状態。

    2つのテンプレートで同じ値を使うので、組み立てを1箇所に置く。
    ずれると「ヘッダーは稼働中、パネルは停止中」という、非常停止の
    スイッチとして最悪の見え方になる。
    """
    now = datetime.now(UTC)
    plain, with_link = posts.monthly_post_counts(now.year, now.month)
    spent = estimate_month_cost(
        plain,
        with_link,
        config.x_cost_per_post_usd,
        config.x_cost_per_post_with_link_usd,
        config.x_cost_per_read_usd,
    )
    return {
        "enabled": switch.is_enabled(),
        # 「概算」と明示する。実際の課金は X 側の集計なので一致を保証できない
        "spent_usd": spent,
        "budget_usd": config.x_monthly_budget_usd,
        "authenticated": load_credentials(tokens) is not None,
        # 上限に達していると、スイッチが有効でもワーカーは送信しない
        # （行は SCHEDULED のまま残る）。スイッチの語だけを見せると
        # 「稼働中なのに出ない」ことの説明が画面に無くなる。
        "over_budget": is_over_budget(spent, config.x_monthly_budget_usd),
    }


@router.get("/x/status", response_class=HTMLResponse)
async def x_status(
    request: Request,
    posts: SocialPostRepository = Depends(get_posts),
    switch: PostingSwitch = Depends(get_x_switch),
    config: Config = Depends(get_config),
    tokens: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """自動投稿の状態、概算コスト、認証の有無（本文パネル）。

    未認証のときは、画面から完結する再認証フローを出さない。X の
    PKCE フローはブラウザのリダイレクトを要求し、コンテナの中では
    完結できない（YouTube を localhost にリダイレクトできないのと
    同じ理由）。代わりに、ローカルで認証してから
    `scripts.push_tokens` で送る手順を案内する。

    **ヘッダーは `/x/status/header` を使う（このパネルを入れない）。**
    以前はヘッダーと本文の両方がこのパネルを読み込み、パネル自身も
    `id="x-status"` を持っていたため、id が文書内に2つ存在した。
    htmx は最初の一致にしか入れ替えを行わないので、スイッチを切り替えても
    本文パネルは古い状態を映したまま残った（非常停止のスイッチが
    「効いていないように見える」のが最悪）。
    """
    return templates.TemplateResponse(
        request, "partials/x_status.html", _x_status_context(posts, switch, config, tokens)
    )


@router.get("/x/status/header", response_class=HTMLResponse)
async def x_status_header(
    request: Request,
    posts: SocialPostRepository = Depends(get_posts),
    switch: PostingSwitch = Depends(get_x_switch),
    config: Config = Depends(get_config),
    tokens: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """ヘッダーに出す状態の点と概算コストだけ。

    設計のワイヤーフレームに合わせている。ヘッダーは一日中見えている
    ものなので、操作（停止ボタン）と手順の説明（再認証の案内）は
    本文パネルに置き、ここは「いま動いているか」「いくら使ったか」の
    2点だけにする。
    """
    return templates.TemplateResponse(
        request, "partials/x_status_header.html", _x_status_context(posts, switch, config, tokens)
    )


@router.post("/x/enabled", response_class=HTMLResponse)
async def x_set_enabled(
    request: Request,
    enabled: bool = Form(...),
    switch: PostingSwitch = Depends(get_x_switch),
    posts: SocialPostRepository = Depends(get_posts),
    config: Config = Depends(get_config),
    tokens: TokenStore = Depends(get_token_store),
) -> HTMLResponse:
    """自動投稿を開始・停止する。

    実体は Azure Files 上のファイル。SQLite に置くとリビジョン更新で
    消え、画面で有効にした翌日にマージした時点で黙って止まる。
    """
    switch.set_enabled(enabled)
    return await x_status(request, posts, switch, config, tokens)


@router.post("/x/posts/{post_id}/cancel", response_class=HTMLResponse)
async def x_cancel_post(
    request: Request,
    post_id: int,
    posts: SocialPostRepository = Depends(get_posts),
    config: Config = Depends(get_config),
) -> HTMLResponse:
    """予約を取り消す。

    操作名を通す。ボタンが「取り消す」なら結果の文言も「取り消しました」。

    **状態を見ずに落とさない。** 送信中（POSTING）の行を取り消せると、
    投稿は X に出たのに行は FAILED になり、ワーカーの `mark_posted` が
    `FAILED -> POSTED` で例外になって記事が消費済みにならず、翌日
    同じ内容がもう一度公開される。送信済み（POSTED）も、キューが最大
    30秒古い値を映すぶん押されうる。どちらも例外を画面に出さず
    （500 だと htmx が何も入れ替えず、押しても無反応に見える）、
    キューを返して理由を伝える。
    """
    try:
        posts.cancel(post_id, "取り消しました")
    except InvalidPostTransition:
        return await x_queue(
            request, posts, config, message="送信中または送信済みのため取り消せませんでした"
        )
    return await x_queue(request, posts, config, message="取り消しました")
