"""シーンの視覚指示の検証。

守っているのは2つ。レイアウトが要求する要素数を満たすこと（レンダラが
描けない形を作らせない）と、ラベルが名札の長さに収まること。
"""

import pytest
from pydantic import ValidationError

from src.models.scene import MAX_LABEL_CHARS, SceneLayout, SceneVisual


def test_statement_takes_no_items() -> None:
    scene = SceneVisual(layout=SceneLayout.STATEMENT, items=[])
    assert scene.items == []


def test_compare_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"])
    assert scene.items == ["従来", "新方式"]


def test_flow_takes_exactly_two_items() -> None:
    scene = SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"])
    assert len(scene.items) == 2


def test_compare_rejects_three_items() -> None:
    """範囲を許すとモデルは上限まで使い、図がグループに割れる。"""
    with pytest.raises(ValidationError, match="ちょうど2個"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["A", "B", "C"])


def test_statement_rejects_items() -> None:
    with pytest.raises(ValidationError, match="ちょうど0個"):
        SceneVisual(layout=SceneLayout.STATEMENT, items=["余計なラベル"])


def test_long_label_is_rejected() -> None:
    """名札に文を入れると、縦画面で図が文字に埋まる。"""
    too_long = "あ" * (MAX_LABEL_CHARS + 1)
    with pytest.raises(ValidationError, match="長すぎます"):
        SceneVisual(layout=SceneLayout.COMPARE, items=[too_long, "短い"])


def test_whitespace_only_label_is_rejected() -> None:
    """全角空白だけのラベルは長さ検査を通ってしまうので strip で見る。"""
    with pytest.raises(ValidationError, match="空です"):
        SceneVisual(layout=SceneLayout.COMPARE, items=["　　", "短い"])


def test_layout_accepts_plain_string() -> None:
    """LLM の JSON からは文字列で来るので、StrEnum に変換されること。"""
    scene = SceneVisual.model_validate({"layout": "compare", "items": ["前", "後"]})
    assert scene.layout is SceneLayout.COMPARE
