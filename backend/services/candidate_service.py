"""Public Demo implementation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from backend.database import queries
from backend.schemas.agent import (
    ShoppingIntent,
    WIRELESS_SUB_CATEGORIES,
    WIRELESS_TRANSPORTS,
    WIRED_TRANSPORTS,
)
from backend.schemas.agent import Clarification
from backend.schemas.common import (
    ESTIMATED_DESCRIPTIVE_CONDITIONS,
    GUARANTEED_NEXTDAY_CONDITIONS,
    OPENED_RETURN_BUYER_OBLIGATION_CONDITIONS,
    REASON_ACCESSORY_OUT_OF_STOCK,
    REASON_EXPIRED,
    REASON_MODE_UNAVAILABLE,
    REASON_PLAN_INCOMPLETE,
    REASON_PRICE_UNKNOWN,
    REASON_REGION_MISMATCH,
    SWEAT_ALLOWED_CONDITIONS,
)
from backend.services.derive import CN_TZ, OfferPlan, build_plan, cn_date, parse_iso

#: 契约 §3/§7.1：本版唯一支持的目的地
DEFAULT_REGION = "CN-SH"

#: 传统基线资格中属"数据未知/不可验证"的原因码（计入 unknown_excluded_count）
_UNKNOWN_BASELINE_REASONS = frozenset(
    {REASON_MODE_UNAVAILABLE, REASON_PLAN_INCOMPLETE, REASON_PRICE_UNKNOWN}
)
#: 基线有效性检查的原因码（库存单列一步，地区仅在用户显式给出时单列）
_BASELINE_REASONS = frozenset(
    {
        REASON_EXPIRED,
        REASON_REGION_MISMATCH,
        REASON_MODE_UNAVAILABLE,
        REASON_PLAN_INCOMPLETE,
        REASON_PRICE_UNKNOWN,
        REASON_ACCESSORY_OUT_OF_STOCK,
    }
)


# ---------------------------------------------------------------------------
# 结果结构
# ---------------------------------------------------------------------------


@dataclass
class FilterRecord:
    """一条硬条件的执行记录（形状对齐契约 §7.2 FilterApplied）。"""

    field: str
    operator: str
    value: object
    unit: str | None
    scope: str
    excluded_count: int = 0
    unknown_excluded_count: int = 0


@dataclass
class Candidate:
    """通过全部硬约束的候选：商品 + 指标 + 全部合格 (Offer, 模式) 方案。

    eligible_plans 按 (landed_price, offer_id, mode_id) 升序，供 S2.3
    Offer 排序与 S2.4 解释使用。
    """

    product: dict
    metrics: dict | None
    eligible_plans: list[OfferPlan]

    @property
    def product_id(self) -> str:
        return self.product["product_id"]


@dataclass
class CandidateSearch:
    """get_eligible_candidates 的结果：候选集 + 澄清 + 过滤统计 + 警告。"""

    status: str  # "ok" | "needs_clarification"
    clarifications: list[Clarification]
    candidates: list[Candidate]
    filters_applied: list[FilterRecord]
    warnings: list[dict]


@dataclass
class _ProductState:
    """管道中的商品中间态。modes=None 表示未表达连接类条件（默认展示模式）。"""

    product: dict
    metrics: dict | None
    offers: list[dict]
    modes: list[dict] | None = None
    plans: list[OfferPlan] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 意图冲突检测
# ---------------------------------------------------------------------------


def detect_intent_conflicts(intent: ShoppingIntent) -> list[Clarification]:
    """识别自相矛盾的硬条件组合；返回非空即 needs_clarification。

    检测项（保守集合，依据字典 §8 与契约 §7.4）：
    - wireless 布尔与 allowed_transports / 无线必然品类互斥；
    - ANC 开启续航要求与"要求无 ANC"互斥；
    - min_active_devices≥2 与 multipoint=false 互斥（多点能力是双设备前提）；
    - 形态允许集与排除集无交集 / 与 open_ear 布尔矛盾。
    """
    hc = intent.hard_constraints
    out: list[Clarification] = []

    if hc.allowed_transports:
        has_wireless = any(t in WIRELESS_TRANSPORTS for t in hc.allowed_transports)
        has_wired = any(t in WIRED_TRANSPORTS for t in hc.allowed_transports)
        if hc.wireless is True and not has_wireless:
            out.append(
                Clarification(
                    field="hard_constraints.allowed_transports",
                    reason="conflicting_constraints",
                    question="您要求无线耳机，但限定的连接方式全为有线，两者矛盾；请确认需要无线还是有线。",
                )
            )
        if hc.wireless is False and not has_wired:
            out.append(
                Clarification(
                    field="hard_constraints.allowed_transports",
                    reason="conflicting_constraints",
                    question="您要求有线耳机，但限定的连接方式不含任何有线传输，两者矛盾；请确认需要无线还是有线。",
                )
            )

    if hc.sub_categories:
        subs = set(hc.sub_categories)
        if hc.wireless is False and subs and subs <= set(WIRELESS_SUB_CATEGORIES):
            out.append(
                Clarification(
                    field="hard_constraints.sub_categories",
                    reason="conflicting_constraints",
                    question="真无线/无线耳塞在结构上无法满足“必须有线上连接”，请确认品类或连接要求。",
                )
            )
        if hc.wireless is True and subs == {"wired_headphones"}:
            out.append(
                Clarification(
                    field="hard_constraints.sub_categories",
                    reason="conflicting_constraints",
                    question="有线耳机品类与“必须无线”矛盾，请确认品类或连接要求。",
                )
            )

    if (
        hc.battery_requirement is not None
        and hc.battery_requirement.anc_state == "on"
        and hc.active_noise_cancelling is False
    ):
        out.append(
            Clarification(
                field="hard_constraints.battery_requirement",
                reason="conflicting_constraints",
                question="您要求 ANC 开启时的续航，又要求耳机不具备主动降噪，两者矛盾。",
            )
        )

    if hc.min_active_devices is not None and hc.min_active_devices >= 2 and hc.multipoint is False:
        out.append(
            Clarification(
                field="hard_constraints.min_active_devices",
                reason="conflicting_constraints",
                question="同时连接多台设备需要多点连接能力，与“必须不支持多点”矛盾。",
            )
        )

    if hc.multipoint is False and any(d.multipoint_required for d in hc.device_requirements or []):
        out.append(
            Clarification(
                field="hard_constraints.device_requirements",
                reason="conflicting_constraints",
                question="设备要求支持多点连接，但同时要求不支持多点；请确认多点要求。",
            )
        )

    if hc.allowed_form_factors and hc.excluded_form_factors:
        if not set(hc.allowed_form_factors) - set(hc.excluded_form_factors):
            out.append(
                Clarification(
                    field="hard_constraints.allowed_form_factors",
                    reason="conflicting_constraints",
                    question="允许的佩戴形态已全部被排除条件覆盖，没有可选形态；请调整形态条件。",
                )
            )

    if hc.open_ear is not None and hc.allowed_form_factors:
        allowed = set(hc.allowed_form_factors)
        if hc.open_ear is True and "open_ear" not in allowed:
            out.append(
                Clarification(
                    field="hard_constraints.allowed_form_factors",
                    reason="conflicting_constraints",
                    question="要求不堵耳道的开放形态，但允许的形态中不含开放式；请确认形态要求。",
                )
            )
        if hc.open_ear is False and allowed == {"open_ear"}:
            out.append(
                Clarification(
                    field="hard_constraints.allowed_form_factors",
                    reason="conflicting_constraints",
                    question="要求堵耳道佩戴，但只允许开放式形态；请确认形态要求。",
                )
            )

    return out


# ---------------------------------------------------------------------------
# 候选检索主管道
# ---------------------------------------------------------------------------


def get_eligible_candidates(
    conn: sqlite3.Connection,
    intent: ShoppingIntent,
    *,
    now: datetime,
) -> CandidateSearch:
    """硬约束过滤入口：冲突/待澄清意图不检索；否则返回候选与过滤统计。"""
    conflicts = detect_intent_conflicts(intent)
    if conflicts:
        return CandidateSearch("needs_clarification", conflicts, [], [], [])
    if intent.clarifications:
        return CandidateSearch(
            "needs_clarification", list(intent.clarifications), [], [], []
        )

    hc = intent.hard_constraints
    delivery = hc.delivery_requirement
    region = (delivery.destination_region if delivery else None) or DEFAULT_REGION
    now_cn_date = cn_date(now)

    rows = queries.search_products(
        conn, q=None, q_active=False, brand=None, sub_category=None, min_rating=None
    )
    product_ids = [row["product_id"] for row in rows]
    offers = queries.fetch_offers_for_products(conn, product_ids)
    offers_by_product: dict[str, list[dict]] = {}
    for offer in offers:
        offers_by_product.setdefault(offer["product_id"], []).append(offer)
    merchant_ids = sorted({o["merchant_id"] for o in offers})
    merchants = queries.fetch_merchants(conn, merchant_ids)

    states = [
        _ProductState(
            product=row["product"],
            metrics=row["metrics"],
            offers=offers_by_product.get(row["product_id"], []),
        )
        for row in rows
    ]
    if _mode_filter_needed(hc):
        for st in states:
            st.modes = [
                m
                for m in (st.product.get("connection_modes") or [])
                if m.get("playback_supported") is True
            ]

    records: list[FilterRecord] = []
    warnings: list[dict] = []
    ctx = _StepContext(records, warnings, now, now_cn_date, region)

    states = _apply_product_steps(states, hc, ctx)
    states = _apply_mode_steps(states, hc, ctx)
    _build_plans(states, merchants, region, now)
    states, stale_seen = _apply_offer_steps(
        states, hc, delivery, intent.budget.max_cents, ctx
    )

    if stale_seen:
        warnings.append(
            {
                "code": "STALE_DATA",
                "message": "配送窗口的观测日期早于请求日期，历史窗口不能验证硬截止；"
                "需要刷新数据，而不是滚动旧日期。",
                "field": "delivery_requirement.deadline_date",
                "product_id": None,
            }
        )

    candidates = [
        Candidate(
            product=st.product,
            metrics=st.metrics,
            eligible_plans=sorted(
                st.plans,
                key=lambda p: (p.landed, p.offer["offer_id"], p.mode_id or ""),
            ),
        )
        for st in states
    ]
    return CandidateSearch("ok", [], candidates, records, warnings)


class _StepContext:
    """步骤间共享的只读上下文与记录器。"""

    def __init__(
        self,
        records: list[FilterRecord],
        warnings: list[dict],
        now: datetime,
        now_cn_date: date,
        region: str,
    ) -> None:
        self.records = records
        self.warnings = warnings
        self.now = now
        self.now_cn_date = now_cn_date
        self.region = region


def _mode_filter_needed(hc) -> bool:
    """是否需要枚举模式（任一连接类条件或续航/设备条件都会绑定模式）。"""
    return (
        hc.wireless is not None
        or hc.active_noise_cancelling is not None
        or hc.microphone is not None
        or hc.requires_power is not None
        or hc.allowed_transports is not None
        or hc.battery_requirement is not None
        or hc.multipoint is not None
        or (hc.min_active_devices is not None and hc.min_active_devices >= 2)
        or bool(hc.device_requirements)
    )


# ---------------------------------------------------------------------------
# 商品级步骤
# ---------------------------------------------------------------------------


def _apply_product_steps(
    states: list[_ProductState], hc, ctx: _StepContext
) -> list[_ProductState]:
    def sub_categories(st, value):
        return st.product.get("sub_category") in value, False

    def allowed_forms(st, value):
        ff = st.product.get("form_factor")
        if ff is None:
            return False, True
        return ff in value, False

    def excluded_forms(st, value):
        # 排除优先且只排除确认命中：形态未知不因排除条件被拒（正面条件另行把关）
        ff = st.product.get("form_factor")
        if ff is None:
            return True, False
        return ff not in value, False

    def boolean(field_name):
        def check(st, value):
            actual = st.product.get(field_name)
            if actual is None:
                return False, True
            return actual is value, False

        return check

    def sweat(st, value):
        statement = st.product.get("sweat_use_statement")
        if statement is None or statement.get("allowed") is None:
            return False, True
        if statement.get("allowed") is not value:
            return False, False
        if value and statement.get("conditions") not in SWEAT_ALLOWED_CONDITIONS:
            
            return False, True
        return True, False

    def ear_tips(st, value):
        tips = st.product.get("ear_tip_sizes")
        if tips is None:
            return False, True
        return set(value) <= set(tips), False

    def min_devices(st, value):
        actual = st.product.get("max_active_devices")
        if actual is None:
            return False, True
        return actual >= value, False

    steps: list[tuple[str, str, object, str | None, object]] = []
    if hc.sub_categories:
        steps.append(("hard_constraints.sub_categories", "in", list(hc.sub_categories), None, sub_categories))
    if hc.allowed_form_factors:
        steps.append(("hard_constraints.allowed_form_factors", "in", list(hc.allowed_form_factors), None, allowed_forms))
    if hc.excluded_form_factors:
        steps.append(("hard_constraints.excluded_form_factors", "not_in", list(hc.excluded_form_factors), None, excluded_forms))
    if hc.open_ear is not None:
        steps.append(("hard_constraints.open_ear", "eq", hc.open_ear, None, boolean("open_ear")))
    if hc.sweat_use_allowed is not None:
        steps.append(("hard_constraints.sweat_use_allowed", "eq", hc.sweat_use_allowed, None, sweat))
    if hc.ear_tip_sizes:
        steps.append(("hard_constraints.ear_tip_sizes", "all", list(hc.ear_tip_sizes), None, ear_tips))
    if hc.min_active_devices is not None:
        steps.append(("hard_constraints.min_active_devices", "gte", hc.min_active_devices, None, min_devices))
    if hc.weight_requirement is not None:
        req = hc.weight_requirement
        steps.append(
            (
                "hard_constraints.weight_requirement.max_grams",
                "lte",
                req.max_grams,
                "g",
                _weight_predicate(req),
            )
        )

    for field_path, operator, value, unit, predicate in steps:
        kept, excluded, unknown = [], 0, 0
        for st in states:
            passed, unknown_fail = predicate(st, value)
            if passed:
                kept.append(st)
            else:
                excluded += 1
                unknown += 1 if unknown_fail else 0
        ctx.records.append(
            FilterRecord(field_path, operator, value, unit, "product", excluded, unknown)
        )
        states = kept
    return states


def _weight_predicate(req):
    def check(st, _value):
        specs = st.product.get("weight_specs") or []
        if not specs:
            return False, True
        matched = [s for s in specs if s.get("basis") == req.basis]
        if not matched:
            # 存在重量数据但无同口径记录：不跨口径换算，按确定不满足计
            return False, False
        for spec in matched:
            if not (spec.get("conditions") or "").strip():
                continue
            grams = spec.get("grams")
            if grams is None:
                continue
            if grams <= req.max_grams:
                return True, False
        return False, any(s.get("grams") is None or not (s.get("conditions") or "").strip() for s in matched)

    return check


# ---------------------------------------------------------------------------
# 模式级步骤（连接方案能力，绑定实际模式）
# ---------------------------------------------------------------------------


def _apply_mode_steps(
    states: list[_ProductState], hc, ctx: _StepContext
) -> list[_ProductState]:
    def _run(field_path, operator, value, unit, filter_modes):
        nonlocal states
        kept, excluded, unknown = [], 0, 0
        for st in states:
            if st.modes is None:
                kept.append(st)
                continue
            kept_modes, dropped_unknown = filter_modes(st)
            st.modes = kept_modes
            if kept_modes:
                kept.append(st)
            else:
                excluded += 1
                unknown += 1 if dropped_unknown else 0
        ctx.records.append(
            FilterRecord(field_path, operator, value, unit, "connection_plan", excluded, unknown)
        )
        states = kept

    if hc.wireless is not None:
        def wireless_filter(st, required=hc.wireless):
            kept, unknown = [], False
            for m in st.modes:
                transport = m.get("transport")
                ok = transport in (WIRELESS_TRANSPORTS if required else WIRED_TRANSPORTS)
                if ok:
                    kept.append(m)
            return kept, unknown

        _run(
            "hard_constraints.wireless", "eq", hc.wireless, None, wireless_filter
        )

    if hc.active_noise_cancelling is not None:
        def anc_filter(st, required=hc.active_noise_cancelling):
            kept, unknown = [], False
            for m in st.modes:
                actual = m.get("active_noise_cancelling")
                if actual is None:
                    unknown = True
                elif actual is required:
                    kept.append(m)
            return kept, unknown

        _run(
            "hard_constraints.active_noise_cancelling",
            "eq",
            hc.active_noise_cancelling,
            None,
            anc_filter,
        )

    if hc.microphone is not None:
        def mic_filter(st, required=hc.microphone):
            kept, unknown = [], False
            for m in st.modes:
                actual = m.get("microphone_supported")
                if actual is None:
                    unknown = True
                elif actual is required:
                    kept.append(m)
            return kept, unknown

        _run("hard_constraints.microphone", "eq", hc.microphone, None, mic_filter)

    if hc.requires_power is not None:
        def power_filter(st, required=hc.requires_power):
            kept, unknown = [], False
            for m in st.modes:
                actual = m.get("requires_power")
                if actual is None:
                    unknown = True
                elif actual is required:
                    kept.append(m)
            return kept, unknown

        _run(
            "hard_constraints.requires_power", "eq", hc.requires_power, None, power_filter
        )

    if hc.allowed_transports:
        def transport_filter(st, allowed=set(hc.allowed_transports)):
            kept = [m for m in st.modes if m.get("transport") in allowed]
            return kept, False

        _run(
            "hard_constraints.allowed_transports",
            "in",
            list(hc.allowed_transports),
            None,
            transport_filter,
        )

    if hc.battery_requirement is not None:
        _run(
            "hard_constraints.battery_requirement.min_hours",
            "gte",
            hc.battery_requirement.min_hours,
            "h",
            _battery_filter(hc.battery_requirement),
        )

    multipoint = hc.multipoint
    if hc.min_active_devices is not None and hc.min_active_devices >= 2:
        multipoint = True
    if multipoint is not None and not hc.device_requirements:
        def multipoint_filter(st):
            # 商品总能力只在唯一模式时定位到实际模式。多模式且无目标设备，
            # 无法从某条兼容记录推断用户将使用的设备；按未知拒绝。
            if len(st.product.get("connection_modes") or []) != 1:
                return [], True
            actual = st.product.get("multipoint")
            if actual is None:
                return [], True
            return (st.modes if actual is multipoint else []), False

        field_path = "hard_constraints.multipoint" if hc.multipoint is not None else "hard_constraints.min_active_devices"
        _run(field_path, "eq" if hc.multipoint is not None else "gte",
             hc.multipoint if hc.multipoint is not None else hc.min_active_devices,
             None, multipoint_filter)

    if hc.min_active_devices is not None and hc.min_active_devices > 2 and hc.device_requirements:
        def device_count_filter(st):
            # Compatibility只描述多点布尔能力（至少2台），没有模式级台数。
            # 多模式商品的总台数不能证明当前模式支持3台及以上。
            if len(st.product.get("connection_modes") or []) != 1:
                return [], True
            return st.modes, False

        _run("hard_constraints.min_active_devices", "gte", hc.min_active_devices,
             None, device_count_filter)

    if hc.device_requirements:
        _run(
            "hard_constraints.device_requirements",
            "compatible",
            [d.model_dump() for d in hc.device_requirements],
            None,
            # S2A-02（2026-10-01 整改）：总体麦克风硬要求贯通到每个目标设备——
            # 模式自身有麦不能证明在该设备上可用麦（字典 §8），逐设备核验。
            _device_filter(hc.device_requirements, multipoint, hc.microphone),
        )

    return states


def _battery_filter(req):
    def filter_modes(st):
        specs = st.product.get("battery_specs") or []
        kept, unknown = [], False
        for m in st.modes:
            if req.mode_id is not None and m.get("mode_id") != req.mode_id:
                continue
            matched = [
                s
                for s in specs
                if s.get("basis") == req.basis
                and s.get("anc_state") == req.anc_state
                and s.get("mode_id") == m.get("mode_id")
            ]
            if not matched:
                if specs:
                    continue  # 有续航数据但无同口径/同条件记录：确定不满足（EDGE07）
                unknown = True  # 商品完全没有续航记录：未知
                continue
            for spec in matched:
                if req.conditions is not None:
                    actual = spec.get("test_conditions")
                    if not actual or not req.conditions.strip() or actual != req.conditions:
                        unknown = True  # 自由文本不匹配不能推断等价
                        continue
                hours = spec.get("hours")
                if hours is None:
                    unknown = True
                elif hours >= req.min_hours:
                    kept.append(m)
                    break
        return kept, unknown

    return filter_modes


def _device_filter(device_reqs, multipoint: bool | None, overall_mic: bool | None = None):
    def filter_modes(st):
        kept, unknown = [], False
        for m in st.modes:
            results = [_match_compatibility(st.product, m, d, multipoint, overall_mic)
                       for d in device_reqs]
            if all(r == "pass" for r in results):
                kept.append(m)
            elif all(r in ("pass", "unknown") for r in results):
                unknown = True
        return kept, unknown

    return filter_modes


def _match_compatibility(product: dict, mode: dict, req, multipoint: bool | None,
                         overall_mic: bool | None = None) -> str:
    """单条设备要求与商品兼容记录的匹配结果：pass / unknown / miss。

    身份字段（device_key/platform/source_output/firmware/transport）不匹配为
    miss；能力字段（playback/microphone/multipoint）与 os_version 为 null 时
    不视为支持（unknown），不凭字符串相似或蓝牙版本推断（EDGE08）。
    意图级 multipoint 在存在设备要求时按兼容记录逐设备核验（目标值跟随意图）。
    总体 microphone=true 同样逐设备核验兼容记录的 microphone_supported（S2A-02）；
    false 不反向传播——"不需要麦"不是设备能力禁令。
    """
    best = "miss"
    for comp in product.get("compatibility") or []:
        if comp.get("mode_id") != mode.get("mode_id"):
            continue
        if req.device_key is not None and comp.get("device_key") != req.device_key:
            continue
        if comp.get("platform") != req.platform:
            continue
        if req.source_output is not None and comp.get("source_output") != req.source_output:
            continue
        if req.firmware_version is not None and comp.get("firmware_version") != req.firmware_version:
            continue
        if req.transport is not None and mode.get("transport") != req.transport:
            continue
        result = "pass" if _compatibility_conditions_met(comp, mode, req) else "unknown"
        if req.os_version is not None:
            actual = comp.get("os_version")
            if actual is None:
                result = "unknown"
            elif actual != req.os_version:
                continue  # 版本明确不符：该记录不可用
        capability_checks: list[tuple[str, bool]] = []
        if req.playback_required:
            capability_checks.append(("playback_supported", True))
        if req.microphone_required or overall_mic is True:
            capability_checks.append(("microphone_supported", True))
        if req.multipoint_required:
            capability_checks.append(("multipoint_supported", True))
        if multipoint is not None:
            # 意图要求 multipoint 且存在设备要求：按该设备兼容记录核验多点能力
            capability_checks.append(("multipoint_supported", multipoint))
        for key, target in capability_checks:
            actual = comp.get(key)
            if actual is None:
                result = "unknown"
            elif actual is not target:
                result = "miss"
                break
        if result == "pass":
            return "pass"
        if result == "unknown":
            best = "unknown"
    return best


def _compatibility_conditions_met(comp: dict, mode: dict, req) -> bool:
    """仅核验已知模拟fixture的前提，不用关键词猜测任意自然语言。

    配件在ConnectionMode声明后，后续build_plan会核验同一Offer的完整性、
    库存与价格。未知/新增条件拒绝自动通过，待补结构化证据或澄清。
    """
    conditions = comp.get("conditions")
    required = set(mode.get("required_accessory_codes") or [])
    transport = mode.get("transport")
    if conditions == "USB直连即插即用":
        return transport == "usb_audio"
    if conditions in (
        "需QG-DONGLE-USB适配器（随部分报价附赠或另购）",
        "同PS5，需同一适配器",
    ):
        return transport == "wireless_dongle" and "QG-DONGLE-USB" in required
    if conditions == "电视需具备光纤输出；VK-TX-OPT发射器随部分报价附赠或另购":
        return (transport == "optical_transmitter" and req.source_output == "optical_pcm"
                and "VK-TX-OPT" in required)
    if conditions == "电脑需具备TRRS复合接口；分离式接口需另购转接线（本商品未含）":
        return (transport == "wired_3_5mm" and req.device_key == "通用PC（3.5mm TRRS复合口）"
                and mode.get("audio_connector") == "3.5mm_TRRS_CTIA")
    return False


# ---------------------------------------------------------------------------
# 报价方案构建与 Offer 级步骤
# ---------------------------------------------------------------------------


def _build_plans(
    states: list[_ProductState],
    merchants: dict[str, dict],
    region: str,
    now: datetime,
) -> None:
    for st in states:
        plans: list[OfferPlan] = []
        for offer in st.offers:
            merchant = merchants[offer["merchant_id"]]
            if st.modes is None:
                plans.append(
                    build_plan(
                        st.product, offer, merchant, mode_id=None, region=region, now=now
                    )
                )
            else:
                for mode in st.modes:
                    plans.append(
                        build_plan(
                            st.product,
                            offer,
                            merchant,
                            mode_id=mode["mode_id"],
                            region=region,
                            now=now,
                        )
                    )
        st.plans = plans


def _apply_offer_steps(
    states: list[_ProductState], hc, delivery, budget_max: int | None, ctx: _StepContext
) -> tuple[list[_ProductState], bool]:
    stale_seen = False

    def _run(field_path, operator, value, unit, predicate):
        nonlocal states
        kept, excluded, unknown = [], 0, 0
        for st in states:
            passed_plans, unknown_fail = [], False
            for plan in st.plans:
                ok, unknown_plan = predicate(st, plan)
                if ok:
                    passed_plans.append(plan)
                elif unknown_plan:
                    unknown_fail = True
            st.plans = passed_plans
            if passed_plans:
                kept.append(st)
            else:
                excluded += 1
                unknown += 1 if unknown_fail else 0
        ctx.records.append(
            FilterRecord(field_path, operator, value, unit, "offer", excluded, unknown)
        )
        states = kept

    if delivery is not None and delivery.destination_region is not None:
        def region_pred(st, plan):
            if REASON_REGION_MISMATCH in plan.reasons:
                unknown = plan.offer.get("delivery", {}).get("destination_region") is None
                return False, unknown
            return True, False

        _run(
            "delivery_requirement.destination_region",
            "eq",
            delivery.destination_region,
            None,
            region_pred,
        )

    def baseline_pred(st, plan):
        fail = _BASELINE_REASONS & set(plan.reasons)
        if not fail:
            return True, False
        return False, bool(fail & _UNKNOWN_BASELINE_REASONS)

    _run("offer.eligible", "eq", True, None, baseline_pred)

    def stock_pred(st, plan):
        stock = plan.offer.get("stock")
        if stock is None:
            return False, True
        return stock > 0, False

    _run("hard_constraints.stock_required", "eq", True, None, stock_pred)

    if budget_max is not None:
        def budget_pred(st, plan, max_cents=budget_max):
            if plan.landed is None:
                return False, True
            return plan.landed <= max_cents, False

        _run("budget.max_cents", "lte", budget_max, "CNY_cent", budget_pred)

    if hc.item_condition is not None:
        def condition_pred(st, plan, required=hc.item_condition):
            actual = plan.offer.get("item_condition")
            if actual is None:
                return False, True
            return actual == required, False

        _run(
            "hard_constraints.item_condition",
            "eq",
            hc.item_condition,
            None,
            condition_pred,
        )

    if hc.opened_return_allowed is not None:
        def opened_pred(st, plan, required=hc.opened_return_allowed):
            policy = plan.policies.return_policy
            actual = policy.opened_return_allowed
            if actual is None:
                return False, True
            if actual is not required:
                return False, False
            if required:
                if policy.conditions not in OPENED_RETURN_BUYER_OBLIGATION_CONDITIONS:
                    # 可开封承诺的适用条款 null/语料外（含"按条款退货"类引用未给出条款）：
                    
                    # 本身已不满足开封退货要求，无需另立规则。
                    return False, True
                if policy.exceptions is None or policy.exceptions:
                    # S2AR-01（REACCEPT-S2A 2026-10-01）：字典 §6 区分 exceptions=[]
                    # （确认无例外，放行）与 null（未知）；非空例外清单（如仅会员可退）
                    # 本版没有可核验语料——有效政策的限制不能因 opened=true 而消失，
                    # 均按未知排除。默认与覆盖两条来源路径同规则。
                    return False, True
            return True, False

        _run(
            "hard_constraints.opened_return_allowed",
            "eq",
            hc.opened_return_allowed,
            None,
            opened_pred,
        )

    if delivery is not None and delivery.commitment_type is not None:
        required = delivery.commitment_type

        def commitment_pred(st, plan):
            offer_delivery = plan.offer.get("delivery", {})
            actual = offer_delivery.get("commitment_type")
            if actual in (None, "unknown"):
                return False, True
            gate = _delivery_conditions_gate(offer_delivery, ctx.now)
            if gate == "unknown":
                return False, True
            if gate == "fail":
                return False, False
            if required == "guaranteed":
                return actual == "guaranteed", False
            # 用户明确接受预计时，更强的保证承诺同样满足
            return actual in ("estimated", "guaranteed"), False

        _run(
            "delivery_requirement.commitment_type",
            "eq",
            required,
            None,
            commitment_pred,
        )

    if delivery is not None and delivery.conditions is not None:
        def conditions_pred(st, plan, required=delivery.conditions):
            offer_delivery = plan.offer.get("delivery", {})
            gate = _delivery_conditions_gate(offer_delivery, ctx.now)
            if gate == "unknown":
                return False, True
            if gate == "fail":
                return False, False
            actual = offer_delivery.get("conditions")
            if actual is None or actual != required:
                # 自由文本条件无法结构化核验：不通过，按未知计
                return False, True
            return True, False

        _run(
            "delivery_requirement.conditions",
            "eq",
            delivery.conditions,
            None,
            conditions_pred,
        )

    if delivery is not None and delivery.deadline_date is not None:
        deadline = delivery.deadline_date

        def deadline_pred(st, plan):
            nonlocal stale_seen
            gate = _delivery_conditions_gate(plan.offer.get("delivery", {}), ctx.now)
            if gate == "unknown":
                return False, True
            if gate == "fail":
                return False, False
            window = plan.delivery_window
            if not window.observed_at:
                return False, True
            observed_date = cn_date(parse_iso(window.observed_at))
            if observed_date != ctx.now_cn_date:
                # 字典 §5：窗口仅对该观测日期下单的方案有效（STALE_DATA）
                stale_seen = True
                return False, True
            if window.latest is None:
                return False, True
            if date.fromisoformat(window.latest) > deadline:
                return False, False
            return _accessories_same_package(st, plan)

        _run(
            "delivery_requirement.deadline_date",
            "lte",
            deadline.isoformat(),
            None,
            deadline_pred,
        )

    return states, stale_seen


def _delivery_conditions_gate(offer_delivery: dict, now: datetime) -> str:
    """Public Demo implementation."""
    conditions = offer_delivery.get("conditions")
    if offer_delivery.get("commitment_type") == "guaranteed":
        if conditions != GUARANTEED_NEXTDAY_CONDITIONS:
            return "unknown"
        local = now.astimezone(CN_TZ)
        if local.weekday() >= 5 or local.hour >= 18:
            return "fail"
        return "pass"
    if conditions is None or conditions in ESTIMATED_DESCRIPTIVE_CONDITIONS:
        return "pass"
    return "unknown"


def _accessories_same_package(st: _ProductState, plan: OfferPlan) -> tuple[bool, bool]:
    """硬截止要求必要配件同包裹（字典 §5），否则无法验证完整方案到货。"""
    mode = None
    for m in st.product.get("connection_modes") or []:
        if m.get("mode_id") == plan.mode_id:
            mode = m
            break
    required = (mode or {}).get("required_accessory_codes")
    if required is None:
        # 必要配件清单未知（S2A-01）：无法验证完整方案到货，按未知计
        return False, True
    if not required:
        return True, False
    accessories = plan.offer.get("accessories")
    if accessories is None:
        return False, True
    for code in required:
        matches = [a for a in accessories if a.get("accessory_code") == code]
        if len(matches) != 1:
            return False, True
        acc = matches[0]
        if acc.get("included") is True:
            continue
        if acc.get("delivery_in_same_package") is not True:
            return False, True
    return True, False
