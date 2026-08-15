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


def test_全角と半角の数値を同じものとして扱う():
    """`\\d` は全角数字にも一致するため、畳まないと別の数値になる。

    まともな投稿が「根拠が無い」と判定されて破棄される（再生成1回で
    通らなければその記事を諦める設計なので、丸ごと失う）。
    """
    source = "調達額は 10億ドルだった。"
    text = "１０億ドルを調達した。"

    assert ungrounded_numbers(text, source) == set()


def test_全角の桁区切りも畳む():
    """区切りが全角だと、数値が途中で切れて別の数値になる。"""
    source = "調達額は 1,200 万ドルだった。"
    text = "１，２００万ドルを調達した。"

    assert ungrounded_numbers(text, source) == set()


def test_全角でも記事に無い数値は検出する():
    """畳むこと自体が検出を甘くしていないことの確認。"""
    source = "調達額は 10億ドルだった。"
    text = "２０億ドルを調達した。"

    assert ungrounded_numbers(text, source) == {"20"}


def test_複数の未根拠な数値をすべて返す():
    source = "モデルが公開された。"
    text = "3倍速く、コストは80%減、対応言語は95。"

    assert ungrounded_numbers(text, source) == {"3", "80", "95"}
