"""S3.2 验收测试：受控表达选择协议与确定性回退（agent/explainer.py）。

覆盖 s3.md S3.2 验收口径、S32R/R2/R3 审验反例与交接验收矩阵：
- 模型只选表达：合法选择按目录套用版本文本，引用与 Comparison 按位置
  继承；文本/槽值/引用/比较没有可写字段（S32R2-01/02 攻击不可表达）；
- S32R3-01：未知维度全部版本区分归一化值 0.5 与总分贡献（详测在
  test_expression_catalog，此处验证套用后的文本）；
- S32R3-02：入口无 context 注入；测试从**实际提示词**解析目录构造选择
  （应答器模式），同一响应重放到下一次调用必然 stale_catalog_id 回退；
- 协议拒绝/裁剪/失败保护/计数口径同交接矩阵。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

import pytest

from backend.agent.explainer import explain_recommendations
from backend.agent.harness_client import (
    HarnessTimeoutError,
    HarnessTurnResult,
    HarnessUnavailableError,
)
from backend.ranking.offer_ranker import recommend
from backend.services.explanation_service import build_explanations
from backend.services.formatting_service import STATEMENT_CATEGORIES, format_context
from tests.test_explanation import _candidate
from tests.test_ranking import _intent

#: 与审验探针同构：P001 不支持开封退货、P002 续航未知、预算 100000
_CANDIDATES = [
    _candidate("P001", comfort=90, price=40000, min_days=1, opened=False),
    _candidate("P002", comfort=60, price=30000, min_days=3, merchant="M002", battery=False),
    _candidate("P003", comfort=75, price=50000, min_days=2, merchant="M003"),
]
_INTENT = _intent(preferences={"price": .4, "comfort": .3, "battery": .15, "weight": .15,
                               "shipping": .5, "merchant_service": .5}, budget=100000)


def _pipeline(candidates=None, intent=None):
    intent = intent or _INTENT
    recs = recommend(candidates if candidates is not None else _CANDIDATES, intent)
    return recs, build_explanations(recs, intent), intent


@dataclass
class FakeHarness:
    """run_intent_turn 替身：responder 从实际提示词即时构造响应并记录。"""

    responder: Callable[[str], str] | None = None
    response: str | None = None
    error: Exception | None = None
    finish_reason: str | None = "stop"
    prompts: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)

    def run_intent_turn(self, prompt: str) -> HarnessTurnResult:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        text = self.responder(prompt) if self.responder is not None else (self.response or "")
        self.responses.append(text)
        return HarnessTurnResult("session-1", text, self.finish_reason)


def _catalog_from_prompt(prompt: str) -> dict:
    context_json = prompt.split("<context>\n", 1)[1].split("\n</context>", 1)[0]
    return json.loads(context_json)["selection_catalog"]


def _selection_payload(prompt: str, pick=None, transform=None) -> dict:
    """从实际提示词解析可见目录并构造合法选择（模型视角）。"""
    catalog = _catalog_from_prompt(prompt)

    def _default(item):
        if pick is not None:
            return pick(item)
        variants = item["variants"]
        return variants[1]["variant_id"] if len(variants) > 1 else variants[0]["variant_id"]

    payload = {
        "catalog_id": catalog["catalog_id"],
        "selections": [
            {"statement_id": item["statement_id"], "variant_id": _default(item)}
            for item in catalog["items"]
        ],
    }
    return transform(payload) if transform is not None else payload


def _harness(pick=None, transform=None, *, error=None, finish_reason="stop", response=None):
    if response is not None or error is not None:
        return FakeHarness(response=response, error=error, finish_reason=finish_reason)
    return FakeHarness(
        responder=lambda prompt: json.dumps(
            _selection_payload(prompt, pick, transform), ensure_ascii=False),
        finish_reason=finish_reason)


def _run(candidates=None, intent=None, *, pick=None, transform=None, char_cap=None,
         error=None, finish_reason="stop"):
    recs, report, used_intent = _pipeline(candidates, intent)
    harness = _harness(pick, transform, error=error, finish_reason=finish_reason)
    options = {} if char_cap is None else {"char_cap": char_cap}
    outcome = explain_recommendations(report, used_intent, recs, harness, **options)
    # 并行上下文仅用于检视目录内容（文本确定性一致；catalog_id 不参与断言）
    parallel = format_context(used_intent, recs, report=report, **options)
    return outcome, report, parallel, harness


def _codes(outcome):
    return {w["code"] for w in outcome.warnings}


def _first_reason_text(report):
    return report.recommendations[0].reason[0].text


# ---------------------------------------------------------------------------
# 正向：模型只选表达，文本由后端目录提供
# ---------------------------------------------------------------------------

def test_valid_selection_applies_catalog_texts_and_keeps_metadata():
    outcome, report, parallel, _ = _run()
    assert outcome.used_llm is True
    assert outcome.warnings == () and outcome.fallback_reason is None
    new_top, old_top = outcome.report.recommendations[0], report.recommendations[0]
    budget_item = parallel.catalog.item_by_id("r1.reason.0")
    selected = budget_item.variants[1].text
    assert selected != old_top.reason[0].text
    assert new_top.reason[0].text == selected
    assert new_top.reason[0].fact_refs == old_top.reason[0].fact_refs
    for category in ("trade_offs", "weaknesses"):
        for new_statement, old_statement in zip(getattr(new_top, category),
                                                getattr(old_top, category)):
            assert new_statement.comparison is old_statement.comparison
    # 两个事实族都有真实不同的措辞生效（预算 + 未知/开封退货）
    changed = sum(
        1
        for cat in STATEMENT_CATEGORIES
        for new_statement, old_statement in zip(getattr(new_top, cat), getattr(old_top, cat))
        if new_statement.text != old_statement.text
    )
    assert changed >= 2


def test_unknown_family_selection_keeps_normalized_wording():
    # S32R3-01：套用后的未知维度文本不得声称 0.5 直接进总分
    outcome, report, _, _ = _run()
    for new_entry, old_entry in zip(outcome.report.recommendations, report.recommendations):
        for new_statement, old_statement in zip(new_entry.weaknesses, old_entry.weaknesses):
            if "未知" in old_statement.text:
                assert "计入总分" not in new_statement.text
                assert "0.5分" not in new_statement.text
                assert "0.5" in new_statement.text


def test_prompt_uses_selection_protocol_and_context_block():
    _, _, _, harness = _run()
    (prompt,) = harness.prompts
    assert "selection_catalog" in prompt and "catalog_id" in prompt
    assert "variant_id" in prompt
    assert "<context>" in prompt and "</context>" in prompt
    assert "API_KEY" not in prompt


def test_zero_candidates_skips_model_call_entirely():
    recs, report, intent = _pipeline([])
    harness = FakeHarness()
    outcome = explain_recommendations(report, intent, recs, harness)
    assert harness.prompts == []
    assert outcome.used_llm is False
    assert outcome.warnings == ()
    assert outcome.fallback_reason is None  # 无可解释对象，不是失败


# ---------------------------------------------------------------------------
# S32R3-02：无注入入口；重放响应必然过期
# ---------------------------------------------------------------------------

def test_replayed_selection_response_rejected_on_second_call():
    recs, report, intent = _pipeline()
    first = _harness()
    outcome1 = explain_recommendations(report, intent, recs, first)
    assert outcome1.used_llm is True
    # 同一响应重放：第二次调用生成全新目录，旧 catalog_id 必然过期
    replay = FakeHarness(response=first.responses[0])
    outcome2 = explain_recommendations(report, intent, recs, replay)
    assert outcome2.used_llm is False
    assert "stale_catalog_id" in outcome2.fallback_reason
    assert _codes(outcome2) == {"EXPLANATION_FALLBACK"}
    # 重放不改变已算出的推荐
    assert outcome2.report.recommendations[0].reason[0].text == _first_reason_text(report)


# ---------------------------------------------------------------------------
# R2 事实反例（S32R2-01/02）：自由文本协议整体不可用，逐例拒绝
# ---------------------------------------------------------------------------

_R2_FREE_TEXT_COUNTEREXAMPLES = [
    ("swap_price_and_budget_roles", "报价OP001到手价100000分，满足40000分预算上限。"),
    ("negative_price", "报价OP001到手价-40000分，满足100000分预算上限。"),
    ("change_cents_to_jiao", "报价OP001到手价40000角，满足100000角预算上限。"),
    ("add_lifetime_warranty", "报价OP001到手价40000分，满足100000分预算上限，享受终身保修。"),
    ("add_new_chinese_entity", "报价OP001到手价40000分，满足100000分预算上限，由新声旗舰店提供。"),
]


@pytest.mark.parametrize("name,rewrite", _R2_FREE_TEXT_COUNTEREXAMPLES,
                         ids=[name for name, _ in _R2_FREE_TEXT_COUNTEREXAMPLES])
def test_r2_free_text_counterexamples_rejected(name, rewrite):
    recs, report, intent = _pipeline()
    payload = {"recommendations": [
        {"rank": entry.rank, **{
            category: [rewrite if (entry.rank == 1 and category == "reason") else s.text
                       for s in getattr(entry, category)]
            for category in STATEMENT_CATEGORIES}}
        for entry in report.recommendations]}
    harness = FakeHarness(response=json.dumps(payload, ensure_ascii=False))
    result = explain_recommendations(report, intent, recs, harness)
    assert result.used_llm is False
    assert "unexpected_top_level_keys" in result.fallback_reason
    assert result.report.recommendations[0].reason[0].text == _first_reason_text(report)


def test_r2_predicate_and_scope_counterexamples_not_expressible():
    _, report, parallel, _ = _run()
    for item in parallel.catalog.items:
        if item.family == "opened_return_blocked":
            assert all(("不支持" in v.text or "无法" in v.text) for v in item.variants)
            assert all("拒绝" not in v.text for v in item.variants)
        if item.family == "unknown_dimension":
            assert all(("未知" in v.text or "缺失" in v.text) for v in item.variants)
        if item.family == "unsupported" and item.category == "trade_offs":
            assert len(item.variants) == 1  # 比较句不提供非原文版本


# ---------------------------------------------------------------------------
# 选择协议拒绝矩阵
# ---------------------------------------------------------------------------

def _rejects(transform, reason_part, *, char_cap=None):
    recs, report, intent = _pipeline()
    harness = _harness(transform=transform)
    result = explain_recommendations(
        report, intent, recs, harness, **({} if char_cap is None else {"char_cap": char_cap}))
    assert result.used_llm is False
    assert reason_part in result.fallback_reason, result.fallback_reason
    assert _codes(result) == {"EXPLANATION_FALLBACK"}


def test_stale_catalog_id_rejected():
    _rejects(lambda p: {**p, "catalog_id": "an-old-catalog-id"}, "stale_catalog_id")


def test_extra_top_level_key_rejected():
    _rejects(lambda p: {**p, "note": "extra"}, "unexpected_top_level_keys")


def test_selection_with_text_field_rejected():
    def _transform(payload):
        first = payload["selections"][0]
        payload["selections"][0] = {**first, "text": "到手价1分"}
        return payload
    _rejects(_transform, "selection_shape")


def test_unknown_statement_rejected():
    _rejects(lambda p: {**p, "selections": [
        *p["selections"], {"statement_id": "r9.reason.0", "variant_id": "canonical"}]},
        "unknown_statement")


def test_duplicated_statement_rejected():
    _rejects(lambda p: {**p, "selections": [*p["selections"], dict(p["selections"][0])]},
             "duplicated_statement")


def test_incomplete_coverage_rejected():
    _rejects(lambda p: {**p, "selections": p["selections"][:-1]}, "incomplete_coverage")


def test_foreign_variant_rejected():
    def _transform(payload):
        first = payload["selections"][0]
        victim = next(s for s in payload["selections"] if s is not first)
        payload["selections"] = [
            {**s, "variant_id": first["variant_id"]}
            if s["statement_id"] == victim["statement_id"] else s
            for s in payload["selections"]]
        return payload
    _rejects(_transform, "foreign_variant")


def test_bogus_variant_rejected():
    _rejects(lambda p: {**p, "selections": [
        {**s, "variant_id": "made_up"} if i == 0 else s
        for i, s in enumerate(p["selections"])]}, "foreign_variant")


def test_non_string_ids_rejected():
    _rejects(lambda p: {**p, "selections": [
        {"statement_id": 1, "variant_id": "canonical"}]}, "statement_id_not_string")


def test_selections_not_list_rejected():
    _rejects(lambda p: {**p, "selections": {"r1.reason.0": "canonical"}}, "selections_not_list")


# ---------------------------------------------------------------------------
# 裁剪（S32R2-03）：可见目录选择被接受，隐藏语句不要求占位
# ---------------------------------------------------------------------------

def _partial_cap():
    recs, report, intent = _pipeline()
    full = format_context(intent, recs, report=report, char_cap=10**9)
    return next(cap for cap in range(6000, len(full.rendered), 250)
                if (ctx := format_context(intent, recs, report=report,
                                          char_cap=cap)).snippets
                and len(ctx.catalog.items) < len(full.catalog.items))


def test_partial_catalog_selection_accepted_and_hidden_kept_canonical():
    cap = _partial_cap()
    # 应答器按实际提示词（只含可见目录）构造选择——旧协议会错拒此场景
    outcome, report, parallel, _ = _run(char_cap=cap)
    assert outcome.used_llm is True
    assert outcome.fallback_reason is None
    visible = {item.statement_id for item in parallel.catalog.items}
    full_count = sum(len(getattr(report.recommendations[r], cat))
                     for r in range(len(report.recommendations))
                     for cat in STATEMENT_CATEGORIES)
    assert 0 < len(visible) < full_count
    for entry in outcome.report.recommendations:
        for category in STATEMENT_CATEGORIES:
            for index, statement in enumerate(getattr(entry, category)):
                if f"r{entry.rank}.{category}.{index}" not in visible:
                    original = getattr(report.recommendations[entry.rank - 1], category)[index]
                    assert statement.text == original.text
                    assert statement.fact_refs == original.fact_refs


def test_selection_for_hidden_statement_rejected():
    cap = _partial_cap()
    recs, report, intent = _pipeline()
    full = format_context(intent, recs, report=report, char_cap=10**9)
    partial = format_context(intent, recs, report=report, char_cap=cap)
    hidden = next(item.statement_id for item in full.catalog.items
                  if partial.catalog.item_by_id(item.statement_id) is None)

    def _transform(payload):
        payload["selections"].append({"statement_id": hidden, "variant_id": "canonical"})
        return payload

    harness = _harness(transform=_transform)
    outcome = explain_recommendations(report, intent, recs, harness, char_cap=cap)
    assert outcome.used_llm is False
    assert "unknown_statement" in outcome.fallback_reason


@pytest.mark.parametrize("cap", [100, 4000])
def test_overflow_caps_skip_model_call(cap):
    recs, report, intent = _pipeline()
    harness = FakeHarness()
    outcome = explain_recommendations(report, intent, recs, harness, char_cap=cap)
    assert harness.prompts == []
    assert outcome.used_llm is False
    assert outcome.fallback_reason == "context_overflow"
    assert _codes(outcome) == {"EXPLANATION_FALLBACK"}


# ---------------------------------------------------------------------------
# 计数口径：零改写不计成功
# ---------------------------------------------------------------------------

def test_all_canonical_selection_is_zero_rewrites():
    outcome, report, _, harness = _run(pick=lambda item: item["variants"][0]["variant_id"])
    assert harness.prompts  # 模型被真实调用
    assert outcome.used_llm is False
    assert outcome.fallback_reason == "zero_rewrites"
    assert _codes(outcome) == {"EXPLANATION_FALLBACK"}
    for new_entry, old_entry in zip(outcome.report.recommendations, report.recommendations):
        for category in STATEMENT_CATEGORIES:
            assert getattr(new_entry, category) == getattr(old_entry, category)


def test_no_variant_expressions_skips_model_call():
    # 无预算、无未知维度、可开封退货：目录只有原文版本
    intent = _intent(preferences={"comfort": 1})
    candidates = [_candidate("PA", comfort=90, price=40000, opened=True)]
    recs, report, _ = _pipeline(candidates, intent)
    context = format_context(intent, recs, report=report)
    assert context.has_variant_items() is False
    harness = FakeHarness()
    outcome = explain_recommendations(report, intent, recs, harness)
    assert harness.prompts == []
    assert outcome.used_llm is False
    assert outcome.fallback_reason == "no_variant_expressions"


# ---------------------------------------------------------------------------
# 失败保护：软回退，已算出的推荐对象不变
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,reason_prefix", [
    (HarnessTimeoutError("boom"), "timeout"),
    (HarnessUnavailableError("boom"), "unavailable"),
])
def test_transport_errors_fall_back(error, reason_prefix):
    recs, report, intent = _pipeline()
    outcome = explain_recommendations(report, intent, recs, FakeHarness(error=error))
    assert outcome.used_llm is False
    assert outcome.fallback_reason.startswith(reason_prefix)
    assert _codes(outcome) == {"EXPLANATION_FALLBACK"}


@pytest.mark.parametrize("finish_reason,reason_part", [
    ("error", "model_turn_failed"),
    ("length", "truncated_output"),
])
def test_bad_finish_reasons_fall_back_even_with_valid_selection(finish_reason, reason_part):
    outcome, _, _, _ = _run(finish_reason=finish_reason)
    assert outcome.used_llm is False
    assert reason_part in outcome.fallback_reason
    assert outcome.finish_reason == finish_reason


@pytest.mark.parametrize("response", ["这不是JSON", "[1,2,3]", ""])
def test_unparseable_output_falls_back(response):
    recs, report, intent = _pipeline()
    outcome = explain_recommendations(report, intent, recs, FakeHarness(response=response))
    assert outcome.used_llm is False and outcome.fallback_reason == "unparseable_output"


def test_deeply_nested_json_falls_back_without_raising():
    recs, report, intent = _pipeline()
    deep = '{"catalog_id":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    outcome = explain_recommendations(report, intent, recs, FakeHarness(response=deep))
    assert outcome.used_llm is False and outcome.fallback_reason


def test_fallback_preserves_structured_facts_verbatim():
    recs, report, intent = _pipeline()
    outcome = explain_recommendations(report, intent, recs, FakeHarness(response="垃圾输出"))
    assert outcome.used_llm is False
    for new_entry, old_entry in zip(outcome.report.recommendations, report.recommendations):
        assert new_entry.product_score is old_entry.product_score
        assert new_entry.recommended is old_entry.recommended
        for category in STATEMENT_CATEGORIES:
            assert getattr(new_entry, category) == getattr(old_entry, category)
    assert outcome.report.decision_summary == report.decision_summary
