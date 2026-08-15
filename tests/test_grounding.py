"""生成文の数値が記事に根拠を持つかの検証。

自動投稿なので、機械的な検証だけが捏造への防衛線になる。
"""

from src.social.grounding import ungrounded_numbers


def test_記事にある数値は通る():
    source = "OpenAI は推論コストを 40% 削減したと発表した。"
    text = "推論コストが40%下がった。"

    assert ungrounded_numbers(text, source) == set()


def test_記事に無い数値は検出する():
    source = "OpenAI は推論コストを削減したと発表した。"
    text = "推論コストが40%下がった。"

    assert ungrounded_numbers(text, source) == {"40"}


def test_列挙表現は除外する():
    """「3つのポイント」は投稿の構成であって記事の数値ではない。"""
    source = "新しいモデルが公開された。"
    text = "ポイントは3つある。1つ目は速度だ。2点目はコスト。"

    assert ungrounded_numbers(text, source) == set()


def test_桁区切りのある数値も突き合わせる():
    source = "調達額は 1,200 万ドルだった。"
    text = "1200万ドルを調達した。"

    assert ungrounded_numbers(text, source) == set()


def test_複数の未根拠な数値をすべて返す():
    source = "モデルが公開された。"
    text = "3倍速く、コストは80%減、対応言語は95。"

    assert ungrounded_numbers(text, source) == {"3", "80", "95"}
