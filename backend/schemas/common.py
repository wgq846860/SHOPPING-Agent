"""api.v0.1 共用输出结构（S1.3）。

对应 docs/api_contract.md §1–§2：meta 固定六键、Source/Derivation、
统一错误信封（error + meta）。实体完整内容仍以数据库 payload 为准，
本模块只约束派生结构与信封的形状。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    """数据字典 §1 的来源记录。字段路径键由调用方放在 field_sources 映射里。"""

    source_type: str
    source_ref: str | None
    observed_at: str
    method: str | None
    missing_reason: str | None


class Derivation(BaseModel):
    """数据字典 §8 的派生记录：稳定规则代码 + 可追溯输入引用。"""

    rule: str
    input_refs: list[str]


class Meta(BaseModel):
    """响应元信息。data_version 在数据库不可用时为 null（键仍存在）。"""

    api_version: str
    schema_version: str
    data_version: str | None
    request_id: str
    generated_at: str
    simulated: bool


class ErrorDetail(BaseModel):
    field: str
    reason: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    retryable: bool


class ErrorEnvelope(BaseModel):
    error: ErrorBody
    meta: Meta


# -- 常用枚举取值（api.v0.1 / commerce.v0.1 固定词汇） -----------------------

#: GET /api/products 的 sort 取值（契约 §3）
SORT_VALUES = ("relevance", "price_asc", "price_desc", "rating_desc")

#: Product.sub_category 枚举（数据字典 §3）
SUB_CATEGORIES = (
    "over_ear_headphones",
    "on_ear_headphones",
    "true_wireless_earbuds",
    "wireless_earbuds",
    "gaming_headsets",
    "wired_headphones",
)

#: 本版只支持的目的地（契约 §3 destination_region 白名单）
ALLOWED_DESTINATION_REGIONS = ("CN-SH",)

#: availability 派生枚举（数据字典 §8 availability.v1）
AVAILABILITY_IN_STOCK = "in_stock"
AVAILABILITY_OUT_OF_STOCK = "out_of_stock"
AVAILABILITY_UNKNOWN = "unknown"

#: policy_origin / warranty_origin（数据字典 §6）
POLICY_ORIGIN_MERCHANT = "merchant_default"
POLICY_ORIGIN_OFFER = "offer_override"

#: 传统 OfferView.eligibility_reasons 稳定原因码（契约 §2/§5：
#: `out_of_stock` 为契约明示，其余为 S1.3 实现补充并在台账记录）
REASON_OUT_OF_STOCK = "out_of_stock"
REASON_STOCK_UNKNOWN = "stock_unknown"
REASON_PRICE_UNKNOWN = "price_unknown"
REASON_EXPIRED = "expired"
REASON_REGION_MISMATCH = "region_mismatch"
REASON_MODE_UNAVAILABLE = "mode_unavailable"
REASON_PLAN_INCOMPLETE = "plan_incomplete"
REASON_ACCESSORY_OUT_OF_STOCK = "accessory_out_of_stock"


# 整句精确匹配（沿用数据字典 §285 Compatibility.conditions 先例）；
# 语料以外的任何文本、以及"政策/能力为 true 但 conditions 为 null"，一律按未知排除，
# 不按关键词放行、不一律排除。扩展语料前须先补核验规则与回归（见 data_dictionary §286）。

#: Offer.delivery.conditions 在 commitment_type=guaranteed 下的唯一可核验条款：
#: 请求时间（Asia/Shanghai）须为周一至周五且早于 18:00（工作日不计法定节假日，仿真简化）；
#: 不满足即该报价不满足 guaranteed 承诺类型与硬截止，排除、不顺延、不重算窗口。
GUARANTEED_NEXTDAY_CONDITIONS = "模拟次日达服务条款（工作日18点前下单）"
#: Offer.delivery.conditions 在 estimated（或承诺类型未知）承诺下的说明性文本，不影响资格。
ESTIMATED_DESCRIPTIVE_CONDITIONS = frozenset(
    {"模拟自营配送", "模拟第三方物流", "模拟华东仓发货", "模拟经济快递"}
)
#: opened_return_allowed=true 的退货政策条件中属"退货时买家义务"的整句（放行）。
OPENED_RETURN_BUYER_OBLIGATION_CONDITIONS = frozenset(
    {"包装和配件齐全", "配件齐全且包装完好", "包装配件齐全，支持开封试戴"}
)
#: SweatStatement.allowed=true 的可核验厂商声明整句（通过普通防汗要求）。
SWEAT_ALLOWED_CONDITIONS = frozenset(
    {"模拟厂商声明：运动汗液适用，训练后需擦拭晾干",
     "模拟厂商声明：室内训练防汗适用，训练后需清洁"}
)
#: 防汗声明中带使用场景局限（仅室内训练）的整句：通过硬要求，但解释层须如实列为不足。
SWEAT_INDOOR_LIMITATION_CONDITIONS = "模拟厂商声明：室内训练防汗适用，训练后需清洁"
