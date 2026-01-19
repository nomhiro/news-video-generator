#!/usr/bin/env python3
"""News Video Generator Web Application.

HTMX + Tailwind CSSを使用したニュース取得・動画生成Webインターフェース。

Usage:
    python web_app.py
    python web_app.py --port 8080
    python web_app.py --host 0.0.0.0 --port 8080
"""

import argparse
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import Config
from src.web.routes import router
from src.web.dependencies import setup_dependencies


def create_app() -> FastAPI:
    """FastAPIアプリケーションを作成・設定する。

    Returns:
        FastAPI: 設定済みアプリケーション
    """
    app = FastAPI(
        title="News Video Generator",
        description="HTMX + Tailwind CSSを使用したニュース取得・動画生成システム",
        version="1.0.0",
    )

    # Mount static files
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Include routes
    app.include_router(router)

    # Setup dependencies
    config = Config.from_env()
    setup_dependencies(app, config)

    return app


def main():
    """メインエントリーポイント。"""
    import sys
    import io

    # Fix Windows console encoding for emoji
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(
        description="News Video Generator Web Server"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="サーバーホストアドレス (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="サーバーポート番号 (default: 8000)"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="開発用自動リロード"
    )

    args = parser.parse_args()

    print(f"News Video Generator Web Server")
    print(f"   URL: http://{args.host}:{args.port}")
    print(f"   Press Ctrl+C to stop")
    print("")

    uvicorn.run(
        "web_app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
