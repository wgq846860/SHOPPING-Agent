"""S3.3 API 测试：POST /api/agent-shopping（api.v0.1 §7）。

验收覆盖（s3.md S3.3 + 契约 §7/§1.2 + 场景 SC01–SC15 + EDGE 锚点）：
- 请求校验：缺少/null/非字符串/额外键/非对象体/空白/超长 → 422
  （空白与超长的 message/reason 按 §7.5）；
- 模式：显式 llm 缺密钥 → 503 LLM_UNAVAILABLE；非法 AGENT_INTENT_MODE
  → 503；rule_based_demo 全程零模型调用；
- 业务状态：needs_clarification（EDGE05 冲突/外币）、no_results（EDGE01
  不自动加预算）、ok/partial（不足3条如实返回）；
- 15 场景端到端：状态合法、mode/algorithm_version 如实、推荐中的
  product/merchant/offer ID 全部能查回数据库；
- llm 模式离线替身：解析+解释成功（mode=llm）、解析不可校验 502、
  超时 504（retryable）、不可用 503、解释失败回退 200+EXPLANATION_FALLBACK
  且推荐不变（解释失败不破坏已算出的推荐）。

时间固定 2026-09-30T12:00+08:00（O001 已过期），与 S1.3/S2 测试一致；
数据来自 small-v1 固定种子（锚点见 tests/test_agent.py 文件头）。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.agent.harness_client import (
    HarnessTimeoutError,
    HarnessTurnResult,
    HarnessUnavailableError,
)
from backend.config import load_settings
from backend.database.db import connect, initialize_database
from backend.main import create_app
from backend.schemas.agent import PREFERENCE_DIMENSIONS
from backend.services import derive

FIXED_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)


@pytest.fixture()
def agent_app(small_data_dir, tmp_path, monkeypatch):
    """隔离库 + 固定时钟 + 强制规则演示模式（与用户 .env 无关）。"""
    monkeypatch.setenv("AGENT_INTENT_MODE", "rule_based_demo")
    initialize_database(tmp_path / "db" / "shopping.sqlite3", small_data_dir)
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    return app


@pytest.fixture()
def agent_client(agent_app) -> TestClient:
    return TestClient(agent_app)


def post_query(client: TestClient, payload):
    return client.post("/api/agent-shopping", json=payload)


# ---------------------------------------------------------------------------
# 请求校验（契约 §7 / §7.5）
# ---------------------------------------------------------------------------


def test_blank_query_422(agent_client):
    response = post_query(agent_client, {"query": "   "})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "请填写购物需求"
    assert body["error"]["details"] == [{"field": "query", "reason": "blank_query"}]
    assert body["error"]["retryable"] is False
    assert body["meta"]["api_version"] == "api.v0.1"


def test_query_too_long_422(agent_client):
    response = post_query(agent_client, {"query": "耳机" * 1200})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["reason"] == "too_long"


@pytest.mark.parametrize(
    "payload",
    [
        {},                          # 缺少 query
        {"query": None},             # null
        {"query": 123},              # 非字符串
        {"query": "无线耳机", "budget": 800},  # 未知额外键
    ],
)
def test_invalid_request_bodies_422(agent_client, payload):
    response = post_query(agent_client, payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_non_object_body_422(agent_client):
    response = agent_client.post("/api/agent-shopping", json=["无线耳机"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_agent_meta_shape(agent_client):
    body = post_query(agent_client, {"query": "800元以内无线主动降噪耳机"}).json()
    assert set(body["meta"]) == {
        "api_version", "schema_version", "data_version",
        "request_id", "generated_at", "simulated",
    }
    assert body["meta"]["data_version"] == "small-v1"
    assert body["meta"]["simulated"] is True


# ---------------------------------------------------------------------------
# 模式解析（llm 缺密钥 / 非法配置）
# ---------------------------------------------------------------------------


def test_mode_llm_without_key_503(agent_app, monkeypatch):
    monkeypatch.setenv("AGENT_INTENT_MODE", "llm")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = TestClient(agent_app)
    response = post_query(client, {"query": "无线耳机"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "LLM_UNAVAILABLE"
    assert body["error"]["details"][0]["field"] == "LLM_API_KEY"


def test_mode_invalid_value_503(agent_app, monkeypatch):
    monkeypatch.setenv("AGENT_INTENT_MODE", "bogus")
    client = TestClient(agent_app)
    response = post_query(client, {"query": "无线耳机"})
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "LLM_UNAVAILABLE"
    assert body["error"]["details"][0]["field"] == "AGENT_INTENT_MODE"


# ---------------------------------------------------------------------------
# 业务状态：澄清 / 无结果（EDGE01/EDGE05）
# ---------------------------------------------------------------------------


def test_conflicting_constraints_needs_clarification(agent_client):
    response = post_query(agent_client, {"query": "必须有线，也必须真无线"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert body["recommendations"] == []
    assert body["intent"]["clarifications"]
    assert all(c["reason"] == "conflicting_constraints" for c in body["intent"]["clarifications"])
    assert body["decision_summary"]["comparison_scope"] == "未检索"
    assert body["filters_applied"] == []
    assert body["ranking_weights"] == {"product": {}, "offer": {}}
    assert body["algorithm_version"] is None
    assert body["mode"] == "rule_based_demo"


def test_foreign_currency_needs_clarification(agent_client):
    response = post_query(agent_client, {"query": "$100以内的无线耳机"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert any(c["field"] == "budget.currency" for c in body["intent"]["clarifications"])


def test_no_results_edge01(agent_client):
    response = post_query(agent_client, {"query": "100元以内无线主动降噪耳机"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_results"
    assert body["recommendations"] == []
    assert body["intent"]["budget"]["max_cents"] == 10000  # 不自动增加预算
    assert body["decision_summary"]["candidate_count"] == 0
    assert body["filters_applied"]  # 有实际执行的过滤记录
    assert body["ranking_weights"] == {"product": {}, "offer": {}}
    assert body["algorithm_version"] is None
    codes = {w["code"] for w in body["warnings"]}
    assert "SIMULATED_DATA" in codes


# ---------------------------------------------------------------------------
# 15 场景端到端：状态合法 + mode 如实 + ID 全部可查回
# ---------------------------------------------------------------------------

SCENARIO_QUERIES = {
    "SC01": "800 元以内，地铁上听播客，要无线和主动降噪，尽量轻、好携带。",
    "SC02": "1500 元以内，长途火车一次听 8 小时，需要无线 ANC，能折叠更好。",
    "SC03": "600 元以内，在共享自习室戴三四个小时，希望减少环境干扰，别太重，尽量少漏音。",
    "SC04": "500 元以内，主要在 Windows 电脑开视频会，必须带麦克风，声音清楚，长会不累。",
    "SC05": "800 元以内，电脑工作、手机接电话，不想每次断开重连，必须支持双设备多点连接。",
    "SC06": "100 元以内，给有3.5mm接口的电脑买备用耳机，必须有线、带麦、插上能用，不接受需要充电的方案。",
    "SC07": "700 元以内，电脑打FPS、和队友语音，优先稳定和低延迟，接受有线。",
    "SC08": "900 元以内，买给PS5用，必须能语音，尽量无线，偶尔接电脑。",
    "SC09": "1000 元以内，在家安静听人声和流行乐，音质优先，不要求无线。",
    "SC10": "800 元以内，夜里看电视不想外放，需要能接家里的电视，尽量音画同步、戴着舒服。",
    "SC11": "400 元以内，健身房训练一小时，必须无线，必须有厂家明确的运动防汗使用说明，动作大时尽量不掉。",
    "SC12": "600 元以内，户外跑步，必须无线和不堵耳道，想留意周围声音，尽量稳。",
    "SC13": "700 元以内，办公室长时间听音，不想塞进耳道，必须非入耳，尽量轻、少漏音。",
    "SC14": "500 元以内，必须是真无线入耳耳塞、附带小号可替换耳塞套，只接受明确允许开封试戴后按条款退货的报价，尽量轻。",
    "SC15": "600 元以内，三天后要送朋友，必须全新、能在截止前送达，希望保修和退换方便。",
}


def _db_rows(conn, sql: str) -> set:
    return {row[0] for row in conn.execute(sql)}


@pytest.mark.parametrize("scenario_id", sorted(SCENARIO_QUERIES))
def test_scenario_end_to_end(agent_client, agent_app, scenario_id):
    response = post_query(agent_client, {"query": SCENARIO_QUERIES[scenario_id]})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] in {"ok", "partial", "no_results", "needs_clarification"}
    assert body["mode"] == "rule_based_demo"
    assert set(body) == {
        "status", "intent", "recommendations", "decision_summary",
        "filters_applied", "ranking_weights", "mode", "algorithm_version",
        "warnings", "meta",
    }

    intent = body["intent"]
    assert intent["hard_constraints"]["stock_required"] is True

    recommendations = body["recommendations"]
    if body["status"] == "needs_clarification":
        assert intent["clarifications"]
        assert recommendations == []
        return
    assert 0 <= len(recommendations) <= 3
    assert body["decision_summary"]["returned_count"] == len(recommendations)
    if body["status"] == "no_results":
        assert not recommendations
        return
    # 有推荐：rank 连续、商品不重复、分数与权重合规
    assert [r["rank"] for r in recommendations] == list(range(1, len(recommendations) + 1))
    product_ids = {r["product"]["product_id"] for r in recommendations}
    assert len(product_ids) == len(recommendations)
    assert all(0 <= r["score"] <= 100 for r in recommendations)
    weights = body["ranking_weights"]
    assert abs(sum(weights["product"].values()) - 1) < 1e-9
    assert abs(sum(weights["offer"].values()) - 1) < 1e-9
    assert isinstance(body["algorithm_version"], str) and body["algorithm_version"]

    # ID 全部能查回数据库（验收条款）
    conn = connect(agent_app.state.settings.db_path)
    try:
        products = _db_rows(conn, "SELECT product_id FROM products")
        merchants = _db_rows(conn, "SELECT merchant_id FROM merchants")
        offers = _db_rows(conn, "SELECT offer_id FROM offers")
        for rec in recommendations:
            assert rec["product"]["product_id"] in products
            assert rec["offer"]["offer_id"] in offers
            assert rec["offer"]["merchant_id"] in merchants
            # 推荐报价必须属于该商品且有货（决策记录与库一致）
            offer_row = conn.execute(
                "SELECT product_id, stock FROM offers WHERE offer_id = ?",
                (rec["offer"]["offer_id"],),
            ).fetchone()
            assert offer_row[0] == rec["product"]["product_id"]
            assert offer_row[1] > 0
    finally:
        conn.close()


def test_sc01_scenario_hard_fields(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC01"]}).json()
    hard = body["intent"]["hard_constraints"]
    assert hard["wireless"] is True
    assert hard["active_noise_cancelling"] is True
    assert body["intent"]["budget"]["max_cents"] == 80000


def test_sc02_scenario_preserves_single_listen_battery_requirement(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC02"]}).json()
    requirement = body["intent"]["hard_constraints"]["battery_requirement"]
    assert requirement["min_hours"] == 8.0
    assert requirement["basis"] == "single_charge"
    assert requirement["anc_state"] == "on"
    for recommendation in body["recommendations"]:
        assert recommendation["selected_specs"]["battery_spec_id"] is not None


def test_sc06_scenario_wired(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC06"]}).json()
    hard = body["intent"]["hard_constraints"]
    assert hard["wireless"] is False
    assert hard["allowed_transports"] == ["wired_3_5mm", "usb_audio"]
    assert hard["microphone"] is True


def test_sc09_explicit_null_not_false(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC09"]}).json()
    # “不要求无线”是 null，不是 false（§7.1 有限语义）
    assert body["intent"]["hard_constraints"]["wireless"] is None


def test_sc13_excluded_in_ear(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC13"]}).json()
    assert body["intent"]["hard_constraints"]["excluded_form_factors"] == ["in_ear"]


def test_sc15_new_condition(agent_client):
    body = post_query(agent_client, {"query": SCENARIO_QUERIES["SC15"]}).json()
    assert body["intent"]["hard_constraints"]["item_condition"] == "new"


# ---------------------------------------------------------------------------
# 响应契约形状（§7.2）：推荐对象、评分分解、解释事实
# ---------------------------------------------------------------------------


def _first_ranked_query(client: TestClient) -> dict:
    for query in SCENARIO_QUERIES.values():
        body = post_query(client, {"query": query}).json()
        if body["recommendations"]:
            return body
    pytest.fail("15个场景在small-v1上没有任何推荐输出")


def test_recommendation_contract_shape(agent_client):
    body = _first_ranked_query(agent_client)
    rec = body["recommendations"][0]

    assert set(rec["product"]) == {
        "product_id", "brand", "product_name", "sub_category", "form_factor",
        "rating", "review_count", "wireless", "active_noise_cancelling",
        "battery_specs", "weight_specs", "image_path", "availability",
        "display_offer", "field_sources", "derivation",
    }
    offer = rec["offer"]
    assert set(offer) == {
        "offer_id", "product_id", "merchant_id", "merchant_name", "mode_id",
        "currency", "price_cents", "shipping_fee_cents", "accessory_total_cents",
        "instant_discount_cents", "landed_price_cents", "stock", "item_condition",
        "delivery_window", "effective_return_policy", "effective_warranty",
        "policy_origin", "warranty_origin", "score", "field_sources", "derivation",
    }
    assert offer["currency"] == "CNY"
    assert offer["stock"] > 0
    # 到手价 = 商品价 + 运费 + 配件 - 立减（landed_price.v1）
    assert offer["landed_price_cents"] == (
        offer["price_cents"] + offer["shipping_fee_cents"]
        + offer["accessory_total_cents"] - offer["instant_discount_cents"]
    )

    assert set(rec["selected_specs"]) == {"battery_spec_id", "weight_spec_id"}
    for spec_key, spec_id in rec["selected_specs"].items():
        if spec_id is not None:
            spec_leaf = spec_key.replace("_spec_id", "_specs")
            ids = {
                spec[f"{spec_key.replace('_spec_id', '_spec')}_id"]
                for spec in (rec["product"][spec_leaf] or [])
            }
            assert spec_id in ids
    breakdown = rec["score_breakdown"]
    assert set(breakdown) == {"product", "offer"}
    for side in ("product", "offer"):
        assert breakdown[side], f"{side} 侧评分分量不能为空"
        for component in breakdown[side]:
            assert set(component) == {
                "dimension", "raw_value", "unit", "basis", "normalized_score",
                "weight", "contribution", "missing", "source_refs",
            }
            assert 0 <= component["normalized_score"] <= 1
            assert component["weight"] >= 0
            if component["missing"]:
                assert component["raw_value"] is None
        total = sum(c["contribution"] for c in breakdown[side])
        expected = rec["score"] if side == "product" else offer["score"]
        assert abs(total - expected) <= 1e-6

    for category in ("reason", "strengths", "weaknesses", "trade_offs"):
        for statement in rec[category]:
            assert set(statement) == {"text", "fact_refs", "comparison"}
            assert statement["text"]
            assert all(isinstance(ref, str) for ref in statement["fact_refs"])


def test_product_summary_matches_list_endpoint(agent_client, agent_app):
    """推荐里的 ProductSummary 与 GET /api/products 同一展示口径。"""
    body = _first_ranked_query(agent_client)
    product_id = body["recommendations"][0]["product"]["product_id"]
    listed = agent_client.get("/api/products", params={"page_size": 100}).json()
    match = [item for item in listed["items"] if item["product_id"] == product_id]
    assert match, "推荐商品必须能被传统列表接口检索到"
    assert match[0]["display_offer"] == body["recommendations"][0]["product"]["display_offer"]


def test_filters_applied_shape(agent_client):
    body = _first_ranked_query(agent_client)
    for record in body["filters_applied"]:
        assert set(record) == {
            "field", "operator", "value", "unit", "scope",
            "excluded_count", "unknown_excluded_count",
        }
        assert record["operator"] in {"eq", "lte", "gte", "in", "not_in", "all", "compatible"}
        assert record["scope"] in {"product", "offer", "connection_plan"}
        assert 0 <= record["unknown_excluded_count"] <= record["excluded_count"]


def test_warning_codes_in_enum(agent_client):
    body = _first_ranked_query(agent_client)
    allowed = {
        "SIMULATED_DATA", "INSUFFICIENT_RESULTS", "MISSING_SOFT_METRIC",
        "UNSUPPORTED_PREFERENCE", "STALE_DATA", "EXPLANATION_FALLBACK",
    }
    assert {w["code"] for w in body["warnings"]} <= allowed
    for warning in body["warnings"]:
        assert set(warning) == {"code", "message", "field", "product_id"}


def test_insufficient_results_warning_on_partial(agent_client):
    """EDGE02：只有1–2个合格商品时 partial + INSUFFICIENT_RESULTS。"""
    for query in SCENARIO_QUERIES.values():
        body = post_query(agent_client, {"query": query}).json()
        if body["status"] == "partial":
            assert any(w["code"] == "INSUFFICIENT_RESULTS" for w in body["warnings"])
            return
    pytest.skip("small-v1 上本轮未出现 partial 场景")


# ---------------------------------------------------------------------------
# llm 模式（离线替身注入 app.state.agent_harness）
# ---------------------------------------------------------------------------


class ScriptedHarness:
    """仅替换外部模型回合；购物回合经真实内部 HTTP 端点执行。"""

    def __init__(self, turns) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []

    def close(self):
        pass

    def run_intent_turn(self, prompt: str) -> HarnessTurnResult:
        self.prompts.append(prompt)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            return HarnessTurnResult("session-x", turn(prompt), "stop")
        return HarnessTurnResult("session-x", turn, "stop")


def _intent_payload(*, hard_overrides=None, pref_overrides=None) -> str:
    hard = {
        "stock_required": True, "wireless": None, "active_noise_cancelling": None,
        "microphone": None, "multipoint": None, "open_ear": None, "requires_power": None,
        "opened_return_allowed": None, "sweat_use_allowed": None, "sub_categories": None,
        "allowed_form_factors": None, "excluded_form_factors": None, "ear_tip_sizes": None,
        "min_active_devices": None, "allowed_transports": None, "battery_requirement": None,
        "weight_requirement": None, "device_requirements": None, "item_condition": None,
        "delivery_requirement": None,
    }
    hard.update(hard_overrides or {})
    prefs = {dim: 0 for dim in PREFERENCE_DIMENSIONS}
    prefs.update(pref_overrides or {})
    payload = {
        "category": "headphones",
        "budget": {"max_cents": 80000, "currency": "CNY", "basis": "landed_price"},
        "hard_constraints": hard,
        "preferences": prefs,
        "clarifications": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _explanation_selection(prompt: str) -> str:
    """按解释协议输出选择：覆盖全部 statement_id，第一条多版本语句选非原文。"""
    start = prompt.index("<context>") + len("<context>")
    end = prompt.index("</context>")
    context = json.loads(prompt[start:end].strip())
    catalog = context["selection_catalog"]
    selections = []
    picked = False
    for item in catalog["items"]:
        variant_id = item["variants"][0]["variant_id"]
        if not picked and len(item["variants"]) > 1:
            variant_id = item["variants"][1]["variant_id"]
            picked = True
        selections.append({"statement_id": item["statement_id"], "variant_id": variant_id})
    return json.dumps({"catalog_id": catalog["catalog_id"], "selections": selections},
                      ensure_ascii=False)


def _tool_turn(app, records=None, mutate=None):
    def execute(prompt):
        arguments = json.loads(prompt.split("<tool_request>")[1].split("</tool_request>")[0])
        if mutate:
            mutate(arguments)
        response = TestClient(app).post(
            "/api/internal/tools/recommend_from_intent", json=arguments,
            headers={"X-Agent-Bridge-Token": "offline-test-bridge-token"},
        )
        if records is not None:
            records.append(response.json())
        # 模型自然语言不是权威结果；测试刻意回显不同商品与价格。
        return '{"recommendations":[{"product_id":"MODEL_INVENTED","price_cents":1}]}'
    return execute


@pytest.fixture()
def llm_app(agent_app, monkeypatch):
    monkeypatch.setenv("AGENT_INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "offline-test-key")
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "offline-test-bridge-token")
    return agent_app


def test_llm_mode_full_success(llm_app):
    harness = ScriptedHarness([
        _intent_payload(hard_overrides={"wireless": True, "active_noise_cancelling": True}),
        _tool_turn(llm_app),
        _explanation_selection,
    ])
    llm_app.state.agent_harness = harness
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "llm"
    assert body["intent"]["hard_constraints"]["wireless"] is True
    assert body["recommendations"]
    assert not any(w["code"] == "EXPLANATION_FALLBACK" for w in body["warnings"])
    assert len(harness.prompts) == 3
    assert "意图解析" in harness.prompts[0] or "ShoppingIntent" in harness.prompts[0]
    assert "recommend_from_intent" in harness.prompts[1]
    assert "selection_catalog" in harness.prompts[2]
    assert "MODEL_INVENTED" not in response.text


@pytest.mark.parametrize("query", ["头戴耳机整副0克以内", "耳机续航至少0小时"])
def test_s3_zero_requirement_is_controlled_clarification(agent_client, query):
    response = post_query(agent_client, {"query": query})
    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"
    assert response.json()["intent"]["clarifications"]


def test_s3_duplicate_key_error_hides_model_text(llm_app):
    llm_app.state.agent_harness = ScriptedHarness(['{"MODEL_RAW_SENTINEL":1,"MODEL_RAW_SENTINEL":2}'])
    response = post_query(TestClient(llm_app), {"query": "推荐耳机"})
    assert response.status_code == 502
    assert "MODEL_RAW_SENTINEL" not in response.text


def test_s3_llm_cannot_succeed_without_same_request_tool(llm_app):
    llm_app.state.agent_harness = ScriptedHarness([_intent_payload(), "已完成推荐，但未调用工具"])
    response = post_query(TestClient(llm_app), {"query": "800元以内耳机"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "LLM_INVALID_OUTPUT"


@pytest.mark.parametrize("mutation", ["intent", "binding"])
def test_s3_tool_cannot_change_intent_or_use_other_request(llm_app, mutation):
    records = []
    def mutate(arguments):
        if mutation == "intent":
            arguments["intent"]["budget"]["max_cents"] = 90000
        else:
            arguments["decision_request_id"] = "0" * 32
    llm_app.state.agent_harness = ScriptedHarness([_intent_payload(), _tool_turn(llm_app, records, mutate)])
    response = post_query(TestClient(llm_app), {"query": "800元以内耳机"})
    assert response.status_code == 502
    assert records[0]["error"]["code"] == "decision_binding_invalid"


def test_s3_request_record_expires_after_response(llm_app):
    records = []
    llm_app.state.agent_harness = ScriptedHarness([
        _intent_payload(), _tool_turn(llm_app, records), _explanation_selection,
    ])
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内耳机"})
    assert response.status_code == 200
    assert [r["offer"]["offer_id"] for r in response.json()["recommendations"]] == [
        r["offer_id"] for r in records[0]["recommendations"][:3]
    ]
    replay = client.post("/api/internal/tools/recommend_from_intent", json={
        "intent": records[0]["intent"], "decision_request_id": records[0]["decision_request_id"],
    }, headers={"X-Agent-Bridge-Token": "offline-test-bridge-token"})
    assert replay.status_code == 409


def test_s3_explanation_warning_hides_untrusted_ids(llm_app):
    def bad_selection(prompt):
        context = json.loads(prompt.split("<context>")[1].split("</context>")[0])
        return json.dumps({"catalog_id": context["selection_catalog"]["catalog_id"],
                           "selections": [{"statement_id": "MODEL_WARNING_SENTINEL", "variant_id": "v0"}]})
    llm_app.state.agent_harness = ScriptedHarness([
        _intent_payload(), _tool_turn(llm_app), bad_selection,
    ])
    response = post_query(TestClient(llm_app), {"query": "800元以内耳机"})
    assert response.status_code == 200
    assert "MODEL_WARNING_SENTINEL" not in response.text
    assert any(w["code"] == "EXPLANATION_FALLBACK" for w in response.json()["warnings"])


def test_s3_explanation_can_rebuild_database_without_mixing_facts(llm_app, tmp_path):
    from scripts.generate_demo_data import generate_profile

    batch = tmp_path / "replacement-batch"
    generate_profile("integration", batch)
    products_path = batch / "products.json"
    products = json.loads(products_path.read_text(encoding="utf-8"))
    for product in products["items"]:
        product["product_name"] = "NEW_BATCH_NAME_" + product["product_id"]
    products_path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
    offers_path = batch / "offers.json"
    offers = json.loads(offers_path.read_text(encoding="utf-8"))
    for offer in offers["items"]:
        offer["price_cents"] = 140000
        offer["original_price_cents"] = 150000
        offer["field_sources"]["original_price_cents"]["missing_reason"] = None
    offers_path.write_text(json.dumps(offers, ensure_ascii=False), encoding="utf-8")
    records = []
    def rebuild_then_explain(prompt):
        initialize_database(llm_app.state.settings.db_path, batch, rebuild=True)
        return _explanation_selection(prompt)
    llm_app.state.agent_harness = ScriptedHarness([
        _intent_payload(hard_overrides={"wireless": True, "active_noise_cancelling": True}),
        _tool_turn(llm_app, records), rebuild_then_explain,
    ])
    response = post_query(TestClient(llm_app), {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["data_version"] == "small-v1"
    assert "NEW_BATCH_NAME_" not in response.text
    for rec, tool_rec in zip(body["recommendations"], records[0]["recommendations"][:3], strict=True):
        assert rec["product"]["product_name"] == tool_rec["product_name"]
        assert rec["offer"]["price_cents"] == tool_rec["price_cents"]
        assert rec["offer"]["landed_price_cents"] <= 80000
    conn = connect(llm_app.state.settings.db_path)
    try:
        assert conn.execute("SELECT data_version FROM data_manifest").fetchone()[0] == "integration-v1"
        assert conn.execute("SELECT price_cents FROM offers LIMIT 1").fetchone()[0] == 140000
    finally:
        conn.close()


@pytest.mark.parametrize("error, expected", [(HarnessTimeoutError("timeout"), 504),
                                            (HarnessUnavailableError("unavailable"), 503)])
def test_s3_tool_turn_failure_has_controlled_error(llm_app, error, expected):
    llm_app.state.agent_harness = ScriptedHarness([_intent_payload(), error])
    response = post_query(TestClient(llm_app), {"query": "800元以内耳机"})
    assert response.status_code == expected


def test_s3_llm_without_bridge_is_unavailable(llm_app, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_TOKEN", raising=False)
    llm_app.state.agent_harness = ScriptedHarness([])
    response = post_query(TestClient(llm_app), {"query": "800元以内耳机"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_UNAVAILABLE"


def test_s3_concurrent_public_requests_keep_independent_tool_records(llm_app):
    from concurrent.futures import ThreadPoolExecutor

    records = []
    turns = []
    for _ in range(5):
        turns.extend([_intent_payload(), _tool_turn(llm_app, records), _explanation_selection])
    llm_app.state.agent_harness = ScriptedHarness(turns)
    with TestClient(llm_app) as client:
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(lambda _: post_query(client, {"query": "800元以内耳机"}), range(5)))
    assert all(r.status_code == 200 for r in responses)
    assert len(records) == 5
    assert len({r["decision_request_id"] for r in records}) == 5
    assert len({r.json()["meta"]["request_id"] for r in responses}) == 5


def test_s3_tool_bridge_uses_actual_loopback_listener_port(llm_app):
    from backend.api.agent import _harness_for

    client = _harness_for(llm_app, ("127.0.0.1", 8129))
    assert client.runtime_config().bridge_origin == "http://127.0.0.1:8129"
    assert not client.started


def test_llm_mode_invalid_parse_502(llm_app):
    llm_app.state.agent_harness = ScriptedHarness(["这不是JSON"])
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "LLM_INVALID_OUTPUT"
    assert body["error"]["retryable"] is False


def test_llm_mode_timeout_504(llm_app):
    llm_app.state.agent_harness = ScriptedHarness([HarnessTimeoutError("回合超时")])
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "LLM_TIMEOUT"
    assert body["error"]["retryable"] is True


def test_llm_mode_unavailable_503(llm_app):
    llm_app.state.agent_harness = ScriptedHarness([HarnessUnavailableError("上游失败")])
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "LLM_UNAVAILABLE"


def test_llm_mode_explanation_fallback_keeps_recommendations(llm_app):
    """解释回合失败：已算出的推荐原样返回 + EXPLANATION_FALLBACK（§1.2）。"""
    llm_app.state.agent_harness = ScriptedHarness([
        _intent_payload(hard_overrides={"wireless": True, "active_noise_cancelling": True}),
        _tool_turn(llm_app),
        "模型胡言乱语",
    ])
    client = TestClient(llm_app)
    response = post_query(client, {"query": "800元以内无线主动降噪耳机"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "llm"  # 解析模式如实保留，不因解释失败改变
    assert body["recommendations"]
    fallback = [w for w in body["warnings"] if w["code"] == "EXPLANATION_FALLBACK"]
    assert len(fallback) == 1


def test_llm_mode_intent_too_long_still_422(llm_app):
    """llm 模式下空白/超长 query 在解析层前置拒绝，不发起模型回合。"""
    harness = ScriptedHarness([])
    llm_app.state.agent_harness = harness
    client = TestClient(llm_app)
    response = post_query(client, {"query": "  "})
    assert response.status_code == 422
    assert harness.prompts == []


def test_missing_database_503(tmp_path, monkeypatch):
    """库未导入：POST /api/agent-shopping → 503 DATA_UNAVAILABLE。"""
    monkeypatch.setenv("AGENT_INTENT_MODE", "rule_based_demo")
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    client = TestClient(app)
    response = post_query(client, {"query": "无线耳机"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATA_UNAVAILABLE"


@pytest.mark.parametrize("description, status, expected_key, reason", [
    ("电脑有3.5mm接口", "needs_clarification", None, "missing_device"),
    ("电脑有分离的3.5mm耳机输出口和麦克风输入口", "no_results", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm TRRS复合接口", "partial", "通用PC（3.5mm TRRS复合口）", None),
    ("电脑有3.5mm接口，必须为TRRS复合口且必须为分离接口", "needs_clarification", None, "conflicting_constraints"),
    ("电脑有3.5mm接口，尽量是TRRS复合接口", "needs_clarification", None, "unsupported_requirement"),
    ("电脑有3.5mm接口，不接受TRRS复合接口，必须为分离接口", "no_results", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm接口，耳机使用TRRS接口", "needs_clarification", None, "missing_device"),
    ("电脑有3.5mm接口，卖家说耳机是TRRS", "needs_clarification", None, "missing_device"),
    ("电脑有3.5mm接口而耳机使用TRRS接口", "needs_clarification", None, "missing_device"),
    ("我的电脑有3.5mm接口但卖家说耳机是TRRS", "needs_clarification", None, "missing_device"),
    ("耳机使用TRRS接口而电脑有3.5mm接口", "needs_clarification", None, "missing_device"),
    ("耳机使用TRRS接口但电脑有分离的3.5mm耳机输出口和麦克风输入口", "no_results", "通用PC（分离3.5mm音频/麦克风接口）", None),
    ("电脑有3.5mm TRRS复合接口而耳机使用TRRS接口", "partial", "通用PC（3.5mm TRRS复合口）", None),
])
def test_s51_pc_port_public_api_and_selected_plan(agent_app, agent_client, description, status, expected_key, reason):
    agent_app.state.clock = lambda: datetime(2026, 9, 29, 10, tzinfo=derive.CN_TZ)
    response = post_query(agent_client, {"query": f"100元以内，{description}，买备用耳机，必须有线、带麦、插上能用，不接受需要充电的方案。"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert body["intent"]["hard_constraints"]["device_requirements"][0]["device_key"] == expected_key
    if reason:
        assert any(c["field"] == "hard_constraints.device_requirements" and c["reason"] == reason
                   for c in body["intent"]["clarifications"])
    if status != "partial":
        assert body["recommendations"] == []
    else:
        assert len(body["recommendations"]) == 1
        rec = body["recommendations"][0]
        assert rec["product"]["product_id"] == "P007"
        assert rec["offer"]["offer_id"] == "O021"
        assert rec["offer"]["landed_price_cents"] == 8900
        conn = connect(agent_app.state.settings.db_path)
        try:
            product = json.loads(conn.execute("SELECT payload FROM products WHERE product_id='P007'").fetchone()[0])
        finally:
            conn.close()
        mode = next(m for m in product["connection_modes"] if m["mode_id"] == rec["offer"]["mode_id"])
        assert mode["mode_id"] == "w35"
        assert mode["audio_connector"] == "3.5mm_TRRS_CTIA"
        assert mode["microphone_supported"] is True
        assert mode["requires_power"] is False


@pytest.mark.parametrize("query, field, value, statuses", [
    ("600元以内耳机，只听音乐，不接受语音通话功能。", "microphone", False, ("ok", "partial")),
    ("500元以内真无线入耳耳塞，不接受附带小号耳塞套的方案。",
     "ear_tip_sizes", None, ("needs_clarification",)),
])
def test_s51_negative_requirements_public_api(agent_app, agent_client, query, field, value, statuses):
    agent_app.state.clock = lambda: datetime(2026, 9, 29, 10, tzinfo=derive.CN_TZ)
    response = post_query(agent_client, {"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in statuses
    assert body["intent"]["hard_constraints"][field] is value
    if field == "ear_tip_sizes":
        assert body["recommendations"] == []
        assert any(c["field"] == "hard_constraints.ear_tip_sizes" and c["reason"] == "unsupported_requirement"
                   for c in body["intent"]["clarifications"])
    else:
        assert body["recommendations"]
        assert (body["status"] == "ok" and len(body["recommendations"]) == 3
                or body["status"] == "partial" and 1 <= len(body["recommendations"]) < 3)
        conn = connect(agent_app.state.settings.db_path)
        try:
            for rec in body["recommendations"]:
                product = json.loads(conn.execute("SELECT payload FROM products WHERE product_id=?",
                                                 (rec["product"]["product_id"],)).fetchone()[0])
                mode = next(m for m in product["connection_modes"] if m["mode_id"] == rec["offer"]["mode_id"])
                assert mode["microphone_supported"] is False
        finally:
            conn.close()


@pytest.mark.parametrize("sid, query", [
    ("SC12", "600 元以内，户外跑步，必须无线和不堵耳道，想留意周围声音，尽量稳。"),
    ("SC14", "500 元以内，必须是真无线入耳耳塞、附带小号可替换耳塞套，只接受明确允许开封试戴后按条款退货的报价，尽量轻。"),
])
def test_s51_positive_requirements_public_api_selected_facts(agent_app, agent_client, sid, query):
    agent_app.state.clock = lambda: datetime(2026, 9, 29, 10, tzinfo=derive.CN_TZ)
    response = post_query(agent_client, {"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] in {"ok", "partial"}
    assert body["recommendations"]
    conn = connect(agent_app.state.settings.db_path)
    try:
        for rec in body["recommendations"]:
            product = json.loads(conn.execute("SELECT payload FROM products WHERE product_id=?",
                                             (rec["product"]["product_id"],)).fetchone()[0])
            mode = next(m for m in product["connection_modes"] if m["mode_id"] == rec["offer"]["mode_id"])
            assert mode["transport"] in {"bluetooth", "wireless_dongle"}
            if sid == "SC12":
                assert product["open_ear"] is True
                assert product["form_factor"] != "in_ear"
            else:
                assert product["sub_category"] == "true_wireless_earbuds"
                assert product["form_factor"] == "in_ear"
                assert "S" in product["ear_tip_sizes"]
                offer = json.loads(conn.execute("SELECT payload FROM offers WHERE offer_id=?",
                                               (rec["offer"]["offer_id"],)).fetchone()[0])
                merchant = json.loads(conn.execute("SELECT payload FROM merchants WHERE merchant_id=?",
                                                  (offer["merchant_id"],)).fetchone()[0])
                policy = (offer["return_policy_override"] if offer["return_policy_override"] is not None
                          else merchant["default_return_policy"])
                assert rec["offer"]["effective_return_policy"] == policy
                assert policy["opened_return_allowed"] is True
                assert policy["conditions"] in {"包装和配件齐全", "配件齐全且包装完好", "包装配件齐全，支持开封试戴"}
                assert policy["exceptions"] == []
    finally:
        conn.close()


def test_s51_relative_delivery_requires_specific_clarification_and_no_plans(agent_app, agent_client):
    agent_app.state.clock = lambda: datetime(2026, 9, 29, 10, tzinfo=derive.CN_TZ)
    response = post_query(agent_client, {"query": "600 元以内，三天后要送朋友，必须全新、能在截止前送达，希望保修和退换方便。"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "needs_clarification"
    assert body["recommendations"] == []
    assert any(c["field"] == "hard_constraints.delivery_requirement.deadline_date"
               and c["reason"] == "unsupported_requirement" for c in body["intent"]["clarifications"])
