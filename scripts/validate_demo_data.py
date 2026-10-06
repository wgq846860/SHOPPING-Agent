"""S1.2 数据校验器：在数据进入 SQLite 之前把commerce.v0.1 口径全部核对一遍。

两层校验：
- validate_payload_record(entity_type, payload, data_version)：
  单条记录——键白名单、类型/枚举/范围、来源覆盖、身份键、
  商品内引用（battery_specs.mode_id / compatibility.mode_id）、
  ANC 与口径一致性、报价金额规则。
- validate_data_dir(data_dir)：
  整目录——manifest 与文件包装一致、数量对账、ID 唯一、
  外键引用、ProductMetrics 一对一、(product, merchant) 报价唯一、
  每个商家至少一条报价、demo 档每商品 3–7 条报价、
  报价配件代码必须对应商品必要配件。

校验失败的输出是逐条错误字符串（带实体ID与字段路径），CLI 退出码 1。
本模块无第三方依赖，可直接被 tests/ 与 backend/database/db.py 复用。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 允许 `python scripts/validate_demo_data.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.commerce_schema import (
    ENTITY_SPECS,
    ENTITY_TYPES,
    MISSING_REASONS,
    PK_FIELDS,
    PROFILE_DATA_VERSIONS,
    PROFILES,
    SCHEMA_VERSION,
    SOURCE_TYPES,
    canonical_identity_key,
    check_entity_shape,
    iter_original_leaves,
)

ENTITY_FILES = {
    "merchant": "merchants.json",
    "product": "products.json",
    "product_metrics": "product_metrics.json",
    "offer": "offers.json",
}
MANIFEST_KEYS = {
    "schema_version", "data_version", "profile", "seed", "generated_at",
    "generator_version", "fixed_observed_at", "counts",
}
WRAPPER_KEYS = {"schema_version", "data_version", "profile", "items"}
SOURCE_KEYS = {"source_type", "source_ref", "observed_at", "method", "missing_reason"}
#: manifest.counts 的复数键 -> 内部单数实体名
COUNT_KEY_TO_ENTITY = {
    "merchants": "merchant",
    "products": "product",
    "offers": "offer",
    "product_metrics": "product_metrics",
}


# ---- 基础工具 ---------------------------------------------------------------


def _reject_constant(value: str):
    raise ValueError(f"JSON 常量 {value} 不允许（禁止 NaN/Infinity）")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _is_dt_with_tz(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---- 单记录校验 -------------------------------------------------------------


def _check_field_sources(entity: dict, entity_type: str, data_version: str,
                         entity_id: str, errors: list[str]) -> None:
    sources = entity.get("field_sources")
    if not isinstance(sources, dict):
        errors.append(f"{entity_id}: field_sources 必须是对象")
        return
    expected_paths = {path for path, _ in iter_original_leaves(entity, entity_type)}
    actual_paths = set(sources)
    for missing in sorted(expected_paths - actual_paths):
        errors.append(f"{entity_id}: 原始叶子缺少来源覆盖：{missing}")
    for extra in sorted(actual_paths - expected_paths):
        errors.append(f"{entity_id}: field_sources 出现未知路径：{extra}")

    prefix = f"demo://{data_version}/"
    delivery_observed: set[str] = set()
    for path, source in sources.items():
        where = f"{entity_id}.field_sources.{path}"
        if not isinstance(source, dict):
            errors.append(f"{where}: 必须是 Source 对象")
            continue
        if set(source) != SOURCE_KEYS:
            errors.append(f"{where}: Source 键集合必须为 {sorted(SOURCE_KEYS)}")
            continue
        if source["source_type"] != "simulated":
            errors.append(f"{where}: 本演示语料 source_type 必须为 simulated")
        ref = source["source_ref"]
        if entity_type == "product_metrics":
            ok = isinstance(ref, str) and ref.startswith(prefix) and ref.endswith("/metrics")
        else:
            ok = isinstance(ref, str) and ref.startswith(prefix)
        if not ok:
            errors.append(f"{where}: source_ref {ref!r} 不符合 {prefix}<ID> 格式")
        if not _is_dt_with_tz(source["observed_at"]):
            errors.append(f"{where}: observed_at 必须是带时区的 ISO8601")
        if not isinstance(source["method"], str) or source["method"] == "":
            errors.append(f"{where}: simulated 来源必须提供 method")
        reason = source["missing_reason"]
        leaf_value = dict(iter_original_leaves(entity, entity_type)).get(path)
        if leaf_value is None:
            if not isinstance(reason, str) or reason not in MISSING_REASONS:
                errors.append(f"{where}: null 叶子的 missing_reason 必须是 "
                              f"{sorted(MISSING_REASONS)} 之一")
        elif reason is not None:
            errors.append(f"{where}: 非null叶子的 missing_reason 必须为 null")
        if path.startswith("delivery.") and source["observed_at"] is not None:
            delivery_observed.add(str(source["observed_at"]))
    if len(delivery_observed) > 1:
        errors.append(f"{entity_id}: delivery 叶子的 observed_at 必须同源一致，"
                      f"实际出现 {len(delivery_observed)} 个不同值")


def _check_structural_id_uniqueness(items, id_field: str, label: str,
                                    pid: str, errors: list[str]) -> None:
    """F4：商品内结构 ID 必须唯一（字典：mode_id 等均为"商品内唯一"）。"""
    if not isinstance(items, list):
        return  # 形状错误已由 check_entity_shape 报告，不再解引用
    seen: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = item.get(id_field)
        if not isinstance(key, str):
            # F1：缺失/类型非法由形状检查报告，不放入集合（不可哈希会崩溃）
            continue
        if key in seen:
            errors.append(f"{pid}: {label} {key!r} 重复（商品内必须唯一）")
        seen.add(key)


def _check_battery_pair_consistency(p: dict, errors: list[str]) -> None:
    """F3/R2：仅对**同模式、同口径、同测试条件**（条件非空且完全一致）的记录
    执行 ANC 开/关续航成对比较——开启不得长于关闭。

    条件缺失（null/空）或条件不同的记录不可比，不参与配对，不跨条件取极值。
    """
    specs = p.get("battery_specs")
    if not isinstance(specs, list):
        return
    groups: dict[tuple, list[dict]] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        conditions = spec.get("test_conditions")
        mode_id = spec.get("mode_id")
        basis = spec.get("basis")
        if not isinstance(mode_id, str) or not isinstance(basis, str):
            continue  # F1：类型非法由形状检查报告，不作分组键（不可哈希会崩溃）
        if not isinstance(conditions, str) or conditions == "":
            continue  # 条件缺失或不可比时不假定相同
        key = (mode_id, basis, conditions)
        groups.setdefault(key, []).append(spec)
    for (mode_id, basis, conditions), group in groups.items():
        on_hours = [s.get("hours") for s in group if s.get("anc_state") == "on"
                    and isinstance(s.get("hours"), (int, float))
                    and not isinstance(s.get("hours"), bool)]
        off_hours = [s.get("hours") for s in group if s.get("anc_state") == "off"
                     and isinstance(s.get("hours"), (int, float))
                     and not isinstance(s.get("hours"), bool)]
        if on_hours and off_hours and max(on_hours) > min(off_hours):
            errors.append(
                f"{p.get('product_id', '<product>')}: 同模式 {mode_id!r} 同口径 "
                f"{basis!r} 同条件 {conditions!r} 下 ANC 开启续航（最长 {max(on_hours)}h）"
                f"不得长于关闭续航（最短 {min(off_hours)}h）")


def _check_product_consistency(p: dict, errors: list[str]) -> None:
    pid = p.get("product_id", "<product>")
    modes = p.get("connection_modes")
    mode_items = modes if isinstance(modes, list) else []
    _check_structural_id_uniqueness(modes, "mode_id", "connection_modes.mode_id",
                                    pid, errors)
    _check_structural_id_uniqueness(p.get("battery_specs"), "battery_spec_id",
                                    "battery_specs.battery_spec_id", pid, errors)
    _check_structural_id_uniqueness(p.get("weight_specs"), "weight_spec_id",
                                    "weight_specs.weight_spec_id", pid, errors)
    _check_structural_id_uniqueness(p.get("compatibility"), "compatibility_id",
                                    "compatibility.compatibility_id", pid, errors)

    # F1：身份键规范化只对五个成分均为字符串时计算（类型错误由形状检查报告）
    identity_parts = (p.get("brand"), p.get("model"), p.get("variant"),
                      p.get("hardware_revision"), p.get("market_region"))
    if all(isinstance(part, str) for part in identity_parts):
        expected = canonical_identity_key(*identity_parts)
        if p.get("identity_key") != expected:
            errors.append(f"{pid}: identity_key 与 (brand,model,variant,"
                          f"hardware_revision,market_region) 规范化结果不一致")
    mode_ids = {m.get("mode_id") for m in mode_items
                if isinstance(m, dict) and isinstance(m.get("mode_id"), str)}
    battery_specs = p.get("battery_specs")
    for spec in battery_specs if isinstance(battery_specs, list) else []:
        if isinstance(spec, dict) and isinstance(spec.get("mode_id"), str) \
                and spec["mode_id"] not in mode_ids:
            errors.append(f"{pid}: battery_spec {spec.get('battery_spec_id')} 的 "
                          f"mode_id {spec['mode_id']!r} 未在 connection_modes 中定义")
    for c in p.get("compatibility") if isinstance(p.get("compatibility"), list) else []:
        if isinstance(c, dict) and isinstance(c.get("mode_id"), str) \
                and c["mode_id"] not in mode_ids:
            errors.append(f"{pid}: compatibility {c.get('compatibility_id')} 的 "
                          f"mode_id {c.get('mode_id')!r} 未在 connection_modes 中定义")
    if p.get("battery_applicability") == "not_applicable" and p.get("battery_specs"):
        errors.append(f"{pid}: battery_applicability=not_applicable 时不得有续航数据")

    has_anc_true = any(m.get("active_noise_cancelling") is True for m in mode_items
                       if isinstance(m, dict))
    if p.get("active_noise_cancelling") is True and mode_items and not has_anc_true:
        errors.append(f"{pid}: 商品级 ANC=true 但没有任何模式支持 ANC")
    if p.get("active_noise_cancelling") is False and has_anc_true:
        errors.append(f"{pid}: 商品级 ANC=false 但存在 ANC=true 的模式")
    for m in mode_items:
        if not isinstance(m, dict):
            continue
        if (m.get("transport") == "wired_3_5mm" and m.get("requires_power") is False
                and m.get("active_noise_cancelling") is True):
            errors.append(f"{pid}: 无源 3.5mm 模式 {m.get('mode_id')!r} 不能有 ANC")
    if p.get("multipoint") is True:
        max_devices = p.get("max_active_devices")
        if not _is_int(max_devices) or max_devices < 2:
            errors.append(f"{pid}: multipoint=true 要求 max_active_devices ≥ 2")
    _check_battery_pair_consistency(p, errors)


def _str_members(value) -> set:
    """R1：仅收集 list 中的合法字符串成员；非 list 或成员类型非法由形状检查报告，
    不参与关联集合（不可哈希成员入 set 会抛 TypeError）。"""
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _check_metrics_consistency(m: dict, errors: list[str]) -> None:
    pid = m.get("product_id", "<metrics>")
    measurements = m.get("measurements")
    _check_structural_id_uniqueness(measurements, "measurement_id",
                                    "measurements.measurement_id", pid, errors)
    if m.get("review_count") == 0 and m.get("rating") is not None:
        errors.append(f"{pid}: review_count=0 时 rating 必须为 null")
    for meas in measurements if isinstance(measurements, list) else []:
        if not isinstance(meas, dict):
            continue
        mid = meas.get("measurement_id", "<measurement>")
        metric = meas.get("metric")
        if not isinstance(metric, str):
            continue  # R1：类型非法由形状检查报告；非字符串不可作字典键
        expected_unit = {"latency_ms": "ms", "leakage_db": "dB",
                         "frequency_response": "report", "tone_profile": "report"}.get(metric)
        if expected_unit and meas.get("unit") != expected_unit:
            errors.append(f"{pid}: measurement {mid} 的 unit 应为 {expected_unit}")


def _check_offer_consistency(o: dict, errors: list[str]) -> None:
    oid = o.get("offer_id", "<offer>")
    # F1：金额比较只对类型有效的值计算；缺失/类型错误由形状检查报告，
    # 否则 int 与 str 比较抛 TypeError、直接索引缺失键抛 KeyError。
    price = o.get("price_cents")
    original = o.get("original_price_cents")
    if _is_int(price) and _is_int(original) and original < price:
        errors.append(f"{oid}: original_price_cents 不得低于 price_cents")
    if _is_int(price) and _is_int(o.get("instant_discount_cents")) \
            and o["instant_discount_cents"] > price:
        errors.append(f"{oid}: instant_discount_cents 不得超过 price_cents")
    # N1（复审 2026-09-30）：非 null 有效期必须可解析且带时区——查询端
    # parse_iso 对非法/无时区值抛错，不能等入库后由 API 500 兜底；
    # null 保持"未给截止"原语义，合法已过期值不是格式错误。
    deadline = o.get("valid_until")
    if deadline is not None and not _is_dt_with_tz(deadline):
        errors.append(
            f"{oid}: valid_until 必须是带时区的 ISO8601（实际 {deadline!r}）")
    accessories = o.get("accessories")
    for acc in accessories if isinstance(accessories, list) else []:
        if not isinstance(acc, dict):
            continue
        code = acc.get("accessory_code", "<accessory>")
        if acc.get("included") is True and acc.get("price_cents") != 0:
            errors.append(f"{oid}: 已包含配件 {code} 的 price_cents 必须为 0")
        if acc.get("included") is False and not (_is_int(acc.get("price_cents"))
                                                 and acc["price_cents"] >= 0):
            errors.append(f"{oid}: 未包含配件 {code} 必须给出非负整数 price_cents")
    delivery = o.get("delivery")
    if not isinstance(delivery, dict):
        return  # None 或非对象：必填/形状错误已由 check_entity_shape 报告，不再解引用
    if (_is_int(delivery.get("min_days")) and _is_int(delivery.get("max_days"))
            and delivery["min_days"] > delivery["max_days"]):
        errors.append(f"{oid}: delivery.min_days 不得大于 max_days")
    # N2（复审 2026-09-30）：配送天数必须能在配送观测日期上安全计算——
    # 极大值会让查询端日期加法溢出（超出 date.max 或 timedelta 上限）。
    observed = _delivery_observed_at_for_validation(o)
    parsed = _parse_dt(observed) if isinstance(observed, str) else None
    if parsed is not None and parsed.tzinfo is not None:
        base = parsed.astimezone(timezone(timedelta(hours=8))).date()
        for label, days in (("min_days", delivery.get("min_days")),
                            ("max_days", delivery.get("max_days"))):
            if not _is_int(days):
                continue
            try:
                base + timedelta(days=days)
            except OverflowError:
                errors.append(
                    f"{oid}: delivery.{label}={days} 在观测日期 "
                    f"{base.isoformat()} 上无法计算配送窗（日期溢出）")


def _delivery_observed_at_for_validation(o: dict) -> str | None:
    """N2：取配送叶子来源的观测时点（叶子优先级与 derive._delivery_observed_at 一致）。"""
    fs = o.get("field_sources")
    if not isinstance(fs, dict):
        return None
    for key in ("delivery.max_days", "delivery.min_days",
                "delivery.destination_region", "delivery.commitment_type",
                "delivery.conditions"):
        source = fs.get(key)
        if isinstance(source, dict) and source.get("observed_at"):
            return source["observed_at"]
    return None


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _check_time_relations(entity: dict, entity_type: str, errors: list[str],
                          now: datetime | None) -> None:
    """F5/R1：observed_at 不得晚于参考时间（严格比较，无容差，等于可通过）；
    updated_at 不得早于任何来源观测时间、不得晚于参考时间。"""
    entity_id = entity.get(PK_FIELDS[entity_type], f"<{entity_type}>")
    reference = now if now is not None else datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    sources = entity.get("field_sources")
    observed: list[datetime] = []
    if isinstance(sources, dict):
        for path, source in sources.items():
            if not isinstance(source, dict):
                continue
            parsed = _parse_dt(source.get("observed_at"))
            if parsed is None:
                continue  # 格式问题已由来源检查报告
            if parsed.tzinfo is None:
                continue
            observed.append(parsed)
            if parsed > reference:
                errors.append(
                    f"{entity_id}.{path}: observed_at {source['observed_at']} 在参考时间"
                    f"之后（观测时间不得为未来）")
    updated = _parse_dt(entity.get("updated_at"))
    if updated is not None and updated.tzinfo is not None:
        if observed and updated < max(observed):
            errors.append(
                f"{entity_id}: updated_at {entity['updated_at']} 早于来源观测时间 "
                f"{max(observed).isoformat()}")
        if updated > reference:
            errors.append(f"{entity_id}: updated_at {entity['updated_at']} 在参考时间之后")


def validate_payload_record(entity_type: str, payload: dict,
                            data_version: str, *,
                            now: datetime | None = None) -> list[str]:
    """单记录校验；返回错误列表（空列表 = 通过）。

    now：可注入的参考时间（带时区）；默认取当前时刻。测试用它固定时钟。
    """
    errors: list[str] = []
    if entity_type not in ENTITY_SPECS:
        return [f"未知实体类型 {entity_type!r}"]
    if not isinstance(payload, dict):
        return [f"记录必须是对象，实际为 {type(payload).__name__}"]
    entity_id = payload.get(PK_FIELDS[entity_type], f"<{entity_type}>")
    if payload.get("data_version") != data_version:
        errors.append(f"{entity_id}: data_version 与批次不一致")
    if not _is_dt_with_tz(payload.get("updated_at")):
        errors.append(f"{entity_id}: updated_at 必须是带时区的 ISO8601")
    check_entity_shape(payload, entity_type, errors)
    if entity_type == "product":
        _check_product_consistency(payload, errors)
    elif entity_type == "product_metrics":
        _check_metrics_consistency(payload, errors)
    elif entity_type == "offer":
        _check_offer_consistency(payload, errors)
    if isinstance(payload.get("field_sources"), dict):
        _check_field_sources(payload, entity_type, data_version, entity_id, errors)
    _check_time_relations(payload, entity_type, errors, now)
    return errors


# ---- 整目录校验 -------------------------------------------------------------


def validate_data_dir(data_dir: Path | str, *,
                      now: datetime | None = None) -> list[str]:
    """整目录校验。now：可注入参考时间（F5），默认取当前时刻。"""
    data_dir = Path(data_dir)
    errors: list[str] = []
    if not data_dir.is_dir():
        return [f"数据目录不存在：{data_dir}"]

    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        return [f"缺少 manifest.json：{data_dir}"]
    try:
        manifest = load_json(manifest_path)
    except (ValueError, OSError) as exc:
        return [f"manifest.json 解析失败：{exc}"]
    if not isinstance(manifest, dict):
        # F1：根包装先查类型，不对其做 set()/索引等依赖合法类型的操作
        return [f"manifest: 必须是对象，实际为 {type(manifest).__name__}"]
    if set(manifest) != MANIFEST_KEYS:
        errors.append(f"manifest: 键集合必须为 {sorted(MANIFEST_KEYS)}")
        return errors
    if manifest["schema_version"] != SCHEMA_VERSION:
        errors.append(f"manifest: schema_version 必须为 {SCHEMA_VERSION}")
    if manifest["profile"] not in PROFILES:
        errors.append(f"manifest: profile {manifest['profile']!r} 不合法")
    elif manifest["data_version"] != PROFILE_DATA_VERSIONS[manifest["profile"]]:
        errors.append(f"manifest: profile={manifest['profile']} 的 data_version 应为 "
                      f"{PROFILE_DATA_VERSIONS[manifest['profile']]}")
    if not _is_int(manifest["seed"]):
        errors.append("manifest: seed 必须是整数")
    if not _is_dt_with_tz(manifest["generated_at"]):
        errors.append("manifest: generated_at 必须是带时区的 ISO8601")
    if not _is_dt_with_tz(manifest["fixed_observed_at"]):
        errors.append("manifest: fixed_observed_at 必须是带时区的 ISO8601")

    entities: dict[str, list[dict]] = {}
    for entity_type, filename in ENTITY_FILES.items():
        path = data_dir / filename
        if not path.is_file():
            errors.append(f"缺少 {filename}")
            entities[entity_type] = []
            continue
        try:
            wrapper = load_json(path)
        except (ValueError, OSError) as exc:
            errors.append(f"{filename} 解析失败：{exc}")
            entities[entity_type] = []
            continue
        if not isinstance(wrapper, dict):
            # F1：包装对象非 dict（如 null）→ 受控错误，不做 set()/索引
            errors.append(
                f"{filename}: 包装必须是对象，实际为 {type(wrapper).__name__}")
            entities[entity_type] = []
            continue
        if set(wrapper) != WRAPPER_KEYS:
            errors.append(f"{filename}: 包装键集合必须为 {sorted(WRAPPER_KEYS)}")
            entities[entity_type] = []
            continue
        if wrapper["schema_version"] != manifest["schema_version"]:
            errors.append(f"{filename}: schema_version 与 manifest 不一致")
        if wrapper["data_version"] != manifest["data_version"]:
            errors.append(f"{filename}: data_version 与 manifest 不一致")
        if wrapper["profile"] != manifest["profile"]:
            errors.append(f"{filename}: profile 与 manifest 不一致")
        if not isinstance(wrapper["items"], list):
            errors.append(f"{filename}: items 必须是数组")
            entities[entity_type] = []
            continue
        entities[entity_type] = wrapper["items"]

    counts = manifest["counts"]
    if not isinstance(counts, dict):
        errors.append("manifest.counts 必须是对象")
    else:
        missing_keys = set(COUNT_KEY_TO_ENTITY) - set(counts)
        extra_keys = set(counts) - set(COUNT_KEY_TO_ENTITY)
        if missing_keys:
            errors.append(f"manifest.counts 缺少键 {sorted(missing_keys)}")
        if extra_keys:
            errors.append(f"manifest.counts 出现未知键 {sorted(extra_keys)}")
        for name, value in counts.items():
            if name in COUNT_KEY_TO_ENTITY and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0):
                errors.append(f"manifest.counts.{name} 必须是非负整数，实际 {value!r}")
        for name, entity_type in COUNT_KEY_TO_ENTITY.items():
            if not isinstance(counts, dict) or name not in counts:
                continue
            actual = len(entities.get(entity_type, []))
            if actual != counts[name]:
                errors.append(f"manifest.counts.{name}={counts[name]} 与实际 {actual} 不符")

    data_version = manifest["data_version"]
    for entity_type in ENTITY_TYPES:
        filename = ENTITY_FILES[entity_type]
        for index, payload in enumerate(entities[entity_type]):
            if not isinstance(payload, dict):
                # R3：非对象记录给出 文件[序号] 定位的受控错误，不进入解引用
                errors.append(
                    f"{filename}[{index}]: 记录必须是对象，"
                    f"实际为 {type(payload).__name__}")
                continue
            errors.extend(validate_payload_record(entity_type, payload, data_version,
                                                  now=now))

    # R3：无效记录（非对象）不参与跨实体检查；其错误已按 文件[序号] 报告
    for entity_type in ENTITY_TYPES:
        entities[entity_type] = [p for p in entities[entity_type]
                                 if isinstance(p, dict)]

    # ---- 跨实体检查 ---------------------------------------------------------
    # F1：ID 集合只收合法字符串（主键为必填 str）；类型非法由形状检查报告，
    # 不得把不可哈希的值放入集合或作字典键。
    merchant_ids = [m.get("merchant_id") for m in entities["merchant"]
                    if isinstance(m.get("merchant_id"), str)]
    product_ids = [p.get("product_id") for p in entities["product"]
                   if isinstance(p.get("product_id"), str)]
    for label, ids in (
            ("merchant", merchant_ids),
            ("product", product_ids),
            ("offer", [o.get("offer_id") for o in entities["offer"]
                       if isinstance(o.get("offer_id"), str)]),
            ("metrics", [m.get("product_id") for m in entities["product_metrics"]
                         if isinstance(m.get("product_id"), str)])):
        seen: set = set()
        for entity_id in ids:
            if entity_id in seen:
                errors.append(f"{entity_id}: {label} ID 重复")
            seen.add(entity_id)

    # identity_key 唯一（commerce.v0.1 第7节：同一身份只建一个 Product）
    seen_identity: set = set()
    for p in entities["product"]:
        key = p.get("identity_key")
        if not isinstance(key, str):
            continue  # F1：类型非法由形状检查报告，不入集合
        if key in seen_identity:
            errors.append(f"{p.get('product_id')}: identity_key 重复：{key!r}")
        seen_identity.add(key)

    merchant_set = set(merchant_ids)
    product_set = set(product_ids)
    metric_ids = {m.get("product_id") for m in entities["product_metrics"]
                  if isinstance(m.get("product_id"), str)}
    for pid in product_set - metric_ids:
        errors.append(f"{pid}: 缺少 ProductMetrics 记录（每个商品必须恰好一条）")
    for pid in metric_ids - product_set:
        errors.append(f"{pid}: ProductMetrics 没有对应 Product")

    offers = entities["offer"]
    seen_pairs: set[tuple] = set()
    offers_by_product: dict[str, list[dict]] = {}
    for off in offers:
        pid, mid = off.get("product_id"), off.get("merchant_id")
        if isinstance(pid, str) and pid not in product_set:
            errors.append(f"{off.get('offer_id')}: 引用了不存在的商品 {pid!r}")
        if isinstance(mid, str) and mid not in merchant_set:
            errors.append(f"{off.get('offer_id')}: 引用了不存在的商家 {mid!r}")
        if isinstance(pid, str) and isinstance(mid, str):
            pair = (pid, mid)
            if pair in seen_pairs:
                errors.append(f"{off.get('offer_id')}: 同批次 (product,merchant) 报价重复：{pair}")
            seen_pairs.add(pair)
            offers_by_product.setdefault(pid, []).append(off)

    offers_by_merchant: dict[str, int] = {}
    for off in offers:
        mid = off.get("merchant_id")
        if isinstance(mid, str):
            offers_by_merchant[mid] = offers_by_merchant.get(mid, 0) + 1
    for mid in merchant_set:
        if offers_by_merchant.get(mid, 0) == 0:
            errors.append(f"{mid}: 商家没有任何报价关联")

    product_modes: dict = {}
    product_accessory_codes: dict = {}
    for p in entities["product"]:
        pid = p.get("product_id")
        if not isinstance(pid, str):
            continue  # F1：非法 ID 由形状检查报告，不作字典键
        modes = p.get("connection_modes")
        product_modes[pid] = (
            {m.get("mode_id") for m in modes
             if isinstance(m, dict) and isinstance(m.get("mode_id"), str)}
            if isinstance(modes, list) else set())
        product_accessory_codes[pid] = (
            {code for m in modes if isinstance(m, dict)
             for code in _str_members(m.get("required_accessory_codes"))}
            if isinstance(modes, list) else set())

    for off in offers:
        pid = off.get("product_id")
        if not isinstance(pid, str):
            continue
        required_codes = product_accessory_codes.get(pid, set())
        for acc in off.get("accessories") if isinstance(off.get("accessories"), list) else []:
            if not isinstance(acc, dict):
                continue
            code = acc.get("accessory_code")
            if not isinstance(code, str):
                continue  # R1：类型非法由形状检查报告，不可作集合成员
            if code not in required_codes:
                errors.append(f"{off.get('offer_id')}: 配件 "
                              f"{code!r} 不是该商品的必要配件")

    # F4：测量的 mode_id 必须引用同商品已定义的连接模式（跨实体引用）
    for m in entities["product_metrics"]:
        pid = m.get("product_id")
        if not isinstance(pid, str):
            continue  # F1：非法 ID 由形状检查报告，不作字典键
        measurements = m.get("measurements")
        if not isinstance(measurements, list):
            continue
        allowed = product_modes.get(pid, set())
        for meas in measurements:
            if not isinstance(meas, dict):
                continue
            if isinstance(meas.get("mode_id"), str) and meas["mode_id"] not in allowed:
                errors.append(
                    f"{pid}: measurement "
                    f"{meas.get('measurement_id', '<measurement>')} 的 mode_id "
                    f"{meas['mode_id']!r} 未在商品 connection_modes 中定义")

    # F4：逐个商品核对报价数量（包括 0 条报价的商品，不再只遍历"已有报价"的键）
    for pid in sorted(product_set):
        count = len(offers_by_product.get(pid, []))
        if count == 0:
            errors.append(f"{pid}: 商品没有任何报价关联")
        elif manifest["profile"] == "demo" and not 3 <= count <= 7:
            errors.append(f"{pid}: demo 档每商品应有 3–7 条报价，实际 {count}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 commerce.v0.1 模拟数据目录")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    args = parser.parse_args()
    errors = validate_data_dir(args.data_dir)
    if errors:
        print(f"FAIL：{len(errors)} 个问题（{args.data_dir}）")
        for err in errors[:50]:
            print(f"  - {err}")
        if len(errors) > 50:
            print(f"  ...（其余 {len(errors) - 50} 条省略）")
        return 1
    print(f"PASS：{args.data_dir} 通过 commerce.v0.1 校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
