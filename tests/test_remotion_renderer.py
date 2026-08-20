"""Remotion レンダラの Python 側。

実レンダリングは tests/test_remotion_render_slow.py が担当する。
ここではコマンドの組み立てとフレーム換算だけを見る（速い）。
"""

import json
from pathlib import Path

import pytest

from src.generators.remotion_renderer import (
    ILLUSTRATION_STYLE_PROMPT,
    RemotionRenderer,
    RemotionRenderError,
    build_illustration_prompt,
    resolve_frame_spans,
)
from src.models.scene import IllustrationConcept, SceneLayout, SceneVisual
from src.utils.line_break import ZWSP

# --------------------------------------------------------------------------
# 挿絵のプロンプト（Task 2 / フラット化と構造化）
# --------------------------------------------------------------------------


def _concept(**overrides: object) -> IllustrationConcept:
    payload: dict[str, object] = {
        "subject": "a router directing each input to one of several stores",
        "key_details": ["a small switch block", "several identical stores behind it"],
        "labels": ["入力", "切替"],
    }
    payload.update(overrides)
    return IllustrationConcept.model_validate(payload)


def test_illustration_prompt_prepends_the_fixed_style() -> None:
    """CardVisual と同じ二段構え。組み立てた構図の指示の前にスタイル文が来ること。"""
    prompt = build_illustration_prompt(_concept())
    assert prompt.startswith(ILLUSTRATION_STYLE_PROMPT)


def test_illustration_prompt_composes_subject_details_and_labels() -> None:
    """subject / key_details / labels を `build_card_prompt` と同じ形に
    組み立てること。

    構図の文章自体を LLM に書かせない（コード側の権威にする）ので、
    ここでは組み立てた結果の文言を検査する。
    """
    prompt = build_illustration_prompt(_concept())
    assert "Subject: a router directing each input to one of several stores" in prompt
    assert "a small switch block; several identical stores behind it" in prompt
    # 名札は引用符付きで並べ、「その部分の隣に描く」指示と一緒に渡す。
    assert '"入力", "切替"' in prompt
    assert "placed beside the element each one names" in prompt


def test_illustration_prompt_states_none_when_there_are_no_labels() -> None:
    """名札が無いときは「無い」と明示すること。

    書かないと、モデルは「説明図」という指示から勝手に見出しや注釈を
    書き足す（カードでも同じ理由で none を明記している）。
    """
    prompt = build_illustration_prompt(_concept(labels=[]))
    assert "Labels: none" in prompt
    assert "Render no text of any kind" in prompt


def test_illustration_style_allows_japanese_labels_but_bans_numerals() -> None:
    """名札は日本語で描かせ、数字だけは禁じ続けること。

    文字の全面禁止をやめた理由（2026-08-20）: 禁止していたために
    「これが何か」を示す手段が構図しか残らず、絵が抽象に振れて
    「概念図すぎる」状態になった。日本語で描かせる根拠は
    `CardVisual._labels_must_be_short`（2026-08-16 に実画像で確認済み）。

    数字を禁じ続ける理由: カードで記事に無い「¥980」が絵に描かれた
    前例があり（880c95f）、挿絵は接地検査の対象外である。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "labels in this image must be japanese" in lowered
    assert "no numerals or digits of any kind" in lowered
    # 文・見出し・段落は禁じたままにする（見出しと字幕は React が描く）。
    assert "no sentence, caption, title, or paragraph anywhere" in lowered


def test_illustration_style_demands_margins() -> None:
    """図と名札を画像の端に寄せさせないこと。

    名札を許した結果、実際に端で文字が欠けた（2026-08-20 の実測: 描画が
    横幅の 4.7%〜96.5% に及び、末尾フレームで右端まで達して「軽ブロック」の
    一部が切れた）。レンダラ側は `Illustration.tsx` を `contain` + 縮小配置に
    直して切り取り自体を無くしたが、**画像が端まで使っていると縮小の余白が
    足りなくなる**ので、生成側にも余白を要求する。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "central 90% of" in lowered
    assert "nothing may touch or approach the edge" in lowered


def test_illustration_style_requires_an_explanatory_diagram() -> None:
    """1つの仕組みを1枚の説明図として描かせること。

    「同じ形の反復＋一部の強調」という構図の固定をやめた理由は
    `remotion_renderer` の設計コメント（2026-08-20）を参照。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "one mechanism explained in one diagram" in lowered
    assert "one idea only" in lowered


def test_illustration_style_forbids_incidental_props() -> None:
    """付随物（コーヒー・観葉植物・部屋など）を明示的に禁じること。

    実際に生成した挿絵が「オフィスで働く人々」という場面を描いた反省
    （`remotion_renderer.py` のコメント参照）を踏まえ、「場面ではない」と
    言い切る一文を含む。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "incidental props" in lowered
    assert "not a scene" in lowered


def test_illustration_style_is_flat_not_hand_drawn() -> None:
    """フラットな図であることを明示し、手描き・チョークは明示的に禁じる側にのみ
    出てくること（旧スタイル文はチョークを肯定する側で使っていた）。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "flat" in lowered
    assert "no hand-drawn wobble" in lowered
    assert "no visible brush or chalk strokes" in lowered
    assert "chalk-like" not in lowered
    assert "hand-drawn illustrated sketch" not in lowered


def test_illustration_style_is_not_the_card_style() -> None:
    """カードのスタイル文を再利用しないこと（地の色が紙 vs 暗い地で違う）。"""
    from src.social.card_visual import CARD_STYLE_PROMPT

    assert ILLUSTRATION_STYLE_PROMPT != CARD_STYLE_PROMPT


def test_illustration_style_uses_the_theme_colors() -> None:
    """テーマの実際の HEX 値を使うこと（デザインとコードがずれると気付きにくい）。"""
    assert "#1b1a1d" in ILLUSTRATION_STYLE_PROMPT  # theme.ts の COLORS.bg
    assert "#2dd4bf" in ILLUSTRATION_STYLE_PROMPT  # COLORS.accent
    assert "#f2a93c" in ILLUSTRATION_STYLE_PROMPT  # COLORS.accent2


def test_illustration_style_bans_human_figures_including_pictograms() -> None:
    """人物ピクトグラムも「人物」として明示的に禁じること。

    以前の「no real people」だけでは、実際の失敗（人物ピクトグラム3体）を
    止められなかった。ピクトグラムは実在の人物ではないため。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "no human figures" in lowered
    assert "pictogram" in lowered


def test_illustration_style_restricts_accent_to_the_emphasis() -> None:
    """アクセントカラーは強調部分だけに使うこと。

    複数の物に装飾的に配色すると（失敗画像の3体の人物が別々の色だった
    ように）、色が意味を運ばなくなりクリップアート的に見える。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "accent discipline" in lowered
    assert "only" in lowered


def test_illustration_style_bans_abstract_quantities_as_subjects() -> None:
    """「効率」「コスト」のような抽象量を主題にすることを禁じること。

    描けない量は別の物体（CPUチップなど）に置き換わり、「削減された」の
    ような程度の情報が失われる。
    """
    lowered = ILLUSTRATION_STYLE_PROMPT.lower()
    assert "abstract quantity" in lowered
    assert "reduced compute" in lowered


def test_spans_cover_the_whole_audio_without_gaps() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans == [(0, 30), (30, 30), (60, 30)]


def test_spans_fall_back_to_even_split_without_timings() -> None:
    """bookmark が取れなかった場合。均等割りにする。"""
    spans = resolve_frame_spans([], 3.0, 30, 3)
    assert [s[1] for s in spans] == [30, 30, 30]
    assert spans[0][0] == 0


def test_every_span_is_at_least_one_frame() -> None:
    """長さ0のシーンを作らせない。

    各開始秒を独立に丸めると、近接したタイミングで長さ0や負のシーンが
    できる。Remotion は例外を出さず、シーンが飛んだ動画を黙って作る
    （ffmpeg が無言で壊れた動画を作るのと同じ壊れ方）。
    """
    spans = resolve_frame_spans([0.0, 0.001, 0.002, 1.0], 1.0, 30, 3)
    assert all(duration >= 1 for _, duration in spans)


def test_spans_are_contiguous_and_monotonic() -> None:
    """隙間も重なりも作らない。"""
    spans = resolve_frame_spans([0.0, 0.4, 0.41, 2.0], 2.0, 30, 3)
    for i in range(len(spans) - 1):
        assert spans[i][0] + spans[i][1] == spans[i + 1][0]


def test_spans_survive_non_monotonic_timings() -> None:
    """タイミングが逆行していても増加を強制する。"""
    spans = resolve_frame_spans([0.0, 1.5, 0.5, 3.0], 3.0, 30, 3)
    starts = [start for start, _ in spans]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_spans_end_exactly_at_the_audio_end() -> None:
    spans = resolve_frame_spans([0.0, 1.0, 2.0, 3.0], 3.0, 30, 3)
    assert spans[-1][0] + spans[-1][1] == 90


def _scenes() -> list[SceneVisual]:
    return [
        SceneVisual(layout=SceneLayout.STATEMENT, items=[], relation=""),
        SceneVisual(layout=SceneLayout.COMPARE, items=["従来", "新方式"], relation="切替"),
        SceneVisual(layout=SceneLayout.FLOW, items=["入力", "選択"], relation="変換"),
    ]


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Remotion と ffmpeg の呼び出しを捕まえ、props を読めるようにする。"""
    calls: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        # --props=<path> の中身を読んでおく（呼び出し後に消えるため）
        for arg in cmd:
            if isinstance(arg, str) and arg.startswith("--props="):
                calls["props"] = json.loads(Path(arg.split("=", 1)[1]).read_text("utf-8"))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    def fake_mux(silent, audio, output, **kwargs):
        calls["mux"] = (silent, audio, output)
        output.write_bytes(b"muxed")

    monkeypatch.setattr("src.generators.remotion_renderer.subprocess.run", fake_run)
    monkeypatch.setattr("src.generators.remotion_renderer.mux_audio", fake_mux)
    monkeypatch.setattr(
        "src.generators.remotion_renderer.RemotionRenderer._audio_duration",
        lambda self, path: 3.0,
    )
    return calls


def test_renderer_needs_exactly_one_image() -> None:
    """動画全体で共有する挿絵1枚だけで足りること。クォータの律速がここで6分の1になる。"""
    assert RemotionRenderer().image_count(6) == 1
    assert RemotionRenderer().image_count(10) == 1


def test_props_carry_resolved_frame_spans(captured, tmp_path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1", "字幕2", "字幕3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert props["width"] == 1080
    assert props["height"] == 1920
    assert props["durationInFrames"] == 90
    assert [s["fromFrame"] for s in props["scenes"]] == [0, 30, 60]
    assert [s["headline"] for s in props["scenes"]] == ["見出し1", "見出し2", "見出し3"]
    assert [s["subtitle"] for s in props["scenes"]] == ["字幕1", "字幕2", "字幕3"]
    assert props["scenes"][1]["items"] == ["従来", "新方式"]


def test_props_carry_relation(captured, tmp_path) -> None:
    """次のディスパッチ（React側）が読む relation プロップ。

    relation はシーンの視覚指示からそのまま来る。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1", "字幕2", "字幕3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert [s["relation"] for s in props["scenes"]] == ["", "切替", "変換"]


def test_props_carry_empty_chapter_when_too_few_scenes_to_allocate(captured, tmp_path) -> None:
    """章ラベルは装飾なので、配分できない短さでもレンダリング自体は成立すること。

    `chapter_labels` は構成パート数（5）未満のセグメント数では空文字列で
    埋める（`segment_allocation` の ValueError をそのまま伝えない）。
    ここで使う `_scenes()` は3個なので、その劣化経路を通る。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["見出し1", "見出し2", "見出し3"],
        segment_narrations=["字幕1", "字幕2", "字幕3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert [s["chapter"] for s in props["scenes"]] == ["", "", ""]


def test_props_get_line_break_opportunities_for_japanese(captured, tmp_path) -> None:
    """日本語なら見出し・字幕に ZWSP（フレーズ境界）が入ること。

    短すぎる文字列は BudouX が1フレーズとみなし ZWSP が入らないので、
    複数フレーズに割れる長さの文を使う。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    headline = "変わったのは、動かす範囲を絞ったことでした"
    subtitle = "変わったのは、動かす範囲を絞ったことでした"
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=[headline] * 3,
        segment_narrations=[subtitle] * 3,
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    props = captured["props"]
    assert all(ZWSP in s["headline"] for s in props["scenes"])
    assert all(ZWSP in s["subtitle"] for s in props["scenes"])


def test_props_have_no_line_break_opportunities_for_english(captured, tmp_path) -> None:
    """英語はスペースで折り返せるため、ZWSP を混ぜない。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["headline 1", "headline 2", "headline 3"],
        segment_narrations=["subtitle 1", "subtitle 2", "subtitle 3"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="en",
        video_format="short",
    )
    props = captured["props"]
    assert all(ZWSP not in s["headline"] for s in props["scenes"])
    assert all(ZWSP not in s["subtitle"] for s in props["scenes"])


def test_concurrency_is_always_explicit(captured, tmp_path) -> None:
    """既定に任せない。ホストのコア数の半分が立ち、コンテナで OOM を招く。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    cmd = captured["cmd"]
    assert any(str(a).startswith("--concurrency=") for a in cmd)


def test_props_file_is_removed(captured, tmp_path) -> None:
    """中間ファイルを残さない。残すと生成物が増え、Blob にも上がる。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
    )
    assert list(tmp_path.glob("*_props.json")) == []
    assert list(tmp_path.glob("*_silent.mp4")) == []


# --------------------------------------------------------------------------
# 挿絵の受け渡し（Task 4）
# --------------------------------------------------------------------------


def test_illustration_lands_in_public_with_filename_only_and_is_cleaned_up(
    captured, tmp_path
) -> None:
    """props には remotion/public 相対のファイル名だけを持たせ、レンダリング後は消すこと。

    `staticFile()` は public/ からの相対名しか受け取らない。props の JSON や
    無音映像と同じ扱いで、残すとコミット済みディレクトリに生成物が積もる。
    """
    from src.generators.remotion_renderer import REMOTION_DIR

    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    illustration = tmp_path / "illustration_src.png"
    illustration.write_bytes(b"fake png bytes")

    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
        illustration_path=illustration,
    )
    props = captured["props"]
    filename = props["illustration"]
    assert filename
    assert "/" not in filename
    assert "\\" not in filename
    assert not (REMOTION_DIR / "public" / filename).exists()


def test_illustration_is_a_top_level_prop_not_per_scene(captured, tmp_path) -> None:
    """挿絵は動画全体で共有する1枚なので、シーンごとに複製しないこと。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    illustration = tmp_path / "illustration_src.png"
    illustration.write_bytes(b"fake png bytes")

    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
        illustration_path=illustration,
    )
    props = captured["props"]
    assert "illustration" in props
    assert all("illustration" not in scene for scene in props["scenes"])


def test_illustration_is_empty_string_when_not_given(captured, tmp_path) -> None:
    """挿絵が無い呼び出し（`illustration_path=None`）では地のみで続行すること。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")

    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
        illustration_path=None,
    )
    assert captured["props"]["illustration"] == ""


def test_missing_illustration_file_does_not_fail_the_render(captured, tmp_path) -> None:
    """挿絵の生成に失敗していても、レンダリング自体は落とさないこと。

    章ラベルと同じ判断: 装飾的な要素の欠落で本体を落とすのは本末転倒。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")

    RemotionRenderer().render(
        audio_path=audio,
        output_path=tmp_path / "out.mp4",
        image_paths=[],
        scenes=_scenes(),
        text_overlays=["a", "b", "c"],
        segment_narrations=["a", "b", "c"],
        segment_timings=[0.0, 1.0, 2.0, 3.0],
        language="ja",
        video_format="short",
        illustration_path=tmp_path / "does_not_exist.png",
    )
    assert captured["props"]["illustration"] == ""


def test_mismatched_lengths_are_rejected(tmp_path) -> None:
    """配列長の不一致はここでも弾く。

    スキーマが担保しているが、レンダラは Script を経由しない呼び出しも
    受けうる。zip(strict=True) で落ちるより、原因の分かる例外にする。
    """
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    with pytest.raises(RemotionRenderError, match="配列長"):
        RemotionRenderer().render(
            audio_path=audio,
            output_path=tmp_path / "out.mp4",
            image_paths=[],
            scenes=_scenes(),
            text_overlays=["a"],
            segment_narrations=["a", "b", "c"],
            segment_timings=[],
            language="ja",
            video_format="short",
        )
