"""BudouX によるフレーズ分割の折り返し補助を検査する。

CSS の `word-break: keep-all` はCJK文字間の悪い改行点を禁止するだけで、
どこで改行できるかは別途与える必要がある。この ZWSP 挿入がその「良い
改行点」を作る側。実際に壊れた実例（「…絞ったこ」/「とでした。」で
「ことでした」が割れた）の再現文で検査する。
"""

from src.utils.line_break import ZWSP, insert_break_opportunities

# 実際に壊れた実例。「ことでした」が「…絞ったこ」/「とでした。」に割れていた。
BROKEN_SENTENCE = "「変わったのは、動かす範囲を絞ったことでした。」"


def test_does_not_split_kotodeshita() -> None:
    """「ことでした」の内部に ZWSP が入ってはならない（実際に壊れた箇所）。"""
    result = insert_break_opportunities(BROKEN_SENTENCE, "ja")
    # 「ことでした」がそのまま連続して現れること（間に ZWSP が挟まっていないこと）
    assert "ことでした" in result


def test_phrase_boundaries_get_zwsp() -> None:
    """BudouX がフレーズに分けた箇所には ZWSP が入る（何も分割されない退行を防ぐ）。"""
    result = insert_break_opportunities(BROKEN_SENTENCE, "ja")
    assert ZWSP in result


def test_english_is_returned_unchanged() -> None:
    """英語はスペースで正しく折り返せるため、そのまま返す。"""
    text = "This is a normal English sentence."
    assert insert_break_opportunities(text, "en") == text


def test_empty_string_is_safe() -> None:
    assert insert_break_opportunities("", "ja") == ""
    assert insert_break_opportunities("", "en") == ""


def test_original_text_is_recoverable() -> None:
    """ZWSP を取り除けば元の文字列に戻る。挿入以外の変更が無いことが最重要。"""
    result = insert_break_opportunities(BROKEN_SENTENCE, "ja")
    assert result.replace(ZWSP, "") == BROKEN_SENTENCE
