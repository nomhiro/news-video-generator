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
    """2秒の**音の出る** MP3 を作る。ffmpeg で生成するので外部素材が要らない。

    **無音（anullsrc）にしてはいけない。** 以前はそうしていて、そのために
    「音声トラックはあるが中身が無音」という壊れ方を検出できなかった
    （`mux_audio` が `-map` を持たず、Remotion の無音ステレオトラックが
    モノラルのナレーションより優先されていた。実測で生成物5本すべてが
    mean_volume -91.0 dB）。無音の入力では、正しい出力と壊れた出力が
    ビット単位で区別できない。

    ナレーションと同じ**モノラル 24kHz** で作る。ステレオにすると
    Remotion 側の無音トラックとチャンネル数で並ぶため、`-map` を消しても
    テストが通ってしまう（既定のストリーム選択はチャンネル数で決める）。
    """
    audio = tmp_path / "tone.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=24000:duration=2",
            "-ac",
            "1",
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


def _mean_volume_db(path: Path) -> float:
    """音声の平均音量（dBFS）を返す。デジタル無音なら -91.0 が返る。

    ffprobe では測れない（ストリームの有無しか分からない）ため
    `volumedetect` フィルタを通す。実際に**音が入っているか**を見るには
    デコードするしかない。
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise AssertionError(f"volumedetect の出力に mean_volume が無い: {result.stderr}")


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
            SceneVisual(layout=SceneLayout.STATEMENT, items=[], relation=""),
            SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation="切替"),
            SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"], relation="変換"),
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
    # **トラックの有無だけでは足りない。** Remotion が焼く無音ステレオトラックが
    # 採用されると、トラックも尺も解像度も正しいまま音だけが消える。
    # 440Hz のサイン波なら十分大きいので、無音（-91.0 dB）と明確に分かれる。
    assert _mean_volume_db(output) > -40.0
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
        scenes=[SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B"], relation="diff")],
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
