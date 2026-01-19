"""FastAPI routes for HTMX web interface."""

import os
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Request, Depends, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.models.news import NewsCategory, NewsArticle
from src.news.aggregator import NewsAggregator
from src.pipeline import Pipeline
from src.uploaders.youtube_uploader import YouTubeUploader
from src.uploaders.tiktok_uploader import TikTokUploader
from src.web.dependencies import (
    get_aggregator,
    get_pipeline,
    get_generation_state,
    get_youtube_uploader,
    get_tiktok_uploader,
    get_config,
    GenerationState,
)
from src.utils.logger import log_step, log_success, log_error


# Setup router and templates
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent.parent / "templates")


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    aggregator: NewsAggregator = Depends(get_aggregator)
):
    """メインページを表示する。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: メインページHTML
    """
    categories = list(NewsCategory)
    selected_count = aggregator.get_selected_count()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "categories": categories,
        "selected_count": selected_count,
        "active_category": NewsCategory.AI,  # AIカテゴリをデフォルトに
    })


@router.get("/news/{category}", response_class=HTMLResponse)
async def get_news_by_category(
    request: Request,
    category: str,
    aggregator: NewsAggregator = Depends(get_aggregator)
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

    return templates.TemplateResponse("partials/news_list.html", {
        "request": request,
        "articles": articles,
        "category": cat,
    })


@router.post("/news/fetch", response_class=HTMLResponse)
async def fetch_news(
    request: Request,
    aggregator: NewsAggregator = Depends(get_aggregator)
):
    """最新ニュースを取得する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: カテゴリタブパーシャルHTML
    """
    config = get_config()

    # 通常カテゴリのニュースを取得
    await aggregator.fetch_and_store()

    # AI関連ニュースも取得
    await aggregator.fetch_ai_news_and_store(
        config.ai_search_queries,
        config.ai_news_limit_per_query
    )

    categories = list(NewsCategory)
    selected_count = aggregator.get_selected_count()

    return templates.TemplateResponse("partials/category_tabs.html", {
        "request": request,
        "categories": categories,
        "active_category": NewsCategory.AI,  # AIカテゴリをデフォルトに
        "selected_count": selected_count,
        "fetch_success": True,
    })


@router.post("/news/{article_id}/toggle", response_class=HTMLResponse)
async def toggle_article_selection(
    request: Request,
    article_id: str,
    aggregator: NewsAggregator = Depends(get_aggregator)
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

    return templates.TemplateResponse("partials/selected_panel.html", {
        "request": request,
        "selected_articles": selected_articles,
        "selected_count": selected_count,
    })


@router.get("/selected", response_class=HTMLResponse)
async def get_selected(
    request: Request,
    aggregator: NewsAggregator = Depends(get_aggregator)
):
    """選択済み記事パネルを取得する（HTMXパーシャル）。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス

    Returns:
        HTMLResponse: 選択パネルパーシャルHTML
    """
    selected_articles = aggregator.get_selected_articles()
    selected_count = len(selected_articles)

    return templates.TemplateResponse("partials/selected_panel.html", {
        "request": request,
        "selected_articles": selected_articles,
        "selected_count": selected_count,
    })


@router.delete("/news/{article_id}/remove", response_class=HTMLResponse)
async def remove_from_selection(
    request: Request,
    article_id: str,
    aggregator: NewsAggregator = Depends(get_aggregator)
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

    return templates.TemplateResponse("partials/selected_panel.html", {
        "request": request,
        "selected_articles": selected_articles,
        "selected_count": selected_count,
    })


@router.post("/generate", response_class=HTMLResponse)
async def generate_videos(
    request: Request,
    background_tasks: BackgroundTasks,
    video_format: str = Form("short"),
    aggregator: NewsAggregator = Depends(get_aggregator),
    pipeline: Pipeline = Depends(get_pipeline),
    state: GenerationState = Depends(get_generation_state)
):
    """選択記事から動画を生成する。

    Args:
        request: FastAPIリクエスト
        background_tasks: バックグラウンドタスク
        video_format: 動画形式 ("short" or "long")
        aggregator: ニュース取得インスタンス
        pipeline: 動画生成パイプライン
        state: 生成状態

    Returns:
        HTMLResponse: 生成ステータスパーシャルHTML
    """
    # Check if already running
    if state.is_running:
        return templates.TemplateResponse("partials/generation_status.html", {
            "request": request,
            "status": "running",
            "total_count": state.total_count,
            "completed_count": state.completed_count,
            "current_article": state.current_article,
        })

    # Scrape content first
    articles = await aggregator.scrape_selected_content()

    if not articles:
        return templates.TemplateResponse("partials/generation_status.html", {
            "request": request,
            "status": "error",
            "message": "記事が選択されていません",
        })

    # Filter articles with content
    articles_with_content = [a for a in articles if a.content]

    if not articles_with_content:
        return templates.TemplateResponse("partials/generation_status.html", {
            "request": request,
            "status": "error",
            "message": "スクレイピングに失敗しました。別の記事を選択してください",
        })

    # Initialize generation state
    state.start(len(articles_with_content))

    # Start background generation
    background_tasks.add_task(
        generate_videos_task,
        articles_with_content,
        pipeline,
        aggregator,
        state,
        video_format
    )

    return templates.TemplateResponse("partials/generation_status.html", {
        "request": request,
        "status": "running",
        "total_count": len(articles_with_content),
        "completed_count": 0,
        "current_article": articles_with_content[0].title if articles_with_content else None,
    })


async def generate_videos_task(
    articles: List[NewsArticle],
    pipeline: Pipeline,
    aggregator: NewsAggregator,
    state: GenerationState,
    video_format: str = "short"
) -> None:
    """バックグラウンドで動画を生成するタスク。

    Args:
        articles: 生成対象の記事リスト
        pipeline: 動画生成パイプライン
        aggregator: ニュース取得インスタンス
        state: 生成状態
        video_format: 動画形式 ("short" or "long")
    """
    format_label = "ロング" if video_format == "long" else "ショート"
    log_step(f"バックグラウンド生成開始: {len(articles)}件 ({format_label})", "🎬")

    for i, article in enumerate(articles, 1):
        try:
            # Update state with current article
            state.update(article.title)

            log_step(f"[{i}/{len(articles)}] {article.title[:30]}...", "📹")

            # Create topic from article
            topic = f"{article.title}\n\n{article.content[:2000]}"

            # Run pipeline with article title as output name
            result = pipeline.run(topic, languages=["ja"], output_name=article.title, video_format=video_format)

            # Mark as generated
            aggregator.mark_as_generated(article.id)

            # Update state
            state.complete_one(article.title, success=True)

            log_success(f"[{i}/{len(articles)}] 完了: {article.title[:30]}")

        except Exception as e:
            state.complete_one(article.title, success=False)
            log_error(f"[{i}/{len(articles)}] 失敗: {article.title[:30]} - {e}")

    # Finish generation
    state.finish()
    log_success(f"バックグラウンド生成完了: {len(articles)}件")


@router.get("/status", response_class=HTMLResponse)
async def get_status(
    request: Request,
    aggregator: NewsAggregator = Depends(get_aggregator),
    state: GenerationState = Depends(get_generation_state)
):
    """生成ステータスを取得する（ポーリング用）。

    Args:
        request: FastAPIリクエスト
        aggregator: ニュース取得インスタンス
        state: 生成状態

    Returns:
        HTMLResponse: ステータスパーシャルHTML
    """
    status = state.get_status()

    return templates.TemplateResponse("partials/generation_status.html", {
        "request": request,
        "status": status,
        "total_count": state.total_count,
        "completed_count": state.completed_count,
        "current_article": state.current_article,
        "completed_articles": state.completed_articles,
        "failed_articles": state.failed_articles,
        "error_message": state.error_message,
    })


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
    return templates.TemplateResponse("legal/terms.html", {
        "request": request,
    })


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request):
    """プライバシーポリシーページを表示する。

    TikTok Developer PortalのURL検証に必要。

    Returns:
        HTMLResponse: プライバシーポリシーページHTML
    """
    return templates.TemplateResponse("legal/privacy.html", {
        "request": request,
    })


# ============================================================
# YouTube Upload Routes
# ============================================================

@router.get("/youtube/status", response_class=HTMLResponse)
async def youtube_auth_status(
    request: Request,
    uploader: YouTubeUploader = Depends(get_youtube_uploader)
):
    """YouTube認証状態を取得する。

    Args:
        request: FastAPIリクエスト
        uploader: YouTubeアップローダー

    Returns:
        HTMLResponse: 認証状態パーシャルHTML
    """
    is_authenticated = uploader.is_authenticated()

    return templates.TemplateResponse("partials/youtube_status.html", {
        "request": request,
        "is_authenticated": is_authenticated,
    })


@router.post("/youtube/auth", response_class=HTMLResponse)
async def youtube_authenticate(
    request: Request,
    uploader: YouTubeUploader = Depends(get_youtube_uploader)
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
        return templates.TemplateResponse("partials/youtube_status.html", {
            "request": request,
            "is_authenticated": True,
            "auth_message": "YouTube認証に成功しました",
        })
    except Exception as e:
        log_error(f"YouTube認証エラー: {e}")
        return templates.TemplateResponse("partials/youtube_status.html", {
            "request": request,
            "is_authenticated": False,
            "auth_message": str(e),
        })


@router.get("/videos", response_class=HTMLResponse)
async def list_videos(request: Request):
    """生成済み動画一覧を表示する。

    Args:
        request: FastAPIリクエスト

    Returns:
        HTMLResponse: 動画一覧パーシャルHTML
    """
    import json
    from src.web.dependencies import get_config
    config = get_config()

    videos_dir = config.output_dir / "videos"
    scripts_dir = config.output_dir / "scripts"
    videos = []

    if videos_dir.exists():
        for video_file in sorted(videos_dir.glob("*.mp4"), reverse=True):
            # Parse filename to extract info
            # Format: YYYYMMDD_HHMMSS_title_lang.mp4
            stem = video_file.stem
            parts = stem.rsplit("_", 1)
            lang = parts[-1] if len(parts) > 1 else "unknown"

            # Try to load description and title from corresponding script JSON
            description = ""
            title = ""
            script_file = scripts_dir / f"{stem}.json"
            if script_file.exists():
                try:
                    with open(script_file, "r", encoding="utf-8") as f:
                        script_data = json.load(f)
                        description = script_data.get("description", "")
                        title = script_data.get("title", "")
                except Exception:
                    pass

            videos.append({
                "filename": video_file.name,
                "path": str(video_file),
                "language": lang,
                "size_mb": round(video_file.stat().st_size / (1024 * 1024), 2),
                "created": video_file.stat().st_mtime,
                "description": description,
                "title": title,
            })

    return templates.TemplateResponse("partials/video_list.html", {
        "request": request,
        "videos": videos[:20],  # Limit to 20 most recent
    })


@router.post("/youtube/upload", response_class=HTMLResponse)
async def youtube_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    video_path: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    uploader: YouTubeUploader = Depends(get_youtube_uploader),
):
    """動画をYouTubeにアップロードする。

    Args:
        request: FastAPIリクエスト
        background_tasks: バックグラウンドタスク
        video_path: 動画ファイルパス
        title: 動画タイトル
        description: 動画説明
        uploader: YouTubeアップローダー

    Returns:
        HTMLResponse: アップロード結果パーシャルHTML
    """
    from src.web.dependencies import get_config
    config = get_config()

    # Verify file exists
    video_file = Path(video_path)
    if not video_file.exists():
        return templates.TemplateResponse("partials/upload_result.html", {
            "request": request,
            "success": False,
            "error_message": f"動画ファイルが見つかりません: {video_path}",
        })

    # Check authentication
    if not uploader.is_authenticated():
        return templates.TemplateResponse("partials/upload_result.html", {
            "request": request,
            "success": False,
            "error_message": "YouTubeにログインしてください",
            "need_auth": True,
        })

    log_step(f"YouTubeアップロード開始: {title}", "📤")

    # Perform upload (this may take a while)
    result = uploader.upload(
        video_path=str(video_file),
        title=title,
        description=description,
        tags=["Shorts", "ニュース", "AI生成"],
        privacy_status=config.youtube_default_privacy,
    )

    if result.success:
        log_success(f"YouTubeアップロード完了: {result.video_url}")
        return templates.TemplateResponse("partials/upload_result.html", {
            "request": request,
            "success": True,
            "video_id": result.video_id,
            "video_url": result.video_url,
            "title": title,
        })
    else:
        log_error(f"YouTubeアップロード失敗: {result.error_message}")
        return templates.TemplateResponse("partials/upload_result.html", {
            "request": request,
            "success": False,
            "error_message": result.error_message,
        })


# ============================================================
# TikTok Upload Routes
# ============================================================

@router.get("/tiktok/status", response_class=HTMLResponse)
async def tiktok_auth_status(
    request: Request,
    uploader: TikTokUploader = Depends(get_tiktok_uploader)
):
    """TikTok認証状態を取得する。

    Args:
        request: FastAPIリクエスト
        uploader: TikTokアップローダー

    Returns:
        HTMLResponse: 認証状態パーシャルHTML
    """
    config = get_config()
    is_configured = config.is_tiktok_configured()
    is_authenticated = uploader.is_authenticated() if is_configured else False

    return templates.TemplateResponse("partials/tiktok_status.html", {
        "request": request,
        "is_configured": is_configured,
        "is_authenticated": is_authenticated,
    })


@router.post("/tiktok/auth", response_class=HTMLResponse)
async def tiktok_authenticate(
    request: Request,
    uploader: TikTokUploader = Depends(get_tiktok_uploader)
):
    """TikTok認証を実行する。

    Args:
        request: FastAPIリクエスト
        uploader: TikTokアップローダー

    Returns:
        HTMLResponse: 認証結果パーシャルHTML
    """
    config = get_config()

    if not config.is_tiktok_configured():
        return templates.TemplateResponse("partials/tiktok_status.html", {
            "request": request,
            "is_configured": False,
            "is_authenticated": False,
            "auth_message": "TikTokのAPIキーが設定されていません。.envファイルにTIKTOK_CLIENT_KEYとTIKTOK_CLIENT_SECRETを設定してください。",
        })

    try:
        uploader.authenticate()
        return templates.TemplateResponse("partials/tiktok_status.html", {
            "request": request,
            "is_configured": True,
            "is_authenticated": True,
            "auth_message": "TikTok認証に成功しました",
        })
    except Exception as e:
        log_error(f"TikTok認証エラー: {e}")
        return templates.TemplateResponse("partials/tiktok_status.html", {
            "request": request,
            "is_configured": True,
            "is_authenticated": False,
            "auth_message": str(e),
        })


@router.post("/tiktok/upload", response_class=HTMLResponse)
async def tiktok_upload(
    request: Request,
    background_tasks: BackgroundTasks,
    video_path: str = Form(...),
    title: str = Form(...),
    uploader: TikTokUploader = Depends(get_tiktok_uploader),
):
    """動画をTikTokにアップロードする。

    Args:
        request: FastAPIリクエスト
        background_tasks: バックグラウンドタスク
        video_path: 動画ファイルパス
        title: 動画タイトル（キャプション）
        uploader: TikTokアップローダー

    Returns:
        HTMLResponse: アップロード結果パーシャルHTML
    """
    config = get_config()

    # Verify TikTok is configured
    if not config.is_tiktok_configured():
        return templates.TemplateResponse("partials/tiktok_upload_result.html", {
            "request": request,
            "success": False,
            "error_message": "TikTokのAPIキーが設定されていません",
        })

    # Verify file exists
    video_file = Path(video_path)
    if not video_file.exists():
        return templates.TemplateResponse("partials/tiktok_upload_result.html", {
            "request": request,
            "success": False,
            "error_message": f"動画ファイルが見つかりません: {video_path}",
        })

    # Check authentication
    if not uploader.is_authenticated():
        return templates.TemplateResponse("partials/tiktok_upload_result.html", {
            "request": request,
            "success": False,
            "error_message": "TikTokにログインしてください",
            "need_auth": True,
        })

    log_step(f"TikTokアップロード開始: {title}", "📤")

    # Perform upload
    result = uploader.upload(
        video_path=str(video_file),
        title=title,
        privacy_level=config.tiktok_default_privacy,
    )

    if result.success:
        log_success(f"TikTokアップロード完了")
        return templates.TemplateResponse("partials/tiktok_upload_result.html", {
            "request": request,
            "success": True,
            "publish_id": result.publish_id,
            "video_url": result.video_url,
            "title": title,
        })
    else:
        log_error(f"TikTokアップロード失敗: {result.error_message}")
        return templates.TemplateResponse("partials/tiktok_upload_result.html", {
            "request": request,
            "success": False,
            "error_message": result.error_message,
        })
