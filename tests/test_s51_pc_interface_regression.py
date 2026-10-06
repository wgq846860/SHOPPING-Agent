"""R3 PC主体回归：固定人工语义标签，规则/离线LLM/公开API同验。

可捕获的错误：商品或未知TRRS被当成电脑口型、明确电脑口型丢失、
排除/软/冲突被硬化，以及LLM护栏接受上述错误转译。数据库只用隔离小档。
"""
from __future__ import annotations

from datetime import datetime
import json

from fastapi.testclient import TestClient
import pytest

from backend.agent.harness_client import HarnessTurnResult
from backend.agent.intent_parser import IntentParseError, RuleBasedDemoParser, parse_intent_with_llm
from backend.agent.query_semantics import pc_35mm_interface
from backend.config import load_settings
from backend.database.db import connect, initialize_database
from backend.main import create_app
from backend.schemas.agent import ShoppingIntent
from backend.services import derive


TRRS = "通用PC（3.5mm TRRS复合口）"
SPLIT = "通用PC（分离3.5mm音频/麦克风接口）"
DEVICE_FIELD = "hard_constraints.device_requirements"

# 期望直接来自原句与契约§7.1，不读取parser结果来生成标签。
CASES = [
    pytest.param("给有3.5mm接口的电脑买备用耳机", "unspecified", None, "missing_device", id="original-sc06"),
    pytest.param("电脑有3.5mm接口，耳机使用TRRS接口", "unspecified", None, "missing_device", id="product-forward"),
    pytest.param("电脑有3.5mm接口，TRRS插头是耳机的插头", "unspecified", None, "missing_device", id="original-r3-inverted"),
    pytest.param("电脑有3.5mm接口，卖家说TRRS是耳机的接口", "unspecified", None, "missing_device", id="original-r3-seller-inverted"),
    pytest.param("电脑有3.5mm接口而TRRS插头是耳机的插头", "unspecified", None, "missing_device", id="same-clause-inverted"),
    pytest.param("电脑有3.5mm接口但卖家说TRRS是耳机的接口", "unspecified", None, "missing_device", id="same-clause-seller"),
    pytest.param("TRRS插头是耳机的插头，电脑有3.5mm接口", "unspecified", None, "missing_device", id="product-before-pc"),
    pytest.param("TRRS插头属于商品而电脑有3.5mm接口", "unspecified", None, "missing_device", id="product-belongs-before-pc"),
    pytest.param("电脑有3.5mm接口，商品参数为TRRS", "unspecified", None, "missing_device", id="product-parameters"),
    pytest.param("电脑有3.5mm接口，耳机的TRRS插头可接电脑", "unspecified", None, "missing_device", id="product-target-is-not-port-owner"),
    pytest.param("电脑有3.5mm接口，TRRS接口很方便", "unspecified", None, "missing_device", id="unknown-owner"),
    pytest.param("电脑有3.5mm接口，要TRRS插头耳机", "unspecified", None, "missing_device", id="quick-product-plug-before-headphone"),
    pytest.param("电脑有3.5mm接口，要TRRS插头的耳机", "unspecified", None, "missing_device", id="quick-product-possessive-plug"),
    pytest.param("电脑有3.5mm接口，必须要TRRS插头耳麦", "unspecified", None, "missing_device", id="quick-product-headset-plug"),
    pytest.param("电脑有3.5mm接口，要分离接口耳机", "unspecified", None, "missing_device", id="quick-product-split-plug"),
    pytest.param("电脑有3.5mm接口，要TRRS插头耳机，必须为TRRS复合口", "unspecified", None, "missing_device", id="quick-product-stops-port-continuation"),
    pytest.param("电脑有3.5mm TRRS复合接口和要TRRS插头耳机", "trrs", TRRS, None, id="quick-confirmed-pc-and-product"),
    pytest.param("电脑有分离的3.5mm耳机输出口和麦克风输入口和要TRRS插头耳机", "split", SPLIT, None, id="quick-confirmed-split-and-product"),
    pytest.param("要TRRS插头耳机和电脑有3.5mm TRRS复合接口", "trrs", TRRS, None, id="quick-product-before-confirmed-pc"),
    pytest.param("电脑有3.5mm接口，要TRRS插头耳机，但电脑接口必须为分离接口", "split", SPLIT, None, id="quick-explicit-pc-restores-owner"),
    pytest.param("电脑有3.5mm接口，不接受分离接口和TRRS复合接口", "unsupported", None, "unsupported_requirement", id="quick-negative-split-and-trrs"),
    pytest.param("电脑有3.5mm接口，不接受TRRS复合接口和分离接口", "unsupported", None, "unsupported_requirement", id="quick-negative-trrs-and-split"),
    pytest.param("电脑有3.5mm接口，不接受分离接口和3.5mm TRRS复合接口", "unsupported", None, "unsupported_requirement", id="quick-repeated-size-negative-group"),
    pytest.param("电脑有3.5mm接口，不接受3.5mm TRRS复合接口和分离接口", "unsupported", None, "unsupported_requirement", id="quick-repeated-size-negative-reverse"),
    pytest.param("电脑有3.5mm接口，不要求3.5毫米TRRS复合接口和分离接口", "unsupported", None, "unsupported_requirement", id="quick-repeated-size-optional-reverse"),
    pytest.param("电脑有3.5mm接口，尽量是分离接口和3.5mm TRRS复合接口", "unsupported", None, "unsupported_requirement", id="quick-repeated-size-soft-group"),
    pytest.param("电脑有3.5mm接口，分离接口和3.5毫米TRRS复合接口都不要", "unsupported", None, "unsupported_requirement", id="quick-repeated-size-negative-suffix"),
    pytest.param("电脑有3.5mm接口，不接受分离的3.5mm耳机输出口和麦克风输入口和TRRS复合接口", "unsupported", None, "unsupported_requirement", id="quick-negative-full-split-and-trrs"),
    pytest.param("电脑有3.5mm接口，不接受TRRS复合接口和分离3.5mm音频/麦克风接口", "unsupported", None, "unsupported_requirement", id="quick-negative-trrs-and-full-split"),
    pytest.param("电脑有3.5mm接口，分离接口和TRRS复合接口都不要", "unsupported", None, "unsupported_requirement", id="quick-negative-group-suffix"),
    pytest.param("电脑有3.5mm接口，TRRS复合接口和分离接口都不是必须", "unsupported", None, "unsupported_requirement", id="quick-optional-group-suffix"),
    pytest.param("电脑有3.5mm接口，TRRS复合接口和分离接口都尽量有", "unsupported", None, "unsupported_requirement", id="quick-soft-group-suffix"),
    pytest.param("电脑有3.5mm接口，不接受分离接口和必须为TRRS复合接口", "trrs", TRRS, None, id="quick-new-hard-cue-stops-negative"),
    pytest.param("电脑有3.5mm接口，不接受TRRS复合接口和必须为分离接口", "split", SPLIT, None, id="quick-new-hard-cue-confirms-split"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合接口和不接受分离接口", "trrs", TRRS, None, id="quick-hard-trrs-then-negative-split"),
    pytest.param("电脑有3.5mm接口，必须为分离接口和不接受TRRS复合接口", "split", SPLIT, None, id="quick-hard-split-then-negative-trrs"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合接口和尽量是分离接口", "trrs", TRRS, None, id="quick-new-soft-cue-preserves-hard"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合接口，分离接口不要", "trrs", TRRS, None, id="quick-separate-negative-suffix-preserves-hard"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合接口和分离接口", "conflicting", None, "conflicting_constraints", id="quick-hard-group-conflicts"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合接口，不接受分离接口和TRRS复合接口", "conflicting", None, "conflicting_constraints", id="quick-negative-group-conflicts-with-confirmed"),
    pytest.param("电脑有3.5mm接口，听说TRRS复合接口很好", "unspecified", None, "missing_device", id="unknown-hearsay"),
    pytest.param("电脑有3.5mm接口，耳机使用TRRS接口，必须为TRRS复合口", "unspecified", None, "missing_device", id="product-stops-pc-continuation"),
    pytest.param("电脑有3.5mm TRRS复合接口", "trrs", TRRS, None, id="pc-forward"),
    pytest.param("TRRS复合口是电脑的3.5mm接口", "trrs", TRRS, None, id="pc-inverted"),
    pytest.param("3.5mm TRRS复合口是我电脑的接口", "trrs", TRRS, None, id="pc-inverted-port-before-owner"),
    pytest.param("电脑的3.5mm接口是TRRS复合口", "trrs", TRRS, None, id="pc-possessive"),
    pytest.param("给有3.5mm TRRS复合接口的电脑买备用耳机", "trrs", TRRS, None, id="pc-attributive"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合口", "trrs", TRRS, None, id="pc-predicate-continuation"),
    pytest.param("电脑有3.5mm TRRS复合接口，分离接口是耳机的接口", "trrs", TRRS, None, id="pc-trrs-product-split"),
    pytest.param("TRRS是耳机的接口但电脑有3.5mm TRRS复合接口", "trrs", TRRS, None, id="pc-restores-owner"),
    pytest.param("电脑有分离的3.5mm耳机输出口和麦克风输入口", "split", SPLIT, None, id="pc-split"),
    pytest.param("分离接口是电脑的3.5mm音频和麦克风接口", "split", SPLIT, None, id="pc-split-inverted"),
    pytest.param("电脑有分离的3.5mm耳机输出口和麦克风输入口，TRRS插头是耳机的插头", "split", SPLIT, None, id="pc-split-product-trrs"),
    pytest.param("电脑有3.5mm接口，不接受TRRS复合接口，必须为分离接口", "split", SPLIT, None, id="excluded-trrs-confirmed-split"),
    pytest.param("电脑有3.5mm接口，不接受TRRS复合接口", "unsupported", None, "unsupported_requirement", id="negative-prefix"),
    pytest.param("电脑有3.5mm接口，TRRS复合接口不要", "unsupported", None, "unsupported_requirement", id="negative-suffix"),
    pytest.param("电脑有3.5mm接口，尽量是TRRS复合接口", "unsupported", None, "unsupported_requirement", id="soft-prefix"),
    pytest.param("电脑有3.5mm接口，TRRS复合接口尽量有", "unsupported", None, "unsupported_requirement", id="soft-suffix"),
    pytest.param("电脑有3.5mm接口，不要求TRRS复合接口", "unsupported", None, "unsupported_requirement", id="optional-prefix"),
    pytest.param("电脑有3.5mm接口，TRRS复合接口不是必须", "unsupported", None, "unsupported_requirement", id="optional-suffix"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合口且必须为分离接口", "conflicting", None, "conflicting_constraints", id="conflicting-kinds"),
    pytest.param("电脑有3.5mm接口，必须为TRRS复合口，TRRS复合口不要", "conflicting", None, "conflicting_constraints", id="conflicting-polarity"),
    pytest.param("电脑有3.5mm接口，不知道是TRRS还是分离接口", "unspecified", None, "missing_device", id="uncertain-port"),
]

for join in ("与", "及", "以及", "并", "、"):
    CASES.extend([
        pytest.param(f"电脑有3.5mm接口，不接受分离接口{join}TRRS复合接口", "unsupported", None, "unsupported_requirement", id=f"quick-negative-group-{join}"),
        pytest.param(f"电脑有3.5mm接口，不要求TRRS复合接口{join}分离接口", "unsupported", None, "unsupported_requirement", id=f"quick-optional-group-{join}"),
        pytest.param(f"电脑有3.5mm接口，尽量是分离接口{join}TRRS复合接口", "unsupported", None, "unsupported_requirement", id=f"quick-soft-group-{join}"),
    ])

for join in ("和", "与", "并"):
    CASES.extend([
        pytest.param(f"电脑有3.5mm TRRS复合接口{join}耳机使用TRRS接口", "trrs", TRRS, None, id=f"pc-and-product-forward-{join}"),
        pytest.param(f"电脑有3.5mm TRRS复合接口{join}分离接口是耳机的接口", "trrs", TRRS, None, id=f"pc-and-product-inverted-{join}"),
        pytest.param(f"TRRS是耳机的接口{join}电脑有3.5mm TRRS复合接口", "trrs", TRRS, None, id=f"product-and-pc-{join}"),
        pytest.param(f"电脑有分离的3.5mm耳机输出口和麦克风输入口{join}TRRS是耳机的接口", "split", SPLIT, None, id=f"split-and-product-{join}"),
        pytest.param(f"TRRS是耳机的接口{join}电脑有分离的3.5mm耳机输出口和麦克风输入口", "split", SPLIT, None, id=f"product-and-split-{join}"),
        pytest.param(f"电脑有3.5mm接口{join}TRRS插头是耳机的插头", "unspecified", None, "missing_device", id=f"unknown-pc-and-product-{join}"),
    ])


def query_for(description):
    return f"100元以内耳机，{description}，必须有线、带麦、无需充电。"


class ReplayHarness:
    """只替换外部LLM回合；生产JSON校验及原句护栏保持真实。"""

    def __init__(self, payload):
        self.response = json.dumps(payload, ensure_ascii=False)

    def run_intent_turn(self, prompt):
        return HarnessTurnResult(session_id="s51-pc-regression", final_response=self.response, finish_reason=None)


def faithful_payload(key, reason):
    payload = ShoppingIntent(category="headphones").model_dump(mode="json")
    payload["budget"]["max_cents"] = 10000
    payload["hard_constraints"].update({
        "wireless": False, "microphone": True, "requires_power": False,
        "device_requirements": [{
            "platform": "windows", "device_key": key, "transport": "wired_3_5mm",
            "playback_required": True, "microphone_required": True,
        }],
    })
    if reason:
        payload["clarifications"] = [{"field": DEVICE_FIELD, "reason": reason, "question": "请确认电脑口型。"}]
    return payload


@pytest.mark.parametrize("description, evidence, key, reason", CASES)
def test_pc_rule_respects_port_owner_and_strength(description, evidence, key, reason):
    query = query_for(description)
    assert pc_35mm_interface(query) == evidence
    intent = RuleBasedDemoParser().parse(query)
    assert intent.hard_constraints.device_requirements[0].device_key == key
    assert intent.hard_constraints.device_requirements[0].transport == "wired_3_5mm"
    assert {(c.field, c.reason) for c in intent.clarifications} == ({(DEVICE_FIELD, reason)} if reason else set())


@pytest.mark.parametrize("description, evidence, key, reason", CASES)
def test_pc_faithful_llm_payload_is_accepted(description, evidence, key, reason):
    intent = parse_intent_with_llm(query_for(description), ReplayHarness(faithful_payload(key, reason)))
    assert intent.hard_constraints.device_requirements[0].device_key == key
    assert {(c.field, c.reason) for c in intent.clarifications} == ({(DEVICE_FIELD, reason)} if reason else set())


@pytest.mark.parametrize("description, evidence, key, reason", [case for case in CASES if case.values[1] != "conflicting"])
def test_pc_llm_cannot_guess_or_change_confirmed_port(description, evidence, key, reason):
    wrong = faithful_payload(None if key == TRRS else TRRS, reason)
    with pytest.raises(IntentParseError) as excinfo:
        parse_intent_with_llm(query_for(description), ReplayHarness(wrong))
    assert excinfo.value.api_status == 502
    assert any(detail["field"] == DEVICE_FIELD for detail in excinfo.value.details)


@pytest.mark.parametrize("claimed_key", [TRRS, SPLIT])
@pytest.mark.parametrize("description, evidence, key, reason", [
    case for case in CASES if case.id.startswith("quick-") and case.values[1] in {"unspecified", "unsupported"}
])
def test_quick_pc_llm_cannot_replace_clarification_with_either_port(description, evidence, key, reason, claimed_key):
    with pytest.raises(IntentParseError) as excinfo:
        parse_intent_with_llm(query_for(description), ReplayHarness(faithful_payload(claimed_key, None)))
    assert excinfo.value.api_status == 502
    assert any(detail["field"] == DEVICE_FIELD for detail in excinfo.value.details)


@pytest.fixture(scope="module")
def pc_api(small_data_dir, tmp_path_factory):
    isolated = tmp_path_factory.mktemp("s51_pc_api")
    initialize_database(isolated / "db" / "shopping.sqlite3", small_data_dir)
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AGENT_INTENT_MODE", "rule_based_demo")
        env.setenv("PYTHON_DOTENV_DISABLED", "1")
        env.setenv("LLM_API_KEY", "")
        app = create_app(load_settings(data_dir=isolated))
        app.state.clock = lambda: datetime(2026, 9, 29, 10, tzinfo=derive.CN_TZ)
        with TestClient(app) as client:
            yield app, client


@pytest.mark.parametrize("description, evidence, key, reason", CASES)
def test_pc_public_api_requires_confirmed_compatible_plan(pc_api, description, evidence, key, reason):
    app, client = pc_api
    response = client.post("/api/agent-shopping", json={"query": query_for(description)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"]["hard_constraints"]["device_requirements"][0]["device_key"] == key
    if reason:
        assert body["status"] == "needs_clarification"
        assert body["recommendations"] == []
        assert {(c["field"], c["reason"]) for c in body["intent"]["clarifications"]} == {(DEVICE_FIELD, reason)}
    elif key == SPLIT:
        assert body["status"] == "no_results"
        assert body["recommendations"] == []
    else:
        assert body["status"] == "partial"
        assert len(body["recommendations"]) == 1
        rec = body["recommendations"][0]
        assert rec["product"]["product_id"] == "P007"
        assert rec["offer"]["offer_id"] == "O021"
        assert rec["offer"]["landed_price_cents"] == 8900
        conn = connect(app.state.settings.db_path)
        try:
            product = json.loads(conn.execute("SELECT payload FROM products WHERE product_id=?", ("P007",)).fetchone()[0])
            offer = json.loads(conn.execute("SELECT payload FROM offers WHERE offer_id=?", ("O021",)).fetchone()[0])
        finally:
            conn.close()
        mode = next(m for m in product["connection_modes"] if m["mode_id"] == rec["offer"]["mode_id"])
        assert mode["transport"] == "wired_3_5mm"
        assert mode["audio_connector"] == "3.5mm_TRRS_CTIA"
        assert mode["microphone_supported"] is True
        assert mode["requires_power"] is False
        assert offer["stock"] > 0
        assert any(c["device_key"] == TRRS and c["mode_id"] == mode["mode_id"]
                   and c["playback_supported"] is True and c["microphone_supported"] is True
                   for c in product["compatibility"])
