"""Hand-labelled tip noun groups: rule, offline LLM and public HTTP boundaries.

These catch lost coordinated members, lost group polarity and postfix softening.
Expected sizes/reasons are literals, independent of the production parser.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.agent.harness_client import HarnessTurnResult
from backend.agent.intent_parser import IntentParseError, RuleBasedDemoParser, parse_intent_with_llm
from backend.agent.query_semantics import ear_tip_evidence
from backend.config import Settings
from backend.database.db import connect, initialize_database
from backend.main import create_app
from backend.schemas.agent import ShoppingIntent
from backend.services.derive import CN_TZ


# query, hard sizes, clarification reason, evidence (value, preferred, conflict)
CASES = [
    pytest.param("必须附带小号和中号耳塞套", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="prefix-required-group"),
    pytest.param("小号和中号耳塞套必须附带", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="postfix-required-group"),
    pytest.param("必须附带小号耳塞套和中号耳塞套", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="repeated-required-noun"),
    pytest.param("耳塞套必须小号和中号", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="noun-first-required"),
    pytest.param("不接受小号和中号耳塞套", None, "unsupported_requirement", {"S": (False, False, False), "M": (False, False, False)}, id="prefix-excluded-group"),
    pytest.param("不接受附带小号和中号耳塞套的方案", None, "unsupported_requirement", {"S": (False, False, False), "M": (False, False, False)}, id="prefix-excluded-scheme"),
    pytest.param("不接受附带小号耳塞套和中号耳塞套的方案", None, "unsupported_requirement", {"S": (False, False, False), "M": (False, False, False)}, id="repeated-excluded-noun"),
    pytest.param("小号和中号耳塞套都不要", None, "unsupported_requirement", {"S": (False, False, False), "M": (False, False, False)}, id="postfix-excluded-group"),
    pytest.param("小号耳塞套和中号耳塞套不接受", None, "unsupported_requirement", {"S": (False, False, False), "M": (False, False, False)}, id="postfix-repeated-excluded"),
    pytest.param("无需附带小号和中号耳塞套", None, None, {"S": (None, False, False), "M": (None, False, False)}, id="prefix-optional-group"),
    pytest.param("小号和中号耳塞套不是必须", None, None, {"S": (None, False, False), "M": (None, False, False)}, id="postfix-optional-group"),
    pytest.param("小号耳塞套和中号耳塞套都无所谓", None, None, {"S": (None, False, False), "M": (None, False, False)}, id="postfix-repeated-optional"),
    pytest.param("尽量附带小号和中号耳塞套", None, "unsupported_requirement", {"S": (None, True, False), "M": (None, True, False)}, id="prefix-soft-group"),
    pytest.param("小号耳塞套尽量附带", None, "unsupported_requirement", {"S": (None, True, False)}, id="postfix-soft-single"),
    pytest.param("小号和中号耳塞套都尽量附带", None, "unsupported_requirement", {"S": (None, True, False), "M": (None, True, False)}, id="postfix-soft-group"),
    pytest.param("小号耳塞套和中号耳塞套最好附带", None, "unsupported_requirement", {"S": (None, True, False), "M": (None, True, False)}, id="postfix-repeated-soft"),
    pytest.param("必须附带中号耳塞套，但小号不要", ["M"], "unsupported_requirement", {"S": (False, False, False), "M": (True, False, False)}, id="positive-m-excluded-s-ellipsis"),
    pytest.param("必须附带中号耳塞套，但小号耳塞套不要", ["M"], "unsupported_requirement", {"S": (False, False, False), "M": (True, False, False)}, id="positive-m-excluded-s-repeated"),
    pytest.param("必须附带小号耳塞套，但不接受小号耳塞套", None, "conflicting_constraints", {"S": (False, False, True)}, id="same-size-conflict"),
    pytest.param("小号收纳盒不要，耳塞套必须中号", ["M"], None, {"M": (True, False, False)}, id="unrelated-case-first"),
    pytest.param("耳塞套必须中号，小号收纳盒不要", ["M"], None, {"M": (True, False, False)}, id="unrelated-case-last"),
    pytest.param("必须附带小号耳塞套和中号收纳盒", ["S"], None, {"S": (True, False, False)}, id="adjacent-tip-and-medium-case"),
    pytest.param("必须附带中号耳塞套和小号收纳盒", ["M"], None, {"M": (True, False, False)}, id="adjacent-tip-and-small-case"),
    pytest.param("必须附带小号收纳盒和中号耳塞套", ["M"], None, {"M": (True, False, False)}, id="adjacent-small-case-and-tip"),
    pytest.param("必须附带小号和中号耳塞套和大号收纳盒", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="shared-tip-group-and-large-case"),
    pytest.param("必须附带小号和中号耳塞套，但大号不要", ["S", "M"], "unsupported_requirement", {"S": (True, False, False), "M": (True, False, False), "L": (False, False, False)}, id="required-group-excluded-l"),
    pytest.param("必须附带小号和中号耳塞套，尽量轻", ["S", "M"], None, {"S": (True, False, False), "M": (True, False, False)}, id="later-soft-weight-separate"),
]


def query(text):
    return f"500元以内耳机，{text}。"


def payload(sizes, reason, *, light=False):
    result = ShoppingIntent(category="headphones").model_dump(mode="json")
    result["budget"]["max_cents"] = 50000
    result["hard_constraints"]["ear_tip_sizes"] = sizes
    if light:
        result["preferences"]["weight"] = 0.6
    result["clarifications"] = ([{
        "field": "hard_constraints.ear_tip_sizes", "reason": reason,
        "question": "请确认必须附带的耳塞套尺寸。",
    }] if reason else [])
    return result


class OfflineHarness:
    """Complete schema response replaces only the external model call."""
    def __init__(self, result):
        self.response = json.dumps(result, ensure_ascii=False)

    def run_intent_turn(self, prompt):
        return HarnessTurnResult(session_id="s51-tip-regression", final_response=self.response, finish_reason=None)


@pytest.mark.parametrize("text,sizes,reason,evidence", CASES)
def test_complete_tip_group_preserves_each_member_strength(text, sizes, reason, evidence):
    actual = ear_tip_evidence(query(text))
    for size in ("S", "M", "L"):
        expected = evidence.get(size, (None, False, False))
        assert actual[size].expressed is (size in evidence)
        assert (actual[size].value, actual[size].preferred, actual[size].conflicting) == expected


@pytest.mark.parametrize("text,sizes,reason,evidence", CASES)
def test_rule_intent_keeps_required_group_or_stops_for_unsupported_relation(text, sizes, reason, evidence):
    intent = RuleBasedDemoParser().parse(query(text))
    assert intent.hard_constraints.ear_tip_sizes == sizes
    assert [(c.field, c.reason) for c in intent.clarifications] == (
        [("hard_constraints.ear_tip_sizes", reason)] if reason else [])


@pytest.mark.parametrize("text,sizes,reason,evidence", CASES)
def test_faithful_offline_llm_keeps_hand_labelled_tip_intent(text, sizes, reason, evidence):
    intent = parse_intent_with_llm(query(text), OfflineHarness(payload(sizes, reason, light="尽量轻" in text)))
    assert intent.hard_constraints.ear_tip_sizes == sizes
    assert [(c.field, c.reason) for c in intent.clarifications] == (
        [("hard_constraints.ear_tip_sizes", reason)] if reason else [])


@pytest.mark.parametrize("text,wrong_sizes", [
    ("必须附带小号和中号耳塞套", ["M"]),
    ("不接受小号和中号耳塞套", ["M"]),
    ("不接受附带小号和中号耳塞套的方案", ["M"]),
    ("小号和中号耳塞套都不要", ["S"]),
    ("小号耳塞套尽量附带", ["S"]),
    ("小号和中号耳塞套都尽量附带", ["S", "M"]),
    ("小号和中号耳塞套不是必须", ["S", "M"]),
    ("必须附带中号耳塞套，但小号不要", ["S", "M"]),
    ("小号收纳盒不要，耳塞套必须中号", ["S", "M"]),
])
def test_offline_llm_cannot_drop_member_or_harden_excluded_optional_soft_sizes(text, wrong_sizes):
    with pytest.raises(IntentParseError) as caught:
        parse_intent_with_llm(query(text), OfflineHarness(payload(wrong_sizes, None)))
    assert caught.value.api_status == 502
    assert any(d["field"] == "hard_constraints.ear_tip_sizes" for d in caught.value.details)


@pytest.fixture(scope="module")
def tip_http(tmp_path_factory, small_data_dir):
    settings = Settings(data_dir=tmp_path_factory.mktemp("s51_tip_http"))
    initialize_database(settings.db_path, small_data_dir)
    app = create_app(settings)
    app.state.clock = lambda: datetime(2026, 9, 30, 12, tzinfo=CN_TZ)
    with pytest.MonkeyPatch.context() as env, TestClient(app) as client:
        env.setenv("AGENT_INTENT_MODE", "rule_based_demo")
        yield client, settings.db_path


@pytest.mark.parametrize("text,sizes,reason,evidence", CASES)
def test_public_api_tip_relations_gate_real_recommendations(tip_http, text, sizes, reason, evidence):
    client, db_path = tip_http
    response = client.post("/api/agent-shopping", json={"query": query(text)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["intent"]["hard_constraints"]["ear_tip_sizes"] == sizes
    assert [(c["field"], c["reason"]) for c in body["intent"]["clarifications"]] == (
        [("hard_constraints.ear_tip_sizes", reason)] if reason else [])
    if reason:
        assert body["status"] == "needs_clarification"
        assert body["recommendations"] == []
    else:
        assert body["status"] in {"ok", "partial"}
        assert body["recommendations"], "Real positive cases must still produce plans."
        conn = connect(db_path)
        try:
            for rec in body["recommendations"]:
                product = json.loads(conn.execute("SELECT payload FROM products WHERE product_id=?", (rec["product"]["product_id"],)).fetchone()[0])
                assert set(sizes or []) <= set(product["ear_tip_sizes"] or [])
                assert rec["offer"]["landed_price_cents"] <= 50000
        finally:
            conn.close()
