"""FastAPI routes for HTMX web interface."""

import json
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import Config
from src.models.job import BatchProgress
from src.models.news import NewsCategory
from src.news.aggregator import NewsAggregator
from src.storage.artifacts import ArtifactStore, ArtifactStoreError
from src.storage.jobs import JobRepository
from src.uploaders.tiktok_uploader import TikTokUploader, parse_privacy_level
from src.uploaders.youtube_uploader import YouTubeUploader
from src.utils.logger import log_error, log_step, log_success
from src.web.dependencies import (
    get_aggregator,
    get_artifact_store,
    get_config,
    get_jobs,
    get_tiktok_uploader,
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

    # AI関連ニュースも取得
    await aggregator.fetch_ai_news_and_store(
        config.ai_search_queries, config.ai_news_limit_per_query
    )

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
