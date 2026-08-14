"""音声合成（Azure AI Speech）の純粋ロジック。

実APIは叩かない。SSML の組み立てと、bookmark のオフセットから
タイミングを作る部分だけを検証する。

なぜここを守るか: セグメントの開始時刻は動画側の画像切り替えに
そのまま渡る（`video_composer._calculate_durations`）。単調増加が
崩れると duration が負になり、ffmpeg が無言で壊れた動画を作る。
"""

import re
from pathlib import Path

import pytest

from src.generators.voice_generator import VoiceGenerationError, VoiceGenerator

SEGMENTS = ["最初の文です。", "次の文です。", "最後の文です。"]


@pytest.fixture
def generator() -> VoiceGenerator:
    """ダミーの資格情報で組み立てる（合成は行わない）。"""
    return VoiceGenerator(api_key="dummy-key", region="japaneast")


# --------------------------------------------------------------------------
# 初期化
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_key", "region"),
    [("", "japaneast"), ("key", "")],
)
def test_missing_credentials_are_rejected(api_key: str, region: str) -> None:
    """キーやリージョンの欠落は、合成を試みる前に弾くこと。

    合成時に初めて落ちると、そこまでの台本生成・画像生成が無駄になる。
    """
    with pytest.raises(ValueError):
        VoiceGenerator(api_key=api_key, region=region)


def test_voices_default_per_language(generator: VoiceGenerator) -> None:
    assert generator.voice_name_ja == VoiceGenerator.DEFAULT_VOICE_JA
    assert generator.voice_name_en == VoiceGenerator.DEFAULT_VOICE_EN


# --------------------------------------------------------------------------
# SSML の組み立て
# --------------------------------------------------------------------------


def test_one_bookmark_per_segment(generator: VoiceGenerator) -> None:
    """セグメント数だけ bookmark が入ること。

    数が合わないとオフセットが取れないセグメントが出て、
    直前の時刻へのフォールバックが起きる。
    """
    ssml = generator.build_ssml(SEGMENTS, "ja", 1.25)
    marks = re.findall(r'<bookmark mark="(seg_\d+)"/>', ssml)
    assert marks == ["seg_0", "seg_1", "seg_2"]


def test_bookmark_precedes_its_segment(generator: VoiceGenerator) -> None:
    """bookmark はセグメントの先頭に置くこと。

    後ろに置くと得られる時刻が「終了時刻」になり、
    画像切り替えが1セグメントぶんずれる。
    """
    ssml = generator.build_ssml(SEGMENTS, "ja", 1.25)
    assert '<bookmark mark="seg_1"/>次の文です。' in ssml


def test_speaking_rate_goes_into_prosody(generator: VoiceGenerator) -> None:
    """形式別の話速が <prosody rate> に入ること。

    Dragon HD 系ボイスは <prosody> 非対応なので、この経路が
    使えるボイスを選んでいる（DEFAULT_VOICE_* を変えるときの注意点）。
    """
    assert '<prosody rate="1.15">' in generator.build_ssml(SEGMENTS, "ja", 1.15)


def test_text_is_xml_escaped(generator: VoiceGenerator) -> None:
    """テキストを XML エスケープすること。

    記事タイトルに & や < が混じることが実際にあり、
    素のまま埋めると SSML が壊れて合成そのものが失敗する。
    """
    ssml = generator.build_ssml(["A & B < C"], "ja", 1.0)
    assert "A &amp; B &lt; C" in ssml
    assert "A & B < C" not in ssml


@pytest.mark.parametrize(
    ("language", "expected_locale", "expected_voice"),
    [
        ("ja", "ja-JP", VoiceGenerator.DEFAULT_VOICE_JA),
        ("en", "en-US", VoiceGenerator.DEFAULT_VOICE_EN),
    ],
)
def test_locale_and_voice_follow_the_language(
    generator: VoiceGenerator, language: str, expected_locale: str, expected_voice: str
) -> None:
    ssml = generator.build_ssml(SEGMENTS, language, 1.0)
    assert f'xml:lang="{expected_locale}"' in ssml
    assert f'<voice name="{expected_voice}">' in ssml


def test_empty_segments_are_rejected(generator: VoiceGenerator, tmp_path: Path) -> None:
    """セグメントが空なら API を叩く前に落ちること。

    空の SSML でも合成は成功してしまい、0秒の音声と空のタイミングが
    そのまま動画合成に流れる。入口で弾く方が原因が分かりやすい。
    """
    with pytest.raises(VoiceGenerationError):
        generator.generate_with_timings([], "ja", tmp_path / "out.mp3")


# --------------------------------------------------------------------------
# bookmark のオフセット -> タイミング
# --------------------------------------------------------------------------


def test_timings_have_one_more_element_than_segments() -> None:
    """要素数はセグメント数 + 1 であること。

    末尾の音声終了時刻がないと、最後のセグメントの表示時間が決まらない。
    `video_composer._calculate_durations` がこの形を前提にしている。
    """
    timings = VoiceGenerator._build_timings({0: 0.0, 1: 3.78, 2: 9.68}, 11.485, 3)
    assert timings == [0.0, 3.78, 9.68, 11.485]


def test_missing_bookmark_falls_back_to_the_previous_start() -> None:
    """bookmark が欠けても単調増加を保つこと。

    崩れると duration が負になり、ffmpeg が無言で壊れた動画を作る。
    """
    timings = VoiceGenerator._build_timings({0: 0.0, 2: 9.0}, 12.0, 3)
    assert timings == [0.0, 0.0, 9.0, 12.0]
    assert timings == sorted(timings)


def test_out_of_order_offsets_are_clamped() -> None:
    """逆行するオフセットは直前の値に丸めること。"""
    timings = VoiceGenerator._build_timings({0: 5.0, 1: 2.0}, 8.0, 2)
    assert timings == [5.0, 5.0, 8.0]


def test_total_duration_never_precedes_the_last_segment() -> None:
    """末尾の終了時刻が最後の開始時刻を下回らないこと。"""
    timings = VoiceGenerator._build_timings({0: 0.0, 1: 9.0}, 8.0, 2)
    assert timings[-1] >= timings[-2]


def test_no_bookmarks_at_all_yields_zeros_and_the_duration() -> None:
    """全部欠けても落ちないこと（動画側は等分に近い挙動になる）。"""
    assert VoiceGenerator._build_timings({}, 10.0, 3) == [0.0, 0.0, 0.0, 10.0]
