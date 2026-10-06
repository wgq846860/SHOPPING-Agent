"""commerce.v0.1 共享模式定义（S1.1/S1.2）。

生成器（generate_demo_data.py）与校验器（validate_demo_data.py）共用本模块，
保证"生成什么字段"与"校验什么字段"来自同一份事实：

- 枚举词汇表：docs/data_dictionary.md 各节的取值集合；
- 字段白名单：每个实体/嵌套结构的键集合与"必填非null"标记
  （字典约定"可选字段不随意省略"，因此校验时要求键集合完全一致）；
- 来源覆盖 walker：按字典第1节计算"原始叶子"路径集合——
  主外键、identity_key、data_version、updated_at、field_sources 自身不需要来源；
  嵌套结构里的标识（mode_id/battery_spec_id 等）是结构ID，同样不需要；
  标量数组（codec_support 等）整体视为一个叶子；
- 身份键规范化：identity_key = JSON数组字符串(brand, model, variant,
  hardware_revision, market_region)，NFC + 去首尾空白 + 统一小写。

本模块不含任何 I/O，方便 tests/ 直接复用。
"""

from __future__ import annotations

import json
import math
import unicodedata
from typing import Any, Iterator

SCHEMA_VERSION = "commerce.v0.1"

PROFILES = ("small", "integration", "demo")
PROFILE_DATA_VERSIONS = {
    "small": "small-v1",
    "integration": "integration-v1",
    "demo": "demo-v1",
}

# ---- 枚举词汇表（docs/data_dictionary.md） ----------------------------------

SUB_CATEGORIES = {
    "over_ear_headphones",
    "on_ear_headphones",
    "true_wireless_earbuds",
    "wireless_earbuds",
    "gaming_headsets",
    "wired_headphones",
}
FORM_FACTORS = {"over_ear", "on_ear", "in_ear", "open_ear"}
FIT_STYLES = {"headband", "ear_tip", "ear_hook", "ear_clip", "neckband"}
ACOUSTIC_DESIGNS = {"open_back", "closed_back", "semi_open"}
TRANSPORTS = {
    "wired_3_5mm",
    "usb_audio",
    "bluetooth",
    "wireless_dongle",
    "optical_transmitter",
}
BATTERY_BASIS = {"single_charge", "with_case"}
ANC_STATES = {"on", "off", "not_applicable", "unknown"}
WEIGHT_BASIS = {"single_earbud", "pair", "headset", "with_case"}
BATTERY_APPLICABILITY = {"applicable", "not_applicable", "unknown"}
MERCHANT_TYPES = {"authorized", "independent", "marketplace"}
SOURCE_TYPES = {"simulated", "manufacturer", "merchant", "measurement", "unknown"}
COMMITMENT_TYPES = {"estimated", "guaranteed", "unknown"}
ITEM_CONDITIONS = {"new", "refurbished", "used"}
PROTECTION_COMPONENTS = {"earbuds", "headset", "case"}
MEASUREMENT_METRICS = {"latency_ms", "leakage_db", "frequency_response", "tone_profile"}
MEASUREMENT_UNITS = {"ms", "dB", "report"}
#: 本字典 allowed 的 missing_reason 取值（非空字符串即可，这里约束为稳定机器码）
MISSING_REASONS = {"not_collected", "not_applicable", "not_disclosed"}

ENUMS: dict[str, frozenset[str]] = {
    "SUB_CATEGORIES": frozenset(SUB_CATEGORIES),
    "FORM_FACTORS": frozenset(FORM_FACTORS),
    "FIT_STYLES": frozenset(FIT_STYLES),
    "ACOUSTIC_DESIGNS": frozenset(ACOUSTIC_DESIGNS),
    "TRANSPORTS": frozenset(TRANSPORTS),
    "BATTERY_BASIS": frozenset(BATTERY_BASIS),
    "ANC_STATES": frozenset(ANC_STATES),
    "WEIGHT_BASIS": frozenset(WEIGHT_BASIS),
    "BATTERY_APPLICABILITY": frozenset(BATTERY_APPLICABILITY),
    "MERCHANT_TYPES": frozenset(MERCHANT_TYPES),
    "SOURCE_TYPES": frozenset(SOURCE_TYPES),
    "COMMITMENT_TYPES": frozenset(COMMITMENT_TYPES),
    "ITEM_CONDITIONS": frozenset(ITEM_CONDITIONS),
    "PROTECTION_COMPONENTS": frozenset(PROTECTION_COMPONENTS),
    "MEASUREMENT_METRICS": frozenset(MEASUREMENT_METRICS),
    "MEASUREMENT_UNITS": frozenset(MEASUREMENT_UNITS),
    "CURRENCIES": frozenset({"CNY"}),
    "CATEGORIES": frozenset({"headphones"}),
}

# ---- 字段白名单 --------------------------------------------------------------
# 每个字段: (kind, required_non_null)。kind:
#   str / bool / int / num / enum:<词表> / array_str /
#   struct:<名> / struct_list:<名> / sources
# required=True 表示键必须存在且值不能为 None；False 表示键必须存在但允许 None。

STRUCT_SPECS: dict[str, dict[str, tuple[str, bool]]] = {
    "ReturnPolicy": {
        "days": ("int", False),
        "opened_return_allowed": ("bool", False),
        "return_shipping_fee_cents": ("int", False),
        "conditions": ("str", False),
        "exceptions": ("array_str", False),
    },
    "Warranty": {
        "months": ("int", False),
        "provider": ("str", False),
        "conditions": ("str", False),
    },
    "ConnectionMode": {
        "mode_id": ("str", True),
        "transport": ("enum:TRANSPORTS", True),
        "audio_connector": ("str", False),
        "microphone_connector": ("str", False),
        "playback_supported": ("bool", False),
        "microphone_supported": ("bool", False),
        "active_noise_cancelling": ("bool", False),
        "requires_power": ("bool", False),
        "required_accessory_codes": ("array_str", False),
    },
    "Compatibility": {
        "compatibility_id": ("str", True),
        "device_key": ("str", True),
        "platform": ("str", True),
        "os_version": ("str", False),
        "source_output": ("str", False),
        "mode_id": ("str", True),
        "playback_supported": ("bool", False),
        "microphone_supported": ("bool", False),
        "multipoint_supported": ("bool", False),
        "firmware_version": ("str", False),
        "conditions": ("str", False),
    },
    "BatterySpec": {
        "battery_spec_id": ("str", True),
        "hours": ("num", False),
        "basis": ("enum:BATTERY_BASIS", True),
        "anc_state": ("enum:ANC_STATES", True),
        "mode_id": ("str", False),
        "test_conditions": ("str", False),
    },
    "WeightSpec": {
        "weight_spec_id": ("str", True),
        "grams": ("num", False),
        "basis": ("enum:WEIGHT_BASIS", True),
        "conditions": ("str", False),
    },
    "FastCharge": {
        "minutes": ("int", True),
        "playback_hours": ("num", True),
        "anc_state": ("enum:ANC_STATES", True),
        "conditions": ("str", False),
    },
    "Dimensions": {
        "length": ("num", False),
        "width": ("num", False),
        "height": ("num", False),
    },
    "Protection": {
        "component": ("enum:PROTECTION_COMPONENTS", True),
        "rating": ("str", False),
        "conditions": ("str", False),
    },
    "SweatStatement": {
        "allowed": ("bool", False),
        "conditions": ("str", False),
    },
    "Accessory": {
        "accessory_code": ("str", True),
        "included": ("bool", True),
        "price_cents": ("int", False),
        "stock": ("int", False),
        "delivery_in_same_package": ("bool", False),
    },
    "Delivery": {
        "destination_region": ("str", False),
        "min_days": ("int", False),
        "max_days": ("int", False),
        "commitment_type": ("enum:COMMITMENT_TYPES", True),
        "conditions": ("str", False),
    },
    "Measurement": {
        "measurement_id": ("str", True),
        "metric": ("enum:MEASUREMENT_METRICS", True),
        "value": ("any", False),
        "unit": ("enum:MEASUREMENT_UNITS", True),
        "mode_id": ("str", False),
        "conditions": ("str", True),
        "comparison_group": ("str", True),
    },
}

ENTITY_SPECS: dict[str, dict[str, tuple[str, bool]]] = {
    "merchant": {
        "merchant_id": ("str", True),
        "merchant_name": ("str", True),
        "merchant_type": ("enum:MERCHANT_TYPES", False),
        "region": ("str", False),
        "merchant_rating": ("num", False),
        "service_score": ("num", False),
        "shipping_score": ("num", False),
        "default_return_policy": ("struct:ReturnPolicy", True),
        "default_warranty": ("struct:Warranty", True),
        "data_version": ("str", True),
        "updated_at": ("str", True),
        "field_sources": ("sources", True),
    },
    "product": {
        "product_id": ("str", True),
        "identity_key": ("str", True),
        "brand": ("str", True),
        "model": ("str", True),
        "product_name": ("str", True),
        "variant": ("str", True),
        "hardware_revision": ("str", True),
        "market_region": ("str", True),
        "category": ("enum:CATEGORIES", True),
        "sub_category": ("enum:SUB_CATEGORIES", True),
        "form_factor": ("enum:FORM_FACTORS", False),
        "fit_style": ("enum:FIT_STYLES", False),
        "open_ear": ("bool", False),
        "acoustic_design": ("enum:ACOUSTIC_DESIGNS", False),
        "wireless": ("bool", False),
        "connection_modes": ("struct_list:ConnectionMode", False),
        "bluetooth_version": ("str", False),
        "active_noise_cancelling": ("bool", False),
        "microphone": ("bool", False),
        "microphone_noise_reduction": ("bool", False),
        "mute_control": ("bool", False),
        "multipoint": ("bool", False),
        "max_active_devices": ("int", False),
        "compatibility": ("struct_list:Compatibility", False),
        "battery_applicability": ("enum:BATTERY_APPLICABILITY", True),
        "battery_specs": ("struct_list:BatterySpec", False),
        "charging_methods": ("array_str", False),
        "fast_charge": ("struct:FastCharge", False),
        "weight_specs": ("struct_list:WeightSpec", False),
        "foldable": ("bool", False),
        "ear_tip_sizes": ("array_str", False),
        "earbud_dimensions_mm": ("struct:Dimensions", False),
        "water_resistance": ("struct_list:Protection", False),
        "sweat_use_statement": ("struct:SweatStatement", False),
        "codec_support": ("array_str", False),
        "description": ("str", False),
        "image_path": ("str", False),
        "data_version": ("str", True),
        "updated_at": ("str", True),
        "field_sources": ("sources", True),
    },
    "product_metrics": {
        "product_id": ("str", True),
        "rating": ("num", False),
        "review_count": ("int", False),
        "return_rate": ("num", False),
        "popularity_score": ("num", False),
        "quality_score": ("num", False),
        "comfort_score": ("num", False),
        "anc_score": ("num", False),
        "sound_quality_score": ("num", False),
        "music_score": ("num", False),
        "call_score": ("num", False),
        "commute_score": ("num", False),
        "gaming_score": ("num", False),
        "portability_score": ("num", False),
        "fit_stability_score": ("num", False),
        "measurements": ("struct_list:Measurement", False),
        "data_version": ("str", True),
        "updated_at": ("str", True),
        "field_sources": ("sources", True),
    },
    "offer": {
        "offer_id": ("str", True),
        "product_id": ("str", True),
        "merchant_id": ("str", True),
        "currency": ("enum:CURRENCIES", True),
        "price_cents": ("int", True),
        "original_price_cents": ("int", False),
        "shipping_fee_cents": ("int", False),
        "promotion": ("str", False),
        "instant_discount_cents": ("int", True),
        "commission_rate": ("num", False),
        "accessories": ("struct_list:Accessory", False),
        "stock": ("int", False),
        "item_condition": ("enum:ITEM_CONDITIONS", False),
        "delivery": ("struct:Delivery", True),
        "return_policy_override": ("struct:ReturnPolicy", False),
        "warranty_override": ("struct:Warranty", False),
        "gift_return_conditions": ("str", False),
        "valid_until": ("str", False),
        "data_version": ("str", True),
        "updated_at": ("str", True),
        "field_sources": ("sources", True),
    },
}

# ---- 数值范围（lo, hi, exclusive_lo）；None 表示无界 -------------------------

TOP_RANGES: dict[tuple[str, str], tuple[float | None, float | None, bool]] = {
    ("merchant", "merchant_rating"): (0, 5, False),
    ("merchant", "service_score"): (0, 100, False),
    ("merchant", "shipping_score"): (0, 100, False),
    ("product", "max_active_devices"): (1, None, False),
    ("product_metrics", "rating"): (0, 5, False),
    ("product_metrics", "review_count"): (0, None, False),
    ("product_metrics", "return_rate"): (0, 1, False),
    ("product_metrics", "popularity_score"): (0, 100, False),
    ("product_metrics", "quality_score"): (0, 100, False),
    ("product_metrics", "comfort_score"): (0, 100, False),
    ("product_metrics", "anc_score"): (0, 100, False),
    ("product_metrics", "sound_quality_score"): (0, 100, False),
    ("product_metrics", "music_score"): (0, 100, False),
    ("product_metrics", "call_score"): (0, 100, False),
    ("product_metrics", "commute_score"): (0, 100, False),
    ("product_metrics", "gaming_score"): (0, 100, False),
    ("product_metrics", "portability_score"): (0, 100, False),
    ("product_metrics", "fit_stability_score"): (0, 100, False),
    ("offer", "price_cents"): (0, None, False),
    ("offer", "original_price_cents"): (0, None, False),
    ("offer", "shipping_fee_cents"): (0, None, False),
    ("offer", "instant_discount_cents"): (0, None, False),
    ("offer", "commission_rate"): (0, 1, False),
    ("offer", "stock"): (0, None, False),
}

STRUCT_RANGES: dict[str, tuple[float | None, float | None, bool]] = {
    "ReturnPolicy.days": (0, None, False),
    "ReturnPolicy.return_shipping_fee_cents": (0, None, False),
    "Warranty.months": (0, None, False),
    "ConnectionMode.max_active_devices": (1, None, False),
    "BatterySpec.hours": (0, None, True),
    "WeightSpec.grams": (0, None, True),
    "FastCharge.minutes": (0, None, True),
    "FastCharge.playback_hours": (0, None, True),
    "Dimensions.length": (0, None, True),
    "Dimensions.width": (0, None, True),
    "Dimensions.height": (0, None, True),
    "Accessory.price_cents": (0, None, False),
    "Accessory.stock": (0, None, False),
    "Delivery.min_days": (0, None, False),
    "Delivery.max_days": (0, None, False),
}

#: 值为对象数组时，数组元素内的这些键是结构ID，不需要来源覆盖
INNER_ID_FIELDS = frozenset(
    {
        "mode_id",
        "battery_spec_id",
        "weight_spec_id",
        "compatibility_id",
        "measurement_id",
        "accessory_code",
    }
)

#: 值为字符串数组时，整个数组视为一个叶子（而不是逐元素）
SCALAR_ARRAY_FIELDS = frozenset(
    {
        "required_accessory_codes",
        "codec_support",
        "charging_methods",
        "ear_tip_sizes",
        "exceptions",
    }
)

#: 实体主键 + 系统字段：不参与来源覆盖
SYSTEM_FIELDS = frozenset({"data_version", "updated_at", "field_sources", "identity_key"})
PK_FIELDS = {
    "merchant": "merchant_id",
    "product": "product_id",
    "product_metrics": "product_id",
    "offer": "offer_id",
}

ENTITY_TYPES = ("merchant", "product", "product_metrics", "offer")


# ---- 身份键 ------------------------------------------------------------------


def normalize_code(value: str) -> str:
    """身份键成分规范化：NFC + 去首尾空白 + 小写（commerce.v0.1 第7节）。"""
    return unicodedata.normalize("NFC", value).strip().lower()


def canonical_identity_key(
    brand: str, model: str, variant: str, hardware_revision: str, market_region: str
) -> str:
    parts = [
        normalize_code(brand),
        normalize_code(model),
        normalize_code(variant),
        normalize_code(hardware_revision),
        normalize_code(market_region),
    ]
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


# ---- 来源覆盖 walker ---------------------------------------------------------


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def iter_original_leaves(entity: dict, entity_type: str) -> Iterator[tuple[str, Any]]:
    """按字典第1节产出 (路径, 值)。

    - 主外键 / identity_key / data_version / updated_at / field_sources 不产出；
    - 结构ID（mode_id 等）不产出；
    - 标量数组整体一个叶子；对象数组逐元素下钻（path[i].field）；
    - 其余标量（含 None）都是叶子。
    """
    skip = SYSTEM_FIELDS | {PK_FIELDS[entity_type]}

    def walk(node: Any, path: str) -> Iterator[tuple[str, Any]]:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else key
                if not path and key in skip:
                    continue
                if path and key in INNER_ID_FIELDS:
                    continue
                if isinstance(value, dict):
                    yield from walk(value, child_path)
                elif isinstance(value, list):
                    if not value or all(_is_scalar(item) for item in value):
                        yield (child_path, value)
                    else:
                        for i, item in enumerate(value):
                            yield from walk(item, f"{child_path}[{i}]")
                else:
                    yield (child_path, value)

    yield from walk(entity, "")


def source_ref_for(entity_type: str, entity: dict, data_version: str) -> str:
    entity_id = entity[PK_FIELDS[entity_type]]
    ref = f"demo://{data_version}/{entity_id}"
    if entity_type == "product_metrics":
        ref += "/metrics"
    return ref


def build_field_sources(
    entity: dict,
    entity_type: str,
    *,
    data_version: str,
    observed_at: str,
    method: str,
    missing_reason_overrides: dict[str, str] | None = None,
) -> dict[str, dict]:
    """为生成数据填充 field_sources（模拟来源）。

    默认规则：非null叶子 missing_reason=None；null叶子 not_collected；
    覆盖表按叶子路径精确指定 not_applicable / not_disclosed。
    """
    overrides = missing_reason_overrides or {}
    ref = source_ref_for(entity_type, entity, data_version)
    sources: dict[str, dict] = {}
    for path, value in iter_original_leaves(entity, entity_type):
        if path in overrides:
            reason = overrides[path]
        elif value is None:
            reason = "not_collected"
        else:
            reason = None
        sources[path] = {
            "source_type": "simulated",
            "source_ref": ref,
            "observed_at": observed_at,
            "method": method,
            "missing_reason": reason,
        }
    return sources


# ---- 校验辅助 ----------------------------------------------------------------


def _check_value(kind: str, value: Any, where: str, errors: list[str]) -> None:
    if value is None:
        return
    if kind == "any":
        if isinstance(value, bool) or not _is_scalar(value):
            errors.append(f"{where}: value 必须是标量")
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{where}: 数值必须有限")
        return
    if kind == "str":
        if not isinstance(value, str):
            errors.append(f"{where}: 必须是字符串")
        elif value == "":
            errors.append(f"{where}: 空字符串禁止（未知请用 null）")
        return
    if kind == "bool":
        if not isinstance(value, bool):
            errors.append(f"{where}: 必须是布尔值")
        return
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where}: 必须是整数（不接受浮点/布尔）")
        return
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{where}: 必须是数字")
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{where}: 数值必须有限")
        return
    if kind.startswith("enum:"):
        vocab_name = kind.split(":", 1)[1]
        vocab = ENUMS[vocab_name]
        if not isinstance(value, str):
            # R1（复验 2026-09-30）：枚举先查字符串类型再查集合——
            # list/dict 等不可哈希值直接 `in` 词表 set 会抛 TypeError
            errors.append(f"{where}: 必须是字符串（枚举 {vocab_name}）")
        elif value not in vocab:
            errors.append(f"{where}: 取值 {value!r} 不在词表 {sorted(vocab)}")
        return
    if kind == "array_str":
        if not isinstance(value, list):
            errors.append(f"{where}: 必须是数组")
        elif not all(isinstance(item, str) and item != "" for item in value):
            errors.append(f"{where}: 数组元素必须是非空字符串")
        return


def check_struct(
    struct_name: str, value: Any, where: str, errors: list[str]
) -> None:
    if value is None:
        return
    spec = STRUCT_SPECS[struct_name]
    if not isinstance(value, dict):
        errors.append(f"{where}: 必须是对象（{struct_name}）")
        return
    extra = set(value) - set(spec)
    missing = set(spec) - set(value)
    if extra:
        errors.append(f"{where}: 出现白名单之外的字段 {sorted(extra)}")
    if missing:
        errors.append(f"{where}: 缺少字段 {sorted(missing)}")
    for key, (kind, required) in spec.items():
        if key not in value:
            continue
        item = value[key]
        child = f"{where}.{key}"
        if required and item is None:
            errors.append(f"{child}: 必填，不能为 null")
            continue
        _check_value(kind, item, child, errors)
        if struct_name == "Measurement" and key == "value" and item is not None:
            metric = value.get("metric")
            if metric in ("latency_ms", "leakage_db"):
                if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
                    errors.append(f"{child}: {metric} 的 value 必须是非负数字")
            elif metric in ("frequency_response", "tone_profile") and not isinstance(item, str):
                errors.append(f"{child}: {metric} 的 value 必须是证据摘要字符串")
        if f"{struct_name}.{key}" in STRUCT_RANGES and isinstance(item, (int, float)) and not isinstance(item, bool):
            lo, hi, excl_lo = STRUCT_RANGES[f"{struct_name}.{key}"]
            if (lo is not None and item <= lo) if excl_lo else (lo is not None and item < lo):
                errors.append(f"{child}: {item} 低于下界 {lo}" + ("（开区间）" if excl_lo else ""))
            if hi is not None and item > hi:
                errors.append(f"{child}: {item} 超过上界 {hi}")


def check_entity_shape(entity: dict, entity_type: str, errors: list[str]) -> None:
    """键集合与逐字段类型/枚举/范围检查（不含跨实体检查）。"""
    spec = ENTITY_SPECS[entity_type]
    where = entity.get(PK_FIELDS[entity_type], f"<{entity_type}>")
    extra = set(entity) - set(spec)
    missing = set(spec) - set(entity)
    if entity_type == "offer":
        # S2.2 extension: older commerce.v0.1 offers may omit commission_rate.
        missing.discard("commission_rate")
    if extra:
        errors.append(f"{where}: 出现白名单之外的字段 {sorted(extra)}")
    if missing:
        errors.append(f"{where}: 缺少字段 {sorted(missing)}")
    for key, (kind, required) in spec.items():
        if key not in entity:
            continue
        value = entity[key]
        child = f"{where}.{key}"
        if required and value is None:
            errors.append(f"{child}: 必填，不能为 null")
            continue
        if kind == "sources":
            continue  # 来源覆盖在 validate_field_sources 里单独核对
        if kind.startswith("struct:"):
            check_struct(kind.split(":", 1)[1], value, child, errors)
        elif kind.startswith("struct_list:"):
            struct_name = kind.split(":", 1)[1]
            if value is None:
                continue
            if not isinstance(value, list):
                errors.append(f"{child}: 必须是数组")
                continue
            for i, item in enumerate(value):
                check_struct(struct_name, item, f"{child}[{i}]", errors)
        else:
            _check_value(kind, value, child, errors)
        if (entity_type, key) in TOP_RANGES and isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi, excl_lo = TOP_RANGES[(entity_type, key)]
            if (lo is not None and value <= lo) if excl_lo else (lo is not None and value < lo):
                errors.append(f"{child}: {value} 低于下界 {lo}" + ("（开区间）" if excl_lo else ""))
            if hi is not None and value > hi:
                errors.append(f"{child}: {value} 超过上界 {hi}")
