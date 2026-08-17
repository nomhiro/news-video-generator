"""レンダラの差し替えが `Pipeline` に配線されていることの検証。

守っている性質は2つ。

- `video_renderer` 設定でレンダラが選ばれること
  （`build_video_renderer` 単体のテストは `test_video_renderer.py`）。
- Remotion を選んだときは `gpt-image-2` を一度も呼ばないこと。
  クォータ（リージョン単位で上限4）の律速が消えるのがこの作業の
  副産物なので、呼ばれていないことを検査で固定する。
"""

from pathlib import Path
from typing import Any

from config import Config
from src.generators.video_renderer import FfmpegRenderer, RemotionRenderer
from src.pipeline import Pipeline

DUMMY_ENV: dict[str, object] = {
    "azure_openai_endpoint": "https://example.openai.azure.com",
    "azure_openai_api_key": "dummy",
    "azure_openai_deployment": "gpt-5.1",
    "azure_openai_image_deployment": "gpt-image-2",
    "azure_speech_api_key": "dummy",
}


def _config(tmp_path: Path, **overrides: object) -> Config:
    """外部サービスを呼ばない範囲で Config を組み立てる。

    `tests/test_pipeline_publish.py` の組み立て方に合わせている。
    """
    return Config(_env_file=None, output_dir=tmp_path / "output", **DUMMY_ENV, **overrides)  # type: ignore[arg-type,call-arg]


def test_default_config_selects_ffmpeg(tmp_path: Path) -> None:
    """マージしても見た目が変わらないこと。"""
    pipeline = Pipeline(_config(tmp_path))
    assert isinstance(pipeline.video_renderer, FfmpegRenderer)


def test_remotion_config_selects_remotion_renderer(tmp_path: Path) -> None:
    pipeline = Pipeline(_config(tmp_path, video_renderer="remotion"))
    assert isinstance(pipeline.video_renderer, RemotionRenderer)


class _FakeScript:
    """`Pipeline.run` が読む属性だけを持つ台本のフェイク。"""

    def __init__(self) -> None:
        self.image_prompts = ["prompt"]
        self.scenes: list[object] = [object()]
        self.text_overlays = ["headline"]
        self.segment_narrations = ["narration"]
        self.full_narration = "narration"

    def to_json_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")


class _FakeScriptGenerator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, *args: object, **kwargs: object) -> _FakeScript:
        self.calls.append(kwargs)
        return _FakeScript()


class _FakeVoiceGenerator:
    def generate_with_timings(
        self, segments: list[str], language: str, audio_path: Path, **kwargs: object
    ) -> tuple[Path, list[float]]:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        return audio_path, [0.0, 1.0]


class _FakeVideoRenderer:
    """`needs_images = False` の状態を模す（実際の Remotion は呼ばない）。"""

    needs_images = False
    draws_scene_text = True

    def render(self, *, output_path: Path, **kwargs: object) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"video")
        return output_path


def _exploding_generate_batch(*args: object, **kwargs: object) -> list[Path]:
    """呼ばれたら即座に失敗する `generate_batch` の代わり。

    Remotion を選んでいるのに画像生成が呼ばれたら、このテストが
    そこで失敗する。`needs_images` の値を見るだけでは、実際に
    `Pipeline.run` がその値を honour しているかは分からない。
    """
    raise AssertionError("画像生成は呼ばれないはずです（Remotion はレンダラが図解を描く）")


def test_pipeline_skips_image_generation_for_remotion(tmp_path: Path) -> None:
    """Remotion では gpt-image-2 を1回も呼ばないこと。

    クォータの律速が消えるのがこの作業の副産物なので、
    呼ばれていないことを検査で固定する。
    """
    pipeline = Pipeline(_config(tmp_path, video_renderer="remotion"))

    # 各ジェネレータをフェイクに差し替える。image_generator.generate_batch
    # だけは「呼ばれたら失敗する」形にして、Pipeline が本当に呼ばないことを
    # 検査する（needs_images を見るだけでは配線を確かめられない）。
    pipeline.script_generator = _FakeScriptGenerator()  # type: ignore[assignment]
    pipeline.voice_generator = _FakeVoiceGenerator()  # type: ignore[assignment]
    pipeline.video_renderer = _FakeVideoRenderer()
    pipeline.image_generator.generate_batch = _exploding_generate_batch  # type: ignore[method-assign]

    result: dict[str, Any] = pipeline.run("トピック", languages=["ja"])

    assert result["status"] == "success"
    assert result["images"] == []


class _FakeNonDrawingRenderer(_FakeVideoRenderer):
    """ffmpeg レンダラと同じ「ラベルを描かない」状態を模す。"""

    needs_images = False
    draws_scene_text = False


def _run_with_fakes(pipeline: Pipeline, renderer: _FakeVideoRenderer) -> _FakeScriptGenerator:
    """外部サービスを一切呼ばずに `Pipeline.run` を1回通す。"""
    script_generator = _FakeScriptGenerator()
    pipeline.script_generator = script_generator  # type: ignore[assignment]
    pipeline.voice_generator = _FakeVoiceGenerator()  # type: ignore[assignment]
    pipeline.video_renderer = renderer
    pipeline.run("トピック", languages=["ja"])
    return script_generator


def test_grounding_enforcement_follows_the_renderer(tmp_path: Path) -> None:
    """数値の根拠の強制を、ラベルを描くレンダラのときだけ有効にすること。

    描かないレンダラ（既定の ffmpeg）で例外にすると、画面に出ない数値のために
    ジョブが3回の試行を使い切って FAILED になる。「マージしても毎朝の自動生成の
    振る舞いは変わらない」という前提が、出力ではなく失敗経路の側で崩れる。
    """
    drawing = _run_with_fakes(Pipeline(_config(tmp_path)), _FakeVideoRenderer())
    assert drawing.calls[0]["enforce_scene_grounding"] is True

    not_drawing = _run_with_fakes(Pipeline(_config(tmp_path)), _FakeNonDrawingRenderer())
    assert not_drawing.calls[0]["enforce_scene_grounding"] is False
