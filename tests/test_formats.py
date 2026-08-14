"""動画形式の仕様の検証。

`formats.py` は解像度・話速・セグメント数・分量を集約した単一の情報源。
散らばっていたときに実際に整合が崩れたので、ここで不変条件を固定する。
"""

import pytest

from src.generators.image_generator import validate_size
from src.models.formats import SPECS, VideoFormat, get_spec


def test_every_format_has_a_spec() -> None:
    for video_format in VideoFormat:
        assert video_format in SPECS


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_image_size_satisfies_gpt_image_2_constraints(video_format: VideoFormat) -> None:
    """全形式の生成サイズが gpt-image-2 の制約を満たすこと。

    満たしていないと実行時に 400 が返る。
    """
    validate_size(SPECS[video_format].image_size)


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_image_aspect_matches_output_aspect(video_format: VideoFormat) -> None:
    """生成画像と完成動画のアスペクト比が厳密に一致すること。

    一致していれば ffmpeg は縮小のみで済み、クロップや
    レターボックスが入らない。
    """
    spec = SPECS[video_format]
    image_w, image_h = validate_size(spec.image_size)
    assert image_w * spec.output_height == image_h * spec.output_width, (
        f"{video_format}: 画像 {spec.image_size} と出力 "
        f"{spec.output_width}x{spec.output_height} のアスペクト比が一致しない"
    )


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_image_is_not_smaller_than_output(video_format: VideoFormat) -> None:
    """生成画像が出力解像度以上であること。

    小さいと ffmpeg が拡大することになり画質が落ちる。
    """
    spec = SPECS[video_format]
    image_w, image_h = validate_size(spec.image_size)
    assert image_w >= spec.output_width
    assert image_h >= spec.output_height


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_duration_range_is_coherent(video_format: VideoFormat) -> None:
    spec = SPECS[video_format]
    assert spec.min_duration_sec < spec.max_duration_sec


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_estimated_duration_falls_inside_the_allowed_range(video_format: VideoFormat) -> None:
    """目標文字数から推定される尺が、その形式の許容範囲に入ること。

    文字数の目標と尺の許容範囲を別々に決めると矛盾する。
    ここで両者の整合を強制する。
    """
    from src.models.script import estimate_duration_sec

    spec = SPECS[video_format]
    low_chars, high_chars = spec.total_chars

    shortest = estimate_duration_sec("あ" * low_chars, "ja")
    longest = estimate_duration_sec("あ" * high_chars, "ja")

    assert shortest >= spec.min_duration_sec, (
        f"{video_format}: 目標下限 {low_chars}文字 は {shortest}秒 で、"
        f"最小尺 {spec.min_duration_sec}秒 に届かない"
    )
    assert longest <= spec.max_duration_sec, (
        f"{video_format}: 目標上限 {high_chars}文字 は {longest}秒 で、"
        f"最大尺 {spec.max_duration_sec}秒 を超える"
    )


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_segment_count_is_positive(video_format: VideoFormat) -> None:
    assert SPECS[video_format].segment_count > 0


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_char_and_word_ranges_are_ordered(video_format: VideoFormat) -> None:
    spec = SPECS[video_format]
    assert spec.chars_per_segment[0] < spec.chars_per_segment[1]
    assert spec.words_per_segment[0] < spec.words_per_segment[1]


def test_total_chars_scales_with_segment_count() -> None:
    spec = SPECS[VideoFormat.SHORT]
    low, high = spec.chars_per_segment
    assert spec.total_chars == (low * spec.segment_count, high * spec.segment_count)


def test_aspect_label() -> None:
    assert SPECS[VideoFormat.SHORT].aspect_label == "9:16"
    assert SPECS[VideoFormat.TIKTOK].aspect_label == "9:16"
    assert SPECS[VideoFormat.LONG].aspect_label == "16:9"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("short", VideoFormat.SHORT),
        ("tiktok", VideoFormat.TIKTOK),
        ("long", VideoFormat.LONG),
    ],
)
def test_get_spec_by_name(name: str, expected: VideoFormat) -> None:
    assert get_spec(name) is SPECS[expected]


def test_get_spec_falls_back_to_short_for_unknown_names() -> None:
    """未知の形式は SHORT にすること。

    CLI は choices で制限しているが、Web のフォームからは
    任意の文字列が届く可能性がある。
    """
    assert get_spec("nonsense") is SPECS[VideoFormat.SHORT]
    assert get_spec("") is SPECS[VideoFormat.SHORT]


def test_speaking_rate_decreases_as_videos_get_longer() -> None:
    """長い動画ほど話速を落とすこと（聞き取りやすさのため）。"""
    assert (
        SPECS[VideoFormat.SHORT].speaking_rate
        > SPECS[VideoFormat.TIKTOK].speaking_rate
        > SPECS[VideoFormat.LONG].speaking_rate
    )
