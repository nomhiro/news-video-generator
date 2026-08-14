"""ログ出力。

守っている実害
--------------
uvicorn が起動時に `logging.config.dictConfig()` を呼び、既存のロガーを
無効化していたため、**Web 経由の実行ではこのモジュールの出力が1行も
出ていなかった**。Container Apps 上で生成の進行もエラーも追えず、
Blob に動画が出たか等の副作用で切り分ける羽目になった。
CLI は uvicorn を通らないので正常に見え、気付きにくい。

`test_logging_survives_uvicorns_dict_config` がそこを見張っている。
併せて、端末以外では絵文字を使わないこと（クラウドのログで化ける、
`ERROR:` で絞れる）も検査する。
"""

import importlib
import io
import logging

import pytest

import src.utils.logger as logger_module


def _reload_with_emoji(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> tuple[object, io.StringIO]:
    """`LOG_EMOJI` を設定してモジュールを読み直し、出力先を差し替える。

    絵文字を使うかは import 時に一度だけ決めるため（行ごとに判定すると
    同じ実行の中で表記が混ざって grep しにくい）、テストでは再 import する。

    `caplog` を使わない理由: 他のテストが uvicorn を起動して
    `logging.config.dictConfig` を実行すると伝播の設定が変わり、
    単体では通るのに全体実行では records が空になる（実際に踏んだ）。
    このロガー自身のハンドラを差し替えて、出力そのものを読む。
    """
    if value is None:
        monkeypatch.delenv("LOG_EMOJI", raising=False)
    else:
        monkeypatch.setenv("LOG_EMOJI", value)
    module = importlib.reload(logger_module)

    stream = io.StringIO()
    logger = module._get_logger()
    logger.handlers = [logging.StreamHandler(stream)]
    logger.setLevel(logging.INFO)
    return module, stream


@pytest.fixture(autouse=True)
def _restore_module() -> object:
    """テスト後にモジュールの状態を戻す。"""
    yield
    importlib.reload(logger_module)


def test_ascii_labels_when_not_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """端末でないときは絵文字を出さないこと。

    pytest の stdout はキャプチャされて TTY ではないので、
    このテスト自体が「コンテナと同じ条件」になる。
    """
    module, stream = _reload_with_emoji(monkeypatch, None)

    module.log_step("処理中")  # type: ignore[attr-defined]
    module.log_success("完了")  # type: ignore[attr-defined]
    module.log_error("失敗")  # type: ignore[attr-defined]
    module.log_warning("注意")  # type: ignore[attr-defined]

    lines = stream.getvalue().splitlines()
    assert lines == ["INFO: 処理中", "OK: 完了", "ERROR: 失敗", "WARN: 注意"]
    assert not any(_has_emoji(line) for line in lines)


def test_emoji_can_be_forced_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_EMOJI=true なら絵文字を使うこと（ローカルの見やすさ）。"""
    module, stream = _reload_with_emoji(monkeypatch, "true")

    module.log_success("完了")  # type: ignore[attr-defined]

    assert stream.getvalue().strip() == "✅ 完了"


def test_emoji_can_be_forced_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_EMOJI=false なら端末でも絵文字を使わないこと。"""
    module, stream = _reload_with_emoji(monkeypatch, "false")

    module.log_step("処理中", "🎬")  # type: ignore[attr-defined]

    assert stream.getvalue().strip() == "INFO: 処理中"


def test_the_message_itself_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """メッセージ本文（日本語）は落とさないこと。

    落とすのは絵文字のプレフィックスだけ。日本語の行は
    Container Apps でも取り込まれている。
    """
    module, stream = _reload_with_emoji(monkeypatch, None)

    module.log_step("ジョブ 12 を開始 (記事タイトル, short, ja)")  # type: ignore[attr-defined]

    assert "ジョブ 12 を開始 (記事タイトル, short, ja)" in stream.getvalue()


def test_logging_survives_uvicorns_dict_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn が logging を設定したあとでも出力が出ること。

    uvicorn の `dictConfig` は既存のロガーを無効化する
    （`logger.disabled = True`）。ここが戻ると、Web 経由の実行で
    アプリのログが丸ごと消える。
    """
    import logging.config

    import uvicorn.config

    module, stream = _reload_with_emoji(monkeypatch, "false")

    # 先に1行出しておく（ロガーが「既存」になる）
    module.log_step("設定前")  # type: ignore[attr-defined]
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    module.log_step("設定後")  # type: ignore[attr-defined]

    output = stream.getvalue()
    assert "設定前" in output
    assert "設定後" in output, "dictConfig 後に出力が消えている"


def _has_emoji(text: str) -> bool:
    """絵文字と記号のプレフィックスが混じっていないか。

    Args:
        text: 検査する文字列

    Returns:
        bool: 絵文字らしい文字を含むなら True
    """
    return any(ord(char) > 0x2000 and not _is_cjk(char) for char in text)


def _is_cjk(char: str) -> bool:
    """日本語（かな・漢字・全角記号）か。"""
    code = ord(char)
    return (
        0x3000 <= code <= 0x30FF  # 全角記号・かな
        or 0x4E00 <= code <= 0x9FFF  # 漢字
        or 0xFF00 <= code <= 0xFFEF  # 全角英数
    )
