"""ログ出力。

**Web 経由ではログが1行も出ていなかった**
------------------------------------------
uvicorn は起動時に `logging.config.dictConfig()` を実行する。これが
**その時点で存在していたロガーを無効化する**（`logger.disabled = True`）。
このモジュールのロガーはインポート時ではなく初回利用時に作られるが、
それでもタイミングによって無効化の対象になり、以降
`log_step()` などの出力が一切消える。

実害: Container Apps 上で生成が進んでいるのかエラーなのかログから
判断できず、Blob に動画が出たか・共有の JSON が更新されたかという
副作用で切り分ける羽目になった。CLI（`main.py`）は uvicorn を通らないので
正常に見えており、Web だけで起きるため気付きにくい。

対策は `_get_logger()` で毎回 `disabled` を戻すこと。ロガーを渡す側で
面倒を見る。あとから誰が dictConfig を呼んでも効く。

絵文字は端末のときだけ
----------------------
プレフィックスは、出力先が端末なら絵文字、それ以外は ASCII のラベル
（`OK:` / `ERROR:`）にする。クラウドのログでは絵文字が化ける環境があり、
`Log_s startswith "ERROR:"` のように絞れる方が実用的。
`LOG_EMOJI=true/false` で明示的に決められる。
"""

import logging
import os
import sys


# 絵文字を使うかどうかの既定。
#
# 端末に出しているならローカル開発と見なして絵文字を使う。
# パイプやコンテナのログ（TTY ではない）では ASCII に落とす。
# `LOG_EMOJI=true` / `false` で明示的に決められる。
def _emoji_enabled() -> bool:
    """絵文字プレフィックスを使うか。

    Returns:
        bool: 使うなら True
    """
    override = os.getenv("LOG_EMOJI")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        # 差し替えられた stdout（テストのキャプチャなど）
        return False


# 起動時に一度だけ決める。行ごとに判定すると、同じ実行の中で
# 表記が混ざって grep しにくくなる。
_USE_EMOJI = _emoji_enabled()

# 絵文字を使わないときのラベル。
_ASCII_LABELS = {
    "step": "INFO:",
    "success": "OK:",
    "error": "ERROR:",
    "warning": "WARN:",
}


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
    """デフォルトロガーを取得する。

    毎回 `disabled` を戻す理由: uvicorn が起動時に
    `logging.config.dictConfig()` を呼び、既存のロガーを無効化するため。
    ここで戻さないと、Web 経由の実行でこのモジュールの出力が全て消える
    （実際に消えていた）。1行の代入なので、毎回やっても負荷は無い。

    Returns:
        logging.Logger: 有効化済みのロガー
    """
    global _logger
    if _logger is None:
        _logger = setup_logger("news_video_generator")
    if _logger.disabled:
        _logger.disabled = False
    return _logger


def _prefix(kind: str, emoji: str) -> str:
    """行の先頭に付けるものを決める。

    Args:
        kind: `_ASCII_LABELS` のキー
        emoji: 端末向けの絵文字

    Returns:
        str: プレフィックス
    """
    return emoji if _USE_EMOJI else _ASCII_LABELS[kind]


def log_step(message: str, emoji: str = "📌") -> None:
    """ステップをログ出力する。

    Args:
        message: ログメッセージ
        emoji: プレフィックス絵文字（端末以外では `INFO:` になる）
    """
    _get_logger().info(f"{_prefix('step', emoji)} {message}")


def log_success(message: str) -> None:
    """成功をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().info(f"{_prefix('success', '✅')} {message}")


def log_error(message: str) -> None:
    """エラーをログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().error(f"{_prefix('error', '❌')} {message}")


def log_warning(message: str) -> None:
    """警告をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().warning(f"{_prefix('warning', '⚠️')} {message}")
