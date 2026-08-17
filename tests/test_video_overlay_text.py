"""字幕オーバーレイのテキストファイルと行間の検証。

ここで見張っているのは「合成は成功するのに字幕が読めない」壊れ方。
ffprobe で分かる情報（尺・解像度・ストリーム）はすべて正しくなるので、
`tests/test_video_compose_slow.py` の既存の検査はどれも通ってしまう。
"""

from pathlib import Path

import pytest

from src.generators.video_composer import VideoComposer, VideoCompositionError


@pytest.fixture
def composer() -> VideoComposer:
    return VideoComposer()


def test_overlay_text_file_uses_lf(composer: VideoComposer, tmp_path: Path) -> None:
    """改行が LF で書かれること。

    テキストモードの既定は環境の改行に変換するため、Windows では CRLF になる。
    drawtext は CR を行の一部として扱わず、改行が2つあるものとして
    **空行を1行挟む**（実測: 2行の字幕の縦幅が 156px → 260px）。
    開発は Windows でコンテナは Linux なので、これを許すと
    手元とクラウドで字幕の見た目が変わる。
    """
    text = "あ" * (composer.TEXT_MAX_CHARS_PER_LINE + 3)

    path = composer._create_text_file(text, output_dir=tmp_path, index=0)

    raw = path.read_bytes()
    assert b"\n" in raw, "改行が書かれていない（折り返しが効いていない）"
    assert b"\r" not in raw


def test_wrap_still_works_without_a_loadable_font(
    composer: VideoComposer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フォントを読めなくても折り返しが例外で止まらないこと。

    フォントが無い場合、`compose` は字幕を諦めて動画自体は作る
    （`VideoCompositionError` を捕まえている）。折り返しが例外を投げると、
    その「字幕だけ諦める」経路まで壊れて動画が1本も出なくなる。
    """

    def fail() -> str:
        raise VideoCompositionError("フォントが無い")

    monkeypatch.setattr(composer, "_resolve_japanese_font_path", fail)

    wrapped = composer._wrap_text("あ" * 30)

    lines = wrapped.split("\n")
    assert len(lines) > 1
    assert "".join(lines) == "あ" * 30


def test_line_spacing_leaves_a_positive_line_advance(composer: VideoComposer) -> None:
    """行送りが字面の高さを下回らないこと。

    `line_spacing` は行送りへの**加算値**なので、フォントサイズに近い負の値を
    入れると2行が同じ位置に重なる（実測: fontsize=64 に対し -70 で完全に重なり、
    字幕がまったく読めない動画が出来ていた）。
    フォントサイズを変えたときに古い絶対値が取り残されるのを防ぐため、
    ここで両者の関係を固定する。
    """
    # 1080x1920 / fontsize=64 での既定の行送りは実測 88px（字面 68px）。
    # 加算値がこれを食い潰すと重なるので、下限を字面ぶんに置く。
    default_advance = composer.TEXT_FONT_SIZE * 1.375
    glyph_height = composer.TEXT_FONT_SIZE * 1.0625

    assert default_advance + composer.TEXT_LINE_SPACING >= glyph_height
