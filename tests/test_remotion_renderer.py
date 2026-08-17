"""Remotion レンダラの Python 側。

実レンダリングは tests/test_remotion_render_slow.py が担当する。
ここではコマンドの組み立てとフレーム換算だけを見る（速い）。
"""

import json
from pathlib import Path

import pytest

from src.generators.remotion_renderer import (
    RemotionRenderer,
    RemotionRenderError,
    resolve_frame_spans,
)
from src.models.scene import SceneLayout, SceneVisual
from src.utils.line_break import ZWSP


def test_spans_cover_the_whole_audio_without_gaps() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans == [(0, 30), (30, 30), (60, 30)]


def test_spans_fall_back_to_even_split_without_timings() -> None:
    """bookmark が取れなかった場合。均等割りにする。"""
    spans = resolve_frame_spans([], 3.0, 30, 3)
    assert [s[1] for s in spans] == [30, 30, 30]
    assert spans[0][0] == 0


def test_every_span_is_at_least_one_frame() -> None:
    """長さ0のシーンを作らせない。

    各開始秒を独立に丸めると、近接したタイミングで長さ0や負のシーンが
    できる。Remotion は例外を出さず、シーンが飛んだ動画を黙って作る
    （ffmpeg が無言で壊れた動画を作るのと同じ壊れ方）。
    """
    spans = resolve_frame_spans([0.0, 0.001, 0.002, 1.0], 1.0, 30, 3)
    assert all(duration >= 1 for _, duration in spans)


def test_spans_are_contiguous_and_monotonic() -> None:
    """隙間も重なりも作らない。"""
    spans = resolve_frame_spans([0.0, 0.4, 0.41, 2.0], 2.0, 30, 3)
    for i in range(len(spans) - 1):
        assert spans[i][0] + spans[i][1] == spans[i + 1][0]


def test_spans_survive_non_monotonic_timings() -> None:
    """タイミングが逆行していても増加を強制する。"""
    spans = resolve_frame_spans([0.0, 1.5, 0.5, 3.0], 3.0, 30, 3)
    starts = [start for start, _ in spans]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_spans_end_exactly_at_the_audio_end() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans[-1][0] + spans[-1][1] == 90


def _scenes() -> list[SceneVisual]:
    return [
        SceneVisual(layout=SceneLayout.STATEMENT, items=[]),
        SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"]),
        SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"]),
    ]


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Remotion と ffmpeg の呼び出しを捕まえ、props を読めるようにする。"""
    calls: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        # --props=<path> の中身を読んでおく（呼び出し後に消えるため）
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--props="):
                calls["props"] = json.loads(Path(arg.split("=", 1)[1]).read_text("utf-8"))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    def fake_mux(silent, audio, output, **kwargs):
        calls["mux"] = (silent, audio, output)
        output.write_bytes(b"muxed")

    monkeypatch.setattr("src.generators.remotion_renderer.subprocess.run", fake_run)
    monkeypatch.setattr("src.generators.remotion_renderer.mux_audio", fake_mux)
    monkeypatch.setattr(
        "src.generators.remotion_renderer.RemotionRenderer._audio_duration",
        lambda self, path: 3.0,
    )
    return calls


def test_renderer_does_not_need_images() -> None:
    """画像生成を飛ばせること。クォータの律速がここで消える。"""
    assert RemotionRenderer().needs_images is False


def test_props_carry_resolved_frame_spans(captured, tmp_path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1", "字幕2", "字幕3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert props["width"] == 1080
    assert props["height"] == 1920
    assert props["durationInFrames"] == 90
    assert [s["fromFrame"] for s in props["scenes"]] == [0, 30, 60]
    assert [s["headline"] for s in props["scenes"]] == ["見出し1", "見出し2", "見出し3"]
    assert [s["subtitle"] for s in props["scenes"]] == ["字幕1", "字幕2", "字幕3"]
    assert props["scenes"][1]["items"] == ["従来", "新方式"]


def test_props_get_line_break_opportunities_for_japanese(captured, tmp_path) -> None:
    """日本語なら見出し・字幕に ZWSP（フレーズ境界）が入ること。

    短すぎる文字列は BudouX が1フレーズとみなし ZWSP が入らないので、
    複数フレーズに割れる長さの文を使う。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    headline = "変わったのは、動かす範囲を絞ったことでした"
    subtitle = "変わったのは、動かす範囲を絞ったことでした"
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=[headline] * 3,
        segment_narrations=[subtitle] * 3,
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert all(ZWSP in s["headline"] for s in props["scenes"])
    assert all(ZWSP in s["subtitle"] for s in props["scenes"])


def test_props_have_no_line_break_opportunities_for_english(captured, tmp_path) -> None:
    """英語はスペースで折り返せるため、ZWSP を混ぜない。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["headline 1", "headline 2", "headline 3"],
        segment_narrations=["subtitle 1", "subtitle 2", "subtitle 3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="en",
        video_format="short",
    )
    props = captured["props"]
    assert all(ZWSP not in s["headline"] for s in props["scenes"])
    assert all(ZWSP not in s["subtitle"] for s in props["scenes"])


def test_concurrency_is_always_explicit(captured, tmp_path) -> None:
    """既定に任せない。ホストのコア数の半分が立ち、コンテナで OOM を招く。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    cmd = captured["cmd"]
    assert any(str(a).startswith("--concurrency=") for a in cmd)


def test_props_file_is_removed(captured, tmp_path) -> None:
    """中間ファイルを残さない。残すと生成物が増え、Blob にも上がる。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    assert list(tmp_path.glob("*_props.json")) == []
    assert list(tmp_path.glob("*_silent.mp4")) == []


def test_mismatched_lengths_are_rejected(tmp_path) -> None:
    """配列長の不一致はここでも弾く。

    スキーマが担保しているが、レンダラは Script を経由しない呼び出しも
    受けうる。zip(strict=True) で落ちるより、原因の分かる例外にする。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    with pytest.raises(RemotionRenderError, match="配列長"):
        RemotionRenderer().render(
            audio_path=audio,
            output_path=tmp_path / "out.mp4",
            image_paths=[],
            scenes=_scenes(),
            text_overlays=["a"],
            segment_narrations=["a", "b", "c"],
            segment_timings=[],
            language="ja",
            video_format="short",
        )
