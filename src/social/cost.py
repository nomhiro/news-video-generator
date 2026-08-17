"""投稿の概算コスト。

単価をコードに埋めない理由: X の料金は 2026-02 に体系ごと変わった。
設定に出しておけば、改定時にデプロイだけで追随できる。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostBudget:
    """上限と単価の組。

    3つの数値を個別の引数で持ち回すのをやめた理由: 単価だけ既定値を
    与えると「料金をコードに埋めない」という方針が崩れる（既定値は
    改定されても誰も直さない）。1つの値オブジェクトにすれば、
    「上限判定をするかしないか」を `None` で表せて、単価に既定値を
    置く必要が無くなる。

    Attributes:
        monthly_usd: 当月の概算コストの上限
        unit_usd: リンク無しの単価
        unit_with_link_usd: リンク有りの単価（13倍違う）
        unit_read_usd: 投稿1件の読み取り単価（計測が投稿ごとに2回読む）
    """

    monthly_usd: float
    unit_usd: float
    unit_with_link_usd: float
    unit_read_usd: float = 0.0


# 1投稿あたりの指標読み取り回数。
#
# `MEASUREMENT_OFFSETS`（24時間後・7日後）で固定なので、実測を持ち回らずに
# 投稿数から導ける。読み取りは同一リソースなら24時間 UTC の窓で重複排除されるが、
# この2回は窓が重ならないので素直に2回分かかる。
READS_PER_POST = 2


def estimate_month_cost(
    plain: int,
    with_link: int,
    unit: float,
    unit_with_link: float,
    unit_read: float = 0.0,
) -> float:
    """当月の概算コストを返す。

    リンク付きを分けて数えるのは、単価が $0.015 と $0.20 で13倍違うため。
    混ぜて数えると上限判定が意味を失う。

    **読み取りも数える。** 当初は投稿の件数だけを見ていた。実請求と突き合わせて
    気付いた欠陥で、計測が投稿ごとに2回の読み取りを行うぶん（月 $2.40 相当）が
    概算から丸ごと落ちていた。上限は支出を止めるための仕組みなので、実支出の
    6割しか見ていない概算では役に立たない。

    読み取り回数は投稿数から導く（`READS_PER_POST`）。計測の回数が設計で
    固定されているので、実測を持ち回る必要がない。

    Args:
        plain: リンクを含まない投稿の件数
        with_link: リンクを含む投稿の件数
        unit: リンク無しの単価
        unit_with_link: リンク有りの単価
        unit_read: 投稿1件の読み取り単価。既定 0.0 は「読み取りを数えない」
            （読み取りを行わない呼び出し元のための後方互換）

    Returns:
        float: 概算コスト（USD）
    """
    posts = plain + with_link
    return plain * unit + with_link * unit_with_link + posts * READS_PER_POST * unit_read


def is_over_budget(spent: float, budget: float) -> bool:
    """上限を超えているか。

    Args:
        spent: 概算の使用額
        budget: 上限

    Returns:
        bool: 超えていれば True
    """
    return spent > budget
