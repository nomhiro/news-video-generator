"""実際に ffmpeg を起動して動画を合成する（`-m slow`）。

なぜフェイクでは足りないか
--------------------------
合成は**2段構え**（① 画像から無音の映像 → ② `-c copy` で音声を混ぜる）に
なっている。1回の ffmpeg で音声ごと作っていた頃、長尺でマクサーが
映像パケットを溜め込み、OOM killer に殺されていた
（実測: ピーク RSS 4,077MB → 4Gi 制限で終了コード -9。
2段構えでは 617MB で完走）。

段を分けたことで、次の壊し方が新しく生まれた。

- 第2段を忘れる/失敗する → **音声トラックの無い動画**が出来る
- 中間ファイル（`*_silent.mp4`）を消し忘れる → 生成物が2倍になり、
  Blob にも余計なものが上がる
- 第1段に `-t` を渡し忘れる → concat の最後の画像が尺を持たないため
  **1フレームで終わる動画**になる

いずれもコマンド文字列の検査では見つからない。実物を作って確かめる。

実行:
    uv run pytest -m slow
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.generators.video_composer import VideoComposer

pytestmark = pytest.mark.slow

# 実尺は短くする（検査したいのは構造で、長さではない）。
AUDIO_SECONDS = 3.0
IMAGE_COUNT = 3


@pytest.fixture(scope="module")
def ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg / ffprobe が PATH にありません")


@pytest.fixture
def inputs(tmp_path: Path, ffmpeg_available: None) -> tuple[Path, list[Path]]:
    """ダミーの音声と画像を作る。"""
    audio = tmp_path / "audio.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={AUDIO_SECONDS}",
            str(audio),
        ],
        check=True,
    )

    images = []
    for i in range(IMAGE_COUNT):
        path = tmp_path / f"image_{i + 1:03d}.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:size=1152x2048:duration=1:rate=1",
                "-frames:v",
                "1",
                str(path),
            ],
            check=True,
        )
        images.append(path)
    return audio, images


def _probe(path: Path) -> dict:
    """ffprobe の結果を返す。"""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(json.loads(result.stdout))


def test_output_has_both_streams(inputs: tuple[Path, list[Path]], tmp_path: Path) -> None:
    """映像と音声の両方が入っていること。

    2段構えの第2段（多重化）が抜けると、音声トラックの無い動画が出る。
    """
    audio, images = inputs
    output = tmp_path / "out.mp4"

    VideoComposer().compose(
        audio,
        images,
        output,
        text_overlays=["1つ目", "2つ目", "3つ目"],
        segment_timings=[0.0, 1.0, 2.0, AUDIO_SECONDS],
        video_format="short",
    )

    probed = _probe(output)
    kinds = sorted(stream["codec_type"] for stream in probed["streams"])
    assert kinds == ["audio", "video"]


def test_output_length_follows_the_audio(inputs: tuple[Path, list[Path]], tmp_path: Path) -> None:
    """実尺が音声の長さに一致すること。

    第1段に `-t` を渡し忘れると、concat の最後の画像が尺を持たないため
    1フレームで終わる動画になる。
    """
    audio, images = inputs
    output = tmp_path / "out.mp4"

    VideoComposer().compose(audio, images, output, segment_timings=[0.0, 1.0, 2.0, AUDIO_SECONDS])

    duration = float(_probe(output)["format"]["duration"])
    assert duration == pytest.approx(AUDIO_SECONDS, abs=0.5)


def test_output_resolution_matches_the_format(
    inputs: tuple[Path, list[Path]], tmp_path: Path
) -> None:
    """形式どおりの解像度で出ること（縦 1080x1920）。"""
    audio, images = inputs
    output = tmp_path / "out.mp4"

    VideoComposer().compose(audio, images, output, video_format="short")

    video = next(s for s in _probe(output)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)


def test_intermediate_file_is_removed(inputs: tuple[Path, list[Path]], tmp_path: Path) -> None:
    """中間の無音動画を残さないこと。

    残すと生成物が2倍になり、Blob にも余計なものが上がる。
    """
    audio, images = inputs
    output = tmp_path / "out.mp4"

    VideoComposer().compose(audio, images, output)

    assert output.exists()
    assert not (tmp_path / "out_silent.mp4").exists()
    assert sorted(p.name for p in tmp_path.glob("*.mp4")) == ["out.mp4"]


def test_wrapped_overlay_lines_do_not_overlap(
    inputs: tuple[Path, list[Path]], tmp_path: Path
) -> None:
    """折り返した字幕の行が重ならないこと。

    `line_spacing` は drawtext の**行送りに加算される**値なので、
    フォントサイズに近い負の値を入れると2行が同じ位置に重なって描かれ、
    字幕が読めなくなる（実測: `line_spacing=-70` / `fontsize=64` で
    2行が完全に重なった）。合成は成功し、解像度も尺も音声も正しいので、
    既存の検査はどれも気付かない。ffprobe でも分からないため、
    実際のフレームの画素を見る。
    """
    audio, images = inputs
    output = tmp_path / "out.mp4"
    composer = VideoComposer()

    # 折り返しが必ず2行になる長さにする。
    text = "あ" * (composer.TEXT_MAX_CHARS_PER_LINE + 4)
    composer.compose(
        audio,
        images,
        output,
        text_overlays=[text, text, text],
        segment_timings=[0.0, 1.0, 2.0, AUDIO_SECONDS],
        video_format="short",
    )

    frame = tmp_path / "frame.png"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.5",
            "-i",
            str(output),
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )

    # 文字色（黄）の画素が縦にどれだけ広がっているかを測る。
    # 2行ぶんなら行送り1つぶん以上の高さになる。重なると1行ぶんに縮む。
    with Image.open(frame) as img:
        rgb = img.convert("RGB")
        rows = [
            y
            for y in range(rgb.height)
            for x in range(0, rgb.width, 4)
            if _is_text_color(rgb.getpixel((x, y)))
        ]
    assert rows, "字幕が1画素も描かれていない"
    band_height = max(rows) - min(rows) + 1
    assert band_height >= composer.TEXT_FONT_SIZE * 1.5, (
        f"2行の字幕が {band_height}px に収まっている。"
        f"line_spacing={composer.TEXT_LINE_SPACING} で行が重なっている疑いがある"
    )


def _is_text_color(pixel: object) -> bool:
    """字幕の文字色（黄）とみなせる画素か。

    アンチエイリアスと縁取り（黒）が混ざるので厳密一致では拾えない。

    `Image.getpixel` の戻り値型は画像のモードで変わるため object で受ける。
    """
    if not isinstance(pixel, tuple):
        return False
    r, g, b = pixel[:3]
    return bool(r > 180 and g > 180 and b < 100)


def test_overlay_text_files_are_cleaned_up(inputs: tuple[Path, list[Path]], tmp_path: Path) -> None:
    """字幕用の一時テキストを残さないこと。"""
    audio, images = inputs
    output = tmp_path / "out.mp4"

    VideoComposer().compose(
        audio,
        images,
        output,
        text_overlays=["1つ目", "2つ目", "3つ目"],
        segment_timings=[0.0, 1.0, 2.0, AUDIO_SECONDS],
    )

    assert list(tmp_path.glob("_overlay_text_*.txt")) == []
