"""Public Demo implementation."""
from __future__ import annotations

import json

import pytest

from backend.ranking.offer_ranker import recommend
from backend.services.explanation_service import build_explanations
from backend.services.formatting_service import (
    DEFAULT_MAX_CANDIDATES,
    MAX_MAX_CANDIDATES,
    MIN_MAX_CANDIDATES,
    format_context,
)
from tests.test_explanation import _candidate
from tests.test_ranking import _intent

#: 多候选场景：价格/舒适度差异保证排序稳定且互不相同
_MANY = [
    _candidate(f"P{n:03d}", comfort=90 - n, price=30000 + n * 5000, min_days=1 + n % 3,
               merchant=f"M{n % 3 + 1:03d}")
    for n in range(12)
]
_INTENT = _intent(preferences={"price": .4, "comfort": .3, "battery": .15, "weight": .15},
                  budget=100000)


def _recs(candidates=None):
    return recommend(candidates if candidates is not None else _MANY, _INTENT)


def _report(recs):
    return build_explanations(recs, _INTENT)


# ---------------------------------------------------------------------------
# 候选数量与参数边界
# ---------------------------------------------------------------------------

def test_default_caps_at_10_ranked_candidates():
    context = format_context(_INTENT, _recs())
    assert len(context.candidates) == DEFAULT_MAX_CANDIDATES
    assert context.total_candidates == 12
    assert [c.rank for c in context.candidates] == list(range(1, 11))
    # 候选顺序必须与传入排序一致（上下文即名次，模型不得也不需要重排）
    ranked_ids = [rec.product_score.product_id for rec in _recs()]
    assert [c.product_id for c in context.candidates] == ranked_ids[:10]


def test_max_candidates_only_accepts_10_to_20():
    for value in (MIN_MAX_CANDIDATES, 15, MAX_MAX_CANDIDATES):
        # 12 个候选：上限 ≥12 时全量，=10 时截到 10
        assert len(format_context(_INTENT, _recs(), max_candidates=value).candidates) == min(value, 12)
    for value in (0, 1, 9, 21, 25):
        with pytest.raises(ValueError, match="max_candidates"):
            format_context(_INTENT, _recs(), max_candidates=value)


def test_char_cap_must_be_positive():
    for value in (0, -1):
        with pytest.raises(ValueError, match="char_cap"):
            format_context(_INTENT, _recs(), char_cap=value)


def test_zero_candidates_context_is_empty_but_valid():
    context = format_context(_INTENT, [])
    assert context.candidates == ()
    assert context.snippets == ()
    assert context.catalog.items == ()
    assert context.decision["candidate_count"] == 0
    payload = json.loads(context.rendered)
    assert payload["candidates"] == []
    assert payload["selection_catalog"]["items"] == []


# ---------------------------------------------------------------------------
# 无关字段与密钥（验收：上下文没有全量数据库、密钥和无关字段）
# ---------------------------------------------------------------------------

def test_context_has_no_secret_or_irrelevant_fields(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-do-not-leak")
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "bridge-secret-do-not-leak")
    rendered = format_context(_INTENT, _recs()).rendered
    assert "sk-test-secret-do-not-leak" not in rendered
    assert "bridge-secret-do-not-leak" not in rendered
    
    # 模型只做表达选择，不需要也不应看到 fact_refs
    for forbidden in ("commission", "api_key", "apiKey", "field_sources",
                      "valid_until", "fact_refs"):
        assert forbidden not in rendered, forbidden


def test_context_keys_are_exactly_the_expected_set():
    payload = json.loads(format_context(_INTENT, _recs()).rendered)
    assert set(payload) == {"intent", "decision", "candidates", "selection_catalog"}
    candidate = payload["candidates"][0]
    assert set(candidate) == {
        "rank", "product_id", "brand", "product_name", "sub_category", "form_factor",
        "platform_steered", "score", "base_score", "lever", "offer", "score_components",
    }
    catalog_item = payload["selection_catalog"]["items"][0]
    assert set(catalog_item) == {"statement_id", "rank", "category", "variants"}
    assert set(catalog_item["variants"][0]) == {"variant_id", "text"}


# ---------------------------------------------------------------------------
# 事实保留：来源、单位、Offer、贡献；未知保持未知
# ---------------------------------------------------------------------------

def test_offer_price_units_and_refs_are_preserved():
    recs = _recs()
    context = format_context(_INTENT, recs)
    top = recs[0]
    fact = context.candidates[0]
    assert fact.offer.offer_id == top.recommended.offer_id
    assert fact.offer.landed_price_cents == top.recommended.plan.landed
    assert fact.offer.mode_id == top.recommended.mode_id
    price_component = next(c for c in fact.components if c.side == "offer" and c.dimension == "price")
    assert price_component.unit == "CNY_cent"
    assert price_component.source_refs


def test_unknown_dimensions_stay_unknown():
    # P002 无续航规格：battery 权重>0 时必须是 missing 且 raw_value=None
    unknown_battery = _candidate("P002", comfort=80, price=40000, battery=False)
    context = format_context(_INTENT, recommend([unknown_battery], _INTENT))
    battery = next(c for c in context.candidates[0].components if c.dimension == "battery")
    assert battery.missing is True
    assert battery.raw_value is None
    compact = context.rendered.replace(" ", "")
    assert '"missing":true' in compact
    assert '"raw_value":null' in compact


def test_catalog_carries_backend_texts_positionally():
    recs = _recs()
    report = _report(recs)
    context = format_context(_INTENT, recs, report=report)
    assert context.catalog.catalog_id  # 一次性目录标识
    total = sum(len(getattr(entry, cat))
                for entry in report.recommendations
                for cat in ("reason", "strengths", "weaknesses", "trade_offs"))
    assert len(context.catalog.items) == total
    deterministic = report.recommendations[0].reason[0]
    item = context.catalog.item_by_id("r1.reason.0")
    assert item is not None and item.canonical == deterministic.text
    # 渲染目录与内部目录一致，canonical 首位
    payload = json.loads(context.rendered)
    rendered_item = next(i for i in payload["selection_catalog"]["items"]
                         if i["statement_id"] == "r1.reason.0")
    assert rendered_item["variants"][0] == {
        "variant_id": "canonical", "text": deterministic.text}


def test_catalog_syncs_with_truncation_groups():
    recs = _recs()
    report = _report(recs)
    full = format_context(_INTENT, recs, report=report, char_cap=10**9)
    partial_cap = next(cap for cap in range(6000, len(full.rendered), 250)
                       if (ctx := format_context(_INTENT, recs, report=report,
                                                 char_cap=cap)).snippets
                       and len(ctx.catalog.items) < len(full.catalog.items))
    partial = format_context(_INTENT, recs, report=report, char_cap=partial_cap)
    visible_keys = set(partial.snippet_counts())
    assert all((item.rank, item.category) in visible_keys for item in partial.catalog.items)
    # 隐藏组不进可见目录（S32R2-03：不要求占位选择）
    hidden_keys = set(full.snippet_counts()) - visible_keys
    if hidden_keys:
        assert not any((item.rank, item.category) in hidden_keys
                       for item in partial.catalog.items)


def test_default_cap_does_not_truncate_small_context():
    context = format_context(_INTENT, _recs())
    assert context.truncated is False
    assert context.overflow is False
    assert context.dropped_candidate_count == 0
    assert len(context.rendered) <= context.char_cap


# ---------------------------------------------------------------------------
# 截断级联（S32R 观察项整改：始终合法 JSON；整组丢弃；overflow 标志）
# ---------------------------------------------------------------------------

def test_rendered_is_always_valid_json_under_any_cap():
    for cap in (4000, 2500, 1200, 500, 100):
        context = format_context(_INTENT, _recs(), char_cap=cap)
        # 任何截断后仍是合法 JSON；overflow 时允许超上限（调用方跳过模型）
        payload = json.loads(context.rendered)
        assert payload["candidates"], cap
        if not context.overflow:
            assert len(context.rendered) <= cap
        assert context.truncated is True
    # 极小上限：连最小上下文都放不下
    overflowed = format_context(_INTENT, _recs(), char_cap=100)
    assert overflowed.overflow is True


def test_snippet_groups_are_dropped_atomically():
    recs = _recs()
    report = _report(recs)
    full_counts = {
        (entry.rank, category): len(getattr(entry, category))
        for entry in report.recommendations
        for category in ("reason", "strengths", "weaknesses", "trade_offs")
    }
    for cap in (6000, 8000, 10000, 12000, 14000):
        context = format_context(_INTENT, recs, char_cap=cap)
        # 可见组要么整组在场（数量=报告数量），要么整组被丢弃（不在计数里）
        for (rank, category), count in context.snippet_counts().items():
            assert count == full_counts[(rank, category)], (cap, rank, category)
        assert len(context.candidates) >= 1
        assert len(context.rendered) <= cap or context.overflow


def test_overflow_skips_hard_slice_and_keeps_structure():
    context = format_context(_INTENT, _recs(), char_cap=100)
    assert context.overflow is True
    payload = json.loads(context.rendered)  # 合法 JSON，而非被硬切的残串
    assert payload["selection_catalog"]["items"] == []
    assert len(context.candidates) == 1  # 候选也减到 1 仍放不下才 overflow
