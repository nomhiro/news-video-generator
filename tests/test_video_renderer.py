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


def test_ffmpeg_renderer_needs_images() -> None:
    assert FfmpegRenderer().needs_images is True


def test_remotion_renderer_does_not_need_images() -> None:
    assert RemotionRenderer().needs_images is False
