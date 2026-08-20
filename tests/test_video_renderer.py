"""レンダラの差し替え。

既定を ffmpeg にしてある理由: これは今日動いているパイプラインで、
クラウドで問題が出たときに環境変数1つで戻れる退路になる。
"""

import pytest

from src.generators.remotion_renderer import RemotionRenderer
from src.generators.video_renderer import (
    FfmpegRenderer,
    build_video_renderer,
)


def test_default_is_ffmpeg() -> None:
    """マージしても見た目が変わらないこと。"""
    assert isinstance(build_video_renderer("ffmpeg"), FfmpegRenderer)


def test_remotion_can_be_selected() -> None:
    assert isinstance(build_video_renderer("remotion"), RemotionRenderer)


def test_unknown_renderer_is_rejected() -> None:
    """未知の名前で黙って既定に落とさない。

    スケジューラの中で初めて分かると、気付くのが翌朝になる。
    """
    with pytest.raises(ValueError, match="未知のレンダラ"):
        build_video_renderer("blender")


def test_ffmpeg_renderer_needs_one_image_per_segment() -> None:
    """ffmpeg は静止画を並べる方式なので、セグメントごとに1枚必要。"""
    assert FfmpegRenderer().image_count(6) == 6
    assert FfmpegRenderer().image_count(10) == 10


def test_remotion_renderer_needs_exactly_one_image() -> None:
    """Remotion は図解を React で描くので、共有する挿絵1枚だけで足りる。

    セグメント数に関わらず常に1枚（`needs_images` が粗すぎたので置き換えた）。
    """
    assert RemotionRenderer().image_count(6) == 1
    assert RemotionRenderer().image_count(10) == 1


def test_ffmpeg_renderer_does_not_draw_scene_text() -> None:
    """ffmpeg レンダラはシーンのラベルを一切描かない。

    `needs_images` とは別の問いなので別のフラグにしてある。混ぜると
    「画像は要らないがラベルは描かない」レンダラが表現できなくなる。
    """
    assert FfmpegRenderer().draws_scene_text is False


def test_remotion_renderer_draws_scene_text() -> None:
    assert RemotionRenderer().draws_scene_text is True
