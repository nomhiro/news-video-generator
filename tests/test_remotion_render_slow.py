"""Remotion を実際に動かして、経路全体が通ることを確認する。

**2秒（60フレーム）のコンポジションで測る。**
.githooks/pre-push は `-m "not live"` なので slow を含む。実運用と同じ
1050フレームを焼くと push が30秒から4分になり、--no-verify される道を
作ってしまう。2秒でも通る経路は同じ（Node が呼ばれる / Chrome が動く /
mp4 ができる / 音声が多重化される / 中間ファイルが消える）。
フル尺の実測は移行時の手動確認で行う。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from src.generators.remotion_renderer import RemotionRenderer
from src.models.scene import SceneLayout, SceneVisual

pytestmark = pytest.mark.slow

REMOTION_DIR = Path(__file__).resolve().parents[1] / "remotion"


@pytest.fixture
def toolchain_available() -> None:
    """Node / ffmpeg / node_modules が揃っていること。

    揃っていなければ skip する。**.githooks/pre-push が node と ffmpeg の
    存在を先に検査している**ので、push 経路では skip されない。
    """
    if shutil.which("node") is None:
        pytest.skip("node が PATH にない")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg / ffprobe が PATH にない")
    if not (REMOTION_DIR / "node_modules").is_dir():
        pytest.skip("remotion/node_modules が無い（cd remotion && npm install）")


@pytest.fixture
def two_second_audio(tmp_path: Path) -> Path:
    """2秒の無音の MP3 を作る。ffmpeg で生成するので外部素材が要らない。"""
    audio = tmp_path / "silence.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "2",
            str(audio),
        ],
        capture_output=True,
        check=True,
    )
    return audio


def _probe(path: Path, stream: str) -> str:
    """ffprobe で指定した種類のストリームの codec_type を返す（無ければ空）。

    末尾のカンマを削る。Remotion が焼く h264 ストリームには side_data
    （実測: 空の side_data_list）が付き、`csv=p=0` がそれを空フィールドとして
    出力してしまう（`"video,"` のように）。ダミー画像から作った動画では
    出ない、実物でしか踏めない類の違い。codec_type 自体は正しく1つだけ
    入っているので、末尾のカンマは無視してよい。
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().rstrip(",")


def test_render_produces_a_playable_video(
    toolchain_available: None, two_second_audio: Path, tmp_path: Path
) -> None:
    """Node の起動から音声の多重化・中間ファイルの後始末まで、経路全体が通ること。

    ここが通らないと（Remotion のレンダリング失敗、多重化の抜け、
    後始末忘れ）本番の35秒レンダリングも同じ壊れ方をする。
    """
    output = tmp_path / "out.mp4"
    RemotionRenderer().render(
        audio_path=two_second_audio,
        output_path=output,
        image_paths=[],
        scenes=[
            SceneVisual(layout=SceneLayout.STATEMENT, items=[]),
            SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"]),
            SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"]),
        ],
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1です。", "字幕2です。", "字幕3です。"],
        segment_timings=[0.0, 0.7, 1.4, 2.0],
        language="ja",
        video_format="short",
    )

    assert output.exists()
    # 音声トラックがあること。無ければ多重化が抜けている
    assert _probe(output, "a:0") == "audio"
    assert _probe(output, "v:0") == "video"
    # 中間ファイルを残さないこと
    assert list(tmp_path.glob("*_silent.mp4")) == []
    assert list(tmp_path.glob("*_props.json")) == []


def test_render_uses_the_format_resolution(
    toolchain_available: None, two_second_audio: Path, tmp_path: Path
) -> None:
    """解像度は formats.py が決める。short は 1080x1920。

    Remotion の props（width/height）が spec からずれていないかを見る。
    ずれると画像生成レンダラと出力仕様が食い違う。
    """
    output = tmp_path / "out.mp4"
    RemotionRenderer().render(
        audio_path=two_second_audio,
        output_path=output,
        image_paths=[],
        scenes=[SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B"])],
        text_overlays=["見出し"],
        segment_narrations=["字幕です。"],
        segment_timings=[0.0, 2.0],
        language="ja",
        video_format="short",
    )
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # 末尾のカンマは _probe と同じ理由（side_data）で削る。
    assert result.stdout.strip().rstrip(",") == "1080,1920"
