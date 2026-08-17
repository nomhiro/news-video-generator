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
        for path in REMOTION_SRC.rglob("*.tsx")
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
