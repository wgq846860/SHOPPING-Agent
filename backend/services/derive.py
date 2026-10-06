"""派生计算（S1.3）：到手价、配件方案、配送窗、有效售后、资格与展示价。

规则代码与输入引用按 docs/data_dictionary.md §5–§8、docs/api_contract.md §2/§5：

- landed_price.v1   = price + shipping_fee + accessory_total - instant_discount
                      运费或必要配件价未知 → 总价 null（未知不按 0 运费处理）；
- accessory_total.v1 = 选中模式必要且不包含的配件价合计；显式 [] 为 0；
                      必要配件清单 null/缺失或任何必要项未知 → null（未知≠无需）；
- delivery_window.v1 = 观测日（Asia/Shanghai）+ min/max_days，含当天 day0；
                      单个分量未知则该端点 null；
- return_policy.v1 / warranty.v1 = Offer 非 null 覆盖对象整体优先，
                      未知叶子不继承商家默认值；policy_origin/warranty_origin 一并输出；
- display_offer.v1  = 有货、可定价、未过期、完整方案报价中最低到手价，平手 offer_id 升序；
- availability.v1   = 有可定价有货 Offer 为 in_stock；全部 stock=0 为 out_of_stock；
                      其余 unknown。

资格（传统 OfferView.eligible）＝默认地区/模式下有货、可定价、未过期且完整方案，
不等于满足任意用户意图（那是 S2 的 Agent 口径）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from backend.schemas.common import (
    AVAILABILITY_IN_STOCK,
    AVAILABILITY_OUT_OF_STOCK,
    AVAILABILITY_UNKNOWN,
    POLICY_ORIGIN_MERCHANT,
    POLICY_ORIGIN_OFFER,
    REASON_ACCESSORY_OUT_OF_STOCK,
    REASON_EXPIRED,
    REASON_MODE_UNAVAILABLE,
    REASON_PLAN_INCOMPLETE,
    REASON_PRICE_UNKNOWN,
    REASON_REGION_MISMATCH,
    REASON_OUT_OF_STOCK,
    REASON_STOCK_UNKNOWN,
    Derivation,
    Source,
)
from backend.schemas.offer import ReturnPolicy, Warranty
from backend.schemas.product import DeliveryWindow
from backend.services.errors import DataError

#: 中国标准时间固定 UTC+8（1991 年后无夏令时），避免依赖系统 tz 数据库。
CN_TZ = timezone(timedelta(hours=8))

#: 有效退货/保修的叶子名（数据字典 §6）
RETURN_POLICY_LEAVES = (
    "days",
    "opened_return_allowed",
    "return_shipping_fee_cents",
    "conditions",
    "exceptions",
)
WARRANTY_LEAVES = ("months", "provider", "conditions")


def parse_iso(value: str) -> datetime:
    """解析带时区的 ISO8601 时间；无时区视为数据错误。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"时间缺少时区：{value!r}")
    return dt


def cn_date(dt: datetime) -> date:
    """按数据字典 §5：observed_at 转 Asia/Shanghai 日期。"""
    return dt.astimezone(CN_TZ).date()


def is_expired(valid_until: str | None, now: datetime) -> bool:
    """valid_until 早于 now 才算过期；恰好等于参考时间仍有效。"""
    if valid_until is None:
        return False
    return parse_iso(valid_until) < now


# ---------------------------------------------------------------------------
# 配件方案与到手价
# ---------------------------------------------------------------------------


def accessory_plan(product: dict, offer: dict, mode_id: str) -> tuple[int | None, bool, str | None]:
    """返回 (accessory_total_cents, plan_complete, incomplete_reason)。

    - 无必要配件（显式 []）：合计 0、方案完整（明确无需，不为不必要配件收费）；
    - 必要配件清单 null/缺失（S2A-01，2026-10-01 整改）：依赖、费用与库存均不可
      确定，合计 null、方案不完整，不把"未知"当成"无需"；
    - 必要配件缺失 / accessories 未知 / 单价未知：合计 null，reason=plan_incomplete；
    - 非包含配件库存不大于 0：合计照算，但方案不完整，reason=accessory_out_of_stock。
    """
    mode = _find_mode(product, mode_id)
    if mode is None:
        return None, False, REASON_MODE_UNAVAILABLE
    required = mode.get("required_accessory_codes")
    if required is None:
        return None, False, REASON_PLAN_INCOMPLETE
    if not required:
        return 0, True, None

    accessories = offer.get("accessories")
    if accessories is None:
        return None, False, REASON_PLAN_INCOMPLETE

    total = 0
    reason: str | None = None
    for code in required:
        matches = [a for a in accessories if a.get("accessory_code") == code]
        if len(matches) != 1:
            return None, False, REASON_PLAN_INCOMPLETE
        acc = matches[0]
        if acc.get("included") is True:
            continue
        price = acc.get("price_cents")
        if price is None:
            return None, False, REASON_PLAN_INCOMPLETE
        total += price
        stock = acc.get("stock")
        if stock is None or stock <= 0:
            reason = REASON_ACCESSORY_OUT_OF_STOCK
    return total, reason is None, reason


def landed_price(offer: dict, accessory_total: int | None) -> int | None:
    """landed_price.v1；运费或必要配件价未知 → null。"""
    shipping = offer.get("shipping_fee_cents")
    if shipping is None or accessory_total is None:
        return None
    return (
        offer["price_cents"]
        + shipping
        + accessory_total
        - offer["instant_discount_cents"]
    )


def _find_mode(product: dict, mode_id: str) -> dict | None:
    for mode in product.get("connection_modes") or []:
        if mode.get("mode_id") == mode_id:
            return mode
    return None


def default_mode(product: dict, offer: dict) -> str | None:
    """display 模式：mode_id 升序第一个 playback 且完整方案可定价的模式。"""
    for mode in sorted(product.get("connection_modes") or [], key=lambda m: str(m.get("mode_id"))):
        if mode.get("playback_supported") is not True:
            continue
        total, complete, _ = accessory_plan(product, offer, mode["mode_id"])
        if not complete or total is None:
            continue
        if offer.get("shipping_fee_cents") is None:
            continue
        return mode["mode_id"]
    return None


# ---------------------------------------------------------------------------
# 配送窗
# ---------------------------------------------------------------------------


def delivery_window(offer: dict) -> tuple[DeliveryWindow, Derivation]:
    """delivery_window.v1；配送叶子来源观测时点作为窗口观测时点。"""
    delivery = offer["delivery"]
    observed_at = _delivery_observed_at(offer)
    base = cn_date(parse_iso(observed_at)) if observed_at else None

    def shift(days: int | None) -> str | None:
        if base is None or days is None:
            return None
        try:
            return (base + timedelta(days=days)).isoformat()
        except OverflowError as exc:
            # N2（复审 2026-09-30）：既有异常数据的查询映射为明确数据错误，
            # 不让溢出变成未知内部错误（API 层转 503 DATA_UNAVAILABLE）。
            raise DataError(
                f"报价 {offer['offer_id']} 的配送天数 {days} 在观测日期 "
                f"{base.isoformat()} 上无法计算配送窗") from exc

    window = DeliveryWindow(
        earliest=shift(delivery.get("min_days")),
        latest=shift(delivery.get("max_days")),
        commitment_type=delivery.get("commitment_type") or "unknown",
        destination_region=delivery.get("destination_region"),
        observed_at=observed_at or "",
    )
    derivation = Derivation(
        rule="delivery_window.v1",
        input_refs=[
            f"{offer['offer_id']}.delivery",
            f"{offer['offer_id']}.field_sources.delivery.max_days.observed_at",
        ],
    )
    return window, derivation


def _delivery_observed_at(offer: dict) -> str | None:
    """取配送叶子来源的观测时点（同批次应一致；优先 max_days 来源）。"""
    fs = offer.get("field_sources") or {}
    for key in (
        "delivery.max_days",
        "delivery.min_days",
        "delivery.destination_region",
        "delivery.commitment_type",
        "delivery.conditions",
    ):
        source = fs.get(key)
        if source and source.get("observed_at"):
            return source["observed_at"]
    return None


# ---------------------------------------------------------------------------
# 有效退货与保修
# ---------------------------------------------------------------------------


@dataclass
class EffectivePolicies:
    return_policy: ReturnPolicy
    warranty: Warranty
    policy_origin: str
    warranty_origin: str
    field_sources: dict[str, Source]
    derivations: dict[str, Derivation]


def effective_policies(offer: dict, merchant: dict) -> EffectivePolicies:
    """return_policy.v1 / warranty.v1：完整覆盖对象整体优先，未知叶子不继承。"""
    offer_fs = offer.get("field_sources") or {}
    merchant_fs = merchant.get("field_sources") or {}

    override = offer.get("return_policy_override")
    if override is not None:
        policy = ReturnPolicy(**{leaf: override.get(leaf) for leaf in RETURN_POLICY_LEAVES})
        policy_origin = POLICY_ORIGIN_OFFER
        policy_map = {
            f"effective_return_policy.{leaf}": offer_fs.get(f"return_policy_override.{leaf}")
            for leaf in RETURN_POLICY_LEAVES
        }
    else:
        default = merchant["default_return_policy"]
        policy = ReturnPolicy(**{leaf: default.get(leaf) for leaf in RETURN_POLICY_LEAVES})
        policy_origin = POLICY_ORIGIN_MERCHANT
        policy_map = {
            f"effective_return_policy.{leaf}": merchant_fs.get(f"default_return_policy.{leaf}")
            for leaf in RETURN_POLICY_LEAVES
        }

    warranty_override = offer.get("warranty_override")
    if warranty_override is not None:
        warranty = Warranty(**{leaf: warranty_override.get(leaf) for leaf in WARRANTY_LEAVES})
        warranty_origin = POLICY_ORIGIN_OFFER
        warranty_map = {
            f"effective_warranty.{leaf}": offer_fs.get(f"warranty_override.{leaf}")
            for leaf in WARRANTY_LEAVES
        }
    else:
        default = merchant["default_warranty"]
        warranty = Warranty(**{leaf: default.get(leaf) for leaf in WARRANTY_LEAVES})
        warranty_origin = POLICY_ORIGIN_MERCHANT
        warranty_map = {
            f"effective_warranty.{leaf}": merchant_fs.get(f"default_warranty.{leaf}")
            for leaf in WARRANTY_LEAVES
        }

    sources = {
        key: Source(**src)
        for key, src in {**policy_map, **warranty_map}.items()
        if src is not None
    }
    derivations = {
        "effective_return_policy": Derivation(
            rule="return_policy.v1",
            input_refs=[
                f"{offer['offer_id']}.return_policy_override",
                f"{merchant['merchant_id']}.default_return_policy",
            ],
        ),
        "effective_warranty": Derivation(
            rule="warranty.v1",
            input_refs=[
                f"{offer['offer_id']}.warranty_override",
                f"{merchant['merchant_id']}.default_warranty",
            ],
        ),
    }
    return EffectivePolicies(
        return_policy=policy,
        warranty=warranty,
        policy_origin=policy_origin,
        warranty_origin=warranty_origin,
        field_sources=sources,
        derivations=derivations,
    )


# ---------------------------------------------------------------------------
# 报价方案（资格 + 派生集合）
# ---------------------------------------------------------------------------


@dataclass
class OfferPlan:
    """一个报价在指定/默认模式下的完整派生结果。"""

    offer: dict
    merchant: dict
    mode_id: str | None
    mode_explicit: bool
    accessory_total: int | None
    landed: int | None
    delivery_window: DeliveryWindow
    delivery_derivation: Derivation
    policies: EffectivePolicies
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    incomplete_reason: str | None = None


def _first_playback_mode(product: dict) -> str | None:
    modes = sorted(
        (m for m in product.get("connection_modes") or [] if m.get("playback_supported") is True),
        key=lambda m: str(m.get("mode_id")),
    )
    return modes[0]["mode_id"] if modes else None


def _unavailable_reason(product: dict, offer: dict) -> str:
    """默认模式不可用时的诊断原因码（说明为什么没有可用模式）。"""
    if offer.get("shipping_fee_cents") is None:
        return REASON_PRICE_UNKNOWN
    probe = _first_playback_mode(product)
    if probe is None:
        return REASON_MODE_UNAVAILABLE
    _, complete, incomplete = accessory_plan(product, offer, probe)
    if not complete:
        return incomplete or REASON_PLAN_INCOMPLETE
    return REASON_PRICE_UNKNOWN


def build_plan(
    product: dict,
    offer: dict,
    merchant: dict,
    *,
    mode_id: str | None,
    region: str,
    now: datetime,
) -> OfferPlan:
    """按 mode_id（显式请求）或默认展示模式生成方案并判定传统资格。

    资格 = 默认地区/模式下有货、所选模式支持听音（playback_supported，
    显式与默认路径同规则）、可定价、未过期且完整方案（契约 §2）。
    方案完整性（必要配件）与可定价（到手价不为 null）分开诊断：
    缺配件报 plan_incomplete/accessory_out_of_stock，缺价格报 price_unknown。
    """
    explicit = mode_id is not None
    selected = mode_id if explicit else default_mode(product, offer)

    # F2（审查 2026-09-30）：显式与默认路径共用模式能力校验——
    # 模式存在但 playback_supported 不为 True（不支持/未知听音）时同样不可购，
    # 显式指定不得绕过。默认路径经 default_mode 已过滤，这里统一复核。
    mode_playable = False
    if selected is not None:
        mode = _find_mode(product, selected)
        mode_playable = mode is not None and mode.get("playback_supported") is True

    if selected is not None and mode_playable:
        total, complete, incomplete = accessory_plan(product, offer, selected)
    else:
        total, complete, incomplete = None, False, None

    landed = landed_price(offer, total)
    window, window_derivation = delivery_window(offer)
    policies = effective_policies(offer, merchant)

    reasons: list[str] = []
    if offer["delivery"].get("destination_region") != region:
        reasons.append(REASON_REGION_MISMATCH)
    if selected is None:
        reasons.append(REASON_MODE_UNAVAILABLE)
        reasons.append(_unavailable_reason(product, offer))
    elif not mode_playable:
        # 能力不满足是根因：不再叠加方案/价格诊断（与默认路径无可用模式时一致）
        reasons.append(REASON_MODE_UNAVAILABLE)
    else:
        if not complete:
            reasons.append(incomplete or REASON_PLAN_INCOMPLETE)
        if landed is None:
            reasons.append(REASON_PRICE_UNKNOWN)
    stock = offer.get("stock")
    if stock is None:
        reasons.append(REASON_STOCK_UNKNOWN)
    elif stock <= 0:
        reasons.append(REASON_OUT_OF_STOCK)
    if is_expired(offer.get("valid_until"), now):
        reasons.append(REASON_EXPIRED)

    # 保持原因码顺序稳定、不重复
    reasons = list(dict.fromkeys(reasons))

    return OfferPlan(
        offer=offer,
        merchant=merchant,
        mode_id=selected,
        mode_explicit=explicit,
        accessory_total=total,
        landed=landed,
        delivery_window=window,
        delivery_derivation=window_derivation,
        policies=policies,
        eligible=not reasons,
        reasons=reasons,
        incomplete_reason=incomplete,
    )


def choose_display(plans: list[OfferPlan]) -> OfferPlan | None:
    """display_offer.v1：合格报价中最低到手价，平手 offer_id 升序。"""
    eligible = [p for p in plans if p.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda p: (p.landed, p.offer["offer_id"]))


def availability(plans: list[OfferPlan], display: OfferPlan | None) -> str:
    """availability.v1：有可定价有货 Offer → in_stock；全部 stock=0 → out_of_stock。"""
    if display is not None:
        return AVAILABILITY_IN_STOCK
    if plans and all(p.offer.get("stock") == 0 for p in plans):
        return AVAILABILITY_OUT_OF_STOCK
    return AVAILABILITY_UNKNOWN
