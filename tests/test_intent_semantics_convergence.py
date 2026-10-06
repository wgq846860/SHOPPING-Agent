"""S3.1 有限语义收敛对照：公开解析入口须忠实保留需求关系。

范围来自 s3_1_convergence_note_20261002.md；无需功能为 null，
明确禁止为 false。只回放完整结构化响应，不调用模型或原演示库。
"""

from __future__ import annotations

import json

import pytest

from backend.agent.harness_client import HarnessTurnResult
from backend.agent.intent_parser import (
    IntentParseError,
    RuleBasedDemoParser,
    extract_query_anchors,
    parse_intent_with_llm,
)
from backend.schemas.agent import ShoppingIntent


class ReplayHarness:
    """仅实现公开解析入口所需的回合响应。"""

    def __init__(self, payload: dict):
        self.response = json.dumps(payload, ensure_ascii=False)

    def run_intent_turn(self, prompt: str) -> HarnessTurnResult:
        return HarnessTurnResult(
            session_id="semantics-convergence",
            final_response=self.response,
            finish_reason=None,
        )


def full_payload(*, budget_cents=None, hard=None, preferences=None, clarifications=None):
    """全键协议；测试中的删除需求用 null 表示，绝不省略 JSON 键。"""
    payload = ShoppingIntent(category="headphones").model_dump(mode="json")
    payload["budget"]["max_cents"] = budget_cents
    payload["hard_constraints"].update(hard or {})
    payload["preferences"].update(preferences or {})
    payload["clarifications"] = clarifications or []
    return payload


def replay(query, payload):
    return parse_intent_with_llm(query, ReplayHarness(payload))


ANC_CASES = [
    pytest.param("无降噪耳机", False, id="without-anc"),
    pytest.param("不带降噪耳机", False, id="no-anc-equipped"),
    pytest.param("不要主动降噪耳机", False, id="reject-anc"),
    pytest.param("必须主动降噪耳机", True, id="require-anc"),
    pytest.param("无需降噪耳机", None, id="anc-not-required"),
    pytest.param("降噪无所谓，选耳机", None, id="anc-indifferent"),
]


@pytest.mark.parametrize("query, expected", ANC_CASES)
def test_anc_three_states_in_query_anchors(query, expected):
    assert extract_query_anchors(query).anc is expected


@pytest.mark.parametrize("query, expected", ANC_CASES)
def test_anc_three_states_in_rule_mode(query, expected):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.hard_constraints.active_noise_cancelling is expected
    assert intent.clarifications == []


@pytest.mark.parametrize("query, expected", ANC_CASES)
def test_anc_faithful_complete_llm_response_is_accepted(query, expected):
    intent = replay(query, full_payload(hard={"active_noise_cancelling": expected}))
    assert intent.hard_constraints.active_noise_cancelling is expected
    assert intent.clarifications == []


def test_anc_not_required_rejects_complete_llm_response_that_forces_anc():
    with pytest.raises(IntentParseError) as excinfo:
        replay("无需降噪耳机", full_payload(hard={"active_noise_cancelling": True}))
    assert excinfo.value.api_status == 502
    assert any(
        d["field"] == "hard_constraints.active_noise_cancelling"
        for d in excinfo.value.details
    )


SOFT_COORDINATED_CASES = [
    pytest.param("最好无线和主动降噪的耳机", id="prefer-both"),
    pytest.param("尽量无线和主动降噪的耳机", id="try-both"),
    pytest.param("耳机，无线和主动降噪都尽量。", id="postfix-try-both"),
]


@pytest.mark.parametrize("query", SOFT_COORDINATED_CASES)
def test_coordinated_soft_features_have_no_hard_anchors(query):
    anchors = extract_query_anchors(query)
    assert anchors.wireless is None
    assert anchors.anc is None


@pytest.mark.parametrize("query", SOFT_COORDINATED_CASES)
def test_coordinated_soft_features_remain_preferences_in_rule_mode(query):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.hard_constraints.wireless is None
    assert intent.hard_constraints.active_noise_cancelling is None
    assert intent.preferences.wireless > 0
    assert intent.preferences.anc > 0
    assert intent.clarifications == []


@pytest.mark.parametrize("query", SOFT_COORDINATED_CASES)
def test_coordinated_soft_features_faithful_llm_response_is_accepted(query):
    intent = replay(query, full_payload(preferences={"wireless": 0.6, "anc": 0.6}))
    assert intent.hard_constraints.wireless is None
    assert intent.hard_constraints.active_noise_cancelling is None
    assert intent.preferences.wireless > 0 and intent.preferences.anc > 0


def test_coordinated_soft_features_reject_complete_llm_response_that_hardens_both():
    payload = full_payload(
        hard={"wireless": True, "active_noise_cancelling": True},
        preferences={"wireless": 0.6, "anc": 0.6},
    )
    with pytest.raises(IntentParseError) as excinfo:
        replay("最好无线和主动降噪的耳机", payload)
    assert excinfo.value.api_status == 502
    assert {d["field"] for d in excinfo.value.details} >= {
        "hard_constraints.wireless", "hard_constraints.active_noise_cancelling",
    }


HARD_THEN_SOFT_QUERY = "800元以内，必须无线和主动降噪并尽量轻"


def test_later_soft_weight_does_not_disable_required_features():
    anchors = extract_query_anchors(HARD_THEN_SOFT_QUERY)
    assert anchors.budget_cents == 80000
    assert anchors.wireless is True and anchors.anc is True
    intent = RuleBasedDemoParser().parse(HARD_THEN_SOFT_QUERY)
    assert intent.budget.max_cents == 80000
    assert intent.hard_constraints.wireless is True
    assert intent.hard_constraints.active_noise_cancelling is True
    assert intent.hard_constraints.weight_requirement is None
    assert intent.preferences.weight > 0


def test_required_features_with_later_soft_weight_accept_faithful_llm_response():
    intent = replay(HARD_THEN_SOFT_QUERY, full_payload(
        budget_cents=80000,
        hard={"wireless": True, "active_noise_cancelling": True},
        preferences={"weight": 0.6},
    ))
    assert intent.hard_constraints.wireless is True
    assert intent.hard_constraints.active_noise_cancelling is True
    assert intent.preferences.weight > 0


def test_complete_llm_response_cannot_delete_required_features():
    payload = full_payload(budget_cents=80000, preferences={"weight": 0.6})
    with pytest.raises(IntentParseError) as excinfo:
        replay(HARD_THEN_SOFT_QUERY, payload)
    assert excinfo.value.api_status == 502
    assert {detail["field"] for detail in excinfo.value.details} >= {
        "hard_constraints.wireless", "hard_constraints.active_noise_cancelling",
    }


CNY_REJECTED_FOREIGN_CASES = [
    pytest.param("人民币预算800元，不要美元报价，必须无线主动降噪耳机", id="comma-prefix-negation"),
    pytest.param("人民币预算800元，美元报价不要，必须无线主动降噪耳机", id="comma-suffix-negation"),
    pytest.param("人民币预算800元且不要美元报价，必须无线主动降噪耳机", id="and-prefix-negation"),
    pytest.param("人民币预算800元但美元报价不要，必须无线主动降噪耳机", id="but-suffix-negation"),
]


@pytest.mark.parametrize("query", CNY_REJECTED_FOREIGN_CASES)
def test_rejected_foreign_quote_does_not_change_cny_budget_anchors(query):
    anchors = extract_query_anchors(query)
    assert anchors.budget_cents == 80000
    assert anchors.foreign_currency is False


@pytest.mark.parametrize("query", CNY_REJECTED_FOREIGN_CASES)
def test_rejected_foreign_quote_keeps_cny_budget_in_rule_mode(query):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.budget.max_cents == 80000
    assert intent.hard_constraints.wireless is True
    assert intent.hard_constraints.active_noise_cancelling is True
    assert intent.clarifications == []


@pytest.mark.parametrize("query", CNY_REJECTED_FOREIGN_CASES)
def test_rejected_foreign_quote_accepts_faithful_complete_llm_response(query):
    intent = replay(query, full_payload(
        budget_cents=80000,
        hard={"wireless": True, "active_noise_cancelling": True},
    ))
    assert intent.budget.max_cents == 80000
    assert intent.clarifications == []


FOREIGN_BUDGET_CASES = [
    pytest.param("$100以内买耳机", id="dollar-prefix"),
    pytest.param("预算USD100买耳机", id="usd-prefix"),
    pytest.param("预算100美元买耳机", id="dollar-suffix"),
]


@pytest.mark.parametrize("query", FOREIGN_BUDGET_CASES)
def test_real_foreign_budget_has_no_cny_amount_anchor(query):
    anchors = extract_query_anchors(query)
    assert anchors.foreign_currency is True
    assert anchors.budget_cents is None


@pytest.mark.parametrize("query", FOREIGN_BUDGET_CASES)
def test_real_foreign_budget_requires_clarification_in_rule_mode(query):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.budget.max_cents is None
    assert any(c.field == "budget.currency" for c in intent.clarifications)


@pytest.mark.parametrize("query", FOREIGN_BUDGET_CASES)
def test_real_foreign_budget_accepts_llm_clarification(query):
    intent = replay(query, full_payload(clarifications=[{
        "field": "budget.currency",
        "reason": "unsupported_requirement",
        "question": "本演示仅支持人民币预算，请提供人民币金额。",
    }]))
    assert intent.budget.max_cents is None
    assert any(c.field == "budget.currency" for c in intent.clarifications)


@pytest.mark.parametrize("query", FOREIGN_BUDGET_CASES)
def test_real_foreign_budget_rejects_unclarified_cny_guess(query):
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, full_payload(budget_cents=10000))
    assert excinfo.value.api_status == 502
    assert any(d["field"] == "budget.currency" for d in excinfo.value.details)


@pytest.mark.parametrize("query, basis, max_grams", [
    pytest.param("头戴式耳机整副300克以下", "headset", 300, id="headset-whole-unit"),
    pytest.param("真无线耳塞整副10克以下", "pair", 10, id="earbuds-whole-unit"),
    pytest.param("不要头戴耳机，真无线耳塞整副10克以下", "pair", 10, id="excluded-headset"),
])
def test_whole_unit_weight_basis_uses_selected_category(query, basis, max_grams):
    intent = RuleBasedDemoParser().parse(query)
    weight = intent.hard_constraints.weight_requirement
    assert weight is not None
    assert weight.basis == basis
    assert weight.max_grams == max_grams
    assert intent.clarifications == []
    if query.startswith("不要头戴"):
        assert intent.hard_constraints.excluded_form_factors == ["over_ear"]
        assert intent.hard_constraints.allowed_form_factors == ["in_ear"]


def test_explicit_weight_without_basis_requests_clarification():
    intent = RuleBasedDemoParser().parse("耳机10克以下")
    assert intent.hard_constraints.weight_requirement is None
    assert any(
        c.field == "hard_constraints.weight_requirement" and c.reason == "missing_basis"
        for c in intent.clarifications
    )


def test_complete_llm_weight_basis_clarification_is_accepted():
    intent = replay("耳机10克以下", full_payload(clarifications=[{
        "field": "hard_constraints.weight_requirement",
        "reason": "missing_basis",
        "question": "10克上限指单只、双只、整副头戴，还是含充电盒重量？",
    }]))
    assert intent.hard_constraints.weight_requirement is None
    assert any(c.reason == "missing_basis" for c in intent.clarifications)


def test_whole_unit_weight_without_confirmed_category_requests_basis_clarification():
    intent = RuleBasedDemoParser().parse("耳机整副10克以下")
    assert intent.hard_constraints.weight_requirement is None
    assert any(
        c.field == "hard_constraints.weight_requirement" and c.reason == "missing_basis"
        for c in intent.clarifications
    )


def test_explicit_required_wired_and_wireless_features_request_conflict_clarification():
    intent = RuleBasedDemoParser().parse("必须无线且必须有线耳机")
    assert any(c.reason == "conflicting_constraints" for c in intent.clarifications)


def test_conflicting_explicit_budget_amounts_are_not_silently_selected():
    intent = RuleBasedDemoParser().parse("预算800元且预算1000元买耳机")
    assert intent.budget.max_cents is None
    assert any(
        c.field.startswith("budget") and c.reason == "conflicting_constraints"
        for c in intent.clarifications
    )


def test_weight_number_does_not_hide_same_clause_cny_budget():
    query = "头戴耳机重量不超过250克且预算800元"
    assert extract_query_anchors(query).budget_cents == 80000
    intent = RuleBasedDemoParser().parse(query)
    assert intent.budget.max_cents == 80000
    weight = intent.hard_constraints.weight_requirement
    assert weight is not None
    assert weight.basis == "headset"
    assert weight.max_grams == 250
    assert intent.clarifications == []


@pytest.mark.parametrize("query, wrong_value", [
    ("耳机，不接受语音通话功能。", True),
    ("耳机，不要麦克风。", True),
    ("耳机，无需麦克风。", False),
    ("耳机，麦克风无所谓。", True),
    ("耳机，尽量带麦克风。", True),
    ("耳机，最好能语音。", False),
])
def test_llm_cannot_reverse_or_harden_microphone_wording(query, wrong_value):
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, full_payload(hard={"microphone": wrong_value}))
    assert excinfo.value.api_status == 502
    assert any(
        detail["field"] == "hard_constraints.microphone"
        for detail in excinfo.value.details
    )


@pytest.mark.parametrize("query, wrong_sizes", [
    ("耳机，不接受附带小号耳塞套的方案。", ["S"]),
    ("耳机，必须附带中号耳塞套，但小号不要。", ["M", "S"]),
    ("耳机，尽量附带小号耳塞套。", ["S"]),
    ("耳机，小号耳塞套不是必须。", ["S"]),
])
def test_llm_cannot_turn_excluded_optional_or_soft_tip_sizes_into_requirements(
    query, wrong_sizes
):
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, full_payload(hard={"ear_tip_sizes": wrong_sizes}))
    assert excinfo.value.api_status == 502
    assert any(
        detail["field"] == "hard_constraints.ear_tip_sizes"
        for detail in excinfo.value.details
    )


@pytest.mark.parametrize("query, expected", [
    ("耳机，必须带麦克风。", True),
    ("耳机，不接受语音通话功能。", False),
    ("耳机，无需麦克风。", None),
    ("耳机，最好能语音。", None),
])
def test_faithful_microphone_llm_output_is_accepted(query, expected):
    intent = replay(query, full_payload(hard={"microphone": expected}))
    assert intent.hard_constraints.microphone is expected
    assert intent.clarifications == []


@pytest.mark.parametrize("query, expected, wrong_device_mic", [
    ("PS5耳机，必须能语音。", True, False),
    ("PS5耳机，不要麦克风。", False, True),
    ("PS5耳机，无需麦克风。", None, True),
    ("PS5耳机，尽量带麦克风。", None, True),
])
def test_llm_device_microphone_requirement_cannot_reverse_source(
    query, expected, wrong_device_mic
):
    device = {"platform": "ps5", "device_key": None, "transport": None,
              "playback_required": True, "microphone_required": wrong_device_mic}
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, full_payload(hard={"microphone": expected, "device_requirements": [device]}))
    assert any(d["field"] == "hard_constraints.device_requirements"
               and d["reason"] == "anchor_violation" for d in excinfo.value.details)
    device["microphone_required"] = expected is True
    intent = replay(query, full_payload(hard={"microphone": expected, "device_requirements": [device]}))
    assert intent.hard_constraints.device_requirements[0].microphone_required is (expected is True)


@pytest.mark.parametrize("query, sizes, clarification_reason", [
    ("耳机，必须附带小号耳塞套。", ["S"], None),
    ("耳机，不接受附带小号耳塞套的方案。", None, "unsupported_requirement"),
    ("耳机，尽量附带小号耳塞套。", None, "unsupported_requirement"),
    ("耳机，中号耳塞套是必须，小号不要。", ["M"], "unsupported_requirement"),
    ("耳机，小号耳塞套不是必须。", None, None),
])
def test_faithful_tip_requirements_and_clarification_are_accepted(query, sizes, clarification_reason):
    clarifications = ([{"field": "hard_constraints.ear_tip_sizes", "reason": clarification_reason,
                        "question": "请确认必须的耳塞套尺寸。"}] if clarification_reason else [])
    intent = replay(query, full_payload(hard={"ear_tip_sizes": sizes}, clarifications=clarifications))
    assert intent.hard_constraints.ear_tip_sizes == sizes
    assert len(intent.clarifications) == len(clarifications)


@pytest.mark.parametrize("query, wireless, microphone", [
    ("最好无线和带麦克风耳机", None, None),
    ("必须无线和带麦克风耳机，尽量轻", True, True),
    ("不要附带小号耳塞套，但必须带麦克风耳机", None, True),
])
def test_conjoined_requirements_do_not_cross_contaminate(query, wireless, microphone):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.hard_constraints.wireless is wireless
    assert intent.hard_constraints.microphone is microphone
    if "不要附带" in query:
        assert intent.hard_constraints.ear_tip_sizes is None
        assert any(c.field == "hard_constraints.ear_tip_sizes" and c.reason == "unsupported_requirement"
                   for c in intent.clarifications)
    else:
        assert intent.clarifications == []


@pytest.mark.parametrize("query", ["耳机ENC降噪", "耳机mic降噪", "耳机降噪ENC", "耳机降噪mic"])
def test_call_noise_reduction_does_not_become_anc_requirement(query):
    assert RuleBasedDemoParser().parse(query).hard_constraints.active_noise_cancelling is None


@pytest.mark.parametrize("description, expected_key, reason", [
    ("电脑有3.5mm接口", None, "missing_device"),
    ("电脑有分离的3.5mm耳机输出口和麦克风输入口", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm TRRS复合接口", "通用PC（3.5mm TRRS复合口）", None),
    ("电脑有3.5mm接口，尽量是TRRS复合接口", None, "unsupported_requirement"),
    ("电脑有3.5mm接口，最好TRRS复合接口", None, "unsupported_requirement"),
    ("电脑有3.5mm接口，不接受TRRS复合接口，必须为分离接口", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm接口，耳机使用TRRS接口", None, "missing_device"),
    ("电脑有3.5mm接口，卖家说耳机是TRRS", None, "missing_device"),
    ("电脑有3.5mm接口而耳机使用TRRS接口", None, "missing_device"),
    ("我的电脑有3.5mm接口但卖家说耳机是TRRS", None, "missing_device"),
    ("耳机使用TRRS接口而电脑有3.5mm接口", None, "missing_device"),
    ("耳机使用TRRS接口但电脑有分离的3.5mm耳机输出口和麦克风输入口", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm TRRS复合接口而耳机使用TRRS接口", "通用PC（3.5mm TRRS复合口）", None),
])
def test_llm_pc_interface_cannot_be_invented_or_changed(description, expected_key, reason):
    query = f"100元以内耳机，{description}，必须有线、带麦、无需充电。"
    device = {"platform": "windows", "device_key": expected_key, "transport": "wired_3_5mm",
              "playback_required": True, "microphone_required": True}
    clarifications = ([{"field": "hard_constraints.device_requirements", "reason": reason,
                        "question": "请确认电脑口型。"}] if reason else [])
    faithful = full_payload(budget_cents=10000, hard={"wireless": False, "microphone": True,
                          "requires_power": False, "device_requirements": [device]}, clarifications=clarifications)
    intent = replay(query, faithful)
    assert intent.hard_constraints.device_requirements[0].device_key == expected_key
    device["device_key"] = "通用PC（3.5mm TRRS复合口）" if expected_key != "通用PC（3.5mm TRRS复合口）" else None
    wrong = full_payload(budget_cents=10000, hard={"wireless": False, "microphone": True,
                         "requires_power": False, "device_requirements": [device]}, clarifications=clarifications)
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, wrong)
    assert any(d["field"] == "hard_constraints.device_requirements" for d in excinfo.value.details)


def test_llm_unknown_pc_interface_requires_specific_clarification():
    device = {"platform": "windows", "device_key": None, "transport": "wired_3_5mm",
              "playback_required": True, "microphone_required": False}
    with pytest.raises(IntentParseError):
        replay("电脑有3.5mm接口，买耳机", full_payload(hard={"device_requirements": [device]}))


@pytest.mark.parametrize("query", ["耳机，无线和带麦都尽量。", "耳机，无线和带麦克风功能都尽量有。"])
def test_postfix_soft_wireless_and_microphone_remain_preferences(query):
    intent = RuleBasedDemoParser().parse(query)
    assert intent.hard_constraints.wireless is None
    assert intent.hard_constraints.microphone is None
    assert intent.preferences.wireless > 0 and intent.preferences.call > 0
    faithful = full_payload(preferences={"wireless": 0.6, "call": 0.6})
    assert replay(query, faithful).clarifications == []
    with pytest.raises(IntentParseError) as excinfo:
        replay(query, full_payload(hard={"wireless": True, "microphone": True}))
    assert {d["field"] for d in excinfo.value.details} >= {
        "hard_constraints.wireless", "hard_constraints.microphone",
    }


@pytest.mark.parametrize("query", [
    "500元以内耳机，小号收纳盒不要，耳塞套必须中号。",
    "500元以内耳机，耳塞套必须中号，小号收纳盒不要。",
])
def test_tip_sizes_are_bound_to_tip_noun_not_other_objects(query):
    faithful = full_payload(budget_cents=50000, hard={"ear_tip_sizes": ["M"]})
    assert replay(query, faithful).hard_constraints.ear_tip_sizes == ["M"]
    with pytest.raises(IntentParseError):
        replay(query, full_payload(budget_cents=50000, hard={"ear_tip_sizes": ["S", "M"]}))
