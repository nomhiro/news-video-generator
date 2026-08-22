"""固有名詞の読み（TTS 用）の検査。

読みは**音声にだけ**当てる。画面（字幕・見出し）には原綴りが出るという
非対称が壊れると、視聴者から原綴りの情報を奪ったことに誰も気付けない
（音を聞かないと分からない）。ここで文字列として突き合わせておく。
"""

import pytest

from src.generators.voice_generator import VoiceGenerator
from src.utils.reading import READINGS, apply_readings


def test_claude_is_read_as_katakana() -> None:
    """実測で読みが崩れていた語が置き換わること。"""
    assert apply_readings("Claudeの文章に透かしが入る。", "ja") == "クロードの文章に透かしが入る。"


def test_english_is_left_alone() -> None:
    """英語のボイスにカタカナを渡すと読めない。"""
    text = "Claude adds an invisible watermark."
    assert apply_readings(text, "en") == text


def test_readings_do_not_fire_inside_a_longer_word() -> None:
    """英数字に挟まれた位置ではマッチしないこと。

    `Claudette` を「クロードtte」にしてはいけない。日本語の助詞は
    ラテン文字ではないので `Claudeの` は置換される（上のテスト）。
    """
    assert apply_readings("Claudette", "ja") == "Claudette"
    assert apply_readings("XClaude", "ja") == "XClaude"


def test_all_readings_are_katakana() -> None:
    """辞書の値がカタカナであること。

    ラテン文字を残した読みを登録すると、置換しても症状が直らない
    （同じ問題が別の綴りで再発する）。
    """
    for term, reading in READINGS.items():
        assert all("゠" <= ch <= "ヿ" or ch == "ー" for ch in reading), (
            f"{term} の読み {reading!r} にカタカナ以外が含まれている"
        )


def test_ssml_carries_the_reading_not_the_spelling() -> None:
    """SSML に読みが入ること。**適用箇所はここ1箇所**という前提の検査。

    `build_ssml` を通らない経路（将来 `<sub alias>` に戻す等）に変えたときに
    落ちる。読みが当たっていないと気付く手段は「聞く」以外に無いので、
    自動で見張る必要がある。
    """
    generator = VoiceGenerator(api_key="dummy", region="japaneast")
    ssml = generator.build_ssml(["Claudeが発表しました。"], "ja", 1.25)
    assert "クロード" in ssml
    assert "Claude" not in ssml


def test_ssml_escapes_after_substitution() -> None:
    """置換はエスケープの前に行うこと。

    順序が逆だと、エスケープ済みの実体参照（`&amp;`）の中を書き換える経路が
    生まれる。`&` を含む記事タイトルは実際にある。
    """
    generator = VoiceGenerator(api_key="dummy", region="japaneast")
    ssml = generator.build_ssml(["Claude & GPT の比較。"], "ja", 1.25)
    assert "&amp;" in ssml
    assert "クロード" in ssml


@pytest.mark.parametrize("language", ["ja", "en"])
def test_empty_text_is_safe(language: str) -> None:
    assert apply_readings("", language) == ""
