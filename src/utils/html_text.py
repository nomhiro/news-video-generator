"""HTML 断片を人が読める平文に落とす。

RSS の `summary` / `description` は **HTML を入れてよい仕様**で、実際に
入ってくる。note.com のフィードは本文の先頭を要素ごと渡してくるため、
素で画面に出すと（テンプレートは正しくエスケープするので）タグが文字として
見える。実測で出ていたもの:

    <br /><h2 id="dc8acd71-3043-4e7e-971c-a7295764839d" name="dc8acd71-...

要約の枠が UUID で埋まって、記事の内容が1文字も読めなかった。

**依存は増やさない。** BeautifulSoup / lxml を入れるほどの仕事ではない
（相手は数百文字の断片で、抽出したいのは「タグの外にある文字」だけ）。
記事本文の抽出は別の仕事で、そちらは trafilatura が担う。
"""

from __future__ import annotations

import html
import re

# script / style は「タグを消す」だけでは中身が本文として残る。
# 要素ごと落とす必要があるので、タグの除去より先に処理する。
_BLOCK_ELEMENTS = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

# 段落・改行の区切り。ここだけは空白1つに置き換える。
# 単に消すと `<p>A</p><p>B</p>` が「AB」になって語が繋がる。
_BREAKS = re.compile(r"</(p|div|li|h[1-6]|tr)\s*>|<(br|hr)\b[^>]*/?>", re.IGNORECASE)

_TAGS = re.compile(r"<[^>]*>")

_WHITESPACE = re.compile(r"\s+")

# 中身が実質空かどうかの判定に使う。記号だけの要約（区切り線の名残など）は
# 「要約なし」として扱いたいので、判定は文字・数字の有無で行う。
_MEANINGFUL = re.compile(r"[^\W_]", re.UNICODE)


def strip_html(text: str) -> str:
    """HTML 断片から平文を取り出す。

    処理の順序に意味がある。

    1. script / style を**要素ごと**落とす（中身が本文として残るため）
    2. 段落・改行の終端を空白に置き換える（消すと語が繋がる）
    3. 残ったタグを落とす
    4. **そのあとで**実体参照を解く

    4 を先にやってはいけない。`&lt;b&gt;` のように「文字としての山括弧」を
    書いている要約が、解いた直後にタグとして除去される経路ができる
    （フィードの文章に山括弧が出るのは珍しくない）。

    Args:
        text: HTML を含みうる文字列

    Returns:
        str: 平文。文字と数字が1つも残らなければ空文字
    """
    if not text:
        return ""

    without_blocks = _BLOCK_ELEMENTS.sub(" ", text)
    with_breaks = _BREAKS.sub(" ", without_blocks)
    without_tags = _TAGS.sub("", with_breaks)
    unescaped = html.unescape(without_tags)

    # 実体参照を解いた結果に改行や連続空白が現れる（&#10; や &nbsp;）ので、
    # 空白の畳み込みは unescape の後に行う。
    collapsed = _WHITESPACE.sub(" ", unescaped).strip()

    if not _MEANINGFUL.search(collapsed):
        return ""
    return collapsed
