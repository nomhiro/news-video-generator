#!/usr/bin/env python3
"""News Video Generator CLI Entry Point.

ニューストピックからYouTube Shorts / TikTok向けのショート動画を自動生成するCLIツール。

Usage:
    python main.py "ニューストピック"
    python main.py "ニューストピック" -l ja
    python main.py "ニューストピック" -l ja en
    python main.py "ニューストピック" -f tiktok       # TikTok用60-90秒動画
    python main.py "ニューストピック" -f long         # 5分解説動画
    python main.py "ニューストピック" -o ./my_videos
    python main.py "ニューストピック" -v
"""

import argparse
import sys
from pathlib import Path

from config import Config
from src.pipeline import Pipeline
from src.utils.logger import setup_logger


def main() -> int:
    """メイン関数。

    Returns:
        int: 終了コード（0: 成功, 1: 失敗）
    """
    parser = argparse.ArgumentParser(
        description="ニュース動画自動生成システム - YouTube Shorts / TikTok向け",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py "Google Veo 3.1発表 - AI動画生成の新時代"
  python main.py "Latest AI News" -l en
  python main.py "ニュース内容" -l ja en -o ./my_videos
        """,
    )
    parser.add_argument("topic", help="ニューストピック（テキスト）")
    parser.add_argument(
        "-l",
        "--languages",
        nargs="+",
        default=["ja", "en"],
        choices=["ja", "en"],
        help="生成する言語 (default: ja en)",
    )
    parser.add_argument(
        "-o", "--output", default="./output", help="出力ディレクトリ (default: ./output)"
    )
    parser.add_argument(
        "-f",
        "--format",
        default="short",
        choices=["short", "tiktok", "long"],
        help="動画形式: short(35秒), tiktok(60-90秒), long(5分) (default: short)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細なログを出力")

    args = parser.parse_args()

    # Setup logger
    setup_logger("news_video_generator", verbose=args.verbose)

    # Load config
    config = Config.from_env()
    config.output_dir = Path(args.output)

    # Validate config
    errors = config.validate()
    if errors:
        print("❌ 設定エラー:")
        for error in errors:
            print(f"   {error}")
        print("")
        print("💡 ヒント: .envファイルにAPIキーを設定してください")
        print("   .env.exampleを参考にしてください")
        return 1

    # Run pipeline
    pipeline = Pipeline(config)
    try:
        format_names = {
            "short": "ショート(35秒)",
            "tiktok": "TikTok(60-90秒)",
            "long": "ロング(5分)",
        }
        print("🚀 動画生成を開始します")
        print(f"   トピック: {args.topic}")
        print(f"   言語: {', '.join(args.languages)}")
        print(f"   形式: {format_names.get(args.format, args.format)}")
        print(f"   出力先: {args.output}")
        print("")

        result = pipeline.run(args.topic, args.languages, video_format=args.format)

        print("")
        print("🎉 完了!")
        for lang, path in result["videos"].items():
            lang_name = "日本語" if lang == "ja" else "英語"
            print(f"   {lang_name}: {path}")

        return 0

    except KeyboardInterrupt:
        print("")
        print("⚠️ 中断されました")
        return 1

    except Exception as e:
        print("")
        print(f"❌ エラー: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
