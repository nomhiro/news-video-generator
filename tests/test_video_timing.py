"""動画の画像切り替えタイミング計算の検証。

`_calculate_durations` は「各画像を何秒表示するか」を決める。
セグメントごとの実測タイミングがあればそれを使い、
無ければ音声全体を均等分割する。ここがずれると
ナレーションと画像・字幕が合わなくなる。
"""

import pytest

from src.generators.video_composer import VideoComposer


@pytest.fixture
def composer() -> VideoComposer:
    return VideoComposer()


def test_falls_back_to_equal_split_without_timings(composer: VideoComposer) -> None:
    """タイミングが無い場合は均等分割すること。"""
    durations = composer._calculate_durations(4, 40.0, None)
    assert durations == [10.0, 10.0, 10.0, 10.0]


def test_falls_back_to_equal_split_with_empty_timings(composer: VideoComposer) -> None:
    durations = composer._calculate_durations(3, 30.0, [])
    assert durations == [10.0, 10.0, 10.0]


def test_uses_variable_durations_from_timings(composer: VideoComposer) -> None:
    """開始時刻の列から各画像の表示時間を差分で求めること。

    segment_timings は「各セグメントの開始時刻」であり、
    末尾に音声全体の終了時刻が入る。
    """
    timings = [0.0, 7.5, 14.0, 22.0]
    durations = composer._calculate_durations(3, 22.0, timings)
    assert durations == pytest.approx([7.5, 6.5, 8.0])


def test_last_image_extends_to_audio_end(composer: VideoComposer) -> None:
    """末尾の開始時刻しか無い場合、最後の画像は音声終了まで表示すること。"""
    timings = [0.0, 5.0, 12.0]
    durations = composer._calculate_durations(3, 20.0, timings)
    assert durations == pytest.approx([5.0, 7.0, 8.0])
    assert sum(durations) == pytest.approx(20.0)


def test_durations_sum_to_audio_duration(composer: VideoComposer) -> None:
    """表示時間の合計が音声長と一致すること。

    ずれると動画の末尾が黒画面になる、または音声が切れる。
    """
    timings = [0.0, 7.42, 14.30, 20.90, 28.42, 34.85, 42.82]
    durations = composer._calculate_durations(6, 42.82, timings)
    assert sum(durations) == pytest.approx(42.82)


def test_falls_back_when_timings_are_fewer_than_images(composer: VideoComposer) -> None:
    """タイミングが画像数に足りない場合は均等分割に落ちること。"""
    durations = composer._calculate_durations(5, 25.0, [0.0, 5.0])
    assert durations == [5.0, 5.0, 5.0, 5.0, 5.0]


def test_enforces_minimum_duration(composer: VideoComposer) -> None:
    """同一時刻が連続しても 0 秒表示にならないこと。

    ffmpeg の concat デマクサは duration 0 を扱えない。
    """
    timings = [0.0, 5.0, 5.0, 10.0]
    durations = composer._calculate_durations(3, 10.0, timings)
    assert all(d > 0 for d in durations)
    assert durations[1] == pytest.approx(0.1)


def test_never_returns_fewer_durations_than_images(composer: VideoComposer) -> None:
    """画像数と同じ数の表示時間を必ず返すこと。

    concat ファイル作成側は zip(strict=True) で突き合わせるため、
    数がずれると失敗する。
    """
    for num_images in (1, 3, 6, 10, 16):
        durations = composer._calculate_durations(num_images, 60.0, None)
        assert len(durations) == num_images


# --------------------------------------------------------------------------
# テキストの折り返し
# --------------------------------------------------------------------------


def test_short_text_is_not_wrapped(composer: VideoComposer) -> None:
    assert composer._wrap_text("短い文字列") == "短い文字列"


def test_long_text_is_wrapped_at_limit(composer: VideoComposer) -> None:
    """既定の文字数で折り返すこと。"""
    text = "あ" * 30
    wrapped = composer._wrap_text(text)
    lines = wrapped.split("\n")
    assert all(len(line) <= composer.TEXT_MAX_CHARS_PER_LINE for line in lines)
    assert "".join(lines) == text


def test_wrap_respects_explicit_limit(composer: VideoComposer) -> None:
    wrapped = composer._wrap_text("abcdefghij", max_chars=3)
    assert wrapped == "abc\ndef\nghi\nj"


def test_wrap_preserves_all_characters(composer: VideoComposer) -> None:
    """折り返しで文字を落とさないこと。"""
    text = "OpenAIがgpt-image-2を一般提供開始しました"
    assert composer._wrap_text(text).replace("\n", "") == text
