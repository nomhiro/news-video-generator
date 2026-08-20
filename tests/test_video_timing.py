"""動画の画像切り替えタイミング計算の検証。

`_calculate_durations` は「各画像を何秒表示するか」を決める。
セグメントごとの実測タイミングがあればそれを使い、
無ければ音声全体を均等分割する。ここがずれると
ナレーションと画像・字幕が合わなくなる。
"""

import pytest

from src.generators.video_composer import VideoComposer


@pytest.fixture
def composer() -> VideoComposer:
    return VideoComposer()


def test_falls_back_to_equal_split_without_timings(composer: VideoComposer) -> None:
    """タイミングが無い場合は均等分割すること。"""
    durations = composer._calculate_durations(4, 40.0, None)
    assert durations == [10.0, 10.0, 10.0, 10.0]


def test_falls_back_to_equal_split_with_empty_timings(composer: VideoComposer) -> None:
    durations = composer._calculate_durations(3, 30.0, [])
    assert durations == [10.0, 10.0, 10.0]


def test_uses_variable_durations_from_timings(composer: VideoComposer) -> None:
    """開始時刻の列から各画像の表示時間を差分で求めること。

    segment_timings は「各セグメントの開始時刻」であり、
    末尾に音声全体の終了時刻が入る。
    """
    timings = [0.0, 7.5, 14.0, 22.0]
    durations = composer._calculate_durations(3, 22.0, timings)
    assert durations == pytest.approx([7.5, 6.5, 8.0])


def test_last_image_extends_to_audio_end(composer: VideoComposer) -> None:
    """末尾の開始時刻しか無い場合、最後の画像は音声終了まで表示すること。"""
    timings = [0.0, 5.0, 12.0]
    durations = composer._calculate_durations(3, 20.0, timings)
    assert durations == pytest.approx([5.0, 7.0, 8.0])
    assert sum(durations) == pytest.approx(20.0)


def test_durations_sum_to_audio_duration(composer: VideoComposer) -> None:
    """表示時間の合計が音声長と一致すること。

    ずれると動画の末尾が黒画面になる、または音声が切れる。
    """
    timings = [0.0, 7.42, 14.30, 20.90, 28.42, 34.85, 42.82]
    durations = composer._calculate_durations(6, 42.82, timings)
    assert sum(durations) == pytest.approx(42.82)


def test_falls_back_when_timings_are_fewer_than_images(composer: VideoComposer) -> None:
    """タイミングが画像数に足りない場合は均等分割に落ちること。"""
    durations = composer._calculate_durations(5, 25.0, [0.0, 5.0])
    assert durations == [5.0, 5.0, 5.0, 5.0, 5.0]


def test_enforces_minimum_duration(composer: VideoComposer) -> None:
    """同一時刻が連続しても 0 秒表示にならないこと。

    ffmpeg の concat デマクサは duration 0 を扱えない。
    """
    timings = [0.0, 5.0, 5.0, 10.0]
    durations = composer._calculate_durations(3, 10.0, timings)
    assert all(d > 0 for d in durations)
    assert durations[1] == pytest.approx(0.1)


def test_never_returns_fewer_durations_than_images(composer: VideoComposer) -> None:
    """画像数と同じ数の表示時間を必ず返すこと。

    concat ファイル作成側は zip(strict=True) で突き合わせるため、
    数がずれると失敗する。
    """
    for num_images in (1, 3, 6, 10, 16):
        durations = composer._calculate_durations(num_images, 60.0, None)
        assert len(durations) == num_images


# --------------------------------------------------------------------------
# テキストの折り返し
# --------------------------------------------------------------------------


def test_short_text_is_not_wrapped(composer: VideoComposer) -> None:
    assert composer._wrap_text("短い文字列") == "短い文字列"


def test_long_text_is_wrapped_at_limit(composer: VideoComposer) -> None:
    """全角だけの行は既定の文字数で折り返すこと。

    幅で折り返すようになったが、全角しか無い行の結果は従来と同じになる
    （上限の幅を `文字数 × フォントサイズ` として定義しているため）。
    """
    text = "あ" * 30
    wrapped = composer._wrap_text(text)
    lines = wrapped.split("\n")
    assert all(len(line) <= composer.TEXT_MAX_CHARS_PER_LINE for line in lines)
    assert "".join(lines) == text


def test_every_line_fits_the_width_budget(composer: VideoComposer) -> None:
    """どの行も上限の幅に収まること。"""
    max_width = composer.TEXT_MAX_CHARS_PER_LINE * composer.TEXT_FONT_SIZE
    for text in (
        "あ" * 30,
        "OpenAIがgpt-image-2を一般提供開始しました",
        "Microsoft announces MAI-Image-2.6 for enterprise customers today",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJ",  # 大文字は半角でも幅を食う
    ):
        for line in composer._wrap_text(text).split("\n"):
            assert composer._line_width(line) <= max_width, f"{line!r} がはみ出している"


def test_half_width_line_uses_the_full_budget(composer: VideoComposer) -> None:
    """半角が混じった行を文字数で切らないこと。

    文字数で折り返していた頃、半角英数が混じった行は極端に短くなった
    （実測 fontsize=64: 全角14文字は 881px だが
    "Anthropicが最強AI" の14文字は 551px）。1行に収まるのに
    折り返され、字幕が無駄に2行になっていた。
    """
    text = "Anthropicが最強AIを自ら暴露"  # 全角換算では14文字を超える

    assert len(text) > composer.TEXT_MAX_CHARS_PER_LINE
    assert "\n" not in composer._wrap_text(text)


def test_wrap_does_not_split_inside_a_word(composer: VideoComposer) -> None:
    """英数の語を途中で割らないこと。

    以前は "Anthro / pic" のように語の途中で改行されていた。
    ハイフンやドットで繋がる型番（gpt-image-2）も1つの語として扱う。
    """
    text = "Microsoft announces gpt-image-2 for enterprise customers today"

    lines = composer._wrap_text(text).split("\n")

    assert len(lines) > 1, "折り返しが起きていない（前提が崩れている）"
    for word in ("Microsoft", "announces", "gpt-image-2", "enterprise", "customers"):
        assert any(word in line for line in lines), f"{word} が分断されている"


def test_wrap_splits_a_word_too_long_for_one_line(composer: VideoComposer) -> None:
    """1行に収まらない語は文字単位で割ること（割らないとはみ出す）。"""
    max_width = composer.TEXT_MAX_CHARS_PER_LINE * composer.TEXT_FONT_SIZE

    lines = composer._wrap_text("A" * 60).split("\n")

    assert len(lines) > 1
    assert all(composer._line_width(line) <= max_width for line in lines)
    assert "".join(lines) == "A" * 60


def test_wrap_does_not_put_punctuation_at_line_start(composer: VideoComposer) -> None:
    """句読点や閉じ括弧を行頭に置かないこと。

    実際に生成した動画で "Claude vs GPT画像 何が違" までが1行に入り、
    `？` だけが2行目に落ちた。直前の1文字を一緒に次の行へ送って避ける。

    3行になるテキストで見ているのは、2行のときは幅の均し
    （`_balance_two_lines`）が別経路で分割位置を選ぶため、
    ここで見たい「送り出し」が働く場面にならないから。
    """
    lines = composer._wrap_text("あ" * 28 + "？").split("\n")

    assert len(lines) == 3, lines
    assert lines[-1] == "あ？", lines  # ？ を単独で残さず直前の1文字を連れてくる
    assert "".join(lines) == "あ" * 28 + "？"


def test_wrap_does_not_leave_an_opening_bracket_at_line_end(composer: VideoComposer) -> None:
    """開き括弧を行末に残さないこと（記事タイトルに実際に出てくる）。"""
    lines = composer._wrap_text("記事タイトル「MAI-Image-2.6」を発表しました").split("\n")

    assert not any(line.endswith("「") for line in lines), lines
    assert any(line.startswith("「") for line in lines), lines


def test_kinsoku_never_overflows_the_width_budget(composer: VideoComposer) -> None:
    """禁則のために幅の上限を破らないこと。

    行頭禁則は「ぶら下げ」にせず「追い出し」にしている。ぶら下げると
    上限を超え、フレームからはみ出して端が切れる。
    """
    max_width = composer.TEXT_MAX_CHARS_PER_LINE * composer.TEXT_FONT_SIZE
    for text in (
        "Claude vs GPT画像 何が違う？",
        "記事タイトル「MAI-Image-2.6」を発表しました",
        "Aは、Bです。Cもあります。Dでした。Eもそうです。",
        "。" * 40,  # 送り先も禁則文字ばかりという極端な場合
        "あ" * 13 + "？",
    ):
        for line in composer._wrap_text(text).split("\n"):
            assert composer._line_width(line) <= max_width, f"{line!r} がはみ出している"


def test_two_lines_are_balanced(composer: VideoComposer) -> None:
    """2行になるときは左右の幅を揃えること。

    貪欲に詰めると1行目を上限まで使うので2行目が1文字だけになる。
    実際に生成した動画で "Claude Opus 5 最新モデル登" / "場" になった。
    """
    lines = composer._wrap_text("Claude Opus 5 最新モデル登場").split("\n")

    assert lines == ["Claude Opus 5", "最新モデル登場"]


def test_balancing_keeps_the_width_difference_small(composer: VideoComposer) -> None:
    """均した2行の幅の差が全角1文字ぶんに収まること。"""
    for text in ("Claude vs GPT画像 何が違う？", "Claude Opus 5 最新モデル登場"):
        lines = composer._wrap_text(text).split("\n")
        assert len(lines) == 2, lines
        first, second = (composer._line_width(line) for line in lines)
        assert abs(first - second) <= composer.TEXT_FONT_SIZE, (text, first, second)


def test_balancing_respects_width_and_kinsoku(composer: VideoComposer) -> None:
    """均した結果が上限の幅と禁則を破らないこと。"""
    max_width = composer.TEXT_MAX_CHARS_PER_LINE * composer.TEXT_FONT_SIZE
    for text in (
        "Claude vs GPT画像 何が違う？",
        "Claude Opus 5 最新モデル登場",
        "記事タイトル「MAI-Image-2.6」発表",
        "Aは、Bです。Cもあります。",
    ):
        lines = composer._wrap_text(text).split("\n")
        for line in lines:
            assert composer._line_width(line) <= max_width, (text, line)
        for line in lines[1:]:
            assert line[0] not in composer.LINE_START_FORBIDDEN, (text, lines)
        for line in lines[:-1]:
            assert line[-1] not in composer.LINE_END_FORBIDDEN, (text, lines)


def test_three_or_more_lines_stay_greedy(composer: VideoComposer) -> None:
    """3行以上になるものは詰めたままにすること（均すのは2行のときだけ）。"""
    lines = composer._wrap_text("あ" * 30).split("\n")

    assert len(lines) == 3
    assert [len(line) for line in lines] == [14, 14, 2]


def test_wrap_respects_explicit_limit(composer: VideoComposer) -> None:
    """`max_chars` は全角換算の文字数として効くこと。"""
    max_width = 3 * composer.TEXT_FONT_SIZE

    lines = composer._wrap_text("あいうえおかきくけこ", max_chars=3).split("\n")

    assert lines == ["あいう", "えおか", "きくけ", "こ"]
    assert all(composer._line_width(line) <= max_width for line in lines)


def test_wrap_preserves_all_characters(composer: VideoComposer) -> None:
    """空白を含まないテキストでは文字を落とさないこと。

    空白で折り返した場合はその空白を捨てる（通常のワードラップ）ので、
    この保証は空白を含まないテキストに限る。
    """
    text = "OpenAIがgpt-image-2を一般提供開始しました"
    assert composer._wrap_text(text).replace("\n", "") == text
