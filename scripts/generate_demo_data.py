"""S1.1 模拟数据生成器（commerce.v0.1）。

设计要点：
- 三个档位：small（5商家/15商品/42报价，全手工锚点）、
  integration（10/100/约400）、demo（50/500/约2500）。
  锚点实体在所有档位中原样保留（固定取舍/异常样例随档位保留），
  联调/正式档在其上做种子化程序生成。
- 锚点数据刻意包含计划 S1.1 要求的样例：
  * 缺货：P006 最便宜报价 O018 stock=0（EDGE09）、P015 两条报价全部缺货；
  * 超预算：P015 高价旗舰；
  * 恰好等于预算：O035 到手价 78500+1500=80000 分（800元）；
  * 运费导致超预算：O019 79000+2000=81000 分（EDGE06）；
  * 缺关键参数：P002 续航/重量未知、P014 ANC 未知（EDGE03）、P014 的 M005 报价库存未知；
  * 仅含盒续航：P005 只有 with_case 32h（EDGE07）；
  * 取舍：便宜但配送慢/无售后（O011/O016 与 M004）、轻但续航短（P014）、
    略贵但售后更好（M005 报价）、配件未含推高到手价（O027/O034）、
    配件未知导致到手价不可算（O028）；
  * P001/O001/M001 与 docs/api_contract.md 的 GET 示例数值对齐。
- 可复现：small 档不使用随机数；integration/demo 用固定种子 random.Random。
  实体时间戳一律取固定观测时点 FIXED_OBSERVED_AT（manifest 记录），
  只有 manifest.generated_at 反映真实运行时刻，因此实体文件逐字节可复现。
- field_sources 由 commerce_schema.build_field_sources 统一填充：
  非null叶子 missing_reason=null，null叶子默认 not_collected，
  结构性不适用的字段用显式覆盖表标 not_applicable。

用法：
    .venv/Scripts/python.exe scripts/generate_demo_data.py --profile small [--out data]
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/generate_demo_data.py` 直接运行
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.commerce_schema import (
    PROFILE_DATA_VERSIONS,
    build_field_sources,
    canonical_identity_key,
)

#: F2/F3 整改后版本：程序商品改用 (seed, product_id) 独立随机源（跨档身份稳定）；
#: ANC 开/关续航由同一基础续航推导（开启不得长于关闭）。
GENERATOR_VERSION = "1.1.0"
DEFAULT_SEED = 20260929
#: 固定观测时点：所有实体 field_sources.observed_at 与 updated_at 都用它，
#: 保证重生成内容逐字节一致（manifest.fixed_observed_at 同步记录）。
FIXED_OBSERVED_AT = "2026-09-29T10:00:00+08:00"
ENTITY_METHOD = "固定fixture，非实测"
METRICS_METHOD = "手工fixture评分，非实测"
DEMO_REGION = "CN-SH"
CURRENCY = "CNY"
SCHEMA_VERSION = "commerce.v0.1"


# ---- 小构造器：所有键都按字典白名单顺序补齐 ---------------------------------


def return_policy(days=None, opened=None, fee=None, conditions=None, exceptions=None):
    return {
        "days": days,
        "opened_return_allowed": opened,
        "return_shipping_fee_cents": fee,
        "conditions": conditions,
        "exceptions": exceptions if exceptions is not None else [],
    }


def warranty(months=None, provider=None, conditions=None):
    return {"months": months, "provider": provider, "conditions": conditions}


def mode(mode_id, transport, *, playback=None, mic=None, anc=None, power=None,
         audio_conn=None, mic_conn=None, acc_codes=None):
    return {
        "mode_id": mode_id,
        "transport": transport,
        "audio_connector": audio_conn,
        "microphone_connector": mic_conn,
        "playback_supported": playback,
        "microphone_supported": mic,
        "active_noise_cancelling": anc,
        "requires_power": power,
        "required_accessory_codes": acc_codes if acc_codes is not None else [],
    }


def compat(cid, device_key, platform, mode_id, *, os_ver=None, source_output=None,
           playback=None, mic=None, multipoint=None, firmware=None, conditions=None):
    return {
        "compatibility_id": cid,
        "device_key": device_key,
        "platform": platform,
        "os_version": os_ver,
        "source_output": source_output,
        "mode_id": mode_id,
        "playback_supported": playback,
        "microphone_supported": mic,
        "multipoint_supported": multipoint,
        "firmware_version": firmware,
        "conditions": conditions,
    }


def bspec(sid, hours, basis, anc_state, mode_id=None, cond=None):
    return {
        "battery_spec_id": sid,
        "hours": hours,
        "basis": basis,
        "anc_state": anc_state,
        "mode_id": mode_id,
        "test_conditions": cond,
    }


def wspec(sid, grams, basis, cond=None):
    return {"weight_spec_id": sid, "grams": grams, "basis": basis, "conditions": cond}


def fast_charge(minutes, playback_hours, anc_state, cond=None):
    return {
        "minutes": minutes,
        "playback_hours": playback_hours,
        "anc_state": anc_state,
        "conditions": cond,
    }


def protection(component, rating=None, conditions=None):
    return {"component": component, "rating": rating, "conditions": conditions}


def sweat_statement(allowed, conditions):
    return {"allowed": allowed, "conditions": conditions}


def measurement(mid, metric, value, unit, mode_id=None, conditions=None, group=None):
    return {
        "measurement_id": mid,
        "metric": metric,
        "value": value,
        "unit": unit,
        "mode_id": mode_id,
        "conditions": conditions,
        "comparison_group": group,
    }


def delivery(min_days, max_days, commitment, conditions=None, dest=DEMO_REGION):
    return {
        "destination_region": dest,
        "min_days": min_days,
        "max_days": max_days,
        "commitment_type": commitment,
        "conditions": conditions,
    }


def accessory(code, included, price_cents=None, stock=None, same_pkg=None):
    return {
        "accessory_code": code,
        "included": included,
        "price_cents": price_cents,
        "stock": stock,
        "delivery_in_same_package": same_pkg,
    }


def product(pid, brand, model, variant, name, sub_category, **kw):
    """Product 实体构造器：键序与 commerce_schema.ENTITY_SPECS 一致。"""
    identity = canonical_identity_key(brand, model, variant, "rev1", "CN")
    return {
        "product_id": pid,
        "identity_key": identity,
        "brand": brand,
        "model": model,
        "product_name": name,
        "variant": variant,
        "hardware_revision": "rev1",
        "market_region": "CN",
        "category": "headphones",
        "sub_category": sub_category,
        "form_factor": kw.get("form_factor"),
        "fit_style": kw.get("fit_style"),
        "open_ear": kw.get("open_ear"),
        "acoustic_design": kw.get("acoustic_design"),
        "wireless": kw.get("wireless"),
        "connection_modes": kw.get("connection_modes"),
        "bluetooth_version": kw.get("bluetooth_version"),
        "active_noise_cancelling": kw.get("active_noise_cancelling"),
        "microphone": kw.get("microphone"),
        "microphone_noise_reduction": kw.get("microphone_noise_reduction"),
        "mute_control": kw.get("mute_control"),
        "multipoint": kw.get("multipoint"),
        "max_active_devices": kw.get("max_active_devices"),
        "compatibility": kw.get("compatibility"),
        "battery_applicability": kw.get("battery_applicability", "unknown"),
        "battery_specs": kw.get("battery_specs"),
        "charging_methods": kw.get("charging_methods"),
        "fast_charge": kw.get("fast_charge"),
        "weight_specs": kw.get("weight_specs"),
        "foldable": kw.get("foldable"),
        "ear_tip_sizes": kw.get("ear_tip_sizes", []),
        "earbud_dimensions_mm": kw.get("earbud_dimensions_mm"),
        "water_resistance": kw.get("water_resistance"),
        "sweat_use_statement": kw.get("sweat_use_statement"),
        "codec_support": kw.get("codec_support"),
        "description": kw.get("description"),
        "image_path": None,
    }


def metrics(pid, **kw):
    return {
        "product_id": pid,
        "rating": kw.get("rating"),
        "review_count": kw.get("review_count"),
        "return_rate": None,
        "popularity_score": kw.get("popularity"),
        "quality_score": kw.get("quality"),
        "comfort_score": kw.get("comfort"),
        "anc_score": kw.get("anc"),
        "sound_quality_score": kw.get("sound"),
        "music_score": kw.get("music"),
        "call_score": kw.get("call"),
        "commute_score": kw.get("commute"),
        "gaming_score": kw.get("gaming"),
        "portability_score": kw.get("portability"),
        "fit_stability_score": kw.get("fit_stab"),
        "measurements": kw.get("measurements"),
    }


def offer(oid, product_id, merchant_id, price, stock, *, ship=0, orig=None,
          instant=0, promo=None, condition="new", acc=None, rp=None, wr=None,
          valid_until=None, gift=None, deliv=None, commission_rate=None):
    return {
        "offer_id": oid,
        "product_id": product_id,
        "merchant_id": merchant_id,
        "currency": CURRENCY,
        "price_cents": price,
        "original_price_cents": orig,
        "shipping_fee_cents": ship,
        "promotion": promo,
        "instant_discount_cents": instant,
        "commission_rate": commission_rate,
        "accessories": acc,
        "stock": stock,
        "item_condition": condition,
        "delivery": deliv,
        "return_policy_override": rp,
        "warranty_override": wr,
        "gift_return_conditions": gift,
        "valid_until": valid_until,
    }


# ---- 商家配送模板 -----------------------------------------------------------

MERCHANT_DELIVERY = {
    "M001": delivery(1, 2, "guaranteed", "模拟次日达服务条款（工作日18点前下单）"),
    "M002": delivery(2, 4, "estimated", "模拟华东仓发货"),
    "M003": delivery(3, 5, "estimated", "模拟第三方物流"),
    "M004": delivery(5, 8, "estimated", "模拟经济快递"),
    "M005": delivery(2, 3, "estimated", "模拟自营配送"),
}

#: O001/O002 的报价级退货覆盖（与 api_contract.md 示例一致）
CONTRACT_RETURN_OVERRIDE = return_policy(
    days=7,
    opened=False,
    fee=1200,
    conditions="仅未开封",
    exceptions=["不接受开封试戴退货"],
)


# ---- 锚点：商家 -------------------------------------------------------------

def anchor_merchants():
    merchants = [
        {
            "merchant_id": "M001",
            "merchant_name": "AudioWorld",
            "merchant_type": "authorized",
            "region": "CN",
            "merchant_rating": 4.7,
            "service_score": 86,
            "shipping_score": 90,
            "default_return_policy": return_policy(
                30, True, 0, "包装和配件齐全", []
            ),
            "default_warranty": warranty(12, "AudioWorld", "模拟本地区保修"),
        },
        {
            "merchant_id": "M002",
            "merchant_name": "SoundHaven",
            "merchant_type": "authorized",
            "region": "CN",
            "merchant_rating": 4.5,
            "service_score": 82,
            "shipping_score": 84,
            "default_return_policy": return_policy(
                15, True, 800, "配件齐全且包装完好", []
            ),
            "default_warranty": warranty(12, "SoundHaven", "模拟门店保修"),
        },
        {
            "merchant_id": "M003",
            "merchant_name": "ValueCart",
            "merchant_type": "marketplace",
            "region": "CN",
            "merchant_rating": 4.2,
            "service_score": 71,
            "shipping_score": 76,
            "default_return_policy": return_policy(
                7, False, 1200, "仅未开封", ["激活后不支持退货"]
            ),
            "default_warranty": warranty(3, "ValueCart", "模拟店铺保修"),
        },
        {
            "merchant_id": "M004",
            "merchant_name": "DealExpress",
            "merchant_type": "independent",
            "region": "CN",
            "merchant_rating": None,  # 新开店，尚无平台评分
            "service_score": 65,
            "shipping_score": 70,
            "default_return_policy": return_policy(
                0, False, None, "不支持退货", []
            ),
            "default_warranty": warranty(0, "DealExpress", "模拟：不提供保修"),
        },
        {
            "merchant_id": "M005",
            "merchant_name": "PrimeAudio Hub",
            "merchant_type": "independent",
            "region": "CN",
            "merchant_rating": 4.8,
            "service_score": 90,
            "shipping_score": 88,
            "default_return_policy": return_policy(
                30, True, 0, "包装配件齐全，支持开封试戴", []
            ),
            "default_warranty": warranty(24, "PrimeAudio Hub", "模拟延保服务"),
        },
    ]
    overrides = {"M004": {"merchant_rating": "not_collected",
                          "default_return_policy.return_shipping_fee_cents": "not_applicable"}}
    return merchants, overrides


# ---- 锚点：商品（含指标） ----------------------------------------------------

def _wireless_mode_overrides(p):
    """无线模式的接头字段为 null 时标 not_applicable（接头不适用于该传输）。"""
    overrides = {}
    for i, m in enumerate(p["connection_modes"] or []):
        if m["transport"] in ("bluetooth", "wireless_dongle", "optical_transmitter"):
            if m["audio_connector"] is None:
                overrides[f"connection_modes[{i}].audio_connector"] = "not_applicable"
            if m["microphone_connector"] is None:
                overrides[f"connection_modes[{i}].microphone_connector"] = "not_applicable"
    return overrides


def compute_product_overrides(p):
    """按结构推导 not_applicable 覆盖（程序生成商品使用；锚点手写同规则）。"""
    overrides = _wireless_mode_overrides(p)
    if p["battery_applicability"] == "not_applicable":
        overrides.update({
            "battery_specs": "not_applicable",
            "charging_methods": "not_applicable",
            "codec_support": "not_applicable",
            "fast_charge": "not_applicable",
            "bluetooth_version": "not_applicable",
            "multipoint": "not_applicable",
            "max_active_devices": "not_applicable",
        })
    if p["sub_category"] in ("true_wireless_earbuds", "wireless_earbuds"):
        overrides.setdefault("acoustic_design", "not_applicable")
        overrides.setdefault("foldable", "not_applicable")
    return overrides


def compute_metric_overrides(p):
    """ANC 功能不存在时不虚构 anc_score；无麦克风不虚构 call_score。"""
    overrides = {}
    if p["active_noise_cancelling"] is False:
        overrides["anc_score"] = "not_applicable"
    if p["microphone"] is False:
        overrides["call_score"] = "not_applicable"
    return overrides


def anchor_products():
    products = []

    # P001：通勤 ANC 头戴（与 api_contract.md §4 示例数值对齐）
    p = product(
        "P001", "Auraloom", "Metro1", "black", "Metro One", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=True, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=True, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 20, "single_charge", "on", "bt", "模拟50%音量SBC听音")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 240, "headset", "整副无配件")],
        foldable=True, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC"], description="模拟通勤耳机",
    )
    products.append(p)

    # P002：续航/重量未知的 ANC 头戴（缺失关键参数样例；多点连接）
    p = product(
        "P002", "Auraloom", "MetroLite", "black", "Metro Lite", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=True, power=True)],
        bluetooth_version="5.4", active_noise_cancelling=True, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=True, max_active_devices=2, compatibility=None,
        battery_applicability="applicable", battery_specs=None,
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=None, foldable=True, ear_tip_sizes=[],
        earbud_dimensions_mm=None, water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC"], description="模拟通勤耳机，续航与重量参数未采集",
    )
    products.append(p)

    # P003：长途旅行（单次 ANC 30h、快充、折叠）
    p = product(
        "P003", "Quietgale", "Hush2", "midnight", "Hush 2", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=True, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=True, microphone=True,
        microphone_noise_reduction=True, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 30, "single_charge", "on", "bt", "模拟50%音量LDAC听音"),
                       bspec("B2", 42, "single_charge", "off", "bt", "模拟50%音量LDAC听音")],
        charging_methods=["usb_c"],
        fast_charge=fast_charge(15, 3, "on", "模拟快充：充电15分钟增加3小时播放"),
        weight_specs=[wspec("W1", 265, "headset", "整副无配件")],
        foldable=True, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC", "LDAC"], description="模拟长途降噪耳机",
    )
    products.append(p)

    # P004：开放式耳挂（防汗声明 + IPX4；SC11/SC12 锚点）
    p = product(
        "P004", "Vektra", "SkyFit", "sand", "SkyFit", "wireless_earbuds",
        form_factor="open_ear", fit_style="ear_hook", open_ear=True,
        acoustic_design=None, wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=False, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 7, "single_charge", "not_applicable", "bt",
                             "模拟50%音量SBC听音")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 8.5, "single_earbud", "单只，不含耳挂配件")],
        foldable=None, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=[protection("earbuds", "IPX4", "模拟：防溅水，不适合浸泡")],
        sweat_use_statement=sweat_statement(True, "模拟厂商声明：运动汗液适用，训练后需擦拭晾干"),
        codec_support=["SBC", "AAC"], description="模拟开放式运动耳挂",
    )
    products.append(p)

    # P005：轻便 TWS，只有含盒续航（EDGE07 锚点），带 S 号耳塞套（SC14 锚点）
    p = product(
        "P005", "Basscamp", "Pulse", "white", "Pulse", "true_wireless_earbuds",
        form_factor="in_ear", fit_style="ear_tip", open_ear=False,
        acoustic_design=None, wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=False, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 32, "with_case", "not_applicable", "bt",
                             "模拟：含充电盒累计播放")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 4.8, "single_earbud", "单只")],
        foldable=None, ear_tip_sizes=["S", "M", "L"], earbud_dimensions_mm=None,
        water_resistance=[protection("earbuds", "IPX5", "模拟：防淋水")],
        sweat_use_statement=None,
        codec_support=["SBC", "AAC"], description="模拟真无线耳塞（厂商仅提供含盒续航）",
    )
    products.append(p)

    # P006：多点连接头戴（EDGE09/EDGE06 锚点：最便宜缺货，有货的都超 800 元）
    p = product(
        "P006", "Quietgale", "DuoSync", "black", "DuoSync", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=True, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=True, microphone=True,
        microphone_noise_reduction=True, mute_control=None,
        multipoint=True, max_active_devices=2, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 26, "single_charge", "on", "bt", "模拟50%音量aptX听音")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 250, "headset", "整副无配件")],
        foldable=True, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC", "aptX"], description="模拟双设备多点连接耳机",
    )
    products.append(p)

    # P007：百元有线耳麦（SC06 锚点：3.5mm TRRS、无需供电）
    p = product(
        "P007", "EchoNest", "ClearVoice300", "black", "ClearVoice 300", "wired_headphones",
        form_factor="on_ear", fit_style="headband", open_ear=False,
        acoustic_design=None, wireless=False,
        connection_modes=[mode("w35", "wired_3_5mm", playback=True, mic=True,
                               anc=False, power=False,
                               audio_conn="3.5mm_TRRS_CTIA", mic_conn="3.5mm_TRRS_CTIA")],
        bluetooth_version=None, active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=None, max_active_devices=None, compatibility=[
            compat("C1", "通用PC（3.5mm TRRS复合口）", "windows", "w35",
                   playback=True, mic=True,
                   conditions="电脑需具备TRRS复合接口；分离式接口需另购转接线（本商品未含）"),
        ],
        battery_applicability="not_applicable", battery_specs=None,
        charging_methods=None, fast_charge=None,
        weight_specs=[wspec("W1", 165, "headset", "整副含线")],
        foldable=None, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=None, description="模拟有线即用耳麦",
    )
    products.append(p)

    # P008：有线游戏耳麦（双接口、静音、延迟测量；SC07 锚点；含官翻报价）
    p = product(
        "P008", "Vektra", "StrikeProG1", "black", "StrikePro G1", "gaming_headsets",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=False,
        connection_modes=[
            mode("usb", "usb_audio", playback=True, mic=True, anc=False,
                 power=True, audio_conn="USB_A", mic_conn="内置声卡"),
            mode("w35", "wired_3_5mm", playback=True, mic=True, anc=False,
                 power=False, audio_conn="3.5mm_TRRS_CTIA", mic_conn="3.5mm_TRRS_CTIA"),
        ],
        bluetooth_version=None, active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=True, mute_control=True,
        multipoint=None, max_active_devices=None, compatibility=[
            compat("C1", "通用PC（USB）", "windows", "usb",
                   playback=True, mic=True, conditions="USB直连即插即用"),
        ],
        battery_applicability="not_applicable", battery_specs=None,
        charging_methods=None, fast_charge=None,
        weight_specs=[wspec("W1", 310, "headset", "整副含可拆卸麦克风")],
        foldable=None, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=None, description="模拟电竞耳麦（双有线接口）",
    )
    products.append(p)

    # P009：PS5 无线游戏耳麦（dongle 必要配件；SC08 锚点；含配件未含/未知报价）
    dongle_code = "QG-DONGLE-USB"
    p = product(
        "P009", "Quietgale", "AstroPlayW", "white", "AstroPlay W", "gaming_headsets",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[
            mode("bt", "bluetooth", playback=True, mic=True, anc=False, power=True),
            mode("dongle", "wireless_dongle", playback=True, mic=True, anc=False,
                 power=True, acc_codes=[dongle_code]),
        ],
        bluetooth_version="5.3", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=True, mute_control=True,
        multipoint=False, max_active_devices=1, compatibility=[
            compat("C1", "Sony PlayStation 5", "ps5", "dongle",
                   playback=True, mic=True,
                   conditions="需QG-DONGLE-USB适配器（随部分报价附赠或另购）"),
            compat("C2", "通用PC（USB）", "windows", "dongle",
                   playback=True, mic=True, conditions="同PS5，需同一适配器"),
        ],
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 35, "single_charge", "not_applicable", "dongle",
                             "模拟50%音量听音，禁用灯效")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 295, "headset", "整副含可拆卸麦克风")],
        foldable=None, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC"], description="模拟主机无线游戏耳麦",
    )
    products.append(p)

    # P010：开放式贴耳办公耳机（漏音测量；SC03/SC13 锚点）
    p = product(
        "P010", "EchoNest", "VelvetOpen", "steel", "Velvet Open", "on_ear_headphones",
        form_factor="on_ear", fit_style="headband", open_ear=False,
        acoustic_design="open_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=False, power=True)],
        bluetooth_version="5.2", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 24, "single_charge", "not_applicable", "bt",
                             "模拟50%音量AAC听音")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 165, "headset", "整副无配件")],
        foldable=False, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC"], description="模拟开放式办公耳机（有声漏音）",
    )
    products.append(p)

    # P011：有线 Hi-Fi 头戴（无麦克风；SC09 锚点）
    p = product(
        "P011", "Basscamp", "Studio40", "black", "Studio 40", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=False,
        connection_modes=[mode("w35", "wired_3_5mm", playback=True, mic=False,
                               anc=False, power=False, audio_conn="3.5mm_TRS")],
        bluetooth_version=None, active_noise_cancelling=False, microphone=False,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=None, max_active_devices=None, compatibility=None,
        battery_applicability="not_applicable", battery_specs=None,
        charging_methods=None, fast_charge=None,
        weight_specs=[wspec("W1", 320, "headset", "整副含可拆卸线材")],
        foldable=False, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=None, description="模拟家用监听耳机",
    )
    products.append(p)

    # P012：电视耳机（光纤发射器模式 + 必要配件；SC10 锚点；O035 恰好800元）
    tx_code = "VK-TX-OPT"
    p = product(
        "P012", "Vektra", "NightLink", "black", "NightLink", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[
            mode("bt", "bluetooth", playback=True, mic=True, anc=False, power=True),
            mode("optical", "optical_transmitter", playback=True, mic=False,
                 anc=False, power=True, acc_codes=[tx_code]),
        ],
        bluetooth_version="5.2", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=[
            compat("C1", "通用电视（光纤数字音频输出）", "tv", "optical",
                   source_output="optical_pcm", playback=True, mic=False,
                   conditions="电视需具备光纤输出；VK-TX-OPT发射器随部分报价附赠或另购"),
        ],
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 30, "single_charge", "not_applicable", "bt",
                             "模拟50%音量SBC听音")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 240, "headset", "整副无配件")],
        foldable=False, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC"], description="模拟电视无线耳机",
    )
    products.append(p)

    # P013：健身 TWS（防汗声明；SC11 锚点）
    p = product(
        "P013", "Basscamp", "RunFree", "white", "RunFree", "true_wireless_earbuds",
        form_factor="in_ear", fit_style="ear_tip", open_ear=False,
        acoustic_design=None, wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=False, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=False, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 6, "single_charge", "not_applicable", "bt",
                             "模拟50%音量AAC听音"),
                       bspec("B2", 22, "with_case", "not_applicable", "bt",
                             "模拟：含充电盒累计播放")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 5.0, "single_earbud", "单只")],
        foldable=None, ear_tip_sizes=["S", "M", "L"], earbud_dimensions_mm=None,
        water_resistance=[protection("earbuds", "IPX4", "模拟：防溅水")],
        sweat_use_statement=sweat_statement(True, "模拟厂商声明：室内训练防汗适用，训练后需清洁"),
        codec_support=["SBC", "AAC"], description="模拟运动真无线耳塞",
    )
    products.append(p)

    # P014：ANC 未知的 TWS（EDGE03 锚点）+ 无防汗声明对照 + 库存未知报价
    p = product(
        "P014", "EchoNest", "AeroBuds", "white", "AeroBuds", "true_wireless_earbuds",
        form_factor="in_ear", fit_style="ear_tip", open_ear=False,
        acoustic_design=None, wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=None, power=True)],
        bluetooth_version="5.3", active_noise_cancelling=None, microphone=True,
        microphone_noise_reduction=None, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 6, "single_charge", "unknown", "bt",
                             "模拟：厂商未注明ANC状态"),
                       bspec("B2", 24, "with_case", "unknown", "bt",
                             "模拟：含充电盒累计，ANC状态未知")],
        charging_methods=["usb_c"], fast_charge=None,
        weight_specs=[wspec("W1", 5.2, "single_earbud", "单只")],
        foldable=None, ear_tip_sizes=["M", "L"], earbud_dimensions_mm=None,
        water_resistance=[protection("earbuds", "IPX5", "模拟：防淋水")],
        sweat_use_statement=None,
        codec_support=["SBC", "AAC"], description="模拟耳塞（ANC能力未披露）",
    )
    products.append(p)

    # P015：旗舰 ANC（超预算锚点）；两条报价全部缺货
    p = product(
        "P015", "Quietgale", "QuietMax", "black", "QuietMax", "over_ear_headphones",
        form_factor="over_ear", fit_style="headband", open_ear=False,
        acoustic_design="closed_back", wireless=True,
        connection_modes=[mode("bt", "bluetooth", playback=True, mic=True,
                               anc=True, power=True)],
        bluetooth_version="5.4", active_noise_cancelling=True, microphone=True,
        microphone_noise_reduction=True, mute_control=None,
        multipoint=False, max_active_devices=1, compatibility=None,
        battery_applicability="applicable",
        battery_specs=[bspec("B1", 40, "single_charge", "on", "bt", "模拟50%音量LDAC听音"),
                       bspec("B2", 55, "single_charge", "off", "bt", "模拟50%音量LDAC听音")],
        charging_methods=["usb_c"],
        fast_charge=fast_charge(10, 5, "on", "模拟快充：充电10分钟增加5小时播放"),
        weight_specs=[wspec("W1", 255, "headset", "整副无配件")],
        foldable=True, ear_tip_sizes=[], earbud_dimensions_mm=None,
        water_resistance=None, sweat_use_statement=None,
        codec_support=["SBC", "AAC", "LDAC"], description="模拟旗舰降噪耳机",
    )
    products.append(p)

    metric_values = {
        "P001": dict(rating=4.5, review_count=120, popularity=70, quality=78,
                     comfort=80, anc=85, sound=75, call=70, commute=82, portability=72),
        "P002": dict(rating=None, review_count=None, popularity=60, quality=72,
                     comfort=74, anc=85, sound=None, call=68, commute=76, portability=65),
        "P003": dict(rating=4.6, review_count=340, popularity=78, quality=82,
                     comfort=83, anc=88, sound=80, music=78, call=72, commute=80,
                     portability=70),
        "P004": dict(rating=4.4, review_count=210, popularity=74, quality=70,
                     comfort=78, sound=68, music=66, call=70, commute=62,
                     portability=82, fit_stab=80),
        "P005": dict(rating=4.3, review_count=180, popularity=72, quality=68,
                     comfort=75, sound=70, music=68, call=66, commute=60,
                     portability=80, fit_stab=76),
        "P006": dict(rating=4.5, review_count=95, popularity=66, quality=79,
                     comfort=81, anc=84, sound=77, music=75, call=74, commute=78,
                     portability=68),
        "P007": dict(rating=4.1, review_count=56, popularity=58, quality=62,
                     comfort=66, sound=60, music=58, call=72, gaming=55, portability=70),
        "P008": dict(rating=4.4, review_count=230, popularity=76, quality=74,
                     comfort=72, sound=72, call=82, gaming=87, portability=40),
        "P009": dict(rating=4.5, review_count=150, popularity=70, quality=75,
                     comfort=76, sound=74, call=80, gaming=85, portability=55),
        "P010": dict(rating=4.2, review_count=88, popularity=62, quality=68,
                     comfort=84, sound=70, music=72, call=64, commute=58, portability=74),
        "P011": dict(rating=4.6, review_count=140, popularity=64, quality=80,
                     comfort=78, sound=88, music=86, portability=45),
        "P012": dict(rating=4.3, review_count=77, popularity=60, quality=72,
                     comfort=79, sound=75, music=70, call=58, gaming=60, portability=62),
        "P013": dict(rating=4.4, review_count=165, popularity=71, quality=69,
                     comfort=77, sound=69, music=67, call=68, commute=61,
                     portability=82, fit_stab=84),
        "P014": dict(rating=4.0, review_count=45, popularity=57, quality=63,
                     comfort=70, sound=66, music=64, call=62, commute=55,
                     portability=80, fit_stab=72),
        "P015": dict(rating=4.8, review_count=260, popularity=85, quality=90,
                     comfort=86, anc=92, sound=90, music=89, call=78, commute=88,
                     portability=66),
    }
    metric_values["P008"]["measurements"] = [
        measurement("MS1", "latency_ms", 32, "ms", "usb",
                    "模拟：Windows PC + USB直连，1000帧平均", "CG-USB-2026")
    ]
    metric_values["P009"]["measurements"] = [
        measurement("MS1", "latency_ms", 38, "ms", "dongle",
                    "模拟：PS5 + QG-DONGLE-USB", "CG-DONGLE-2026")
    ]
    metric_values["P010"]["measurements"] = [
        measurement("MS1", "leakage_db", 48, "dB", "bt",
                    "模拟：60%音量粉噪，1米处", "CG-LEAK-2026")
    ]
    metric_values["P012"]["measurements"] = [
        measurement("MS1", "latency_ms", 45, "ms", "optical",
                    "模拟：光纤发射器直连", "CG-TX-2026")
    ]
    product_metrics = [metrics(pid, **metric_values[pid]) for pid in
                       [f"P{n:03d}" for n in range(1, 16)]]
    return products, product_metrics


# ---- 锚点：报价（42条） ------------------------------------------------------

def anchor_offers():
    rp_contract = CONTRACT_RETURN_OVERRIDE
    rp_try = return_policy(14, True, 800, "支持开封试戴后按条款退货", [])
    dongle_code = "QG-DONGLE-USB"
    tx_code = "VK-TX-OPT"
    offers = [
        # P001（O001 与 api_contract.md §5 示例对齐）
        offer("O001", "P001", "M001", 69900, 12, orig=79900, rp=rp_contract,
              valid_until="2026-09-29T23:59:59+08:00"),
        offer("O003", "P001", "M002", 71900, 8),
        offer("O004", "P001", "M003", 69900, 25, ship=1500, instant=500,
              promo="模拟限时直降5元"),
        offer("O005", "P001", "M004", 68900, 6, ship=3000),
        # P002
        offer("O002", "P002", "M001", 69900, 12, rp=rp_contract),
        offer("O006", "P002", "M002", 71500, 9),
        offer("O007", "P002", "M003", 67900, 20, ship=2500),
        offer("O008", "P002", "M004", 69900, 7, ship=800, instant=400,
              promo="模拟新店立减4元"),
        # P003（O011：便宜但配送慢且无售后）
        offer("O009", "P003", "M001", 129900, 10, orig=137900),
        offer("O010", "P003", "M003", 124900, 15, ship=2000),
        offer("O011", "P003", "M004", 119900, 5, ship=3500),
        # P004
        offer("O012", "P004", "M002", 39900, 18),
        offer("O013", "P004", "M004", 38900, 9, ship=1500),
        offer("O014", "P004", "M005", 40900, 11),
        # P005（O015 允许开封试戴；O016 最便宜但无退货）
        offer("O015", "P005", "M002", 34900, 14, rp=rp_try),
        offer("O016", "P005", "M004", 31900, 22, ship=1200),
        offer("O017", "P005", "M005", 33900, 8, ship=2000,
              valid_until="2026-10-15T23:59:59+08:00"),
        # P006（EDGE09+EDGE06）
        offer("O018", "P006", "M001", 64900, 0),
        offer("O019", "P006", "M002", 79000, 8, ship=2000),
        offer("O020", "P006", "M005", 84500, 5, orig=89900),
        # P007
        offer("O021", "P007", "M003", 8900, 30),
        offer("O022", "P007", "M005", 9900, 25, ship=800),
        # P008（O025 官翻）
        offer("O023", "P008", "M001", 44900, 16),
        offer("O024", "P008", "M003", 42900, 28, ship=1500),
        offer("O025", "P008", "M004", 38900, 12, condition="refurbished",
              promo="模拟官翻机"),
        # P009（配件方案三种形态：随附/另购/未知）
        offer("O026", "P009", "M001", 79900, 10,
              acc=[accessory(dongle_code, True, 0, None, True)]),
        offer("O027", "P009", "M003", 74900, 30,
              acc=[accessory(dongle_code, False, 9900, 6, True)]),
        offer("O028", "P009", "M004", 76900, 8, acc=None),
        # P010
        offer("O029", "P010", "M002", 29900, 21),
        offer("O030", "P010", "M005", 28900, 13, ship=1000),
        # P011
        offer("O031", "P011", "M001", 59900, 9),
        offer("O032", "P011", "M004", 57900, 4, ship=2000),
        # P012（O035 到手价 78500+1500 = 80000 分，恰好等于 800 元预算）
        offer("O033", "P012", "M001", 69900, 11,
              acc=[accessory(tx_code, True, 0, None, True)]),
        offer("O034", "P012", "M003", 65900, 26,
              acc=[accessory(tx_code, False, 8900, 5, True)],
              promo="模拟以旧换新补贴"),
        offer("O035", "P012", "M005", 78500, 7, ship=1500,
              acc=[accessory(tx_code, True, 0, None, True)]),
        # P013
        offer("O036", "P013", "M002", 29900, 17),
        offer("O037", "P013", "M004", 27900, 10, ship=1500),
        # P014（O040 库存未知）
        offer("O038", "P014", "M002", 24900, 19, orig=26900),
        offer("O039", "P014", "M003", 23900, 24, ship=1500),
        offer("O040", "P014", "M005", 25900, None),
        # P015（全部缺货；O041 带礼品退货说明）
        offer("O041", "P015", "M001", 189900, 0, orig=199900,
              gift="模拟：可开礼品收据"),
        offer("O042", "P015", "M002", 179900, 0, ship=3000),
    ]
    # 报价配送继承商家模板（所有叶子 observed_at 同源）
    for off in offers:
        off["delivery"] = MERCHANT_DELIVERY[off["merchant_id"]]
    return offers


# ---- 联调/正式档的程序生成 ---------------------------------------------------

PROC_BRANDS = ["Sonicmille", "Waveroam", "Cloudbeat", "Airzen", "Pulseform",
               "Northsound", "Lumeaudio", "Kitehear"]
PROC_CODENAMES = ["Wave", "Drift", "Nimbus", "Coral", "Zephyr", "Aster", "Halcyon",
                  "Marlin", "Fjord", "Solace", "Onyx", "Lyra", "Mistral", "Vela",
                  "Sable", "Quartz", "Juniper", "Fathom", "Arden", "Kestrel"]
PROC_MERCHANT_NAMES = [
    "Nova Audio Store", "Urban Electronics", "Direct Digital", "Global Tech Mall",
    "Smart Sound Shop", "Mega Gadget Hub", "Sun Audio Lab", "Star Digital",
    "Fast Electronics", "Clear Sound Market", "Blue Tech Depot", "Oak Audio House",
]
PROC_MERCHANT_TYPES = ["authorized", "independent", "marketplace"]
PROC_VARIANTS = ["black", "white", "silver", "midnight", "sand"]

#: 每档位：(程序生成商家数, 商品数, 每商品报价数范围)
#: integration 约400 条（85×4.5+锚点）；demo 最低2,500 条（485×5.5+锚点），
#: 且每商品报价数始终落在正式档要求的 3–7 条区间内。
PROFILE_TARGETS = {
    "integration": {"extra_merchants": 5, "extra_products": 85, "offers_per": (4, 5)},
    "demo": {"extra_merchants": 45, "extra_products": 485, "offers_per": (4, 7)},
}


def _dt_after_days(days: int) -> str:
    base = datetime.fromisoformat(FIXED_OBSERVED_AT)
    return (base + timedelta(days=days)).isoformat(timespec="seconds")


def procedural_merchants(count: int, rng: random.Random, start_index: int = 101):
    merchants = []
    delivery_map = {}
    for i in range(count):
        mid = f"M{start_index + i:03d}"
        mtype = PROC_MERCHANT_TYPES[i % 3]
        rating = None if rng.random() < 0.06 else round(rng.uniform(3.9, 4.9), 1)
        days = rng.choice([7, 15, 30])
        fee = rng.choice([0, 0, 800, 1200])
        merchants.append({
            "merchant_id": mid,
            "merchant_name": PROC_MERCHANT_NAMES[i % len(PROC_MERCHANT_NAMES)]
            + (f" {start_index + i}" if i >= len(PROC_MERCHANT_NAMES) else ""),
            "merchant_type": mtype,
            "region": "CN",
            "merchant_rating": rating,
            "service_score": round(rng.uniform(60, 95)),
            "shipping_score": round(rng.uniform(60, 95)),
            "default_return_policy": return_policy(
                days, rng.random() < 0.6, fee, "模拟商家默认退货政策", []),
            "default_warranty": warranty(
                rng.choice([3, 6, 12, 24]), f"商家{mid}", "模拟商家保修"),
        })
        base = rng.randint(2, 5)
        delivery_map[mid] = delivery(
            base, base + rng.randint(1, 3), "estimated", "模拟商家默认配送")
    return merchants, delivery_map


def procedural_product(pid, rng):
    brand = rng.choice(PROC_BRANDS)
    codename = rng.choice(PROC_CODENAMES)
    # 模型号直接使用商品序号，保证 (brand, model, variant, rev, region) 不重复
    num = int(pid[1:])
    model = f"{brand[:3].lower()}{num}"
    name = f"{codename} {num // 10}"

    sub = rng.choices(
        ["over_ear_headphones", "true_wireless_earbuds", "gaming_headsets",
         "wireless_earbuds", "on_ear_headphones", "wired_headphones"],
        weights=[30, 22, 14, 12, 10, 12],
    )[0]
    is_wired = sub == "wired_headphones"
    wireless = not is_wired
    if sub in ("over_ear_headphones", "on_ear_headphones"):
        wireless = rng.random() < 0.8
    elif sub == "gaming_headsets":
        wireless = rng.random() < 0.6

    form = {"over_ear_headphones": "over_ear", "on_ear_headphones": "on_ear",
            "true_wireless_earbuds": "in_ear", "wireless_earbuds": "in_ear",
            "gaming_headsets": "over_ear", "wired_headphones": "over_ear"}[sub]
    if sub == "wireless_earbuds" and rng.random() < 0.35:
        form = "open_ear"
    fit = {"over_ear": "headband", "on_ear": "headband", "in_ear": "ear_tip",
           "open_ear": "ear_hook"}[form]

    dongle_code = None
    modes = []
    anc = None
    if wireless:
        anc = rng.random() < 0.55 if sub != "true_wireless_earbuds" else rng.random() < 0.35
        modes.append(mode("bt", "bluetooth", playback=True, mic=True,
                          anc=anc, power=True))
        if sub in ("over_ear_headphones", "on_ear_headphones") and rng.random() < 0.25:
            modes.append(mode("w35", "wired_3_5mm", playback=True, mic=True,
                              anc=False, power=False,
                              audio_conn="3.5mm_TRRS_CTIA", mic_conn="3.5mm_TRRS_CTIA"))
        if sub == "gaming_headsets" and wireless and rng.random() < 0.4:
            dongle_code = f"{brand[:2].upper()}-DONGLE-USB"
            modes.append(mode("dongle", "wireless_dongle", playback=True, mic=True,
                              anc=False, power=True, acc_codes=[dongle_code]))
    else:
        has_mic = rng.random() < 0.7
        conn = "3.5mm_TRRS_CTIA" if has_mic else "3.5mm_TRS"
        modes.append(mode("w35", "wired_3_5mm", playback=True, mic=has_mic,
                          anc=False, power=False, audio_conn=conn,
                          mic_conn="3.5mm_TRRS_CTIA" if has_mic else None))
        if rng.random() < 0.15:
            modes.append(mode("usb", "usb_audio", playback=True, mic=True,
                              anc=False, power=True, audio_conn="USB_A"))

    has_anc_mode = any(m["active_noise_cancelling"] is True for m in modes)
    if not wireless:
        # 非无线（含有线版头戴/耳塞）：无电池、无充电、无蓝牙与编码口径
        battery_applicability = "not_applicable"
        battery_specs = None
        charging = None
        bt_version = None
    else:
        battery_applicability = "applicable"
        charging = ["usb_c"]
        bt_version = "5.3" if rng.random() < 0.7 else "5.4"
        if rng.random() < 0.05:
            battery_specs = None  # 续航未知样例
        elif sub == "true_wireless_earbuds" and rng.random() < 0.06:
            battery_specs = [bspec("B1", round(rng.uniform(18, 40)),
                                   "with_case", "not_applicable" if not has_anc_mode else "off",
                                   "bt", "模拟：仅提供含盒续航")]
        else:
            battery_specs = []
            if anc:
                # F3：以同一基础续航（ANC 关闭）为基准，按耗电关系推导开启续航，
                # 不再对开/关独立抽样——杜绝"开启比关闭更久"的矛盾参数。
                base_hours = rng.uniform(10, 60)
                on_hours = max(1, round(base_hours * rng.uniform(0.5, 0.85)))
                battery_specs.append(bspec("B1", on_hours, "single_charge", "on",
                                           "bt", "模拟50%音量听音"))
                if rng.random() < 0.7:
                    battery_specs.append(bspec("B2", round(base_hours), "single_charge",
                                               "off", "bt", "模拟50%音量听音"))
                # 含盒累计以最大的单次（ANC 关闭/基础续航）为基准推导
                single_ref = round(base_hours)
            else:
                single_ref = round(rng.uniform(5, 40))
                battery_specs.append(bspec("B1", single_ref, "single_charge",
                                           "not_applicable" if anc is False else "unknown",
                                           "bt", "模拟50%音量听音"))
            if sub in ("true_wireless_earbuds", "wireless_earbuds") and rng.random() < 0.8:
                # 含盒累计续航应明显长于单次（同一基准推导，保持参数相互合理）
                battery_specs.append(bspec("B3", single_ref + rng.randint(12, 30),
                                           "with_case", "not_applicable", "bt",
                                           "模拟：含充电盒累计播放"))

    if is_wired:
        weight_specs = [wspec("W1", round(rng.uniform(140, 350)), "headset", "整副含线")]
    elif form == "on_ear":
        weight_specs = [wspec("W1", round(rng.uniform(140, 220)), "headset", "整副无配件")]
    elif form == "over_ear":
        weight_specs = [wspec("W1", round(rng.uniform(200, 350)), "headset", "整副无配件")]
    else:
        weight_specs = [wspec("W1", round(rng.uniform(4, 9), 1), "single_earbud", "单只")]

    price_bands = {
        "over_ear_headphones": (49900, 299900),
        "on_ear_headphones": (19900, 89900),
        "true_wireless_earbuds": (14900, 149900),
        "wireless_earbuds": (19900, 99900),
        "gaming_headsets": (29900, 159900),
        "wired_headphones": (8900, 99900),
    }
    lo, hi = price_bands[sub]
    base_price = int(round(rng.uniform(lo, hi) / 100) * 100)

    quality = round(rng.uniform(55, 90))

    def jittered(low_gap=-15, high_gap=12, lo_bound=40, hi_bound=95):
        return max(lo_bound, min(hi_bound, round(quality + rng.uniform(low_gap, high_gap))))

    rating = None if rng.random() < 0.05 else round(rng.uniform(3.6, 4.9), 1)
    has_mic_mode = any(m["microphone_supported"] for m in modes)
    metrics_kwargs = dict(
        rating=rating,
        review_count=None if rating is None else rng.randint(10, 2000),
        popularity=jittered(-20, 15, 30, 95),
        quality=quality,
        comfort=jittered(-12, 10),
        anc=None if anc is not True else jittered(-10, 8),
        sound=jittered(-8, 8),
        music=jittered(-10, 6),
        call=None if not has_mic_mode else jittered(-12, 8),
        commute=jittered(-15, 8),
        gaming=None if sub != "gaming_headsets" else jittered(-5, 8),
        portability=jittered(-10, 15),
        fit_stab=None if form in ("over_ear", "on_ear") and rng.random() < 0.6
        else jittered(-12, 10),
    )

    if wireless:
        multipoint = rng.random() < 0.2
        max_active = (2 if rng.random() < 0.7 else 3) if multipoint else 1
    else:
        multipoint = None
        max_active = None

    p = product(
        pid, brand, model, rng.choice(PROC_VARIANTS), name, sub,
        form_factor=form, fit_style=fit, open_ear=form == "open_ear",
        acoustic_design=None if form in ("in_ear", "open_ear")
        else rng.choice(["closed_back", "open_back", "semi_open"]),
        wireless=wireless, connection_modes=modes,
        bluetooth_version=bt_version, active_noise_cancelling=anc,
        microphone=any(m["microphone_supported"] for m in modes),
        microphone_noise_reduction=True if rng.random() < 0.3 else None,
        mute_control=True if rng.random() < 0.2 else None,
        multipoint=multipoint,
        max_active_devices=max_active,
        compatibility=None,
        battery_applicability=battery_applicability,
        battery_specs=battery_specs, charging_methods=charging, fast_charge=None,
        weight_specs=weight_specs,
        foldable=None if form in ("in_ear", "open_ear") else rng.random() < 0.6,
        ear_tip_sizes=["S", "M", "L"] if form == "in_ear" and rng.random() < 0.8
        else (["M", "L"] if form == "in_ear" else []),
        water_resistance=([protection("earbuds" if form in ("in_ear", "open_ear")
                                      else "headset", rng.choice(["IPX4", "IPX5"]),
                                      "模拟：厂商防护等级")]
                          if rng.random() < 0.4 else None),
        sweat_use_statement=None,
        codec_support=None if not wireless else ["SBC", "AAC"],
        description="模拟程序生成商品",
    )
    return p, metrics(pid, **metrics_kwargs), base_price, dongle_code


def procedural_offer(oid, p, base_price, merchant, rng, delivery_map):
    price = int(round(base_price * rng.uniform(0.85, 1.15) / 100) * 100)
    ship = 0 if rng.random() < 0.55 else int(round(rng.uniform(500, 3500) / 100) * 100)
    instant = 0 if rng.random() >= 0.15 else min(
        price, int(round(rng.uniform(100, 2000) / 100) * 100))
    stock_roll = rng.random()
    if stock_roll < 0.08:
        stock = 0
    elif stock_roll < 0.13:
        stock = None
    elif stock_roll < 0.23:
        stock = rng.randint(1, 5)
    else:
        stock = rng.randint(20, 150)

    deliv = delivery_map[merchant["merchant_id"]]
    new_deliv = delivery(
        min_days=max(0, (deliv["min_days"] or 0) + rng.randint(-1, 1)),
        max_days=max(1, (deliv["max_days"] or 1) + rng.randint(-1, 1)),
        commitment=deliv["commitment_type"],
        conditions=deliv["conditions"],
    )
    if new_deliv["min_days"] > new_deliv["max_days"]:
        new_deliv["min_days"] = new_deliv["max_days"]

    rp = None
    if rng.random() < 0.15:
        rp = (return_policy(7, False, 1200, "仅未开封", [])
              if rng.random() < 0.7 else
              return_policy(30, True, 0, "包装配件齐全，支持开封试戴", []))
    valid_until = None
    if rng.random() < 0.08:
        valid_until = _dt_after_days(rng.randint(14, 60))

    acc = []
    for m in p["connection_modes"] or []:
        for code in m["required_accessory_codes"] or []:
            if rng.random() < 0.6:
                acc.append(accessory(code, True, 0, None, True))
            else:
                acc.append(accessory(code, False,
                                     int(round(rng.uniform(6900, 12900) / 100) * 100),
                                     rng.randint(3, 20), True))

    return offer(oid, p["product_id"], merchant["merchant_id"], price, stock,
                 ship=ship, instant=instant, rp=rp, valid_until=valid_until,
                 acc=acc, deliv=new_deliv)


#: 锚点商品在扩档时的保护规则：保证契约展示锚点与异常锚点不被冲淡
ANCHOR_TOPUP_GUARDS = {
    "P001": {"min_landed": 70000},
    "P002": {"min_landed": 70000},
    "P006": {"force_stock": 0},
    "P015": {"force_stock": 0},
}


def generate_profile(profile: str, out_dir: Path, seed: int = DEFAULT_SEED) -> dict:
    """生成指定档位数据并写入 out_dir，返回 manifest dict。"""
    if profile not in PROFILE_DATA_VERSIONS:
        raise ValueError(f"未知档位 {profile!r}，可选：{sorted(PROFILE_DATA_VERSIONS)}")
    data_version = PROFILE_DATA_VERSIONS[profile]
    rng = random.Random(seed)

    merchants, merchant_overrides = anchor_merchants()
    products, product_metrics_list = anchor_products()
    offers = anchor_offers()

    if profile != "small":
        target = PROFILE_TARGETS[profile]
        pro_merchants, proc_delivery = procedural_merchants(target["extra_merchants"], rng)
        merchants.extend(pro_merchants)
        delivery_map = {**MERCHANT_DELIVERY, **proc_delivery}

        anchor_offer_counts = {p["product_id"]: 0 for p in products}
        for off in offers:
            anchor_offer_counts[off["product_id"]] += 1

        next_offer_id = 101
        for i in range(target["extra_products"]):
            pid = f"P{101 + i:03d}"
            # F2：每个商品用 (seed, product_id) 派生的独立随机源——
            # 商品身份（品牌/型号/颜色/参数）只取决于自身编号，与档位无关，
            # 保证 integration/demo 共同 ID 指向同一 identity_key。
            product_rng = random.Random(f"{seed}:product:{pid}")
            p, m, base_price, _ = procedural_product(pid, product_rng)
            products.append(p)
            product_metrics_list.append(m)
            k = rng.randint(*target["offers_per"])
            chosen = rng.sample(merchants, k)
            for merchant in chosen:
                offers.append(procedural_offer(
                    f"O{next_offer_id:03d}", p, base_price, merchant, rng, delivery_map))
                next_offer_id += 1

        # 锚点商品补报价：保证 demo 档每商品 3–7 条；带保护规则
        anchor_ids = [f"P{n:03d}" for n in range(1, 16)]
        anchor_prices: dict[str, list[int]] = {}
        for off in offers:
            anchor_prices.setdefault(off["product_id"], []).append(off["price_cents"])
        for pid in anchor_ids:
            p = next(x for x in products if x["product_id"] == pid)
            current = anchor_offer_counts[pid]
            if profile == "integration":
                want = max(0, 3 - current) + (1 if rng.random() < 0.3 else 0)
            else:
                want = max(0, 3 - current) + rng.randint(1, 2)
            guard = ANCHOR_TOPUP_GUARDS.get(pid, {})
            base = sum(anchor_prices[pid]) // len(anchor_prices[pid])
            existing_merchants = {off["merchant_id"] for off in offers
                                  if off["product_id"] == pid}
            candidates = [m for m in merchants if m["merchant_id"] not in existing_merchants]
            rng.shuffle(candidates)
            for merchant in candidates[:want]:
                off = procedural_offer(f"O{next_offer_id:03d}", p, base, merchant, rng, delivery_map)
                if "min_landed" in guard:
                    # 抬价直到到手价不低于保护线（保持契约展示锚点）
                    floor = guard["min_landed"] + off["shipping_fee_cents"] \
                        + off["instant_discount_cents"]
                    off["price_cents"] = max(off["price_cents"], floor)
                if "force_stock" in guard:
                    off["stock"] = guard["force_stock"]
                    off["item_condition"] = "new"
                offers.append(off)
                next_offer_id += 1

        # 商家覆盖：确保每个商家至少有一条报价
        have_offer = {off["merchant_id"] for off in offers}
        for merchant in merchants:
            if merchant["merchant_id"] in have_offer:
                continue
            candidates = [p for p in products
                          if sum(1 for off in offers if off["product_id"] == p["product_id"]) < 7
                          and p["product_id"] not in ANCHOR_TOPUP_GUARDS]
            p = rng.choice(candidates)
            base_price = next(off["price_cents"] for off in offers
                              if off["product_id"] == p["product_id"])
            offers.append(procedural_offer(
                f"O{next_offer_id:03d}", p, base_price, merchant, rng, delivery_map))
            next_offer_id += 1

    # ---- 附加公共字段与来源 -------------------------------------------------
    for m in merchants:
        m["data_version"] = data_version
        m["updated_at"] = FIXED_OBSERVED_AT
        m["field_sources"] = build_field_sources(
            m, "merchant", data_version=data_version,
            observed_at=FIXED_OBSERVED_AT, method=ENTITY_METHOD,
            missing_reason_overrides=merchant_overrides.get(m["merchant_id"], {}))

    for p in products:
        p["data_version"] = data_version
        p["updated_at"] = FIXED_OBSERVED_AT
        p["field_sources"] = build_field_sources(
            p, "product", data_version=data_version,
            observed_at=FIXED_OBSERVED_AT, method=ENTITY_METHOD,
            missing_reason_overrides=compute_product_overrides(p))

    for mrec in product_metrics_list:
        mrec["data_version"] = data_version
        mrec["updated_at"] = FIXED_OBSERVED_AT
        anchor_product = next(p for p in products
                              if p["product_id"] == mrec["product_id"])
        mrec["field_sources"] = build_field_sources(
            mrec, "product_metrics", data_version=data_version,
            observed_at=FIXED_OBSERVED_AT, method=METRICS_METHOD,
            missing_reason_overrides=compute_metric_overrides(anchor_product))

    for off in offers:
        off["data_version"] = data_version
        off["updated_at"] = FIXED_OBSERVED_AT
        off["field_sources"] = build_field_sources(
            off, "offer", data_version=data_version,
            observed_at=FIXED_OBSERVED_AT, method=ENTITY_METHOD)

    entities = {
        "merchants": merchants,
        "products": products,
        "offers": offers,
        "product_metrics": product_metrics_list,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_version": data_version,
        "profile": profile,
        "seed": seed,
        "generated_at": datetime.now(timezone(timedelta(hours=8)))
        .isoformat(timespec="seconds"),
        "generator_version": GENERATOR_VERSION,
        "fixed_observed_at": FIXED_OBSERVED_AT,
        "counts": {name: len(items) for name, items in entities.items()},
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, items in entities.items():
        wrapper = {
            "schema_version": SCHEMA_VERSION,
            "data_version": data_version,
            "profile": profile,
            "items": items,
        }
        _write_json(out_dir / f"{name}.json", wrapper)
    _write_json(out_dir / "manifest.json", manifest)
    return manifest


def _write_json(path: Path, payload) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 commerce.v0.1 模拟数据")
    parser.add_argument("--profile", choices=sorted(PROFILE_DATA_VERSIONS),
                        default="small", help="数据档位（默认 small）")
    parser.add_argument("--out", default="data", help="输出目录（默认项目根 data/）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    args = parser.parse_args()
    out_dir = Path(args.out)
    manifest = generate_profile(args.profile, out_dir, args.seed)
    print(f"已生成 {args.profile} 档（{manifest['data_version']}）→ {out_dir}/")
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
