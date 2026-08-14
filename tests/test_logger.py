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

**cp932 の端末では絵文字で落ちる**
----------------------------------
Windows の日本語コンソールは TTY だが cp932 で、絵文字を print すると
化けるのではなく `UnicodeEncodeError` になる。CLI が絵文字を直書きして
いたため、起動直後に
`'cp932' codec can't encode character '\\U0001f680'` で死んだ。

見張っているのは3点。書き込めない出力先では絵文字を使わないこと
（`LOG_EMOJI=true` より優先すること）、CLI が `prefix()` を通さず
絵文字を print していないこと、そして判定用のプローブが
実際に使っている絵文字を網羅していること
（走査範囲を絞ると見逃す。最初に書いたときは18種類漏れていた）。
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


class _Cp932Stdout(io.StringIO):
    """cp932 の端末を模した stdout。

    Windows の日本語コンソールは TTY なのに絵文字をエンコードできない。
    `isatty()` が True かつ `encoding` が cp932 という組み合わせが再現点。
    """

    encoding = "cp932"

    def isatty(self) -> bool:
        return True


def test_no_emoji_when_stdout_cannot_encode_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """書き込めない出力先では絵文字を使わないこと。

    cp932 の端末に絵文字を print すると、化けるのではなく
    `UnicodeEncodeError` で**落ちる**。実際に CLI が起動直後に
    `'cp932' codec can't encode character '\\U0001f680'` で死んだ。
    """
    monkeypatch.setattr("sys.stdout", _Cp932Stdout())
    monkeypatch.delenv("LOG_EMOJI", raising=False)
    module = importlib.reload(logger_module)

    assert module.prefix("error", "❌") == "ERROR:"


def test_encodability_overrides_log_emoji_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_EMOJI=true でも書き込めなければ絵文字を使わないこと。

    「絵文字が出ない」より「実行が落ちる」方が実害が大きい。
    """
    monkeypatch.setattr("sys.stdout", _Cp932Stdout())
    monkeypatch.setenv("LOG_EMOJI", "true")
    module = importlib.reload(logger_module)

    assert module.prefix("success", "✅") == "OK:"


def test_every_emoji_in_use_is_covered_by_the_probe() -> None:
    """実際に使う絵文字が判定用の文字列に入っていること。

    プローブに無い絵文字を後から足すと、その文字だけ
    エンコードできない環境で落ちる余地が残る。走査範囲を
    `logger.py` と `main.py` に絞ると見逃す（実際に10種類漏れていた）ので、
    `src/` 配下すべてを見る。
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    sources = [repo_root / "main.py", *(repo_root / "src").rglob("*.py")]

    # log_step()/prefix() の第2引数、および log_step の既定値から拾う
    patterns = [
        r"log_step\(\s*(?:[^,]|\([^)]*\))+,\s*[\"']([^\"']+)[\"']",
        r"prefix\([^,]+,\s*[\"']([^\"']+)[\"']",
        r"emoji: str = [\"']([^\"']+)[\"']",
    ]
    used: set[str] = set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            used.update(re.findall(pattern, text))
    # ASCII だけの引数（emoji="" など）は対象外
    used = {e for e in used if _has_emoji(e)}

    assert used, "絵文字リテラルを1つも拾えていない（正規表現が古い）"
    missing = {e for e in used if e not in logger_module._EMOJI_PROBE}
    assert not missing, f"_EMOJI_PROBE に無い絵文字: {missing}"


def test_cli_does_not_print_bare_emoji() -> None:
    """CLI が絵文字を直書きしないこと。

    直書きすると cp932 の端末で `UnicodeEncodeError` で落ちる。
    `prefix()` を通せば ASCII のラベルに落ちる。
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    # docstring は説明のために絵文字を含むので、コード部分だけを見る
    body = source.split('"""', 2)[2]
    offenders = [
        line.strip()
        for line in body.splitlines()
        if "print(" in line and _has_emoji(line) and "prefix(" not in line
    ]
    assert not offenders, f"prefix() を通さず絵文字を print している: {offenders}"


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
