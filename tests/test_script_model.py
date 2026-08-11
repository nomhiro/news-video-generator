"""台本モデル（Structured Outputs のスキーマ）の検証。

ここで守っているのは、実際に踏んだ2つの失敗である。

1. モデルが「250〜300文字」「厳密に6セグメント」「連結が full_narration と
   一致」を同時に要求されると、自然な5文を書いた後に**空の6番目**を
   足して一致だけを守った。要素数と非空性はスキーマで強制する必要がある。
2. full_narration を LLM に出させると上記の矛盾が生まれるため、
   segment_narrations の連結でコード側が導出する。
"""

import json

import pytest
from pydantic import ValidationError

from src.models.script import Script, ScriptDraft, _join_narration


def _draft(**overrides: object) -> ScriptDraft:
    """検証を通る最小の下書きを作り、必要な項目だけ差し替える。"""
    payload: dict[str, object] = {
        "title": "テストタイトル",
        "description": "テスト説明",
        "hashtags": ["shorts", "test"],
        "hook": "冒頭のフック",
        "main_points": ["ポイント1", "ポイント2"],
        "conclusion": "締めの一言",
        "image_prompts": ["Scene 1", "Scene 2", "Scene 3"],
        "text_overlays": ["overlay 1", "overlay 2", "overlay 3"],
        "estimated_duration": 35,
        "segment_narrations": ["文A。", "文B。", "文C。"],
    }
    payload.update(overrides)
    return ScriptDraft.model_validate(payload)


def test_valid_draft_passes() -> None:
    draft = _draft()
    assert len(draft.segment_narrations) == 3


def test_draft_has_no_full_narration_field() -> None:
    """full_narration は LLM への要求項目に含めない。

    含めると「連結と一致させる」制約が生まれ、空セグメントの
    パディングを誘発する。
    """
    assert "full_narration" not in ScriptDraft.model_fields


def test_draft_has_no_language_field() -> None:
    """language は呼び出し元が権威を持つのでモデルに出させない。"""
    assert "language" not in ScriptDraft.model_fields


def test_rejects_empty_segment() -> None:
    """空セグメントを弾くこと。実際にモデルが6番目を空で返した。"""
    with pytest.raises(ValidationError, match="segment_narrations の3番目が空です"):
        _draft(segment_narrations=["文A。", "文B。", ""])


def test_rejects_whitespace_only_segment() -> None:
    """空白だけのセグメントも空として扱うこと。"""
    with pytest.raises(ValidationError, match="segment_narrations の2番目が空です"):
        _draft(segment_narrations=["文A。", "   ", "文C。"])


def test_rejects_all_empty_segments_list() -> None:
    with pytest.raises(ValidationError, match="segment_narrations が空です"):
        _draft(segment_narrations=[], image_prompts=[], text_overlays=[])


@pytest.mark.parametrize(
    ("segments", "prompts", "overlays"),
    [
        (["a", "b"], ["p1", "p2", "p3"], ["o1", "o2", "o3"]),  # セグメントが少ない
        (["a", "b", "c"], ["p1", "p2"], ["o1", "o2", "o3"]),  # プロンプトが少ない
        (["a", "b", "c"], ["p1", "p2", "p3"], ["o1", "o2"]),  # オーバーレイが少ない
    ],
)
def test_rejects_length_mismatch(
    segments: list[str], prompts: list[str], overlays: list[str]
) -> None:
    """3配列の要素数がずれたら弾くこと。

    音声のタイミング同期と動画合成が一致を前提にしている。
    """
    with pytest.raises(ValidationError, match="配列長の不一致"):
        _draft(segment_narrations=segments, image_prompts=prompts, text_overlays=overlays)


def test_rejects_empty_image_prompt() -> None:
    with pytest.raises(ValidationError, match="image_prompts の2番目が空です"):
        _draft(image_prompts=["Scene 1", "", "Scene 3"])


def test_rejects_empty_text_overlay() -> None:
    with pytest.raises(ValidationError, match="text_overlays の1番目が空です"):
        _draft(text_overlays=["", "overlay 2", "overlay 3"])


# --------------------------------------------------------------------------
# ナレーションの導出
# --------------------------------------------------------------------------


def test_join_narration_japanese_has_no_separator() -> None:
    """日本語は語間に空白を入れないこと。

    空白を入れると TTS が不自然に区切って読む。
    """
    assert (
        _join_narration(["こんにちは。", "今日は晴れです。"], "ja")
        == "こんにちは。今日は晴れです。"
    )


def test_join_narration_english_uses_space() -> None:
    """英語は単語境界が必要なので半角空白で連結すること。"""
    assert _join_narration(["Hello there.", "It is sunny."], "en") == "Hello there. It is sunny."


def test_join_narration_strips_each_segment() -> None:
    """セグメント端の空白は落とし、二重空白を作らないこと。"""
    assert _join_narration(["  Hello.  ", "  World.  "], "en") == "Hello. World."
    assert _join_narration(["  あ。 ", " い。 "], "ja") == "あ。い。"


def test_to_script_derives_full_narration() -> None:
    """full_narration がセグメントの連結になっていること。"""
    draft = _draft(segment_narrations=["文A。", "文B。", "文C。"])
    script = draft.to_script("ja")
    assert script.full_narration == "文A。文B。文C。"


def test_to_script_sets_language_from_caller() -> None:
    draft = _draft()
    assert draft.to_script("en").language == "en"
    assert draft.to_script("ja").language == "ja"


def test_to_script_preserves_all_other_fields() -> None:
    """導出以外のフィールドがそのまま引き継がれること。"""
    draft = _draft(title="固有タイトル", estimated_duration=42)
    script = draft.to_script("ja")
    assert script.title == "固有タイトル"
    assert script.estimated_duration == 42
    assert script.segment_narrations == draft.segment_narrations
    assert script.image_prompts == draft.image_prompts


def test_script_round_trips_through_json_file(tmp_path) -> None:
    """保存した台本を読み戻せること。

    output/scripts/*.json は生成物のレビューに使うため、
    読み書きの往復が壊れていないことを確認する。
    """
    script = _draft().to_script("ja")
    path = tmp_path / "nested" / "script.json"
    script.to_json_file(path)

    assert path.exists()
    restored = Script.from_json_file(path)
    assert restored == script

    # ファイル自身が UTF-8 でエスケープされていないこと（レビューしやすさ）
    raw = path.read_text(encoding="utf-8")
    assert "テストタイトル" in raw
    assert json.loads(raw)["language"] == "ja"


def test_script_validates_on_load() -> None:
    """壊れた JSON からの復元も検証を通ること。

    手で編集した台本を読み込むケースがあるため。
    """
    data = _draft().to_script("ja").to_dict()
    data["segment_narrations"] = ["文A。", ""]
    with pytest.raises(ValidationError):
        Script.from_dict(data)
