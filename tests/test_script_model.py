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

from src.models.script import (
    MIN_INSIGHT_CHARS,
    Script,
    ScriptDraft,
    _join_narration,
    estimate_duration_sec,
)


def _draft(**overrides: object) -> ScriptDraft:
    """検証を通る最小の下書きを作り、必要な項目だけ差し替える。"""
    payload: dict[str, object] = {
        "title": "テストタイトル",
        "description": "テスト説明",
        "hashtags": ["shorts", "test"],
        "hook": "冒頭のフック",
        "main_points": ["ポイント1", "ポイント2"],
        "conclusion": "締めの一言",
        "technical_insight": (
            "内部では既存モデルの推論結果をキャッシュして再利用する仕組みになっているため、"
            "2回目以降の応答が速い。"
        ),
        "practical_impact": (
            "現場では手作業だったレビュー工程を自動化でき、日次の運用コストが下がる。"
            "レビュー担当は判断だけに集中できる。"
        ),
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
    draft = _draft(title="固有タイトル")
    script = draft.to_script("ja")
    assert script.title == "固有タイトル"
    assert script.segment_narrations == draft.segment_narrations
    assert script.image_prompts == draft.image_prompts
    assert script.hashtags == draft.hashtags
    assert script.conclusion == draft.conclusion


def test_to_script_overrides_model_reported_duration() -> None:
    """estimated_duration にモデルの自己申告を使わないこと。

    実測でモデルは 35 と申告しながら実尺59.6秒の台本を返した。
    申告値は当てにならないので文字数から推定する。
    """
    draft = _draft(estimated_duration=999)
    script = draft.to_script("ja")
    assert script.estimated_duration != 999
    # 12文字 ÷ 6.0文字/秒 = 2秒
    assert script.estimated_duration == 2


def test_to_script_uses_measured_duration_when_given() -> None:
    """実測値が渡されたらそれを採用すること。"""
    draft = _draft(estimated_duration=999)
    script = draft.to_script("ja", actual_duration_sec=42.4)
    assert script.estimated_duration == 42


@pytest.mark.parametrize(
    ("narration", "language", "expected"),
    [
        ("あ" * 60, "ja", 10),  # 60文字 ÷ 6.0 = 10秒
        ("あ" * 255, "ja", 42),  # 実測（255文字 → 42.8秒）とほぼ一致
        ("あ" * 300, "ja", 50),  # 300文字 ÷ 6.0 = 50秒
        (" ".join(["word"] * 26), "en", 10),  # 26語 ÷ 2.6 = 10秒
        ("", "ja", 1),  # 空でも最低1秒
    ],
)
def test_estimate_duration_sec(narration: str, language: str, expected: int) -> None:
    assert estimate_duration_sec(narration, language) == expected


# --------------------------------------------------------------------------
# 分量の予算チェック
#
# プロンプトの文字数指示は守られない（実測で47%超過）ため、
# 検査して引き直させる。
# --------------------------------------------------------------------------


def test_length_within_budget_passes() -> None:
    draft = _draft(segment_narrations=["あ" * 40, "い" * 40, "う" * 40])
    assert draft.check_length_budget("ja", (100, 150)) is None


def test_length_at_the_limit_is_allowed() -> None:
    """上限ちょうどは許すこと（境界の扱いを固定する）。"""
    draft = _draft(segment_narrations=["あ" * 100, "い" * 100, "う" * 100])
    assert draft.check_length_budget("ja", (200, 300)) is None


def test_length_over_budget_is_reported_with_percentage() -> None:
    """超過は割合付きで報告すること。ログから深刻度が分かるように。"""
    # 実測で踏んだ状況を再現: 上限330文字に対して484文字（47%超過）
    draft = _draft(
        segment_narrations=["あ" * 162, "い" * 161, "う" * 161],
        image_prompts=["p1", "p2", "p3"],
        text_overlays=["o1", "o2", "o3"],
    )
    problem = draft.check_length_budget("ja", (250, 330))
    assert problem is not None
    assert "長すぎます" in problem
    assert "484文字" in problem
    assert "47%" in problem


def test_length_far_below_budget_is_reported() -> None:
    """極端に短い場合も報告すること（内容が足りていない）。"""
    draft = _draft(segment_narrations=["あ", "い", "う"])
    problem = draft.check_length_budget("ja", (200, 300))
    assert problem is not None
    assert "短すぎます" in problem


def test_slightly_below_budget_is_tolerated() -> None:
    """下限をやや下回る程度は許すこと。

    短すぎるより「尺を稼ぐために内容を薄く伸ばす」方が有害なので、
    下限は緩く見る。
    """
    draft = _draft(segment_narrations=["あ" * 40, "い" * 40, "う" * 40])
    assert draft.check_length_budget("ja", (150, 300)) is None


def test_english_budget_counts_characters_not_words() -> None:
    """英語も文字数で超過判定すること（1語≒6文字換算）。"""
    draft = _draft(segment_narrations=["word " * 20, "word " * 20, "word " * 20])
    problem = draft.check_length_budget("en", (50, 100))
    assert problem is not None


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


# --------------------------------------------------------------------------
# 独自解説（Issue #2）
#
# ニュースをなぞるだけの台本は埋もれるうえ、YouTube の
# 「再利用されたコンテンツ」ポリシーに抵触するリスクがある。
# Structured Outputs では必須フィールドをモデルが省略できないので、
# 「解説が入っていること」はスキーマで担保する。
# 「質」は担保できないので、生成物を読む工程は別に必要。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["technical_insight", "practical_impact"])
def test_rejects_empty_insight(field_name: str) -> None:
    """独自解説が空だと弾くこと。"""
    with pytest.raises(ValidationError):
        _draft(**{field_name: ""})


@pytest.mark.parametrize("field_name", ["technical_insight", "practical_impact"])
def test_rejects_whitespace_only_insight(field_name: str) -> None:
    """空白だけの独自解説を弾くこと。

    `Field(min_length=...)` は「全角空白を40個」を通してしまうため、
    strip 後の長さで見る必要がある。
    """
    with pytest.raises(ValidationError, match=f"{field_name} が空です"):
        _draft(**{field_name: "　" * (MIN_INSIGHT_CHARS + 10)})


@pytest.mark.parametrize("field_name", ["technical_insight", "practical_impact"])
def test_rejects_too_short_insight(field_name: str) -> None:
    """一言で流した独自解説を弾くこと。"""
    with pytest.raises(ValidationError):
        _draft(**{field_name: "あ" * (MIN_INSIGHT_CHARS - 1)})


@pytest.mark.parametrize("field_name", ["technical_insight", "practical_impact"])
def test_accepts_insight_at_the_minimum(field_name: str) -> None:
    """下限ちょうどは許すこと（境界の扱いを固定する）。"""
    draft = _draft(**{field_name: "あ" * MIN_INSIGHT_CHARS})
    assert len(getattr(draft, field_name)) == MIN_INSIGHT_CHARS


def test_script_rejects_missing_insight() -> None:
    """Script 側でも独自解説を必須にすること。

    output/scripts/*.json はここを読んでレビューするので、
    保存されるモデルにも無いと意味がない。
    """
    data = _draft().to_script("ja").to_dict()
    del data["technical_insight"]
    with pytest.raises(ValidationError):
        Script.from_dict(data)


def test_to_script_preserves_insights() -> None:
    """独自解説が Script に引き継がれること。"""
    draft = _draft()
    script = draft.to_script("ja")
    assert script.technical_insight == draft.technical_insight
    assert script.practical_impact == draft.practical_impact


# --------------------------------------------------------------------------
# 出典 URL
#
# ScriptDraft には持たせない。モデルは URL を知らない（プロンプト入力は
# 記事のタイトルと本文だけ）ので、出させれば捏造する。
# language と同じく呼び出し元が権威を持つ。
# --------------------------------------------------------------------------


def test_draft_has_no_source_url_field() -> None:
    """source_url は LLM への要求項目に含めない。

    モデルは URL を知らないので、出させると捏造する。
    """
    assert "source_url" not in ScriptDraft.model_fields


def test_to_script_appends_source_to_description_ja() -> None:
    draft = _draft(description="要約文")
    script = draft.to_script("ja", source_url="https://example.com/a")
    assert script.description == "要約文\n\n出典: https://example.com/a"
    assert script.source_url == "https://example.com/a"


def test_to_script_appends_source_to_description_en() -> None:
    draft = _draft(description="Summary")
    script = draft.to_script("en", source_url="https://example.com/a")
    assert script.description == "Summary\n\nSource: https://example.com/a"


def test_to_script_does_not_duplicate_existing_url() -> None:
    """モデルが説明文に URL を書いていたら二重に足さないこと。"""
    draft = _draft(description="要約文\n\n参考: https://example.com/a")
    script = draft.to_script("ja", source_url="https://example.com/a")
    assert script.description.count("https://example.com/a") == 1


def test_to_script_without_source_leaves_description_untouched() -> None:
    """URL が無い呼び出しでは説明文を変えないこと。

    CLI は自由テキストのトピックを取るので、URL を持たない実行がある。
    """
    draft = _draft(description="要約文")
    script = draft.to_script("ja")
    assert script.description == "要約文"
    assert script.source_url == ""
