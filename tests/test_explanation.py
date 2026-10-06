"""S2.4 acceptance tests: deterministic fact-based explanations.

Anchors the plan S2.4 acceptance list: top-3 cap without duplicate products,
fewer-than-three reported honestly, zero candidates fabricate nothing, price/
weight/delivery differences exactly computable, every statement carries fact
references, and comparative superlatives declare their scope.
"""
from __future__ import annotations

import pytest

from backend.ranking.offer_ranker import recommend
from backend.ranking.product_ranker import IntentDistortion
from backend.services.candidate_service import CandidateSearch
from backend.services.explanation_service import (
    SUPERLATIVE_MARKERS,
    build_explanations,
)
from tests.test_offer_ranking import _cand, _merchant_full, _override_offer, _plan
from tests.test_ranking import _intent, _metrics, _mode, _product

SCOPE_MARKERS = ("本次", "合格报价", "合格商品", "推荐内", "该商品")


def _candidate(product_id, *, comfort, price, grams=225, hours=24, min_days=1,
               service=90, merchant="M001", battery=True, commission=None, opened=True):
    from tests.test_ranking import _battery, _product, _weight
    product = _product(product_id, batteries=[_battery(hours=hours)] if battery else [],
                       weights=[_weight("W1", grams)])
    offers = [_override_offer(f"O{product_id}", product_id, price=price, merchant_id=merchant,
                              min_days=min_days, commission=commission)]
    return _cand(product_id, product=product, metrics=_metrics(product_id, comfort_score=comfort),
                 offers=offers, merchants={merchant: _merchant_full(merchant, service=service,
                                                                   opened=opened)})


def _report(candidates, intent, **kwargs):
    return build_explanations(recommend(candidates, intent), intent, **kwargs)


def _texts(statements):
    return [s.text for s in statements]


# ---------------------------------------------------------------------------
# 数量与诚实性（验收：≤3 不重复 / 不足三个 / 零候选 / 待澄清）
# ---------------------------------------------------------------------------

def test_top3_cap_ranks_contiguous_and_products_unique():
    candidates = [_candidate(f"P{n}", comfort=90 - n, price=30000 + n * 10000) for n in range(4)]
    report = _report(candidates, _intent(preferences={"price": .5, "comfort": .5}, budget=100000))
    assert [r.rank for r in report.recommendations] == [1, 2, 3]
    assert len({r.product_score.product_id for r in report.recommendations}) == 3
    assert report.decision_summary.candidate_count == 4
    assert report.decision_summary.returned_count == 3
    assert report.decision_summary.message == "4个合格商品，返回前3个"


def test_fewer_than_three_reported_as_is_with_warning():
    candidates = [_candidate("PA", comfort=90, price=40000), _candidate("PB", comfort=60, price=30000)]
    report = _report(candidates, _intent(preferences={"comfort": 1}, budget=100000))
    assert [r.rank for r in report.recommendations] == [1, 2]
    codes = [w["code"] for w in report.warnings]
    assert "INSUFFICIENT_RESULTS" in codes
    assert report.decision_summary.message == "只有2个合格商品"


def test_zero_candidates_fabricate_nothing():
    report = build_explanations([], _intent(preferences={"price": 1}, budget=100000),
                                data_version="small-v1")
    assert report.recommendations == []
    assert report.decision_summary.candidate_count == 0
    assert report.decision_summary.returned_count == 0
    assert report.decision_summary.message == "没有符合硬条件的商品"
    assert report.algorithm_versions == {}
    assert report.data_version == "small-v1"
    assert not any(w["code"] in ("MISSING_SOFT_METRIC", "STALE_DATA") for w in report.warnings)


def test_needs_clarification_search_is_rejected():
    search = CandidateSearch(status="needs_clarification", clarifications=[], candidates=[],
                             filters_applied=[], warnings=[])
    with pytest.raises(ValueError, match="澄清"):
        build_explanations([], _intent(budget=100000), search=search)


# ---------------------------------------------------------------------------
# 引用与比较词范围（验收：所有理由有依据；"最高/最早"注明范围）
# ---------------------------------------------------------------------------

def test_every_statement_has_refs_and_superlatives_declare_scope():
    candidates = [
        _candidate("PA", comfort=90, price=40000, grams=225, hours=24, min_days=1),
        _candidate("PB", comfort=60, price=30000, grams=250, hours=30, min_days=3, merchant="M002"),
        _candidate("PC", comfort=75, price=50000, grams=230, hours=24, min_days=2, merchant="M003"),
    ]
    intent = _intent(preferences={"price": .4, "comfort": .3, "weight": .15, "battery": .15,
                                  "merchant_service": .5, "shipping": .5}, budget=100000)
    report = _report(candidates, intent)
    for rec in report.recommendations:
        for statement in rec.reason + rec.strengths + rec.weaknesses + rec.trade_offs:
            assert statement.fact_refs, statement.text
            if any(marker in statement.text for marker in SUPERLATIVE_MARKERS):
                assert any(scope in statement.text for scope in SCOPE_MARKERS), statement.text
                if statement.comparison is not None:
                    assert statement.comparison.scope


# ---------------------------------------------------------------------------
# 可核算差异（验收：价格差、重量差、配送差可以核算）
# ---------------------------------------------------------------------------

def _comparisons(rec, field):
    return [s.comparison for s in rec.trade_offs if s.comparison and s.comparison.field == field]


def test_price_score_and_delivery_diffs_are_exact():
    candidates = [
        _candidate("PA", comfort=90, price=40000, min_days=1),
        _candidate("PB", comfort=60, price=30000, min_days=3, merchant="M002"),
    ]
    intent = _intent(preferences={"price": .4, "comfort": .3, "shipping": .3}, budget=100000)
    report = _report(candidates, intent)
    top, second = report.recommendations
    price = _comparisons(top, "landed_price_cents")
    assert len(price) == 1 and price[0].difference == 40000 - 30000
    assert isinstance(price[0].difference, int) and price[0].unit == "CNY_cent"
    assert price[0].other_product_id == "PB" and price[0].other_offer_id == "OPB"
    # S2A-04：适配陈述按 base_score（正常模式下与排序分相等）
    score = _comparisons(top, "base_score")
    assert len(score) == 1
    assert score[0].difference == pytest.approx(top.product_score.base_score - second.product_score.base_score)
    # S2A-07：配送比较按各自 delivery_window.earliest（同观测日 fixture 下差值=1-3 天）
    delivery = _comparisons(top, "delivery_window.earliest")
    assert len(delivery) == 1 and delivery[0].difference == 1 - 3 and delivery[0].unit == "day"


def test_weight_diff_only_on_same_basis():
    same_basis = [
        _candidate("PA", comfort=90, price=40000, grams=225),
        _candidate("PB", comfort=60, price=30000, grams=250, merchant="M002"),
    ]
    intent = _intent(preferences={"price": .5, "comfort": .3, "weight": .2}, budget=100000)
    report = _report(same_basis, intent)
    weight = _comparisons(report.recommendations[0], "weight.headset")
    assert len(weight) == 1 and weight[0].difference == 225 - 250 and weight[0].unit == "g"
    assert any("同口径headset" in t for t in _texts(report.recommendations[0].trade_offs))
    # 跨口径（headset vs single_earbud）不比较
    from tests.test_ranking import _battery, _product, _weight
    in_ear = _cand("PI", product=_product("PI", form="in_ear",
                                          weights=[_weight("W9", 5, basis="single_earbud")]),
                   metrics=_metrics("PI", comfort_score=70),
                   offers=[_override_offer("OPI", "PI", price=35000, merchant_id="M002")],
                   merchants={"M002": _merchant_full("M002")})
    report2 = _report([_candidate("PA", comfort=90, price=40000), in_ear], intent)
    assert _comparisons(report2.recommendations[0], "weight.headset") == []
    assert _comparisons(report2.recommendations[0], "weight.single_earbud") == []


def test_battery_diff_skipped_when_unknown_and_reported_as_weakness():
    known = _candidate("PA", comfort=90, price=40000, hours=24)
    unknown = _candidate("PB", comfort=60, price=30000, battery=False, merchant="M002")
    intent = _intent(preferences={"price": .4, "comfort": .3, "battery": .3}, budget=100000)
    report = _report([known, unknown], intent)
    assert _comparisons(report.recommendations[0], "battery.single_charge") == []
    weakness = _texts(report.recommendations[1].weaknesses)
    assert any("续航未知" in text and "中性0.5" in text for text in weakness)
    missing = [w for w in report.warnings if w["code"] == "MISSING_SOFT_METRIC" and w["field"] == "battery"]
    assert missing and missing[0]["product_id"] == "PB"


def test_equal_price_and_score_tradeoff_wording():
    candidates = [
        _candidate("PA", comfort=80, price=40000),
        _candidate("PB", comfort=60, price=40000, merchant="M002"),
    ]
    report = _report(candidates, _intent(preferences={"comfort": 1}, budget=100000))
    texts = _texts(report.recommendations[0].trade_offs)
    assert any("相同" in text for text in texts)
    assert all(c.difference == 0 for c in _comparisons(report.recommendations[0], "landed_price_cents"))


# ---------------------------------------------------------------------------
# 优势 / 不足 / 推荐原因的事实依据
# ---------------------------------------------------------------------------

def test_strengths_follow_highest_contributions():
    candidate = _candidate("PA", comfort=90, price=20000)
    intent = _intent(preferences={"price": .3, "comfort": .7}, budget=100000)
    report = _report([candidate], intent)
    strengths = report.recommendations[0].strengths
    assert strengths[0].text.startswith("舒适度维度")
    assert "贡献" in strengths[0].text
    comfort = next(c for c in report.recommendations[0].product_score.components
                   if c.dimension == "comfort")
    assert f"{comfort.contribution:.2f}" in strengths[0].text


def test_opened_return_false_is_a_fact_based_weakness():
    candidate = _candidate("PA", comfort=90, price=40000, merchant="M003", opened=False)
    intent = _intent(preferences={"comfort": 1, "returns": 1}, budget=100000)
    report = _report([candidate], intent)
    weakness = _texts(report.recommendations[0].weaknesses)
    assert any("不支持开封退货" in text for text in weakness)


def test_pricier_than_own_cheapest_offer_is_a_weakness():
    cheap = _override_offer("Ocheap", "PA", price=30000, merchant_id="M003", min_days=1)
    good = _override_offer("Ogood", "PA", price=38000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50), "M001": _merchant_full("M001", service=95)}
    candidate = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[cheap, good],
                      merchants=merchants)
    intent = _intent(preferences={"comfort": .5, "merchant_service": .5}, budget=100000)
    report = _report([candidate], intent)
    rec = report.recommendations[0]
    assert rec.recommended.offer_id == "Ogood"
    premium = [s for s in rec.weaknesses if s.comparison and s.comparison.field == "landed_price_cents"]
    assert len(premium) == 1
    assert premium[0].comparison.difference == 8000 and premium[0].comparison.other_offer_id == "Ocheap"
    assert "该商品全部2条合格报价" in premium[0].comparison.scope


def test_normal_reason_states_offer_service_best_with_scope():
    cheap = _override_offer("Ocheap", "PA", price=30000, merchant_id="M003", min_days=1)
    good = _override_offer("Ogood", "PA", price=38000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50), "M001": _merchant_full("M001", service=95)}
    candidate = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[cheap, good],
                      merchants=merchants)
    intent = _intent(preferences={"comfort": .5, "merchant_service": .5}, budget=100000)
    report = _report([candidate], intent)
    reason = _texts(report.recommendations[0].reason)
    assert any("2条合格报价中Offer服务分最高" in text for text in reason)
    single = _report([_candidate("PB", comfort=70, price=31000)], intent)
    assert any("唯一一条合格报价" in text for text in _texts(single.recommendations[0].reason))


def test_steered_reason_is_honest_about_lever_choice():
    plain = _override_offer("Oa", "PA", price=40000, merchant_id="M001")
    lucrative = _override_offer("Ob", "PA", price=48000, merchant_id="M002", commission=.5)
    merchants = {"M001": _merchant_full("M001"), "M002": _merchant_full("M002")}
    candidate = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[plain, lucrative],
                      merchants=merchants)
    intent = _intent(preferences={"price": 1}, budget=100000)
    report = build_explanations(recommend([candidate], intent, platform_bias=1.0,
                                          selection_policy="lever_score"), intent)
    rec = report.recommendations[0]
    assert rec.platform_steered is True and rec.recommended.offer_id == "Ob"
    reason = _texts(rec.reason)
    # 实验理由分报曝光分/用户适配分/λ与μ（裁定3），不把曝光分称"商品适配分"
    stmt = next(t for t in reason if "曝光分" in t)
    assert "用户适配分" in stmt and "λ=1.0" in stmt and "μ=0" in stmt
    assert "商品适配分" not in stmt
    # 报价选择依据按实际：非服务分最高时如实写明"按杠杆分选定"
    assert any("按杠杆分选定，非该商品2条合格报价中Offer服务分最高" in t for t in reason)
    assert not any("该报价同时是该商品" in t for t in reason)


# ---------------------------------------------------------------------------
# 汇总、版本与警告
# ---------------------------------------------------------------------------

def test_decision_summary_counts_and_scope():
    candidates = [_candidate("PA", comfort=90, price=40000),
                  _candidate("PB", comfort=60, price=30000, merchant="M002")]
    report = _report(candidates, _intent(preferences={"comfort": 1}, budget=100000))
    summary = report.decision_summary
    assert summary.candidate_count == 2 and summary.returned_count == 2
    assert summary.eligible_offer_count == 2 and summary.eligible_plan_count == 2
    assert summary.comparison_scope == "本次全部2个合格商品和2条报价"
    assert summary.relaxations == []


def test_versions_are_preserved_with_data_version():
    report = _report([_candidate("PA", comfort=90, price=40000)],
                     _intent(preferences={"comfort": 1}, budget=100000), data_version="small-v1")
    assert report.algorithm_versions == {"product": "product-ranking.v1",
                                         "offer": "offer-ranking.v1",
                                         "lever": "platform-levers.v1"}
    assert report.data_version == "small-v1"
    assert report.explanation_version == "explanation.v2"


def test_warning_aggregation_and_stale_data_passthrough():
    stale = {"code": "STALE_DATA", "message": "配送窗口的观测日期早于请求日期。",
             "field": "delivery_requirement.deadline_date", "product_id": None}
    search = CandidateSearch(status="ok", clarifications=[],
                             candidates=[_candidate("PA", comfort=90, price=40000, battery=False)],
                             filters_applied=[], warnings=[stale])
    unknown_battery = _candidate("PA", comfort=90, price=40000, battery=False)
    report = build_explanations(recommend([unknown_battery], _intent(
        preferences={"comfort": .5, "battery": .5}, budget=100000)),
        _intent(preferences={"comfort": .5, "battery": .5}, budget=100000), search=search)
    codes = [w["code"] for w in report.warnings]
    assert codes.count("SIMULATED_DATA") == 1
    assert codes.count("STALE_DATA") == 1
    missing = [w for w in report.warnings if w["code"] == "MISSING_SOFT_METRIC"]
    assert len({(w["product_id"], w["field"]) for w in missing}) == len(missing)  # 去重


def test_single_recommendation_states_no_cross_comparison():
    report = _report([_candidate("PA", comfort=90, price=40000)],
                     _intent(preferences={"comfort": 1}, budget=100000))
    texts = _texts(report.recommendations[0].trade_offs)
    assert texts == ["本次仅1个推荐，暂无商品间对比。"]


# ---------------------------------------------------------------------------
# FIX-S2A-R1 批次②（REVIEW-S2-ALL 2026-10-01）：S2A-04/05/06/07 对照
# ---------------------------------------------------------------------------


def _steered_report(candidate, intent, **levers):
    return build_explanations(
        recommend([candidate], intent, selection_policy="lever_score", **levers), intent)


def _lever_candidate(*, commission_ob=None, commission_oa=None):
    plain = _override_offer("Oa", "PA", price=40000, merchant_id="M001", commission=commission_oa)
    lucrative = _override_offer("Ob", "PA", price=48000, merchant_id="M002", commission=commission_ob)
    merchants = {"M001": _merchant_full("M001"), "M002": _merchant_full("M002")}
    return _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[plain, lucrative],
                 merchants=merchants)


def test_s2a04_lambda_only_reports_zero_mu_contribution():
    intent = _intent(preferences={"price": 1}, budget=100000)
    report = _steered_report(_lever_candidate(commission_ob=.5), intent, platform_bias=1.0)
    rec = report.recommendations[0]
    assert rec.product_score.bias_contribution > 0 and rec.product_score.distortion_contribution == 0
    stmt = next(t for t in _texts(rec.reason) if "曝光分" in t)
    assert "λ=1.0" in stmt and "μ=0" in stmt
    mu_part = stmt.split("意图扭曲")[1]
    assert "贡献0.00分" in mu_part  # μ 零实际贡献如实报 0


def test_s2a04_mu_only_reports_zero_lambda_contribution():
    intent = _intent(preferences={"comfort": 1}, budget=100000)
    report = _steered_report(_lever_candidate(), intent,
                             intent_distortion=IntentDistortion(.5, {"price": 1}))
    rec = report.recommendations[0]
    assert rec.product_score.bias_contribution == 0 and rec.product_score.distortion_contribution != 0
    stmt = next(t for t in _texts(rec.reason) if "曝光分" in t)
    assert "λ=0.0" in stmt and "μ=0.5" in stmt
    assert "λ=0.0，贡献0.00分" in stmt


def test_s2a04_zero_treatment_reports_equal_scores_and_zero_contributions():
    intent = _intent(preferences={"price": 1}, budget=100000)
    report = _steered_report(_lever_candidate(), intent)  # lever_score 00 格：λ=μ=0
    rec = report.recommendations[0]
    assert rec.platform_steered is True
    assert rec.product_score.score == pytest.approx(rec.product_score.base_score)
    stmt = next(t for t in _texts(rec.reason) if "曝光分" in t)
    assert "λ=0.0，贡献0.00分" in stmt and "μ=0.0，贡献0.00分" in stmt
    assert stmt.count(f"{rec.product_score.score:.2f}") >= 2  # 曝光分与用户适配分同值


def test_s2a04_service_best_offer_stated_as_fact_when_true():
    # 杠杆分选中的报价恰好也是服务分最高——如实说明，不一律断言"非服务分最高"
    good = _override_offer("Ob", "PA", price=40000, merchant_id="M001", commission=.5)
    worse = _override_offer("Oa", "PA", price=45000, merchant_id="M003", commission=.05)
    merchants = {"M001": _merchant_full("M001", service=95), "M003": _merchant_full("M003", service=50)}
    candidate = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[worse, good],
                      merchants=merchants)
    intent = _intent(preferences={"price": 1, "merchant_service": 1}, budget=100000)
    report = _steered_report(candidate, intent, platform_bias=1.0)
    rec = report.recommendations[0]
    assert rec.recommended.offer_id == "Ob"
    reason = _texts(rec.reason)
    assert any("该报价同时是该商品2条合格报价中Offer服务分最高" in t for t in reason)
    assert not any("按杠杆分选定，非" in t for t in reason)


def test_s2a05_battery_diff_requires_same_anc_state_mode_and_conditions():
    from tests.test_ranking import _battery, _product
    # P003 反例形态：同 basis 30h，但 ANC 状态与测试条件（LDAC/SBC）不同
    ldac = _cand("PA", product=_product("PA", batteries=[_battery("B1", 30, anc="on",
                                                                  conditions="LDAC 50%音量")]),
                 metrics=_metrics("PA", comfort_score=80),
                 offers=[_override_offer("OPA", "PA", price=40000, merchant_id="M001")],
                 merchants={"M001": _merchant_full("M001")})
    sbc = _cand("PB", product=_product("PB", modes=[_mode(anc=False)],
                                       batteries=[_battery("B2", 30, anc="not_applicable",
                                                           conditions="SBC 默认音量")]),
                metrics=_metrics("PB", comfort_score=60),
                offers=[_override_offer("OPB", "PB", price=30000, merchant_id="M002")],
                merchants={"M002": _merchant_full("M002")})
    intent = _intent(preferences={"price": .5, "battery": .5}, budget=100000)
    report = _report([ldac, sbc], intent)
    assert _comparisons(report.recommendations[0], "battery.single_charge") == []
    texts = _texts(report.recommendations[0].trade_offs)
    dual = next(t for t in texts if "不可直接比较" in t)
    assert "30小时" in dual and "ANC状态on" in dual and "ANC状态not_applicable" in dual
    assert "LDAC 50%音量" in dual and "SBC 默认音量" in dual
    assert "不比较优劣" in dual


def test_s2a05_battery_same_conditions_still_compared():
    from tests.test_ranking import _battery, _product
    longer = _cand("PA", product=_product("PA", batteries=[_battery("B1", 30, conditions="SBC 默认音量")]),
                   metrics=_metrics("PA", comfort_score=80),
                   offers=[_override_offer("OPA", "PA", price=40000, merchant_id="M001")],
                   merchants={"M001": _merchant_full("M001")})
    shorter = _cand("PB", product=_product("PB", batteries=[_battery("B2", 24, conditions="SBC 默认音量")]),
                    metrics=_metrics("PB", comfort_score=60),
                    offers=[_override_offer("OPB", "PB", price=30000, merchant_id="M002")],
                    merchants={"M002": _merchant_full("M002")})
    report = _report([longer, shorter], _intent(preferences={"price": .5, "battery": .5}, budget=100000))
    battery = _comparisons(report.recommendations[0], "battery.single_charge")
    assert len(battery) == 1 and battery[0].difference == 30 - 24


def test_s2a05_weight_diff_requires_same_conditions_scope():
    from tests.test_ranking import _product, _weight
    with_cable = _cand("PA", product=_product("PA", weights=[_weight("W1", 225, conditions="整副含线")]),
                       metrics=_metrics("PA", comfort_score=80),
                       offers=[_override_offer("OPA", "PA", price=40000, merchant_id="M001")],
                       merchants={"M001": _merchant_full("M001")})
    bare = _cand("PB", product=_product("PB", weights=[_weight("W2", 250, conditions="整副无配件")]),
                 metrics=_metrics("PB", comfort_score=60),
                 offers=[_override_offer("OPB", "PB", price=30000, merchant_id="M002")],
                 merchants={"M002": _merchant_full("M002")})
    intent = _intent(preferences={"price": .5, "weight": .5}, budget=100000)
    report = _report([with_cable, bare], intent)
    assert _comparisons(report.recommendations[0], "weight.headset") == []
    dual = next(t for t in _texts(report.recommendations[0].trade_offs) if "不可直接比较" in t)
    assert "整副含线" in dual and "整副无配件" in dual


def test_s2a06_offer_count_dedupes_offer_id_across_modes():
    from backend.services.candidate_service import Candidate
    product = _product("PA", modes=[_mode("bt"), _mode("dongle", "wireless_dongle")])
    offer = _override_offer("Oa", "PA", price=40000, merchant_id="M001")
    merchant = _merchant_full("M001")
    plans = [_plan(product, offer, merchant, mode_id="bt"),
             _plan(product, offer, merchant, mode_id="dongle")]  # 同一报价两模式 → 2 方案 1 报价
    candidate = Candidate(product=product, metrics=_metrics("PA", comfort_score=80),
                          eligible_plans=plans)
    intent = _intent(preferences={"price": 1}, budget=100000)
    report = _report([candidate], intent)
    summary = report.decision_summary
    assert summary.eligible_offer_count == 1 and summary.eligible_plan_count == 2
    assert any("唯一一条合格报价" in t for t in _texts(report.recommendations[0].reason))


def test_s2a07_shipping_tradeoff_uses_each_plans_observed_dates():
    early = _override_offer("OPA", "PA", price=40000, merchant_id="M001", min_days=1)
    late = _override_offer("OPB", "PB", price=30000, merchant_id="M002", min_days=1)
    # OPA 观测于 09-28（earliest=09-29），OPB 观测于 09-29（earliest=09-30）：
    # 相对历时相同但绝对到达差 1 天，文本必须给出两个绝对日期
    for leaf in ("delivery.min_days", "delivery.max_days"):
        early["field_sources"][leaf]["observed_at"] = "2026-09-28T10:00:00+08:00"
        late["field_sources"][leaf]["observed_at"] = "2026-09-29T10:00:00+08:00"
    merchants = {"M001": _merchant_full("M001"), "M002": _merchant_full("M002")}
    pa = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[early], merchants=merchants)
    pb = _cand("PB", metrics=_metrics("PB", comfort_score=60), offers=[late], merchants=merchants)
    report = _report([pa, pb], _intent(preferences={"comfort": .9, "price": .1}, budget=100000))
    delivery = _comparisons(report.recommendations[0], "delivery_window.earliest")
    assert len(delivery) == 1 and delivery[0].difference == -1
    text = next(s.text for s in report.recommendations[0].trade_offs
                if s.comparison and s.comparison.field == "delivery_window.earliest")
    assert "2026-09-29" in text and "2026-09-30" in text and "早1天" in text


def test_s2a03_p013_indoor_sweat_limitation_is_a_weakness():
    from tests.test_ranking import _product
    from backend.schemas.common import SWEAT_INDOOR_LIMITATION_CONDITIONS
    product = _product("PA")
    product["sweat_use_statement"] = {"allowed": True,
                                      "conditions": SWEAT_INDOOR_LIMITATION_CONDITIONS}
    candidate = _cand("PA", product=product, metrics=_metrics("PA", comfort_score=80),
                      offers=[_override_offer("Oa", "PA", price=40000, merchant_id="M001")],
                      merchants={"M001": _merchant_full("M001")})
    report = _report([candidate], _intent(preferences={"comfort": 1}, budget=100000))
    weakness = _texts(report.recommendations[0].weaknesses)
    stmt = next(s for s in report.recommendations[0].weaknesses if "室内训练" in s.text)
    assert "户外" in stmt.text
    assert "PA.sweat_use_statement.conditions" in stmt.fact_refs


# ---------------------------------------------------------------------------
# FIX-S2A-R2（REACCEPT-S2A 2026-10-01）：S2AR-02 服务分并列最高的如实陈述
# ---------------------------------------------------------------------------


def test_s2ar02_tied_service_best_not_misdescribed_as_not_best():
    # 审验反例同形：两条报价服务分并列 100（Offer 组只含 returns），价格决胜使便宜者
    # 排第一；杠杆（λ=.5，佣金 .3）选中较贵报价——并列最高必须如实说"并列最高"，
    # 不得虚构服务质量取舍写"非…服务分最高"。
    pricey = _override_offer("Oa", "PA", price=40000, merchant_id="M001", commission=.3)
    cheap = _override_offer("Ob", "PA", price=30000, merchant_id="M002")
    merchants = {"M001": _merchant_full("M001"), "M002": _merchant_full("M002")}
    candidate = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[pricey, cheap],
                      merchants=merchants)
    intent = _intent(preferences={"comfort": 1, "returns": 1}, budget=100000)
    report = _steered_report(candidate, intent, platform_bias=.5)
    rec = report.recommendations[0]
    assert rec.recommended.offer_id == "Oa"
    assert rec.offer_scores[0].offer_id == "Ob"  # 服务并列，价格决胜使 Ob 排第一
    assert rec.recommended.score == rec.offer_scores[0].score == pytest.approx(100)
    reason = _texts(rec.reason)
    assert any("该报价同时是该商品2条合格报价中Offer服务分并列最高（100.00）" in t
               for t in reason)
    assert not any("按杠杆分选定，非" in t for t in reason)


def test_s2ar02_same_offer_two_modes_tied_not_misdescribed():
    # 同一报价双模式：服务分同分（同报价特征），杠杆按 dongle 模式续航取高分——
    # 唯一一条合格报价不得被误述为"非…服务分最高"。
    from tests.test_ranking import _battery
    product = _product("PA", modes=[_mode("bt"), _mode("dongle", "wireless_dongle")],
                       batteries=[_battery("B1", 30, mode="dongle")])
    offer = _override_offer("Oa", "PA", price=40000, merchant_id="M001")
    merchant = _merchant_full("M001")
    plans = [_plan(product, offer, merchant, mode_id="bt"),
             _plan(product, offer, merchant, mode_id="dongle")]
    from backend.services.candidate_service import Candidate
    candidate = Candidate(product=product, metrics=_metrics("PA", comfort_score=80),
                          eligible_plans=plans)
    intent = _intent(preferences={"battery": 1, "returns": 1}, budget=100000)
    report = _steered_report(candidate, intent)  # lever_score 00 格：dongle 续航胜出
    rec = report.recommendations[0]
    assert rec.recommended.mode_id == "dongle"
    assert rec.offer_scores[0].mode_id == "bt"  # 服务同分，mode_id 决胜使 bt 排第一
    reason = _texts(rec.reason)
    assert any("唯一一条合格报价" in t for t in reason)
    assert not any("非该商品" in t for t in reason)
