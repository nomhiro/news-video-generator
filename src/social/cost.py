"""投稿の概算コスト。

単価をコードに埋めない理由: X の料金は 2026-02 に体系ごと変わった。
設定に出しておけば、改定時にデプロイだけで追随できる。
"""

from __future__ import annotations


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
