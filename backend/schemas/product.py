"""商品侧派生输出结构（S1.3）。

对应 docs/api_contract.md §2–§4：
- ProductSummary：列表/摘要返回的全部键（含 display_offer 与逐叶子 field_sources）；
- DisplayOffer：实际选中的报价与模式（到手价、配送窗、来源、派生）。

battery_specs / weight_specs 按契约"对应Product，保留spec_id、口径、条件"整组透传，
不在这里另造第二套规格结构。
"""

from __future__ import annotations

from pydantic import BaseModel

from backend.schemas.common import Derivation, Source


class DeliveryWindow(BaseModel):
    """配送窗（契约 §2）：观测时点与历史窗口一起返回，不宣称当前必达。"""

    earliest: str | None
    latest: str | None
    commitment_type: str
    destination_region: str | None
    observed_at: str


class DisplayOffer(BaseModel):
    offer_id: str
    merchant_id: str
    merchant_name: str
    mode_id: str
    currency: str
    landed_price_cents: int
    delivery_window: DeliveryWindow
    derivation: dict[str, Derivation]
    field_sources: dict[str, Source]


class ProductSummary(BaseModel):
    product_id: str
    brand: str
    product_name: str
    sub_category: str
    form_factor: str | None
    rating: float | None
    review_count: int | None
    wireless: bool | None
    active_noise_cancelling: bool | None
    battery_specs: list[dict] | None
    weight_specs: list[dict] | None
    image_path: str | None
    availability: str
    display_offer: DisplayOffer | None
    field_sources: dict[str, Source]
    derivation: dict[str, Derivation]
