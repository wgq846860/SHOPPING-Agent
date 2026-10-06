"""Lock down the price-priority clause used by the S5.3 walkthrough."""
import pytest

from backend.agent.intent_parser import parse_intent


@pytest.mark.parametrize("clause", ["价格最重要", "价格 最重要"])
def test_walkthrough_price_priority_is_captured(clause):
    intent = parse_intent(
        f"800元以内，买耳机，必须无线和主动降噪，{clause}。",
        mode="rule_based_demo",
    )
    assert intent.preferences.price > 0
    assert intent.preferences.price > max(intent.preferences.battery, intent.preferences.comfort)
    assert intent.budget.max_cents == 80000
    assert intent.hard_constraints.wireless is True
    assert intent.hard_constraints.active_noise_cancelling is True


@pytest.mark.parametrize("clause", ["价格不是最重要", "并非价格最重要", "价格不是第一考虑"])
def test_negated_price_priority_is_not_added(clause):
    intent = parse_intent(f"800元以内，买耳机，{clause}。", mode="rule_based_demo")
    assert intent.preferences.price == 0
