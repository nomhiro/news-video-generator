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

**「端末である」だけでは足りない。** Windows の日本語コンソールは
TTY だがコードページが cp932 で、絵文字を書き込むと化けるのではなく
`UnicodeEncodeError` で**落ちる**。実際に CLI が起動直後に
`'cp932' codec can't encode character '\\U0001f680'` で死んだ。
出力先が実際にエンコードできるかも確かめる（`_can_encode`）。
"""

import logging
import os
import sys

# エンコード可否の判定に使う文字。
#
# コードベースで `log_step` / `prefix` に渡している絵文字を**全部**並べる。
# 1文字だけで代表させると「その文字は書けるが別の絵文字は書けない」
# エンコーディングで落ちる余地が残る。全部書ければ絵文字を使う。
#
# 絵文字を新しく使うときはここにも足す。
# `tests/test_logger.py::test_every_emoji_in_use_is_covered_by_the_probe` が
# src/ と main.py を走査して、漏れていれば失敗する。
_EMOJI_PROBE = "📌✅❌⚠️🚀🎉💡🎬🎨🎙️📝♻️🎞️📤📥📰🔉🔐🔧🗄️🤖⚙️🎯⏱️⏭️🗓️🔍☁️🧵🐦"


def _can_encode(text: str) -> bool:
    """出力先が文字を書き込めるか。

    書けない文字を print すると `UnicodeEncodeError` で**落ちる**
    （化けるだけでは済まない）。Windows の日本語コンソールは TTY だが
    cp932 なので、絵文字がこれに当たる。

    Args:
        text: 検査する文字列

    Returns:
        bool: 書き込めるなら True
    """
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        # 差し替えられた stdout（io.StringIO 等）はエンコードを伴わないので
        # 制約が無い。ここで False にすると LOG_EMOJI=true が効かなくなる。
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# 絵文字を使うかどうかの既定。
#
# 端末に出しているならローカル開発と見なして絵文字を使う。
# パイプやコンテナのログ（TTY ではない）では ASCII に落とす。
# `LOG_EMOJI=true` / `false` で明示的に決められる。
def _emoji_enabled() -> bool:
    """絵文字プレフィックスを使うか。

    `LOG_EMOJI` の明示指定でも、書き込めない出力先では使わない。
    「絵文字が出ない」より「実行が落ちる」方が実害が大きいので、
    エンコード可否は上書きより優先する。

    Returns:
        bool: 使うなら True
    """
    if not _can_encode(_EMOJI_PROBE):
        return False
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


def prefix(kind: str, emoji: str) -> str:
    """行の先頭に付けるものを決める。

    公開しているのは `main.py` が `print` で同じ判断を使うため。
    CLI が絵文字を直書きしていて、cp932 の端末で起動直後に落ちた。

    Args:
        kind: `_ASCII_LABELS` のキー（"step" / "success" / "error" / "warning"）
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
    _get_logger().info(f"{prefix('step', emoji)} {message}")


def log_success(message: str) -> None:
    """成功をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().info(f"{prefix('success', '✅')} {message}")


def log_error(message: str) -> None:
    """エラーをログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().error(f"{prefix('error', '❌')} {message}")


def log_warning(message: str) -> None:
    """警告をログ出力する。

    Args:
        message: ログメッセージ
    """
    _get_logger().warning(f"{prefix('warning', '⚠️')} {message}")
