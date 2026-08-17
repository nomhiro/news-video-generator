"""概算コストと上限判定。"""

import pytest

from src.social.cost import estimate_month_cost, is_over_budget


def test_リンク付きは13倍で数える():
    """$0.015 と $0.20 の差を無視すると、上限が意味を失う。"""
    cost = estimate_month_cost(plain=200, with_link=30, unit=0.015, unit_with_link=0.20)

    assert cost == 200 * 0.015 + 30 * 0.20


def test_上限を超えたら_True():
    assert is_over_budget(spent=20.5, budget=20.0) is True


def test_上限ちょうどは_超えていない():
    assert is_over_budget(spent=20.0, budget=20.0) is False


def test_読み取りコストも数える():
    """計測は投稿ごとに2回読む。数えないと上限が実支出の一部しか見ない。

    実請求と突き合わせて気付いた欠陥（2026-08-17）。投稿1件 $0.015 に対して
    実請求は $0.02 で、差額は認証確認で叩いたユーザー読み取り $0.010 だった。
    概算は投稿の件数だけを見ていたので、この種の支出が丸ごと落ちていた。
    """
    posts_only = estimate_month_cost(plain=200, with_link=30, unit=0.015, unit_with_link=0.20)
    with_reads = estimate_month_cost(
        plain=200, with_link=30, unit=0.015, unit_with_link=0.20, unit_read=0.005
    )

    # 投稿230件 × 2回 × $0.005 = $2.30 が上乗せされる
    assert with_reads == pytest.approx(posts_only + 230 * 2 * 0.005)
    # 読み取りを数えないと実支出の6割程度しか見ないことになる
    assert posts_only / with_reads < 0.8


def test_読み取り単価の既定は0で後方互換():
    """読み取りを行わない呼び出し元の挙動を変えない。"""
    assert estimate_month_cost(plain=10, with_link=0, unit=0.015, unit_with_link=0.20) == (
        estimate_month_cost(plain=10, with_link=0, unit=0.015, unit_with_link=0.20, unit_read=0.0)
    )
