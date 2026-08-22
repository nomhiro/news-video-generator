"""Remotion のデザイン側の規約を検査する。

**これは既知の1つを名前で狙い撃つだけ**で、遅い描画一般を防ぐものではない
（box-shadow を10枚重ねれば同じことが起きる）。それでも置くのは、実測で
3倍の差が出ていて、tests/test_deploy_workflow.py や
tests/test_container_image.py と同じ「ファイルの中身を検査する」型に
収まるから。

コメントは除いてから検査する
--------------------------
`Background.tsx` と `theme.ts` は、禁止している構文そのもの（`blur(` /
`@font-face`）を**コメントの中で名指し**して、なぜ禁止かを説明している
（実測値つき）。そのコメントはファイル中で最も価値のある行なので、
「コメントを言い換えて検査を回避する」のではなく、検査側がコメントを
読み飛ばす。

除去は正規表現の近似（`//` 以降と `/* ... */` を削るだけ）で、
文字列リテラルの中に埋め込まれた違反はすり抜ける。厳密な構文解析はしていない。
"""

import re
from pathlib import Path

REMOTION_SRC = Path(__file__).resolve().parents[1] / "remotion" / "src"

_LINE_COMMENT = re.compile(r"//.*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(text: str) -> str:
    """`//` 以降と `/* ... */` を取り除く。

    近似的な実装であることに注意（docstring 参照）。TypeScript の完全な
    トークナイザではないので、文字列リテラルの中に `//` や `/*` があると
    誤って削ってしまう可能性がある。それでもコメントで説明を書きたい
    このリポジトリの流儀とは相性が良い。
    """
    without_block = _BLOCK_COMMENT.sub("", text)
    return _LINE_COMMENT.sub("", without_block)


def test_no_blur_filter_anywhere() -> None:
    """全画面 blur は 199秒 → 598秒（3倍）にする。実測（2026-08-17）。

    グローを出したいときは blur ではなくグラデーションと不透明度で作る。
    """
    offenders = [
        path.relative_to(REMOTION_SRC).as_posix()
        # `.tsx` だけを見ると、`theme.ts` のような `.ts` に置いた共有ヘルパが
        # 検査をすり抜ける。Web フォントの検査と同じ範囲にそろえる。
        for path in REMOTION_SRC.rglob("*.ts*")
        if "blur(" in _strip_comments(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"filter: blur() を使っているファイル: {offenders}"


def test_no_web_fonts() -> None:
    """@font-face / @remotion/google-fonts を使わないこと。

    非同期に読ませると、delayRender / waitForFonts で待たない限り最初の
    数フレームだけフォールバックフォントで焼かれる。エラーにならないので
    気付きにくい。システムの fonts-noto-cjk を font-family で参照する。
    """
    for path in REMOTION_SRC.rglob("*.ts*"):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        assert "@font-face" not in text, f"{path.name} が @font-face を使っている"
        assert "google-fonts" not in text, f"{path.name} が google-fonts を使っている"


def _number_constant(path: Path, name: str) -> float:
    """`const NAME = 123;` の数値を取り出す。

    TS 側の定数を Python から読む。**TS のユニットテストランナーが無い**
    （`remotion/package.json` には typecheck しか無い）ため、算術の不変条件を
    検査する手段がこれしかない。`test_deploy_workflow.py` /
    `test_container_image.py` が「ファイルの中身を検査する」型で置かれているのと
    同じ考え方。
    """
    text = _strip_comments(path.read_text(encoding="utf-8"))
    match = re.search(rf"^const {name} = ([0-9.]+);", text, re.MULTILINE)
    assert match is not None, f"{path.name} に const {name} が見つからない"
    return float(match.group(1))


def test_four_subtitle_lines_fit_in_the_zone_at_base_size() -> None:
    """字幕の4行が、縮小に頼らず基準サイズでゾーンに収まること。

    **`fitSubtitleSize` の存在は、この不変条件の代わりにはならない。**
    自動縮小があるので、パディングを食い潰しても「文字が切れる」症状は
    出ず、代わりに**文字が小さくなる**。実際に確かめた: 旧ジオメトリ
    （上160 / 下96 → テキストに使える高さ94px）に戻すと、切れの検査
    （`test_remotion_render_slow.py`）は**通ったまま**フォントが
    46px → 31px に落ちた。「切れていない」だけを見張ると、読めない
    大きさへ静かに退化する経路が残る。

    ここで4行を要求する根拠は、`FormatSpec.segment_char_cap`（short で
    48文字）が3〜4行に対応するため。3行しか収まらない設定に戻すと、
    上限どおりの台本で字幕が縮む。
    """
    subtitle = REMOTION_SRC / "Subtitle.tsx"
    base = _number_constant(subtitle, "BASE_SIZE")
    line_height = _number_constant(subtitle, "LINE_HEIGHT")
    padding_top = _number_constant(subtitle, "PADDING_TOP")
    padding_bottom = _number_constant(subtitle, "PADDING_BOTTOM")

    # 字幕ゾーンの高さ。`zones.ts` の `subtitle` は 350/1920（縦画面）。
    # TS 側と共有する仕組みが無いので値を写している（`zones.ts` を触ったら
    # ここも直す。`ILLUSTRATION_SIZE` と同じ構造の重複）。
    zone_height = 350
    available = zone_height - padding_top - padding_bottom
    needed = 4 * base * line_height
    assert needed <= available, (
        f"基準サイズ{base}px・行送り{line_height}で4行（{needed}px）が"
        f"字幕ゾーンの使える高さ（{available}px）に収まらない。"
        "自動縮小が働くので切れはしないが、字幕が小さくなる"
    )
