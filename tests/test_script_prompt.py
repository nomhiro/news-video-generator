"""システムプロンプトの組み立ての検証（API は呼ばない）。

なぜこのテストがあるか
----------------------
プロンプトは short/tiktok/long × ja/en で6種類ある。差し込みトークンを
追加したとき、**一部のテンプレートだけ置換し忘れる**のが一番起きやすい壊れ方で、
その場合はモデルに `<<STRUCTURE_SPEC>>` という文字列がそのまま渡る
（エラーにならず、静かに指示が消える）。ここで6種類すべてを見る。

同じ理由で、`<output_format>` の JSON 例に必須フィールドが載っていることも
確認する。例が欠けているとモデルは例に寄せた出力をしようとし、
スキーマ違反で再試行を消費する。
"""

import itertools

import pytest

from src.generators.script_generator import ScriptGenerator, chapter_labels, segment_allocation
from src.models.formats import SPECS, VideoFormat, get_spec
from src.models.script import MAX_HEADLINE_CHARS

FORMATS = ["short", "tiktok", "long"]
LANGUAGES = ["ja", "en"]
ALL_COMBINATIONS = list(itertools.product(FORMATS, LANGUAGES))


def _prompt(language: str, video_format: str) -> str:
    """API クライアントを作らずにプロンプトだけを組み立てる。

    `_build_system_prompt` は classmethod なので、`__init__`
    （OpenAI クライアントの生成）を通さずに呼べる。
    """
    return ScriptGenerator._build_system_prompt(language=language, video_format=video_format)


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_no_placeholder_token_remains(video_format: str, language: str) -> None:
    """置換漏れが無いこと。残っているとモデルに指示が届かない。"""
    prompt = _prompt(language, video_format)
    assert ScriptGenerator.NARRATION_SPEC_TOKEN not in prompt
    assert ScriptGenerator.STRUCTURE_SPEC_TOKEN not in prompt
    assert ScriptGenerator.ILLUSTRATION_SPEC_TOKEN not in prompt


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_structure_instruction_is_injected(video_format: str, language: str) -> None:
    """構成順序の指示が実際に入っていること。"""
    prompt = _prompt(language, video_format)
    assert "<narrative_structure>" in prompt
    # 解説の2フィールドを本文に反映させる指示が届いていること
    assert "technical_insight" in prompt
    assert "practical_impact" in prompt


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_output_example_includes_insight_fields(video_format: str, language: str) -> None:
    """JSON 例に必須フィールドが載っていること。

    例に無いとモデルは例に寄せた出力をして、スキーマ違反で再試行になる。
    """
    prompt = _prompt(language, video_format)
    example = prompt.split("<output_format>")[1].split("</output_format>")[0]
    assert '"technical_insight"' in example
    assert '"practical_impact"' in example
    # source_url はモデルに出させない（URL を知らないので捏造する）
    assert '"source_url"' not in example


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_segment_numbers_match_the_format_spec(video_format: str, language: str) -> None:
    """構成の指示が形式のセグメント数と整合していること。

    プロンプトに番号をハードコードすると仕様とずれる（実際にずれていた）。
    最後のパートの末尾番号が segment_count に一致することで確認する。
    """
    prompt = _prompt(language, video_format)
    count = get_spec(video_format).segment_count
    label = "セグメント" if language == "ja" else "Segment "
    assert f"{label}{count}:" in prompt


@pytest.mark.parametrize("video_format", list(VideoFormat))
def test_allocation_sums_to_segment_count(video_format: VideoFormat) -> None:
    """配分の合計がセグメント数に一致すること。

    ずれると構成の指示が存在しないセグメントを指す、または
    最後のセグメントに指示が無い状態になる。
    """
    count = SPECS[video_format].segment_count
    allocation = segment_allocation(count)
    assert sum(allocation.values()) == count
    assert all(v >= 1 for v in allocation.values())


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (6, {"hook": 1, "facts": 1, "mechanism": 2, "impact": 1, "conclusion": 1}),
        (10, {"hook": 1, "facts": 2, "mechanism": 3, "impact": 3, "conclusion": 1}),
    ],
)
def test_allocation_favors_the_analysis_parts(count: int, expected: dict[str, int]) -> None:
    """端数は解説側（仕組み・インパクト）に寄せること。

    独自解説を厚くするのが配分の目的なので、余りを事実に回すと逆になる。
    """
    assert segment_allocation(count) == expected


def test_allocation_rejects_too_few_segments() -> None:
    """パート数を下回るセグメント数は黙って進めないこと。

    0や負の割り当てはセグメント番号の範囲を壊す。
    """
    with pytest.raises(ValueError, match="構成パート数を下回っています"):
        segment_allocation(4)


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_prompt_has_no_unreplaced_tokens(video_format: str, language: str) -> None:
    """差し込みトークンが全部置換されていること。

    残ると `<<SCENES_SPEC>>` という文字列がそのままモデルに渡り、
    シーンの指示が一切効かないまま動く（気付きにくい）。
    """
    prompt = _prompt(language, video_format)
    assert "<<" not in prompt, f"{language}/{video_format} に未置換のトークンがある"


def test_scenes_example_has_one_entry_per_segment() -> None:
    """例の要素数が形式のセグメント数と一致すること。

    プロンプトに個数を直接書くと仕様とずれる（formats.py 冒頭の教訓）。
    """
    spec = get_spec("long")
    example = ScriptGenerator._scenes_example(spec)
    assert example.count('"layout"') == spec.segment_count


def test_scenes_spec_states_the_statement_limit() -> None:
    """statement の上限が指示文に出ていること。

    `str(segment_count // 2)` を素の数字だけで検査すると、"3つから選ぶ"
    のような無関係な箇所の数字と衝突して、上限の記述が無くても通ってしまう
    （short は segment_count // 2 == 3 で、layout の選択肢が3つある）。
    フレーズ全体で検査する。
    """
    spec = get_spec("short")
    text = ScriptGenerator._scenes_spec("ja", spec)
    limit = spec.segment_count // 2
    assert f"最大{limit}個" in text


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_overlay_instruction_states_the_enforced_limit(video_format: str, language: str) -> None:
    """見出しの上限が、実際に強制している値としてプロンプトに出ていること。

    以前はプロンプトが「15-25文字」「8-15 words」とだけ言い、モデル側には
    上限が一切無かった（バリデータも見ていなかった）。指示と検査が食い違うと、
    モデルが指示どおりに書いても検査で落ちる（またはその逆）。
    数値はプロンプトに書かず `MAX_HEADLINE_CHARS` から作る。
    """
    prompt = _prompt(language, video_format)
    assert str(MAX_HEADLINE_CHARS) in prompt


# --------------------------------------------------------------------------
# illustration_concept（Remotion レンダラが動画全体で共有する挿絵の主題を
# 「2つの要素とその関係」で表したもの）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_illustration_instruction_is_injected(video_format: str, language: str) -> None:
    """挿絵の主題を出させる指示が実際に入っていること。left/right/relation の
    3つを求める指示であること（自由文の1文ではない）。
    """
    prompt = _prompt(language, video_format)
    assert "illustration_concept" in prompt
    illustration_rule = prompt.split("illustration_concept")[1].split("\n")[0]
    assert "left" in illustration_rule
    assert "right" in illustration_rule
    assert "relation" in illustration_rule


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_illustration_instruction_forbids_scene_descriptions(
    video_format: str, language: str
) -> None:
    """場面・情景の描写を明示的に禁じること。

    実際に生成した挿絵で、記事の主題（ルーティングでコストを1/10にする）に
    対して「オフィスで働く人々」という場面を描いてしまった。自由文が場面を
    作らせる以上、構造を固定するだけでなく、この指示自体でも明示的に禁じる。
    """
    prompt = _prompt(language, video_format)
    illustration_rule = prompt.split("illustration_concept")[1]
    if language == "ja":
        assert "場面" in illustration_rule or "情景" in illustration_rule
    else:
        assert "scene" in illustration_rule.lower() or "setting" in illustration_rule.lower()


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_illustration_instruction_forbids_style_words(video_format: str, language: str) -> None:
    """スタイル語（画材・配色・技法）を書かせないこと。

    スタイルは固定のスタイル文（`ILLUSTRATION_STYLE_PROMPT`）としてコード側が
    前置する。モデルが同時に画材や配色を指示すると、`CardVisual` の
    `enhance=False` の教訓（1024x1024 を要求しつつ「9:16 縦構図で」と
    矛盾する指示が1つのプロンプトに混ざった）と同じ壊れ方をする。
    """
    prompt = _prompt(language, video_format)
    illustration_rule = prompt.split("illustration_concept")[1]
    if language == "ja":
        assert "スタイル" in illustration_rule or "画材" in illustration_rule
    else:
        assert "style" in illustration_rule.lower() or "medium" in illustration_rule.lower()


@pytest.mark.parametrize(("video_format", "language"), ALL_COMBINATIONS)
def test_output_example_includes_illustration_concept(video_format: str, language: str) -> None:
    """JSON 例に必須フィールドが載っていること。

    例に無いとモデルは例に寄せた出力をして、スキーマ違反で再試行になる。
    """
    prompt = _prompt(language, video_format)
    example = prompt.split("<output_format>")[1].split("</output_format>")[0]
    assert '"illustration_concept"' in example
    illustration_example = example.split('"illustration_concept"')[1].split("}")[0]
    assert '"left"' in illustration_example
    assert '"right"' in illustration_example
    assert '"relation"' in illustration_example


def test_overlay_examples_do_not_restate_the_limit() -> None:
    """`<output_format>` の例に別の数値を書かないこと。

    例に「15-25文字」のような第2の値を残すと、上限を変えたときに片方だけ
    直され、モデルには矛盾した2つの指示が届く（`formats.py` 冒頭の教訓）。
    """
    for video_format, language in ALL_COMBINATIONS:
        prompt = _prompt(language, video_format)
        example = prompt.split("<output_format>")[1].split("</output_format>")[0]
        overlays = example.split('"text_overlays"')[1]
        assert "文字）" not in overlays, f"{language}/{video_format} の例に文字数が残っている"
        assert "words)" not in overlays, f"{language}/{video_format} の例に語数が残っている"


def test_ungrounded_scene_numbers_flags_invented_figures(draft_factory) -> None:
    """記事に無い数値がラベルに入っていたら検出すること。

    カードでは記事に無い ¥980 が絵に描かれた（880c95f）。あちらは画像なので
    機械的に検査できなかったが、シーンのラベルはデータなので突き合わせられる。
    """
    draft = draft_factory(
        scenes=[
            {"layout": "compare", "items": ["50%", "従来"], "relation": "改善"},
            {"layout": "flow", "items": ["入力", "選択"], "relation": "変換"},
            {"layout": "statement", "items": [], "relation": ""},
        ]
    )
    assert ScriptGenerator._ungrounded_scene_numbers(draft, "記事本文に数値は無い") == {"50"}


def test_grounded_scene_numbers_pass(draft_factory) -> None:
    """記事にある数値だけなら合格すること。"""
    draft = draft_factory(
        scenes=[
            {"layout": "compare", "items": ["50%", "従来"], "relation": "改善"},
            {"layout": "flow", "items": ["入力", "選択"], "relation": "変換"},
            {"layout": "statement", "items": [], "relation": ""},
        ]
    )
    assert ScriptGenerator._ungrounded_scene_numbers(draft, "精度は50%向上した") == set()


# --------------------------------------------------------------------------
# chapter_labels（章ラベル。LLM 出力ではなくセグメント番号から導出する）
# --------------------------------------------------------------------------


def test_chapter_labels_for_six_segments() -> None:
    """短尺・TikTok（6セグメント）の並び。仕組みが2つ占めるので繰り返す。"""
    assert chapter_labels(6, "ja") == [
        "フック",
        "事実",
        "仕組み",
        "仕組み",
        "インパクト",
        "結論",
    ]


def test_chapter_labels_for_ten_segments() -> None:
    """長尺（10セグメント）の並び。仕組みとインパクトが3つずつ占める。"""
    assert chapter_labels(10, "ja") == [
        "フック",
        "事実",
        "事実",
        "仕組み",
        "仕組み",
        "仕組み",
        "インパクト",
        "インパクト",
        "インパクト",
        "結論",
    ]


def test_chapter_labels_in_english() -> None:
    assert chapter_labels(6, "en") == [
        "Hook",
        "Facts",
        "How it works",
        "How it works",
        "Impact",
        "Takeaway",
    ]


def test_chapter_labels_length_matches_segment_count() -> None:
    """要素数が segment_count に一致すること（renderer が zip(strict=True) で
    使うための前提）。"""
    for count in (6, 10):
        assert len(chapter_labels(count, "ja")) == count


def test_chapter_labels_degrades_instead_of_raising_for_too_few_segments() -> None:
    """章ラベルは装飾なので、配分できない短さでも例外にしないこと。

    `segment_allocation` は構成パート数（5）未満を ValueError で拒否するが、
    それをそのまま伝えると `RemotionRenderer.render()` が章ラベルのためだけに
    落ちてしまう（装飾のために本体を落とすのは本末転倒）。空文字列で埋めて
    「描く文字が無い」と伝える。
    """
    assert chapter_labels(4, "ja") == ["", "", "", ""]
    assert chapter_labels(1, "ja") == [""]
