"""シーンの視覚指示の検証。

守っているのは3つ。レイアウトが要求する要素数を満たすこと（レンダラが
描けない形を作らせない）、ラベルが名札の長さに収まること、そして
関係性ラベル（relation）が compare/flow では必須で statement では
存在しないこと。
"""

import pytest
from pydantic import ValidationError

from src.models.scene import MAX_LABEL_CHARS, MAX_RELATION_CHARS, SceneLayout, SceneVisual


def test_statement_takes_no_items() -> None:
    scene = SceneVisual(layout=SceneLayout.STATEMENT, items=[], relation="")
    assert scene.items == []


def test_compare_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation="切替")
    assert scene.items == ["従来", "新方式"]


def test_flow_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"], relation="変換")
    assert len(scene.items) == 2


def test_compare_rejects_three_items() -> None:
    """範囲を許すとモデルは上限まで使い、図がグループに割れる。"""
    with pytest.raises(ValidationError, match="ちょうど2個"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B", "C"], relation="切替")


def test_statement_rejects_items() -> None:
    with pytest.raises(ValidationError, match="ちょうど0個"):
        SceneVisual(layout=SceneLayout.STATEMENT, items=["余計なラベル"], relation="")


def test_long_label_is_rejected() -> None:
    """名札に文を入れると、縦画面で図が文字に埋まる。"""
    too_long = "あ" * (MAX_LABEL_CHARS + 1)
    with pytest.raises(ValidationError, match="長すぎます"):
        SceneVisual(layout=SceneLayout.COMPARE, items=[too_long, "短い"], relation="切替")


def test_whitespace_only_label_is_rejected() -> None:
    """全角空白だけのラベルは長さ検査を通ってしまうので strip で見る。"""
    with pytest.raises(ValidationError, match="空です"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["　　", "短い"], relation="切替")


def test_layout_accepts_plain_string() -> None:
    """LLM の JSON からは文字列で来るので、StrEnum に変換されること。"""
    scene = SceneVisual.model_validate(
        {"layout": "compare", "items": ["前", "後"], "relation": "切替"}
    )
    assert scene.layout is SceneLayout.COMPARE


# --------------------------------------------------------------------------
# relation（2つの要素の関係性ラベル）
# --------------------------------------------------------------------------


def test_compare_requires_relation() -> None:
    """compare は2つの要素を並べるだけでは何が言いたいか分からない。"""
    with pytest.raises(ValidationError, match="relation が空です"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation="")


def test_flow_requires_relation() -> None:
    with pytest.raises(ValidationError, match="relation が空です"):
        SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"], relation="")


def test_flow_rejects_whitespace_only_relation() -> None:
    """全角空白だけの relation は長さ検査を通ってしまうので strip で見る。"""
    with pytest.raises(ValidationError, match="relation が空です"):
        SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"], relation="　　")


def test_relation_too_long_is_rejected() -> None:
    too_long = "あ" * (MAX_RELATION_CHARS + 1)
    with pytest.raises(ValidationError, match="relation が長すぎます"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation=too_long)


def test_relation_at_the_limit_is_accepted() -> None:
    """上限ちょうどは通すこと（境界での off-by-one を防ぐ）。"""
    at_limit = "あ" * MAX_RELATION_CHARS
    scene = SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation=at_limit)
    assert scene.relation == at_limit


def test_statement_rejects_nonempty_relation() -> None:
    """statement には対比・因果の図が無いので、関係性を描く場所が無い。"""
    with pytest.raises(ValidationError, match="relation を空文字列にする必要があります"):
        SceneVisual(layout=SceneLayout.STATEMENT, items=[], relation="切替")
