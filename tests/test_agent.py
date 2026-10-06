"""S2.1 测试：结构化意图（ShoppingIntent）与硬约束候选检索。

验收覆盖（计划 S2.1 + 契约 §7.1/§7.4 + 字典 §8 + 15 场景/EDGE 锚点）：
- 意图结构：extra=forbid、stock_required 固定 true、preferences 全维度、
  空数组/非法枚举/NaN 拒绝；
- 冲突检测：无线vs有线、EDGE05 有线vs真无线、ANC 续航vs无 ANC、
  多设备vs无多点、形态允许/排除互斥——冲突返回澄清且不检索；
- 硬约束管道：预算边界（EDGE06 运费超预算 / 恰好等于预算 / 到手价而非商品价）、
  库存（缺货最低价不取 EDGE09、库存未知）、模式绑定（EDGE03 ANC 未知、
  EDGE07 含盒续航、双模有线方案、dongle 续航模式枚举）、设备兼容
  （SC08 PS5 / SC10 TV 光纤 / os_version 未知）、重量口径不换算、
  耳塞套全含（SC14）、开封退货合并（SC14）、开放耳（SC12）、防汗（SC11）、
  形态排除优先（SC13）、成色、配送承诺/条件/截止（同观测日通过、跨日
  STALE_DATA 拒绝）、filters_applied 顺序计数。

锚点数据来自生成器固定种子（small-v1，配送观测日 2026-09-29）：
P001 报价 O001(过期)/O003(71900)/O004(70900)/O005(71900)；P002 无续航/重量；
P005 仅含盒续航 32h；P006 O018 缺货/O019=81000(EDGE06)；P009 dongle 模式
（O026 含发射器 79900、O027 加钱 84800、O028 配件未知）；P012 O035=80000
恰好等于预算；P013 O036(M002 开封可退 29900)/O037(M004 不可退)；
P014 耳塞套无 S、ANC 未知；P015 全缺货。
"""

from __future__ import annotations

from datetime import date, datetime
import json

import pytest
from pydantic import ValidationError

from backend.database.db import connect, initialize_database
from backend.schemas.agent import (
    PREFERENCE_DIMENSIONS,
    BatteryRequirement,
    Budget,
    Clarification,
    DeliveryRequirement,
    DeviceRequirement,
    HardConstraints,
    Preferences,
    ShoppingIntent,
    WeightRequirement,
)
from backend.services import derive
from backend.services.candidate_service import (
    detect_intent_conflicts,
    get_eligible_candidates,
)

#: 与配送观测日同日的请求时间（窗口可用于硬截止核验）
NOW_ON_OBSERVED = datetime(2026, 9, 29, 12, 0, 0, tzinfo=derive.CN_TZ)
#: 观测日次日的请求时间（历史窗口 → STALE_DATA）
NOW_NEXT_DAY = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)
#: 与 S1.3 API 测试一致的常规参考时间（O001 已过期）
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)


@pytest.fixture()
def db_conn(small_data_dir, tmp_path):
    """隔离数据库（small-v1 批次）的直连连接。"""
    db_path = tmp_path / "db" / "shopping.sqlite3"
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    yield conn
    conn.close()


# ---- 构造助手 -----------------------------------------------------------------


def _intent(hard: HardConstraints | None = None, budget_max: int | None = None) -> ShoppingIntent:
    return ShoppingIntent(
        budget=Budget(max_cents=budget_max) if budget_max is not None else Budget(),
        hard_constraints=hard or HardConstraints(),
    )


def _ids(result) -> list[str]:
    return [c.product_id for c in result.candidates]


def _plans(result, product_id: str) -> list[tuple[int, str, str]]:
    """(landed_price, offer_id, mode_id) 升序的合格方案。"""
    cand = next(c for c in result.candidates if c.product_id == product_id)
    return [
        (p.landed, p.offer["offer_id"], p.mode_id) for p in cand.eligible_plans
    ]


def _record(result, field: str):
    return next(r for r in result.filters_applied if r.field == field)


# ---- 意图结构校验 ---------------------------------------------------------------


def test_intent_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        ShoppingIntent(**{"favorite_color": "blue"})


def test_stock_required_cannot_be_turned_off():
    with pytest.raises(ValidationError):
        HardConstraints(stock_required=False)


def test_preferences_cover_all_dimensions_and_default_zero():
    prefs = Preferences().model_dump()
    assert set(prefs) == set(PREFERENCE_DIMENSIONS)
    assert all(v == 0.0 for v in prefs.values())


def test_preferences_reject_out_of_range_and_nan():
    with pytest.raises(ValidationError):
        Preferences(price=1.5)
    with pytest.raises(ValidationError):
        Preferences(price=float("nan"))
    with pytest.raises(ValidationError):
        Preferences(price=float("inf"))


def test_constraint_arrays_must_be_nonempty_or_null():
    with pytest.raises(ValidationError):
        HardConstraints(sub_categories=[])
    with pytest.raises(ValidationError):
        HardConstraints(allowed_transports=[])


def test_battery_requirement_validates_anc_state_and_hours():
    with pytest.raises(ValidationError):
        BatteryRequirement(min_hours=8, basis="single_charge", anc_state="unknown")
    with pytest.raises(ValidationError):
        BatteryRequirement(min_hours=0, basis="single_charge", anc_state="on")
    with pytest.raises(ValidationError):
        BatteryRequirement(min_hours=8, basis="per_week", anc_state="on")


def test_default_intent_constructs_with_all_null_constraints():
    intent = ShoppingIntent()
    assert intent.category is None
    assert intent.budget.max_cents is None
    assert intent.budget.currency == "CNY"
    assert intent.hard_constraints.wireless is None
    assert intent.hard_constraints.stock_required is True
    assert intent.clarifications == []


# ---- 冲突检测（EDGE05 等）：冲突返回澄清，不择一执行、不放宽 -----------------------


def test_conflict_wireless_true_with_wired_transports_only(db_conn):
    intent = _intent(HardConstraints(wireless=True, allowed_transports=["wired_3_5mm", "usb_audio"]))
    conflicts = detect_intent_conflicts(intent)
    assert len(conflicts) == 1
    assert conflicts[0].reason == "conflicting_constraints"
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert result.status == "needs_clarification"
    assert result.candidates == [] and result.filters_applied == []


def test_conflict_wireless_false_with_bluetooth_transport(db_conn):
    intent = _intent(HardConstraints(wireless=False, allowed_transports=["bluetooth"]))
    assert detect_intent_conflicts(intent)
    assert get_eligible_candidates(db_conn, intent, now=NOW).status == "needs_clarification"


def test_conflict_edge05_wired_vs_true_wireless(db_conn):
    intent = _intent(HardConstraints(wireless=False, sub_categories=["true_wireless_earbuds"]))
    conflicts = detect_intent_conflicts(intent)
    assert any(c.field == "hard_constraints.sub_categories" for c in conflicts)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert result.status == "needs_clarification" and result.candidates == []


def test_conflict_battery_anc_on_with_anc_false(db_conn):
    intent = _intent(
        HardConstraints(
            active_noise_cancelling=False,
            battery_requirement=BatteryRequirement(
                min_hours=8, basis="single_charge", anc_state="on"
            ),
        )
    )
    assert detect_intent_conflicts(intent)
    assert get_eligible_candidates(db_conn, intent, now=NOW).status == "needs_clarification"


def test_conflict_min_active_devices_with_multipoint_false(db_conn):
    intent = _intent(HardConstraints(min_active_devices=2, multipoint=False))
    assert detect_intent_conflicts(intent)
    assert get_eligible_candidates(db_conn, intent, now=NOW).status == "needs_clarification"


def test_conflict_allowed_and_excluded_form_factors_disjoint(db_conn):
    intent = _intent(
        HardConstraints(allowed_form_factors=["in_ear"], excluded_form_factors=["in_ear"])
    )
    assert detect_intent_conflicts(intent)


def test_preexisting_clarifications_skip_retrieval(db_conn):
    intent = ShoppingIntent(
        clarifications=[
            Clarification(
                field="hard_constraints.delivery_requirement",
                reason="missing_deadline",
                question="请问需要哪天之前送达？",
            )
        ]
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert result.status == "needs_clarification"
    assert result.clarifications == intent.clarifications
    assert result.candidates == [] and result.filters_applied == []


def test_consistent_intent_has_no_conflicts():
    intent = _intent(HardConstraints(wireless=True, active_noise_cancelling=True), budget_max=80000)
    assert detect_intent_conflicts(intent) == []


# ---- 基线检索与候选形态 -----------------------------------------------------------


def test_unconstrained_intent_returns_all_in_stock_products(db_conn):
    result = get_eligible_candidates(db_conn, _intent(), now=NOW)
    assert result.status == "ok"
    # P015 两条报价均缺货；其余商品默认模式至少一条完整有货报价（O001 已过期被基线拒绝）
    assert _ids(result) == [f"P{i:03d}" for i in range(1, 15)]
    cand = result.candidates[0]
    assert cand.product["product_id"] == "P001"
    assert cand.metrics is not None and cand.metrics["product_id"] == "P001"
    # O004(70900) 最低；O003/O005 同为 71900 按 offer_id 稳定排序
    assert _plans(result, "P001") == [(70900, "O004", "bt"), (71900, "O003", "bt"), (71900, "O005", "bt")]


def test_candidate_plans_all_traditionally_eligible(db_conn):
    result = get_eligible_candidates(db_conn, _intent(), now=NOW)
    for cand in result.candidates:
        for plan in cand.eligible_plans:
            assert plan.eligible is True
            assert plan.landed is not None and plan.offer["stock"] > 0


# ---- 预算：到手价口径与边界（EDGE06 / 恰好等于预算 / 运费与配件计价） -----------------


def test_budget_boundary_exact_equality_passes(db_conn):
    """O035 = 78500 + 1500 = 80000 恰好等于预算 80000，必须通过。"""
    result = get_eligible_candidates(db_conn, _intent(budget_max=80000), now=NOW)
    assert "P012" in _ids(result)
    assert (80000, "O035", "bt") in _plans(result, "P012")


def test_budget_uses_landed_price_with_shipping_edge06(db_conn):
    """SC01：800 元预算 + 无线 ANC。P006 最便宜 O018 缺货，O019=79000+2000=81000
    超预算（EDGE06）、O020 超预算 → P006 整体出局；不拿缺货报价的价格凑数（EDGE09）。"""
    intent = _intent(HardConstraints(wireless=True, active_noise_cancelling=True), budget_max=80000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P001", "P002"]


def test_budget_counts_shipping_not_goods_price(db_conn):
    """P001 商品价 69900 ≤ 70000，但全部到手价（70900/71900）> 70000 → 出局。"""
    result = get_eligible_candidates(db_conn, _intent(budget_max=70000), now=NOW)
    assert "P001" not in _ids(result)


def test_budget_unknown_price_cannot_pass(db_conn):
    """SC08 视角：dongle 模式下 O028 配件未知 → 到手价 null，不能进预算合格方案。"""
    intent = _intent(
        HardConstraints(device_requirements=[DeviceRequirement(platform="ps5", microphone_required=True)]),
        budget_max=90000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P009"]
    offers = [t[1] for t in _plans(result, "P009")]
    assert offers == ["O026", "O027"]  # O028 配件未知被拒
    assert all(mode == "dongle" for _, _, mode in _plans(result, "P009"))


def test_no_results_not_relaxed_edge01(db_conn):
    """EDGE01：100 元以内无线 ANC → 零候选，如实返回不放宽。"""
    intent = _intent(HardConstraints(wireless=True, active_noise_cancelling=True), budget_max=10000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert result.status == "ok"
    assert result.candidates == []


# ---- 库存：作用于同一 Offer --------------------------------------------------------


def test_out_of_stock_and_unknown_stock_offers_excluded(db_conn):
    result = get_eligible_candidates(db_conn, _intent(budget_max=30000), now=NOW)
    # P014：O040 库存未知被拒（不作为有货推荐），仅剩 O038/O039
    offers = [t[1] for t in _plans(result, "P014")]
    assert offers == ["O038", "O039"]
    stock_rec = _record(result, "hard_constraints.stock_required")
    # 商品被整步排除的只有 P015（两条报价 stock=0）；P014 有其他有货报价故保留
    assert stock_rec.excluded_count == 1
    assert stock_rec.unknown_excluded_count == 0


# ---- 模式绑定能力：wireless / ANC / 麦克风 / 供电 / 传输 ------------------------------


def test_anc_unknown_not_satisfied_edge03(db_conn):
    """EDGE03：P014 ANC=null（商品与模式均未知）不能通过 ANC=true。"""
    result = get_eligible_candidates(db_conn, _intent(HardConstraints(active_noise_cancelling=True)), now=NOW)
    ids = _ids(result)
    assert "P014" not in ids
    assert set(ids) == {"P001", "P002", "P003", "P006"}
    rec = _record(result, "hard_constraints.active_noise_cancelling")
    assert rec.unknown_excluded_count == 1  # P014 因未知被拒


def test_wireless_false_accepts_wired_modes_of_dual_mode_product(db_conn):
    """wireless=false 绑定实际传输：P008（usb+w35 双有线模式）两模式都可用；
    P012 的 optical_transmitter 不算有线也不算无线 → 不满足 wireless=false。"""
    result = get_eligible_candidates(db_conn, _intent(HardConstraints(wireless=False)), now=NOW)
    assert set(_ids(result)) == {"P007", "P008", "P011"}
    modes = {mode for _, _, mode in _plans(result, "P008")}
    assert modes == {"usb", "w35"}
    assert "P012" not in _ids(result)


def test_wireless_true_rejects_wired_only_products(db_conn):
    result = get_eligible_candidates(db_conn, _intent(HardConstraints(wireless=True)), now=NOW)
    assert not {"P007", "P008", "P011"} & set(_ids(result))


def test_sc06_wired_3_5mm_microphone_no_power_budget(db_conn):
    """SC06：100 元内有线 3.5mm、带麦、插上能用（不需充电）。"""
    intent = _intent(
        HardConstraints(
            allowed_transports=["wired_3_5mm"],
            microphone=True,
            requires_power=False,
        ),
        budget_max=10000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P007"]
    assert _plans(result, "P007") == [(8900, "O021", "w35")]


def test_sc05_multipoint_with_budget(db_conn):
    """SC05：800 元内多点连接。P006 有多点但可购报价均超预算 → 只剩 P002。"""
    intent = _intent(HardConstraints(multipoint=True, wireless=True), budget_max=80000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P002"]


def test_min_active_devices_unknown_excluded(db_conn):
    result = get_eligible_candidates(db_conn, _intent(HardConstraints(min_active_devices=2)), now=NOW)
    assert set(_ids(result)) == {"P002", "P006"}
    rec = _record(result, "hard_constraints.min_active_devices")
    assert rec.unknown_excluded_count == 3  # P007/P008/P011 max_active_devices 未知


# ---- 续航与重量：basis/anc_state/mode 完全一致，不跨口径 -----------------------------


def test_sc02_battery_single_charge_anc_on(db_conn):
    """SC02：无线 ANC + ANC 开启单次 ≥8h。P015 全缺货出局。"""
    intent = _intent(
        HardConstraints(
            wireless=True,
            active_noise_cancelling=True,
            battery_requirement=BatteryRequirement(
                min_hours=8, basis="single_charge", anc_state="on"
            ),
        ),
        budget_max=150000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P001", "P003", "P006"]


def test_battery_edge07_with_case_hours_not_single_charge(db_conn):
    """EDGE07：P005 仅含盒 32h，不能通过单次续航硬要求。"""
    intent = _intent(
        HardConstraints(
            battery_requirement=BatteryRequirement(
                min_hours=8, basis="single_charge", anc_state="not_applicable"
            )
        )
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    ids = _ids(result)
    assert "P005" not in ids  # 只有 with_case 记录
    assert "P002" not in ids  # 无续航记录（未知）


def test_battery_requirement_enumerates_matching_mode(db_conn):
    """续航绑定实际模式：P009 的 35h 记录在 dongle 模式 → 方案切到 dongle 并计价发射器。
    （31h 阈值用于隔离 P009——P012 的 30h bt 记录同样满足 30h 阈值。）"""
    intent = _intent(
        HardConstraints(
            battery_requirement=BatteryRequirement(
                min_hours=31, basis="single_charge", anc_state="not_applicable"
            )
        ),
        budget_max=100000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P009"]
    assert _plans(result, "P009") == [(79900, "O026", "dongle"), (84800, "O027", "dongle")]


def test_battery_anc_state_unknown_not_satisfied(db_conn):
    """P014 续航 anc_state=unknown：不通过 ANC 开启续航要求。"""
    intent = _intent(
        HardConstraints(
            battery_requirement=BatteryRequirement(
                min_hours=5, basis="single_charge", anc_state="on"
            )
        )
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P014" not in _ids(result)


def test_weight_same_basis_only_no_conversion(db_conn):
    """single_earbud ≤5.0g：P005(4.8)/P013(5.0 边界) 通过；P014(5.2) 不通过；
    头戴的 headset 克数不换算成单只。"""
    intent = _intent(
        HardConstraints(weight_requirement=WeightRequirement(max_grams=5.0, basis="single_earbud"))
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P005", "P013"}


def test_weight_unknown_not_satisfied(db_conn):
    intent = _intent(
        HardConstraints(weight_requirement=WeightRequirement(max_grams=300, basis="headset"))
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P002" not in _ids(result)  # 无重量记录
    rec = _record(result, "hard_constraints.weight_requirement.max_grams")
    assert rec.unknown_excluded_count == 1


# ---- 设备兼容：结构化匹配，不凭推断 --------------------------------------------------


def test_sc08_ps5_device_requirement(db_conn):
    intent = _intent(
        HardConstraints(device_requirements=[DeviceRequirement(platform="ps5", microphone_required=True)]),
        budget_max=90000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P009"]
    assert _plans(result, "P009") == [(79900, "O026", "dongle"), (84800, "O027", "dongle")]


def test_sc10_tv_optical_output_includes_transmitter_pricing(db_conn):
    """SC10：电视光纤输出 → P012 optical 模式；必要发射器计入到手价，
    O035=78500+1500=80000 恰好等于预算。"""
    intent = _intent(
        HardConstraints(
            device_requirements=[
                DeviceRequirement(platform="tv", source_output="optical_pcm")
            ]
        ),
        budget_max=80000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P012"]
    assert _plans(result, "P012") == [
        (69900, "O033", "optical"),
        (74800, "O034", "optical"),
        (80000, "O035", "optical"),
    ]


def test_device_requirement_with_wireless_binds_compatible_mode(db_conn):
    """windows 兼容在 P009 的 dongle 模式上：无线要求与设备要求同时约束模式。"""
    intent = _intent(
        HardConstraints(
            wireless=True,
            device_requirements=[DeviceRequirement(platform="windows")],
        )
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P009"]
    assert all(mode == "dongle" for _, _, mode in _plans(result, "P009"))


def test_device_os_version_unknown_not_assumed_supported(db_conn):
    """兼容记录 os_version=null 不代表支持任何版本（EDGE08 同源原则：不推断）。"""
    intent = _intent(
        HardConstraints(device_requirements=[DeviceRequirement(platform="windows", os_version="Windows 11")])
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == []
    rec = _record(result, "hard_constraints.device_requirements")
    assert rec.unknown_excluded_count >= 1


# ---- 商品级条件：形态 / 开放耳 / 防汗 / 耳塞套 / 成色 ---------------------------------


def test_sc13_excluded_form_factors_take_precedence(db_conn):
    """SC13：排除入耳式（排除优先）；P012 经 bt 模式 69900 ≤ 70000 可入选。"""
    intent = _intent(HardConstraints(excluded_form_factors=["in_ear"]), budget_max=70000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P002", "P004", "P007", "P008", "P010", "P011", "P012"}
    assert not {"P005", "P013", "P014"} & set(_ids(result))


def test_sub_categories_or_semantics(db_conn):
    intent = _intent(HardConstraints(sub_categories=["true_wireless_earbuds", "wireless_earbuds"]))
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P004", "P005", "P013", "P014"}


def test_sc12_open_ear_wireless(db_conn):
    intent = _intent(HardConstraints(open_ear=True, wireless=True), budget_max=60000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P004"]


def test_sc11_sweat_statement_required(db_conn):
    """SC11：必须有明确防汗声明——IPX 或无声明不算（P013/P004 有声明）。"""
    intent = _intent(HardConstraints(sweat_use_allowed=True, wireless=True), budget_max=40000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P004", "P013"}


def test_ear_tip_sizes_require_all(db_conn):
    intent = _intent(HardConstraints(ear_tip_sizes=["S", "M"]))
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P005", "P013"}  # P014 无 S 号


def test_item_condition_refurbished_only(db_conn):
    intent = _intent(HardConstraints(item_condition="refurbished"))
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P008"]
    # 无连接类条件 → 默认展示模式（P008 为 usb），每报价一个方案
    assert _plans(result, "P008") == [(38900, "O025", "usb")]


def test_item_condition_new_excludes_refurb(db_conn):
    intent = _intent(HardConstraints(item_condition="new"), budget_max=50000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _plans(result, "P008") == [(44400, "O024", "usb"), (44900, "O023", "usb")]


# ---- 售后：有效开封退货条件（覆盖优先，未知不通过） -----------------------------------


def test_sc14_tws_in_ear_tips_opened_return(db_conn):
    """Public Demo implementation."""
    intent = _intent(
        HardConstraints(
            sub_categories=["true_wireless_earbuds"],
            allowed_form_factors=["in_ear"],
            ear_tip_sizes=["S"],
            opened_return_allowed=True,
        ),
        budget_max=50000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P005", "P013"]
    # P005：O015 条款语料外（引用未给出条款）被拒，O016(M004 False) 拒绝，仅剩 O017
    assert _plans(result, "P005") == [(35900, "O017", "bt")]
    # P013：O036(M002 默认 opened=True，买家义务条款放行) 通过，O037(M004 False) 拒绝
    assert _plans(result, "P013") == [(29900, "O036", "bt")]


# ---- 配送：承诺类型 / 条件 / 截止日期（同一 Offer、观测日新鲜度、配件同包裹） -------------


def test_sc15_deadline_on_observed_day_passes(db_conn):
    """观测日（2026-09-29）当天请求、截止 2026-10-02：latest ≤ 截止的报价通过。"""
    intent = ShoppingIntent(
        budget=Budget(max_cents=60000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(deadline_date=date(2026, 10, 2))
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW_ON_OBSERVED)
    assert set(_ids(result)) == {"P004", "P005", "P007", "P008", "P010", "P011"}
    assert _plans(result, "P010") == [(29900, "O030", "bt")]
    assert _plans(result, "P007") == [(10700, "O022", "w35")]
    assert "P013" not in _ids(result)  # 最快也要 10-03 送达


def test_deadline_stale_window_rejected_with_stale_data_warning(db_conn):
    """观测日次日请求：历史窗口不能验证硬截止 → 零候选 + STALE_DATA。"""
    intent = ShoppingIntent(
        budget=Budget(max_cents=60000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(deadline_date=date(2026, 10, 2))
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW_NEXT_DAY)
    assert result.candidates == []
    codes = [w["code"] for w in result.warnings]
    assert "STALE_DATA" in codes
    rec = _record(result, "delivery_requirement.deadline_date")
    assert rec.excluded_count == 8 and rec.unknown_excluded_count == 8


def test_deadline_accessory_same_package_required(db_conn):
    """硬截止下必要配件必须同包裹：SC10 场景 O027 的发射器 same=True 才能整方案到货。"""
    intent = ShoppingIntent(
        budget=Budget(max_cents=90000),
        hard_constraints=HardConstraints(
            device_requirements=[DeviceRequirement(platform="ps5", microphone_required=True)],
            delivery_requirement=DeliveryRequirement(deadline_date=date(2026, 10, 1)),
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW_ON_OBSERVED)
    assert _ids(result) == ["P009"]
    # O026（1-2 天 → 10-01 截止内）；O027（3-5 天 → 10-04 超截止）
    assert _plans(result, "P009") == [(79900, "O026", "dongle")]


def test_commitment_type_guaranteed(db_conn):
    intent = ShoppingIntent(
        budget=Budget(max_cents=50000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(commitment_type="guaranteed")
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert _ids(result) == ["P008"]
    assert _plans(result, "P008") == [(44900, "O023", "usb")]


def test_commitment_type_estimated_accepts_guaranteed(db_conn):
    intent = ShoppingIntent(
        budget=Budget(max_cents=50000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(commitment_type="estimated")
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    offers = {t[1] for t in _plans(result, "P008")}
    assert offers == {"O023", "O024", "O025"}  # guaranteed 的 O023 同样满足


def test_delivery_conditions_exact_match(db_conn):
    intent = ShoppingIntent(
        budget=Budget(max_cents=80000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(conditions="模拟华东仓发货")
        ),
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P001", "P002", "P004", "P005", "P010", "P013", "P014"}
    assert _plans(result, "P001") == [(71900, "O003", "bt")]


# ---- filters_applied 顺序统计 --------------------------------------------------------


def test_filters_applied_sequential_counts_sc01(db_conn):
    """SC01 主链路：各步商品排除数按执行顺序累计，与候选集吻合。"""
    intent = _intent(HardConstraints(wireless=True, active_noise_cancelling=True), budget_max=80000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert [(r.field, r.excluded_count) for r in result.filters_applied] == [
        ("hard_constraints.wireless", 3),
        ("hard_constraints.active_noise_cancelling", 7),
        ("offer.eligible", 0),
        ("hard_constraints.stock_required", 1),
        ("budget.max_cents", 2),
    ]
    scopes = {r.field: r.scope for r in result.filters_applied}
    assert scopes["hard_constraints.wireless"] == "connection_plan"
    assert scopes["budget.max_cents"] == "offer"
    assert _ids(result) == ["P001", "P002"]


def test_filters_applied_only_for_expressed_constraints(db_conn):
    """未表达的条件不出现在 filters_applied（不显示未执行条件为通过）。"""
    result = get_eligible_candidates(db_conn, _intent(budget_max=80000), now=NOW)
    fields = [r.field for r in result.filters_applied]
    assert fields == ["offer.eligible", "hard_constraints.stock_required", "budget.max_cents"]


# ---- S2-R1–R5：不能忽略模式、矛盾或适用前提 ------------------------------------

def _edit_product(conn, product_id, edit):
    """只修改本测试隔离库的完整商品payload，不碰演示库。"""
    row = conn.execute("SELECT payload FROM products WHERE product_id=?", (product_id,)).fetchone()
    product = json.loads(row[0])
    edit(product)
    conn.execute("UPDATE products SET payload=? WHERE product_id=?", (json.dumps(product), product_id))
    conn.commit()


@pytest.mark.parametrize("constraint", [{"multipoint": True}, {"min_active_devices": 2}])
def test_multi_mode_cannot_inherit_product_multipoint(db_conn, constraint):
    def edit(p):
        p['multipoint'] = True
        p['max_active_devices'] = 2
        for c in p['compatibility']:
            c['multipoint_supported'] = False
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints=constraint), now=NOW)
    assert 'P009' not in _ids(result)


def test_multi_mode_multipoint_with_device_evidence(db_conn):
    def edit(p):
        p['multipoint'] = True
        p['max_active_devices'] = 2
        for c in p['compatibility']:
            c['multipoint_supported'] = True
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'min_active_devices': 2, 'device_requirements': [{'platform': 'windows'}],
    }), now=NOW)
    assert _plans(result, 'P009') == [(79900, 'O026', 'dongle'), (84800, 'O027', 'dongle')]


def test_multi_mode_device_count_cannot_inherit_product_maximum(db_conn):
    def edit(p):
        p['multipoint'] = True
        p['max_active_devices'] = 3
        for c in p['compatibility']:
            c['multipoint_supported'] = True
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'min_active_devices': 3, 'device_requirements': [{'platform': 'windows'}],
    }), now=NOW)
    assert result.candidates == []


def test_device_multipoint_cannot_override_explicit_false(db_conn):
    def edit(p):
        for c in p['compatibility']:
            c['multipoint_supported'] = True
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'multipoint': False,
        'device_requirements': [{'platform': 'windows', 'multipoint_required': True}],
    }), now=NOW)
    assert result.status == 'needs_clarification'
    assert result.candidates == []
    assert result.filters_applied == []


@pytest.mark.parametrize(('actual', 'required', 'passes'), [
    ('fixed-volume', 'fixed-volume', True),
    ('low-volume', 'high-volume', False),
    (None, 'high-volume', False),
    ('', 'high-volume', False),
    ('low-volume', None, True),
])
def test_battery_explicit_conditions_are_checked(db_conn, actual, required, passes):
    def edit(p):
        for s in p['battery_specs']:
            s['test_conditions'] = actual
    _edit_product(db_conn, 'P001', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'battery_requirement': {'min_hours': 8, 'basis': 'single_charge',
                                'anc_state': 'on', 'conditions': required},
    }), now=NOW)
    assert ('P001' in _ids(result)) is passes
    if not passes:
        assert _record(result, 'hard_constraints.battery_requirement.min_hours').unknown_excluded_count >= 1


@pytest.mark.parametrize(('conditions', 'passes'), [(None, False), ('', False), ('整副含线', True)])
def test_weight_unknown_scope_is_not_satisfied(db_conn, conditions, passes):
    def edit(p):
        for s in p['weight_specs']:
            s['conditions'] = conditions
    _edit_product(db_conn, 'P001', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'weight_requirement': {'max_grams': 1000, 'basis': 'headset'},
    }), now=NOW)
    assert ('P001' in _ids(result)) is passes
    if not passes:
        assert _record(result, 'hard_constraints.weight_requirement.max_grams').unknown_excluded_count >= 1


@pytest.mark.parametrize('conditions', [None, '', 'Requires unverified custom firmware',
    '需QG-DONGLE-USB适配器（随部分报价附赠或另购）；还需特殊固件'])
def test_unverified_compatibility_conditions_reject(db_conn, conditions):
    def edit(p):
        for c in p['compatibility']:
            c['conditions'] = conditions
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'device_requirements': [{'platform': 'ps5', 'playback_required': True}],
    }), now=NOW)
    assert result.candidates == []
    assert _record(result, 'hard_constraints.device_requirements').unknown_excluded_count >= 1


def test_known_compatibility_adapter_must_be_in_selected_plan(db_conn):
    def edit(p):
        next(m for m in p['connection_modes'] if m['mode_id'] == 'dongle')['required_accessory_codes'] = []
    _edit_product(db_conn, 'P009', edit)
    result = get_eligible_candidates(db_conn, ShoppingIntent(hard_constraints={
        'device_requirements': [{'platform': 'ps5', 'playback_required': True}],
    }), now=NOW)
    assert result.candidates == []


def test_trrs_compatibility_requires_confirmed_connector(db_conn):
    vague = ShoppingIntent(hard_constraints={'device_requirements': [{'platform': 'windows'}]})
    result = get_eligible_candidates(db_conn, vague, now=NOW)
    assert 'P007' not in _ids(result)
    exact = ShoppingIntent(hard_constraints={'device_requirements': [{
        'platform': 'windows', 'device_key': '通用PC（3.5mm TRRS复合口）',
        'microphone_required': True,
    }]})
    result = get_eligible_candidates(db_conn, exact, now=NOW)
    assert _plans(result, 'P007') == [(8900, 'O021', 'w35'), (10700, 'O022', 'w35')]


# ---- FIX-S2A-R1 批次①（REVIEW-S2-ALL 2026-10-01）：S2A-01/02/03 硬约束整改 ------------



def _edit_offer(conn, offer_id, edit):
    """只修改本测试隔离库的完整报价payload，不碰演示库。"""
    row = conn.execute("SELECT payload FROM offers WHERE offer_id=?", (offer_id,)).fetchone()
    offer = json.loads(row[0])
    edit(offer)
    conn.execute("UPDATE offers SET payload=? WHERE offer_id=?", (json.dumps(offer), offer_id))
    conn.commit()


# S2A-01：必要配件清单 null ≠ []


def test_s2a01_required_accessory_codes_null_is_unknown(db_conn):
    """S2A-01 反例：P001 bt 模式必要配件清单 null——费用/库存不可确定，
    不得按"无需配件"给出完整方案与预算结论（accessory_total=0、landed 可算）。"""

    def edit(p):
        p["connection_modes"][0]["required_accessory_codes"] = None

    _edit_product(db_conn, "P001", edit)
    intent = _intent(HardConstraints(wireless=True, active_noise_cancelling=True), budget_max=80000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P001" not in _ids(result)
    rec = _record(result, "offer.eligible")
    assert rec.unknown_excluded_count == 1


def test_s2a01_accessory_plan_null_vs_empty_contrast(db_conn):
    """S2A-01 四对照：null → 未知费用+方案不完整；[] → 0 元完整；
    必要配件缺失（O028 配件清单 null）→ 不完整；已知完整方案（O026 含发射器）→ 可定价。"""
    row = db_conn.execute("SELECT payload FROM products WHERE product_id='P001'").fetchone()
    p001 = json.loads(row[0])

    def plan_with(required):
        product = json.loads(json.dumps(p001))
        product["connection_modes"][0]["required_accessory_codes"] = required
        return derive.accessory_plan(product, {"accessories": []}, "bt")

    assert plan_with(None) == (None, False, "plan_incomplete")  # null ≠ 无需
    assert plan_with([]) == (0, True, None)  # 显式 [] 才是无需配件

    row9 = db_conn.execute("SELECT payload FROM products WHERE product_id='P009'").fetchone()
    p009 = json.loads(row9[0])
    o26 = json.loads(db_conn.execute("SELECT payload FROM offers WHERE offer_id='O026'").fetchone()[0])
    o28 = json.loads(db_conn.execute("SELECT payload FROM offers WHERE offer_id='O028'").fetchone()[0])
    total, complete, reason = derive.accessory_plan(p009, o26, "dongle")
    assert total == 0 and complete is True and reason is None  # 随附：完整且 0 元
    total, complete, reason = derive.accessory_plan(p009, o28, "dongle")
    assert total is None and complete is False  # 配件清单 null：未知
    assert reason == "plan_incomplete"


# S2A-02：总体麦克风要求贯通目标设备


@pytest.mark.parametrize("comp_mic", [False, None])
def test_s2a02_overall_microphone_propagates_to_target_device(db_conn, comp_mic):
    """S2A-02 反例：意图只写总体 microphone=true（未在设备上重复填写）；
    P009 的 PS5 兼容记录麦克风 false/null 时必须拒绝——模式有麦不能证明 PS5 可用麦。"""

    def edit(p):
        for c in p["compatibility"]:
            if c["platform"] == "ps5":
                c["microphone_supported"] = comp_mic

    _edit_product(db_conn, "P009", edit)
    intent = _intent(
        HardConstraints(
            microphone=True,
            device_requirements=[DeviceRequirement(platform="ps5", playback_required=True)],
        ),
        budget_max=90000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P009" not in _ids(result)
    if comp_mic is None:
        assert _record(result, "hard_constraints.device_requirements").unknown_excluded_count >= 1


def test_s2a02_controls_device_mic_true_or_not_required(db_conn):
    """对照：兼容记录麦克风 true + 总体要求 → 通过（SC08 语义不变）；
    无总体/设备麦克风要求时，兼容记录麦克风取值不影响资格。"""
    intent = _intent(
        HardConstraints(
            microphone=True,
            device_requirements=[DeviceRequirement(platform="ps5", playback_required=True)],
        ),
        budget_max=90000,
    )
    assert _ids(get_eligible_candidates(db_conn, intent, now=NOW)) == ["P009"]

    def edit(p):
        for c in p["compatibility"]:
            if c["platform"] == "ps5":
                c["microphone_supported"] = False

    _edit_product(db_conn, "P009", edit)
    no_mic = _intent(
        HardConstraints(device_requirements=[DeviceRequirement(platform="ps5", playback_required=True)]),
        budget_max=90000,
    )
    assert _ids(get_eligible_candidates(db_conn, no_mic, now=NOW)) == ["P009"]


def test_s2a02_explicit_device_mic_still_rejected_when_false(db_conn):
    """既有行为对照：显式 microphone_required=true 在记录 false 时照旧拒绝。"""

    def edit(p):
        for c in p["compatibility"]:
            if c["platform"] == "ps5":
                c["microphone_supported"] = False

    _edit_product(db_conn, "P009", edit)
    intent = _intent(
        HardConstraints(device_requirements=[DeviceRequirement(platform="ps5", microphone_required=True)]),
        budget_max=90000,
    )
    assert "P009" not in _ids(get_eligible_candidates(db_conn, intent, now=NOW))


# S2A-03：条款前置条件固定语料整句精确匹配（配送截单优先）


def _guaranteed_intent(budget=50000):
    return ShoppingIntent(
        budget=Budget(max_cents=budget),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(commitment_type="guaranteed")
        ),
    )


def test_s2a03_nextday_before_1800_weekday_passes(db_conn):
    """18:00 前对照：周二 12:00 请求，guaranteed 报价通过（O023 语料条款可核验且满足）。"""
    result = get_eligible_candidates(db_conn, _guaranteed_intent(), now=NOW_ON_OBSERVED)
    assert _plans(result, "P008") == [(44900, "O023", "usb")]


def test_s2a03_nextday_after_1800_excluded(db_conn):
    """18:00 后：次日达保证条款不适用（不顺延、不重算窗口），guaranteed 报价不满足承诺类型。"""
    evening = datetime(2026, 9, 29, 19, 0, 0, tzinfo=derive.CN_TZ)
    result = get_eligible_candidates(db_conn, _guaranteed_intent(), now=evening)
    assert result.candidates == []
    rec = _record(result, "delivery_requirement.commitment_type")
    assert rec.excluded_count >= 1 and rec.unknown_excluded_count == 0
    # 硬截止同样不满足该保证（不被窗口满足所掩盖）
    deadline = ShoppingIntent(
        budget=Budget(max_cents=50000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(deadline_date=date(2026, 10, 2))
        ),
    )
    result = get_eligible_candidates(db_conn, deadline, now=evening)
    assert "P008" not in _ids(result)


def test_s2a03_nextday_weekend_excluded(db_conn):
    """周末对照：2026-10-03 为周六，工作日条款不适用 → guaranteed 报价排除。"""
    saturday = datetime(2026, 10, 3, 12, 0, 0, tzinfo=derive.CN_TZ)
    result = get_eligible_candidates(db_conn, _guaranteed_intent(), now=saturday)
    assert result.candidates == []


@pytest.mark.parametrize("conditions", [None, "仅观测日09:00前下单适用", "模拟次日达服务条款（工作日18点前下单）；另需核验"])
def test_s2a03_guaranteed_unknown_conditions_excluded(db_conn, conditions):
    """S2A-03 反例：guaranteed 承诺条款 null/语料外 → 未知排除（不按关键词放行）。"""

    def edit(o):
        o["delivery"]["conditions"] = conditions

    _edit_offer(db_conn, "O023", edit)
    result = get_eligible_candidates(db_conn, _guaranteed_intent(), now=NOW)
    assert "P008" not in _ids(result)
    rec = _record(result, "delivery_requirement.commitment_type")
    assert rec.unknown_excluded_count >= 1


def test_s2a03_estimated_out_of_corpus_excluded_only_with_delivery_requirement(db_conn):
    """estimated 条款语料外：表达配送硬要求（截止）时按未知排除；未表达配送要求时不设门槛。"""

    def edit(o):
        o["delivery"]["conditions"] = "会员专享线路"

    _edit_offer(db_conn, "O030", edit)
    deadline = ShoppingIntent(
        budget=Budget(max_cents=60000),
        hard_constraints=HardConstraints(
            delivery_requirement=DeliveryRequirement(deadline_date=date(2026, 10, 2))
        ),
    )
    result = get_eligible_candidates(db_conn, deadline, now=NOW_ON_OBSERVED)
    assert "P010" not in _ids(result)
    assert _record(result, "delivery_requirement.deadline_date").unknown_excluded_count >= 1
    plain = get_eligible_candidates(db_conn, _intent(budget_max=60000), now=NOW_ON_OBSERVED)
    assert "P010" in _ids(plain)


@pytest.mark.parametrize("conditions", [None, "开封退货须联系客服专员核验"])
def test_s2a03_opened_return_unknown_conditions_excluded(db_conn, conditions):
    """S2A-03 反例：opened_return_allowed=true 但适用条款 null/语料外 → 未知排除。"""

    def edit(m):  # 直接改商家默认政策（O017 无覆盖）
        m["default_return_policy"]["conditions"] = conditions

    row = db_conn.execute("SELECT payload FROM merchants WHERE merchant_id='M005'").fetchone()
    merchant = json.loads(row[0])
    edit(merchant)
    db_conn.execute("UPDATE merchants SET payload=? WHERE merchant_id='M005'",
                    (json.dumps(merchant),))
    db_conn.commit()
    intent = _intent(
        HardConstraints(
            sub_categories=["true_wireless_earbuds"],
            allowed_form_factors=["in_ear"],
            ear_tip_sizes=["S"],
            opened_return_allowed=True,
        ),
        budget_max=50000,
    )
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P005" not in _ids(result)  # 仅剩的 O017 政策条款未知
    assert _record(result, "hard_constraints.opened_return_allowed").unknown_excluded_count >= 1


@pytest.mark.parametrize("conditions", [None, "IPX7 及以下均适用"])
def test_s2a03_sweat_unknown_conditions_excluded(db_conn, conditions):
    """S2A-03 反例：防汗 allowed=true 但 conditions=null/语料外 → 未知排除。"""

    def edit(p):
        p["sweat_use_statement"]["conditions"] = conditions

    _edit_product(db_conn, "P004", edit)
    intent = _intent(HardConstraints(sweat_use_allowed=True, wireless=True), budget_max=40000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert "P004" not in _ids(result)
    assert _record(result, "hard_constraints.sweat_use_allowed").unknown_excluded_count >= 1


def test_s2a03_sweat_indoor_limitation_still_passes(db_conn):
    """对照（裁定2）：P013"仅室内训练"整句通过普通防汗要求（SC11 语义保持）。"""
    intent = _intent(HardConstraints(sweat_use_allowed=True, wireless=True), budget_max=40000)
    result = get_eligible_candidates(db_conn, intent, now=NOW)
    assert set(_ids(result)) == {"P004", "P013"}


# ---- FIX-S2A-R2（REACCEPT-S2A 2026-10-01）：S2AR-01 有效退货政策 exceptions 核验 -------


def _edit_merchant_default_return(conn, edit):
    row = conn.execute("SELECT payload FROM merchants WHERE merchant_id='M005'").fetchone()
    merchant = json.loads(row[0])
    edit(merchant["default_return_policy"])
    conn.execute("UPDATE merchants SET payload=? WHERE merchant_id='M005'",
                 (json.dumps(merchant),))
    conn.commit()


def _opened_intent():
    return _intent(
        HardConstraints(
            sub_categories=["true_wireless_earbuds"],
            allowed_form_factors=["in_ear"],
            ear_tip_sizes=["S"],
            opened_return_allowed=True,
        ),
        budget_max=50000,
    )


@pytest.mark.parametrize("exceptions", [None, ["仅经核实的付费会员可申请开封退货"]])
def test_s2ar01_return_exceptions_unknown_or_unverifiable_excluded(db_conn, exceptions):
    """S2AR-01 反例（商家默认路径）：opened=true 且条款为已知买家义务，但 exceptions
    为 null（未知）或含会员门槛（本版无可核验语料）——不得当作可验证承诺放行。"""
    _edit_merchant_default_return(db_conn, lambda policy: policy.__setitem__("exceptions", exceptions))
    result = get_eligible_candidates(db_conn, _opened_intent(), now=NOW)
    assert "P005" not in _ids(result)  # 仅剩的 O017 走 M005 默认政策，例外未通过
    rec = _record(result, "hard_constraints.opened_return_allowed")
    assert rec.unknown_excluded_count >= 1


def test_s2ar01_return_exceptions_empty_list_passes_via_default(db_conn):
    """对照：exceptions=[] 为确认无例外，正常放行（M005 默认即此形态）。"""
    result = get_eligible_candidates(db_conn, _opened_intent(), now=NOW)
    assert _plans(result, "P005") == [(35900, "O017", "bt")]


@pytest.mark.parametrize("exceptions,passes", [
    ([], True),
    (None, False),
    (["仅经核实的付费会员可申请开封退货"], False),
])
def test_s2ar01_return_exceptions_checked_via_offer_override(db_conn, exceptions, passes):
    """S2AR-01（覆盖路径，审验反例同形）：O036 增退货覆盖——opened=true、条款为已知
    买家义务，exceptions []/null/会员例外三态分别放行/未知排除/未知排除。"""

    def edit(o):
        o["return_policy_override"] = {
            "days": 15, "opened_return_allowed": True, "return_shipping_fee_cents": 800,
            "conditions": "包装和配件齐全", "exceptions": exceptions,
        }

    _edit_offer(db_conn, "O036", edit)
    result = get_eligible_candidates(db_conn, _opened_intent(), now=NOW)
    if passes:
        assert _plans(result, "P013") == [(29900, "O036", "bt")]
    else:
        assert "P013" not in _ids(result)
        assert _record(result, "hard_constraints.opened_return_allowed").unknown_excluded_count >= 1
