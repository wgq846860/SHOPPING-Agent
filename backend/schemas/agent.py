"""结构化购物意图（S2.1）：api.v0.1 §7.1 的受校验内部结构。

ShoppingIntent 由 S3.1 意图解析产出（或规则模式/测试直接构造），是
硬约束过滤（services/candidate_service）与排序（S2.2/S2.3）的唯一输入。
表内所有键固定、extra=forbid；补充新的条件类型先修订契约，不塞自由键。

口径要点（契约 §7.1 / 数据字典 §3、§8）：
- 未表达的硬条件为 null 而非 false；stock_required 首版固定 true；
- 数组条件非空（空集语义上应写 null）；多个 sub_categories/allowed 为 OR，
  排除优先；
- 布尔硬条件 true 要求确认有、false 要求确认无；未知不视为满足；
- preferences 为全部固定维度的 0–1 有限数（0=未表达），是重要度输入，
  不是归一化算法权重（归一化与中性处理由 S2.2 基线另行定义）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

#: ConnectionMode.transport 枚举（字典 §3.1）
TRANSPORTS = (
    "wired_3_5mm",
    "usb_audio",
    "bluetooth",
    "wireless_dongle",
    "optical_transmitter",
)

#: wireless=true 的模式证据（字典 §8：optical_transmitter 不自动视为无线）
WIRELESS_TRANSPORTS = ("bluetooth", "wireless_dongle")
WIRED_TRANSPORTS = ("wired_3_5mm", "usb_audio")

#: Product.form_factor 枚举（字典 §3）
FORM_FACTORS = ("over_ear", "on_ear", "in_ear", "open_ear")

#: BatterySpec.basis 枚举（字典 §3.2）
BATTERY_BASES = ("single_charge", "with_case")

#: BatteryRequirement.anc_state（"unknown" 不是合法需求值：无法对未知提硬要求）
BATTERY_ANC_STATES = ("on", "off", "not_applicable")

#: WeightSpec.basis 枚举（字典 §3.2）；不自行跨口径换算
WEIGHT_BASES = ("single_earbud", "pair", "headset", "with_case")

#: Offer.item_condition 枚举（字典 §5）
ITEM_CONDITIONS = ("new", "refurbished", "used")

#: Clarification.reason 枚举（契约 §7.1）
CLARIFICATION_REASONS = (
    "missing_device",
    "missing_basis",
    "missing_destination",
    "missing_deadline",
    "missing_commitment",
    "conflicting_constraints",
    "unsupported_requirement",
)

#: 本版数据只支持的目的地白名单（契约 §3/§7.1）
ALLOWED_DESTINATION_REGIONS = ("CN-SH",)

#: preferences 固定维度（契约 §7.1，顺序即契约顺序）
PREFERENCE_DIMENSIONS = (
    "price",
    "battery",
    "weight",
    "comfort",
    "anc",
    "sound_quality",
    "call",
    "gaming",
    "portability",
    "commute",
    "fit_stability",
    "low_leakage",
    "low_latency",
    "shipping",
    "merchant_service",
    "returns",
    "warranty",
    "wireless",
    "foldable",
    "fast_charge",
    "tone_match",
    "environment_awareness",
)

#: 结构上必然无线的 sub_category（wired 需求与之矛盾，用于冲突检测）
WIRELESS_SUB_CATEGORIES = ("true_wireless_earbuds", "wireless_earbuds")

#: 0–1 有限数（契约 §1 禁 NaN/Infinity）
Finite01 = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
#: 正有限数（续航小时/重量克等阈值）
PositiveFinite = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(_StrictModel):
    """预算：max_cents 未知为 null（不用 0 冒充）；basis 固定到手价。"""

    max_cents: int | None = Field(None, ge=0)
    currency: Literal["CNY"] = "CNY"
    basis: Literal["landed_price"] = "landed_price"


class BatteryRequirement(_StrictModel):
    """连续听音时长的硬阈值；只有用户给出硬阈值才由解析器设置。

    anc_state 不接受 "unknown"——对未知状态提硬要求无意义；
    mode_id 为 null 表示不限定模式（资格层把规格绑定到实际选中模式）。
    """

    min_hours: PositiveFinite
    basis: Literal["single_charge", "with_case"]
    anc_state: Literal["on", "off", "not_applicable"]
    mode_id: str | None = Field(None, min_length=1, max_length=64)
    conditions: str | None = None


class WeightRequirement(_StrictModel):
    """重量硬上限；basis 必须与 WeightSpec 完全一致，不跨口径换算。"""

    max_grams: PositiveFinite
    basis: Literal["single_earbud", "pair", "headset", "with_case"]


class DeviceRequirement(_StrictModel):
    """目标设备要求：与 Compatibility 按结构化字段匹配，不做字符串相似推断。"""

    device_key: str | None = None
    platform: str = Field(min_length=1, max_length=80)
    os_version: str | None = None
    source_output: str | None = None
    transport: Literal["wired_3_5mm", "usb_audio", "bluetooth", "wireless_dongle", "optical_transmitter"] | None = None
    playback_required: bool = False
    microphone_required: bool = False
    multipoint_required: bool = False
    firmware_version: str | None = None


class DeliveryRequirement(_StrictModel):
    """配送硬要求：截止日期为绝对日期；承诺口径 estimated 仅当用户明确接受。"""

    destination_region: Literal["CN-SH"] | None = None
    deadline_date: date | None = None
    commitment_type: Literal["guaranteed", "estimated"] | None = None
    conditions: str | None = None


class Clarification(_StrictModel):
    """澄清请求：field 为意图路径；空 clarifications = 无需澄清。"""

    field: str
    reason: Literal[
        "missing_device",
        "missing_basis",
        "missing_destination",
        "missing_deadline",
        "missing_commitment",
        "conflicting_constraints",
        "unsupported_requirement",
    ]
    question: str = Field(min_length=1)


class HardConstraints(_StrictModel):
    """全部硬条件；null=未表达，true 要求确认有，false 要求确认无。"""

    #: 契约 §7.1：首版固定 true，语言或模型不得关闭有货检查
    stock_required: Literal[True] = True
    wireless: bool | None = None
    active_noise_cancelling: bool | None = None
    microphone: bool | None = None
    multipoint: bool | None = None
    open_ear: bool | None = None
    requires_power: bool | None = None
    opened_return_allowed: bool | None = None
    sweat_use_allowed: bool | None = None
    sub_categories: list[Literal[
        "over_ear_headphones",
        "on_ear_headphones",
        "true_wireless_earbuds",
        "wireless_earbuds",
        "gaming_headsets",
        "wired_headphones",
    ]] | None = Field(None, min_length=1)
    allowed_form_factors: list[Literal["over_ear", "on_ear", "in_ear", "open_ear"]] | None = Field(None, min_length=1)
    excluded_form_factors: list[Literal["over_ear", "on_ear", "in_ear", "open_ear"]] | None = Field(None, min_length=1)
    ear_tip_sizes: list[str] | None = Field(None, min_length=1)
    min_active_devices: int | None = Field(None, ge=1)
    allowed_transports: list[Literal[
        "wired_3_5mm",
        "usb_audio",
        "bluetooth",
        "wireless_dongle",
        "optical_transmitter",
    ]] | None = Field(None, min_length=1)
    battery_requirement: BatteryRequirement | None = None
    weight_requirement: WeightRequirement | None = None
    device_requirements: list[DeviceRequirement] | None = Field(None, min_length=1)
    item_condition: Literal["new", "refurbished", "used"] | None = None
    delivery_requirement: DeliveryRequirement | None = None


class Preferences(_StrictModel):
    """固定 22 维偏好（0=未表达）。字段集合须与 PREFERENCE_DIMENSIONS 一致。"""

    price: Finite01 = 0.0
    battery: Finite01 = 0.0
    weight: Finite01 = 0.0
    comfort: Finite01 = 0.0
    anc: Finite01 = 0.0
    sound_quality: Finite01 = 0.0
    call: Finite01 = 0.0
    gaming: Finite01 = 0.0
    portability: Finite01 = 0.0
    commute: Finite01 = 0.0
    fit_stability: Finite01 = 0.0
    low_leakage: Finite01 = 0.0
    low_latency: Finite01 = 0.0
    shipping: Finite01 = 0.0
    merchant_service: Finite01 = 0.0
    returns: Finite01 = 0.0
    warranty: Finite01 = 0.0
    wireless: Finite01 = 0.0
    foldable: Finite01 = 0.0
    fast_charge: Finite01 = 0.0
    tone_match: Finite01 = 0.0
    environment_awareness: Finite01 = 0.0


class ShoppingIntent(_StrictModel):
    """自然语言 → 受校验结构化意图；语言只转换为条件，资格由数据库验证。"""

    category: Literal["headphones"] | None = None
    budget: Budget = Field(default_factory=Budget)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    preferences: Preferences = Field(default_factory=Preferences)
    clarifications: list[Clarification] = Field(default_factory=list)
