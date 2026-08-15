"""概算コストと上限判定。"""

from src.social.cost import estimate_month_cost, is_over_budget


def test_リンク付きは13倍で数える():
    """$0.015 と $0.20 の差を無視すると、上限が意味を失う。"""
    cost = estimate_month_cost(plain=200, with_link=30, unit=0.015, unit_with_link=0.20)

    assert cost == 200 * 0.015 + 30 * 0.20


def test_上限を超えたら_True():
    assert is_over_budget(spent=20.5, budget=20.0) is True


def test_上限ちょうどは_超えていない():
    assert is_over_budget(spent=20.0, budget=20.0) is False
