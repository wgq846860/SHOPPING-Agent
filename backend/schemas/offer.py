"""报价侧派生输出结构（S1.3）。

对应 docs/api_contract.md §2、§5：OfferView 携带完整 Offer/Merchant 原始实体
（含各自 field_sources），再加默认/请求模式下的方案派生值、有效售后与资格说明。
"""

from __future__ import annotations

from pydantic import BaseModel

from backend.schemas.common import Derivation, Source
from backend.schemas.product import DeliveryWindow


class ReturnPolicy(BaseModel):
    """数据字典 §6：未知叶子为 null，不用最佳值填补。"""

    days: int | None
    opened_return_allowed: bool | None
    return_shipping_fee_cents: int | None
    conditions: str | None
    exceptions: list | None


class Warranty(BaseModel):
    months: int | None
    provider: str | None
    conditions: str | None


class OfferView(BaseModel):
    offer: dict
    merchant: dict
    #: 实际生成方案的模式；无可用模式为 null（契约 §2）
    mode_id: str | None
    #: 未知总价为 null，即使返回原始价也不假装可购
    accessory_total_cents: int | None
    landed_price_cents: int | None
    delivery_window: DeliveryWindow
    effective_return_policy: ReturnPolicy
    effective_warranty: Warranty
    policy_origin: str
    warranty_origin: str
    #: 传统口径：默认地区/模式下有货、可定价、未过期且完整方案
    eligible: bool
    eligibility_reasons: list[str]
    field_sources: dict[str, Source]
    derivation: dict[str, Derivation]
