"""生成した投稿文の数値が、記事本文に根拠を持つかを検証する。

なぜ機械的に検証するか
----------------------
投稿は完全自動で公開される。人が読む工程が無いので、捏造を止める手段は
コードしかない。数値は捏造されると最も害が大きく（誤った統計が拡散する）、
かつ機械的に検証できる唯一の要素なので、ここだけは必ず突き合わせる。

固有名詞は同じ方法では検証できない（記事の言い換えを許す必要がある）。
そちらは「モデルに URL と媒体名を渡さない」ことで防いでいる。
"""

from __future__ import annotations

import re
import unicodedata

# 数値の抽出。桁区切りと小数を含む。
_NUMBER_PATTERN = re.compile(r"\d[\d,\.]*")

# 投稿の構成を表す数え上げ。記事の数値ではないので検証から外す。
#
# 「ポイントは3つ」「1つ目は」といった書き方は投稿として自然だが、
# 記事本文には現れない。除外しないと、まともな投稿が毎回破棄される。
_ENUMERATION_SUFFIXES = ("つ", "つ目", "点", "点目", "番目", "個", "回", "度目")


def _normalize(value: str) -> str:
    """比較用に桁区切りと末尾のドットを落とす。

    記事が「1,200」、投稿が「1200」と書くのは正常な言い換えなので、
    そのままでは一致しない。
    """
    return value.replace(",", "").rstrip(".")


def _nfkc(value: str) -> str:
    """全角の数字・記号を半角に畳む。

    `\\d` は全角数字（`１`）にも一致するため、畳まないと記事の「10億」と
    投稿の「１０億」が別の数値として扱われ、**まともな投稿が
    「根拠が無い」と判定されて破棄される**（再生成1回で通らなければ
    その記事の投稿を諦める設計なので、丸ごと失う）。

    桁区切りも畳む必要がある。`_NUMBER_PATTERN` の区切り文字は半角の
    `,` と `.` だけなので、全角の `，` があると数値が途中で切れる
    （`１，２００` の一致が `1` だけになる）。
    """
    return unicodedata.normalize("NFKC", value)


def ungrounded_numbers(text: str, source: str) -> set[str]:
    """記事本文に根拠が無い数値を返す。

    Args:
        text: 生成した投稿文
        source: 記事本文（タイトルを含めてよい）

    Returns:
        set[str]: 根拠の無い数値（正規化済み）。空なら合格
    """
    # 数値を抜く前に全角を畳む（理由は `_nfkc`）。
    text = _nfkc(text)
    source_numbers = {_normalize(m) for m in _NUMBER_PATTERN.findall(_nfkc(source))}

    ungrounded: set[str] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        raw = match.group()
        # 数え上げ表現は投稿の構成なので見ない
        tail = text[match.end() : match.end() + 3]
        if any(tail.startswith(suffix) for suffix in _ENUMERATION_SUFFIXES):
            continue
        normalized = _normalize(raw)
        if normalized not in source_numbers:
            ungrounded.add(normalized)
    return ungrounded
