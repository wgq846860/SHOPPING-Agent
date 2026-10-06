"""S32R2 整改验收：受控表达目录（expression_catalog）的事实安全。

交接文档验收矩阵要求"验证目录本身没有生成同类错误"——只证明新协议
拒绝字符串不够，必须直接检验模板渲染：数值按字段绑定（不借值/反号/
换单位）、命题状态（未知/开封退货）按原命题保持、canonical 与确定性
原文逐字一致、漂移降级为仅原文、无模板语句不模板化。
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.ranking.offer_ranker import recommend
from backend.services.explanation_service import ExpressionSpec, build_explanations
from backend.services.expression_catalog import (
    CANONICAL_VARIANT_ID,
    build_catalog,
)
from backend.services.formatting_service import STATEMENT_CATEGORIES
from tests.test_explanation import _candidate
from tests.test_ranking import _intent

_CANDIDATES = [
    _candidate("P001", comfort=90, price=40000, min_days=1, opened=False),
    _candidate("P002", comfort=60, price=30000, min_days=3, merchant="M002", battery=False),
    _candidate("P003", comfort=75, price=50000, min_days=2, merchant="M003"),
]
_INTENT = _intent(preferences={"price": .4, "comfort": .3, "battery": .15, "weight": .15,
                               "shipping": .5, "merchant_service": .5}, budget=100000)


def _catalog():
    recs = recommend(_CANDIDATES, _INTENT)
    report = build_explanations(recs, _INTENT)
    return report, build_catalog(report, catalog_id="test-catalog")


def _items_by_family(report, catalog):
    by_family: dict[str, list] = {}
    for entry in report.recommendations:
        for category in STATEMENT_CATEGORIES:
            for index in range(len(getattr(entry, category))):
                key = (entry.rank, category, index)
                if key in report.expression_specs:
                    item = catalog.item_by_id(f"r{entry.rank}.{category}.{index}")
                    by_family.setdefault(report.expression_specs[key].family, []).append(item)
    return by_family


# ---------------------------------------------------------------------------
# 目录覆盖与 canonical 一致性
# ---------------------------------------------------------------------------

def test_catalog_covers_every_statement_with_positional_ids():
    report, catalog = _catalog()
    total = sum(len(getattr(entry, category))
                for entry in report.recommendations for category in STATEMENT_CATEGORIES)
    assert len(catalog.items) == total
    for entry in report.recommendations:
        for category in STATEMENT_CATEGORIES:
            for index, statement in enumerate(getattr(entry, category)):
                item = catalog.item_by_id(f"r{entry.rank}.{category}.{index}")
                assert item is not None
                assert item.canonical == statement.text  # 原文由后端持有
                assert item.variants[0].variant_id == CANONICAL_VARIANT_ID
                assert item.variants[0].text == statement.text


def test_unsupported_statements_stay_canonical_only():
    report, catalog = _catalog()
    supported = set(report.expression_specs)  # (rank, category, index)
    for item in catalog.items:
        key = (item.rank, item.category, item.index)
        if key not in supported:
            assert len(item.variants) == 1, item.statement_id


# ---------------------------------------------------------------------------
# 数值角色（S32R2-01）：金额按字段绑定，不借值/不反号/不换单位
# ---------------------------------------------------------------------------

def test_budget_family_binds_amounts_to_fields():
    report, catalog = _catalog()
    items = _items_by_family(report, catalog)["budget_landed"]
    assert items, "锚点缺失：预算语句"
    assert (report.recommendations[0].recommended.landed, _INTENT.budget.max_cents) == (40000, 100000)
    for item in items:
        entry = report.recommendations[item.rank - 1]
        landed = entry.recommended.landed  # 每个条目用自己的 Offer 金额
        budget = _INTENT.budget.max_cents
        texts = [v.text for v in item.variants]
        assert len(texts) >= 2, "预算语句必须有非原文安全措辞"
        for text in texts:
            # 每个版本内两个金额都来自原字段
            assert str(landed) in text and str(budget) in text
        joined = " ".join(texts)
        # 角色不可交换（S32R2-01 反例：到手价/预算互换）
        assert f"到手价{budget}" not in joined and f"预算上限{landed}" not in joined
        # 符号与单位不可变化（负号、角/元等其他单位不出现）
        for bad in (f"-{landed}", "角", "元", "%"):
            assert bad not in joined, bad


def test_comparison_and_price_statements_are_never_templated():
    # 比较句/价格取舍句暂不支持模板：必须保持原文（比较双方/差值/范围不动）
    report, catalog = _catalog()
    templated = {item.statement_id for item in catalog.items if len(item.variants) > 1}
    for entry in report.recommendations:
        for index, statement in enumerate(entry.trade_offs):
            assert f"r{entry.rank}.trade_offs.{index}" not in templated


# ---------------------------------------------------------------------------
# 命题状态（S32R2-02）：未知与开封退货按原命题保持
# ---------------------------------------------------------------------------

def test_unknown_family_keeps_unknown_proposition():
    report, catalog = _catalog()
    items = _items_by_family(report, catalog)["unknown_dimension"]
    assert items, "锚点缺失：续航未知语句"
    for item in items:
        assert len(item.variants) >= 2
        for variant in item.variants:
            assert "未知" in variant.text or "缺失" in variant.text
            for affirmative in ("已知", "充足", "适合全天"):
                assert affirmative not in variant.text, variant.text
            assert "0.5" in variant.text  # 中性处理如实保留
        # 主体不可换：续航语句的版本不能谈论价格/重量等其他维度
        assert all("价格" not in v.text and "重量" not in v.text for v in item.variants)


# ---------------------------------------------------------------------------
# S32R3-01：0.5 是归一化值，不是对 0–100 总分的贡献
# contribution = normalized_score(0.5) × weight × 100
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prefs,expected_weight,contribution_text", [
    ({"price": .4, "comfort": .3, "battery": .15, "weight": .15}, 0.15, "7.5"),
    ({"battery": .5, "comfort": .5}, 0.50, "25"),
])
def test_unknown_variants_distinguish_normalized_from_contribution(
        prefs, expected_weight, contribution_text):
    intent = _intent(preferences=prefs, budget=100000)
    candidates = [_candidate("PX", comfort=70, price=30000, battery=False)]
    report = build_explanations(recommend(candidates, intent), intent)
    key = next(key for key, spec in report.expression_specs.items()
               if spec.family == "unknown_dimension")
    component = next(c for c in report.recommendations[key[0] - 1].product_score.components
                     if c.dimension == "battery")
    assert component.missing and component.normalized_score == 0.5
    assert abs(component.contribution - 0.5 * expected_weight * 100) < 1e-9
    item = build_catalog(report, "weights").item_by_id(f"r{key[0]}.{key[1]}.{key[2]}")
    assert item and len(item.variants) >= 2
    for variant in item.variants:
        assert "0.5" in variant.text, variant.text            # 归一化中性值在场
        assert "计入总分" not in variant.text, variant.text     # 不得声称直接进总分
        assert "0.5分" not in variant.text, variant.text        # 0.5 不得被写成"分"
        assert f"{contribution_text}分" not in variant.text, variant.text  # 不冒充真实贡献
        assert f"{expected_weight:.2f}" in variant.text, variant.text  # 权重如实
    reason_first = next(v for v in item.variants if v.variant_id == "reason_first")
    assert "归一化" in reason_first.text  # S32R3-01 修法：明确写归一化值


def test_opened_return_family_keeps_negative_proposition():
    report, catalog = _catalog()
    items = _items_by_family(report, catalog)["opened_return_blocked"]
    assert items, "锚点缺失：不支持开封退货语句"
    for item in items:
        assert len(item.variants) >= 2
        for variant in item.variants:
            assert ("不支持" in variant.text) or ("无法" in variant.text)
            # 注意"不支持开封退货"包含子串"支持开封退货"，用完整主谓判断
            for affirmative in ("该报价支持", "可以开封", "开封可退", "可退货"):
                assert affirmative not in variant.text, variant.text


def test_variant_texts_are_distinct_and_nonempty():
    _, catalog = _catalog()
    for item in catalog.items:
        texts = [v.text for v in item.variants]
        assert all(texts), item.statement_id
        assert len(set(texts)) == len(texts), item.statement_id
        for text in texts:
            assert text.endswith("。") and "\n" not in text


# ---------------------------------------------------------------------------
# 漂移防护：canonical 模板与原文不一致时降级为仅原文
# ---------------------------------------------------------------------------

def test_spec_drift_degrades_to_canonical_only():
    report, _ = _catalog()
    doctored = dict(report.expression_specs)
    key = next(key for key, spec in doctored.items() if spec.family == "budget_landed")
    doctored[key] = ExpressionSpec("budget_landed", {
        "offer_id": doctored[key].params["offer_id"],
        "landed_cents": doctored[key].params["landed_cents"],
        "budget_cents": doctored[key].params["budget_cents"] + 1,  # 模拟句型/参数漂移
    })
    drifted = dataclasses.replace(report, expression_specs=doctored)
    item = build_catalog(drifted, "drift").item_by_id(
        f"r{key[0]}.{key[1]}.{key[2]}")
    assert item.canonical == getattr(
        drifted.recommendations[key[0] - 1], key[1])[key[2]].text
    assert len(item.variants) == 1  # 不提供可能与原文命题不一致的版本


def test_missing_param_degrades_to_canonical_only():
    report, _ = _catalog()
    key = next(key for key, spec in report.expression_specs.items()
               if spec.family == "unknown_dimension")
    doctored = dict(report.expression_specs)
    doctored[key] = ExpressionSpec("unknown_dimension", {"label": "续航"})  # 缺 weight_text
    drifted = dataclasses.replace(report, expression_specs=doctored)
    item = build_catalog(drifted, "missing").item_by_id(f"r{key[0]}.{key[1]}.{key[2]}")
    assert len(item.variants) == 1
