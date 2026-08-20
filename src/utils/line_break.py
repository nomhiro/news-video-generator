"""日本語の折り返し位置を CSS に渡すための前処理。

なぜ Python で解くか
--------------------
`VideoComposer` のタイミング解決と同じ理由。テキストの扱いを Python /
TypeScript の2言語に割ると、直すのは片方だけになりがちで、もう片方は
気付かれずに壊れたまま残る。フレーズ分割はテキストの意味に依存する処理
なので、レイアウトを描く TSX 側ではなく、台本を扱う Python 側に置く。

なぜ ZWSP（U+200B）で `<wbr>` ではないか
----------------------------------------
`text_overlays` / `segment_narrations` は props の JSON を経由して
Remotion（React）に渡る、ただの文字列。`<wbr>` は JSX の要素なので、
文字列のままでは効かず、React 側でテキストをトークンに割って
`<wbr>` を挟む変換が要る（タグと地の文が混在するので escape も要る）。
ZWSP は文字なので、JSON の文字列としてそのまま生き残り、TSX 側の
変更が要らない。

CSS 側の前提（remotion/src/Subtitle.tsx / scenes/Headline.tsx）
-----------------------------------------------------------------
`word-break: keep-all` と組みで使う。日本語は既定でほぼ任意の文字間で
折れるため、`keep-all` だけでは「良い位置」を選べず、ZWSP だけでは
「悪い位置」を禁止できない。両方が要る。
"""

from __future__ import annotations

import budoux

# ゼロ幅スペース。改行してよい位置にだけ入れる不可視文字。
# TSX 側の文字数計算（見出しの縮小率）もこの定数を基準に除外するので、
# 値を変えるならレンダラ側の対応する処理も見直す。
ZWSP = "​"

# BudouX のモデルは同梱データを読み込むだけで、API 呼び出しは無い。
# セグメントごとに呼ばれるループの中で毎回読み込むのは無駄なので、
# モジュールレベルで1回だけ生成する。
_PARSER = budoux.load_default_japanese_parser()


def insert_break_opportunities(text: str, language: str) -> str:
    """日本語の文字列にフレーズ境界の ZWSP を挿入する。

    英語はスペースで正しく折り返せる（`word-break: keep-all` はスペースでの
    折り返しを妨げない）ため、素通しする。

    Args:
        text: 見出し・字幕の原文
        language: `Script.language`（"ja" / "en"）

    Returns:
        str: 日本語ならフレーズ境界に ZWSP を挿入した文字列。
            英語や空文字列は元の文字列そのまま
    """
    if language != "ja" or not text:
        return text
    return ZWSP.join(_PARSER.parse(text))
