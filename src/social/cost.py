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
    """

    monthly_usd: float
    unit_usd: float
    unit_with_link_usd: float


def estimate_month_cost(plain: int, with_link: int, unit: float, unit_with_link: float) -> float:
    """当月の概算コストを返す。

    リンク付きを分けて数えるのは、単価が $0.015 と $0.20 で13倍違うため。
    混ぜて数えると上限判定が意味を失う。

    Args:
        plain: リンクを含まない投稿の件数
        with_link: リンクを含む投稿の件数
        unit: リンク無しの単価
        unit_with_link: リンク有りの単価

    Returns:
        float: 概算コスト（USD）
    """
    return plain * unit + with_link * unit_with_link


def is_over_budget(spent: float, budget: float) -> bool:
    """上限を超えているか。

    Args:
        spent: 概算の使用額
        budget: 上限

    Returns:
        bool: 超えていれば True
    """
    return spent > budget
