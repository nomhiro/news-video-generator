"""Logging utilities with emoji prefixes."""

import logging
import sys


def setup_logger(name: str, verbose: bool = False) -> logging.Logger:
    """ロガーをセットアップする。

    Args:
        name: ロガー名
        verbose: 詳細ログを出力するかどうか

    Returns:
        logging.Logger: 設定済みのロガー
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


# Default logger instance
_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    """デフォルトロガーを取得する。"""
    global _logger
    if _logger is None:
        _logger = setup_logger("news_video_generator")
    return _logger


def log_step(message: str, emoji: str = "📌") -> None:
    """ステップをログ出力する。

    Args:
        message: ログメッセージ
        emoji: プレフィックス絵文字
    """
    _get_logger().info(f"{emoji} {message}")


def log_success(message: str) -> None:
    """成功をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().info(f"✅ {message}")


def log_error(message: str) -> None:
    """エラーをログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().error(f"❌ {message}")


def log_warning(message: str) -> None:
    """警告をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().warning(f"⚠️ {message}")
