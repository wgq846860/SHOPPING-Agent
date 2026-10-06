"""受校验的意图解析（S3.1；S31/RE31 整改版）：自然语言 → ShoppingIntent。

两种模式（契约 §7.2 mode）：
- "llm"：提示词交给 Harness 会话，模型输出 JSON；服务端提取并经
  Pydantic 严格校验（extra=forbid、枚举、0–1 有限数、stock_required
  固定 true），失败不得进入排序（502 LLM_INVALID_OUTPUT）。
- "rule_based_demo"：确定性关键词规则的演示解析，必须由配置显式启用
  并在响应中如实标记；不因 LLM 失败悄悄切换。

有限语义收口：
- 明确必须/禁止分别保留 true/false；无需/无所谓为 null；并列功能继承
  同组修饰词，后面的尽量轻不追溯修改前面的必须功能。
- 金额与实际相邻币种/单位绑定；非金额阈值不遮挡另一项预算；外币和
  多个冲突的明确预算先澄清。
- 整副重量口径使用确认的正向品类，排除词不参与；无法确定时澄清。
- 模型输出仍必须是全键、唯一 JSON 对象，并经过严格类型/枚举/日期
  校验；显式偏好/不强求不得被硬化，未知语义仍交给语言解析。

安全边界：模型/规则产出的只是"条件"；资格（预算、库存、模式、配送）
始终由 S2.1 candidate_service 按数据库重算，解析层无法跳过。
"""

from __future__ import annotations

import json
import math
import re
import types
import typing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

from pydantic import BaseModel, ValidationError

from backend.agent.harness_client import (
    HarnessTimeoutError,
    HarnessUnavailableError,
    ShoppingHarnessClient,
    is_failed_finish_reason,
)
from backend.agent.prompts import build_intent_prompt
from backend.agent.query_semantics import (
    PC35mmInterface,
    boolean_evidence,
    budget_evidence,
    ear_tip_evidence,
    pc_35mm_interface,
    whole_weight_basis,
)
from backend.config import Settings, load_settings
from backend.schemas.agent import (
    Budget,
    BatteryRequirement,
    Clarification,
    DeviceRequirement,
    HardConstraints,
    Preferences,
    ShoppingIntent,
    WeightRequirement,
)
from backend.services.candidate_service import detect_intent_conflicts

#: 契约 §7：去空白后 1–2000 字符
MAX_QUERY_LENGTH = 2000


class IntentParseError(Exception):
    """意图解析失败；kind 映射 api.v0.1 §1.2 错误码。"""

    #: kind -> (http, error.code, retryable)
    _KIND_MAP = {
        "invalid_query": (422, "VALIDATION_ERROR", False),
        "invalid_output": (502, "LLM_INVALID_OUTPUT", False),
        "timeout": (504, "LLM_TIMEOUT", True),
        "unavailable": (503, "LLM_UNAVAILABLE", True),
    }

    def __init__(self, kind: str, message: str, details: list[dict] | None = None) -> None:
        super().__init__(message)
        if kind not in self._KIND_MAP:
            raise ValueError(f"未知解析错误类型: {kind}")
        self.kind = kind
        self.message = message
        self.details = details or []

    @property
    def api_status(self) -> int:
        return self._KIND_MAP[self.kind][0]

    @property
    def api_code(self) -> str:
        return self._KIND_MAP[self.kind][1]

    @property
    def retryable(self) -> bool:
        return self._KIND_MAP[self.kind][2]


class _HarnessLike(Protocol):
    def run_intent_turn(self, prompt: str): ...


# ---------------------------------------------------------------------------
# 原句证据：规则模式和 LLM 护栏使用同一有限语义判断
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"[，。；、！？,;!?\n]+")


def _split_segments(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


_BATTERY_RE = re.compile(
    r"(?P<approx_lead>大约|约)?\s*(?:续航|(?:一次|每次|单次|连续)(?:地)?(?:连续)?(?:听(?:音)?|播放))"
    r"(?P<direction>至少|不少于|不低于|要|得|最多|至多|不超过|不高于|小于|低于|少于|不足|不到|不满)?"
    r"\s*(?P<approx_prefix>大约|约)?\s*"
    r"(?P<hours>\d+(?:\.\d+)?)\s*(?:个)?小时"
    r"(?P<minimum>以上|往上|起|\+)?(?P<approx>左右|上下|附近)?"
)
_BATTERY_NOT_REQUIRED_RE = re.compile(
    r"(?:不需要|不要求|不强求|不必要|无需|无须|不用|不必|不是必须|并非必须|不要|别要?)"
    r"\s*(?:(?:达到|满足|保持|实现|提供|连续|持续|有|要|能|能够|进行|听|播放|续航)\s*)*$"
)
_BATTERY_NOT_REQUIRED_SUFFIX_RE = re.compile(
    r"^\s*(?:不需要|不要求|不强求|无需|无须|不用|不必|不是必须|并非必须|不要|别要?)"
    r"(?:\s*(?:达到|满足|保持|实现|提供|连续|持续|有|要|能|能够|进行|听|播放|续航))*"
)
_BATTERY_SOFT_RE = re.compile(
    r"(?:最好|尽量|优先|希望|想要?|倾向于)"
    r"(?:(?:能够|可以|能|做到|达到|保持|实现|连续|有|要|听|播放)\s*)*$"
)
_BATTERY_SOFT_SUFFIX_RE = re.compile(r"^\s*(?:最好|尽量|优先|更好|为佳)")
_BATTERY_UPPER_PREFIX_RE = re.compile(
    r"(?:最多|至多|不超过|不高于|小于|低于|少于|不足|不到|不满|上限(?:为|是)?)"
    r"\s*(?:(?:一次|每次|单次|连续|听|播放|续航|达到|保持)\s*)*$"
)
_BATTERY_UPPER_SUFFIX_RE = re.compile(
    r"^\s*(?:以内|之内|以下|之下|不到|不超过|至多|最多|不高于|小于|低于)"
)
_BATTERY_SINGLE_LISTEN_RE = re.compile(r"(?:一次|每次|单次|连续)(?:地)?(?:连续)?(?:听(?:音)?|播放)")
_BATTERY_SINGLE_PREFIX_RE = re.compile(r"(?:一次|每次|单次|连续)(?:地)?(?:连续)?(?:充电)?\s*$")
_BATTERY_CASE_NEGATED_PREFIX_RE = re.compile(
    r"(?:不|无|非|排除|不接受|不要|不需要|不要求|不强求|不必|无需|无须|不用)\s*$"
)


def _battery_evidence(
    text: str, anc: bool | None,
) -> tuple[str, float | None, str | None, str | None]:
    matches = list(_BATTERY_RE.finditer(text))
    if not matches:
        return ("soft", None, None, None) if re.search(r"续航长|续航久", text) else ("none", None, None, None)

    anc_state = {True: "on", False: "off", None: "not_applicable"}[anc]
    case_markers = []
    for marker in re.finditer(r"含(?:有)?(?:充电)?盒|连同充电盒", text):
        before = _split_segments(text[:marker.start()])
        after = _SENTENCE_SPLIT_RE.split(text[marker.end():], maxsplit=1)[0]
        prefix = before[-1] if before else ""
        if (_BATTERY_CASE_NEGATED_PREFIX_RE.search(prefix)
                or _BATTERY_SOFT_RE.search(prefix)
                or _BATTERY_NOT_REQUIRED_SUFFIX_RE.match(after)
                or _BATTERY_SOFT_SUFFIX_RE.match(after)):
            continue
        case_markers.append(marker)
    evidence: list[tuple[str, float, str, str]] = []
    case_owners: set[int] = set()

    def marker_distance(marker: re.Match[str], match: re.Match[str]) -> int:
        if marker.end() < match.start():
            return match.start() - marker.end()
        if match.end() < marker.start():
            return marker.start() - match.end()
        return 0

    for marker in case_markers:
        eligible = []
        for index, match in enumerate(matches):
            before = _split_segments(text[:match.start()])
            prefix = before[-1] if before else ""
            if (_BATTERY_SINGLE_LISTEN_RE.search(match.group(0))
                    or _BATTERY_SINGLE_PREFIX_RE.search(prefix)):
                continue
            if marker.end() <= match.start():
                gap = text[marker.end():match.start()]
            elif match.end() <= marker.start():
                gap = text[match.end():marker.start()]
            else:
                gap = ""
            gap = re.sub(r"[，,；;：:\s]", "", gap)
            if re.fullmatch(r"(?:的)?(?:总)?", gap):
                eligible.append((index, match))
        if eligible:
            owner = min(
                eligible,
                key=lambda pair: marker_distance(marker, pair[1]),
            )[0]
            case_owners.add(owner)

    for index, match in enumerate(matches):
        hours = float(match.group("hours"))
        before = _split_segments(text[:match.start()])
        after = _SENTENCE_SPLIT_RE.split(text[match.end():], maxsplit=1)[0]
        prefix = before[-1] if before else ""
        basis = "with_case" if index in case_owners else "single_charge"

        if (_BATTERY_NOT_REQUIRED_RE.search(prefix)
                or _BATTERY_NOT_REQUIRED_SUFFIX_RE.match(after)):
            kind = "not_required"
        elif (match.group("approx_lead") or match.group("approx_prefix") or match.group("approx")
              or _BATTERY_SOFT_RE.search(prefix) or _BATTERY_SOFT_SUFFIX_RE.match(after)):
            kind = "soft"
        elif (match.group("direction") in {
                  "最多", "至多", "不超过", "不高于", "小于", "低于", "少于", "不足", "不到", "不满",
              }
              or _BATTERY_UPPER_PREFIX_RE.search(prefix)
              or _BATTERY_UPPER_SUFFIX_RE.match(after)):
            kind = "unsupported"
        elif hours <= 0 or not math.isfinite(hours):
            kind = "unsupported"
        else:
            kind = "hard"
        evidence.append((kind, hours, basis, anc_state))

    unsupported = [item for item in evidence if item[0] == "unsupported"]
    hard = [item for item in evidence if item[0] == "hard"]
    if unsupported:
        return "unsupported", max(item[1] for item in unsupported), None, anc_state
    if hard:
        if len({(item[2], item[3]) for item in hard}) > 1:
            return "unsupported", max(item[1] for item in hard), None, anc_state
        _, hours, basis, state = max(hard, key=lambda item: item[1])
        return "hard", hours, basis, state
    soft = [item for item in evidence if item[0] == "soft"]
    if soft:
        _, hours, basis, state = max(soft, key=lambda item: item[1])
        return "soft", hours, basis, state
    not_required = [item for item in evidence if item[0] == "not_required"]
    if not_required:
        _, hours, basis, state = max(not_required, key=lambda item: item[1])
        return "not_required", hours, basis, state
    return "none", None, None, None


@dataclass(frozen=True)
class QueryAnchors:
    """有限原句证据；显式不强求/偏好的 null 与未知 null 分开保护。"""

    budget_cents: int | None = None
    foreign_currency: bool = False
    wireless: bool | None = None
    anc: bool | None = None
    microphone: bool | None = None
    battery_requirement: tuple[float, str, str] | None = None
    ear_tip_requirements: tuple[str, ...] | None = None
    pc_interface: PC35mmInterface = "unmentioned"
    explicit_null_fields: tuple[str, ...] = ()
    conflicting_fields: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()


def extract_query_anchors(query: str) -> QueryAnchors:
    budget = budget_evidence(query)
    features = boolean_evidence(query)
    battery_kind, battery_hours, battery_basis, battery_anc_state = _battery_evidence(
        query, features["anc"].value
    )
    pc_interface = pc_35mm_interface(query)
    paths = {"wireless": "hard_constraints.wireless",
             "anc": "hard_constraints.active_noise_cancelling",
             "microphone": "hard_constraints.microphone"}
    battery_path = "hard_constraints.battery_requirement"
    tip_features = ear_tip_evidence(query)
    tip_path = "hard_constraints.ear_tip_sizes"
    tip_requirements = tuple(
        size for size, evidence in tip_features.items()
        if evidence.value is True and not evidence.conflicting
    )
    tip_expressed = any(evidence.expressed for evidence in tip_features.values())
    tip_conflicting = any(evidence.conflicting for evidence in tip_features.values())
    tip_unsupported = any(
        not evidence.conflicting and (evidence.value is False or evidence.preferred)
        for evidence in tip_features.values()
    )
    return QueryAnchors(
        budget_cents=budget.max_cents,
        foreign_currency=budget.foreign,
        wireless=features["wireless"].value,
        anc=features["anc"].value,
        microphone=features["microphone"].value,
        battery_requirement=(battery_hours, battery_basis, battery_anc_state)
        if battery_kind == "hard" else None,
        ear_tip_requirements=tip_requirements or None,
        pc_interface=pc_interface,
        explicit_null_fields=tuple(paths[f] for f, e in features.items()
                                   if e.expressed and e.value is None)
        + ((tip_path,) if tip_expressed and not tip_requirements and not tip_conflicting else ())
        + ((battery_path,) if battery_kind != "hard" else ()),
        conflicting_fields=tuple(
            (["budget.max_cents"] if budget.conflicting else [])
            + [paths[f] for f, e in features.items() if e.conflicting]
            + ([tip_path] if tip_conflicting else [])
            + (["hard_constraints.device_requirements"] if pc_interface == "conflicting" else [])),
        unsupported_fields=tuple(
            ([tip_path] if tip_unsupported else [])
            + ([battery_path] if battery_kind == "unsupported" else [])),
    )

def _verify_against_anchors(intent: ShoppingIntent, anchors: QueryAnchors) -> None:
    """模型转译不得删除/放大/矛盾原话的明确条件（S31-01/RE31-01/06）。"""
    violations: list[dict] = []
    if anchors.budget_cents is not None and intent.budget.max_cents != anchors.budget_cents:
        violations.append({"field": "budget.max_cents", "reason": "anchor_violation"})
    for field, expected, actual in (
        ("hard_constraints.wireless", anchors.wireless, intent.hard_constraints.wireless),
        ("hard_constraints.active_noise_cancelling", anchors.anc,
         intent.hard_constraints.active_noise_cancelling),
        ("hard_constraints.microphone", anchors.microphone,
         intent.hard_constraints.microphone),
    ):
        if field in anchors.conflicting_fields:
            continue  # 矛盾原句先请求澄清，不能选其中一项后进入排序。
        if (expected is not None or field in anchors.explicit_null_fields) and actual != expected:
            violations.append({"field": field, "reason": "anchor_violation"})
    battery_field = "hard_constraints.battery_requirement"
    if battery_field not in anchors.conflicting_fields:
        requirement = intent.hard_constraints.battery_requirement
        if anchors.battery_requirement is not None:
            expected_hours, expected_basis, expected_anc_state = anchors.battery_requirement
            actual = None if requirement is None else (
                requirement.min_hours, requirement.basis, requirement.anc_state
            )
            if (actual != (expected_hours, expected_basis, expected_anc_state)
                    or requirement is None or requirement.mode_id is not None
                    or requirement.conditions is not None):
                violations.append({"field": battery_field, "reason": "anchor_violation"})
        elif battery_field in anchors.explicit_null_fields and requirement is not None:
            violations.append({"field": battery_field, "reason": "anchor_violation"})
    mic_field = "hard_constraints.microphone"
    if (mic_field not in anchors.conflicting_fields
            and (anchors.microphone is not None or mic_field in anchors.explicit_null_fields)):
        if any(d.microphone_required is not (anchors.microphone is True)
               for d in intent.hard_constraints.device_requirements or []):
            violations.append({"field": "hard_constraints.device_requirements", "reason": "anchor_violation"})
    tip_field = "hard_constraints.ear_tip_sizes"
    if tip_field not in anchors.conflicting_fields:
        actual_sizes = intent.hard_constraints.ear_tip_sizes
        if anchors.ear_tip_requirements is not None:
            if actual_sizes is None or set(actual_sizes) != set(anchors.ear_tip_requirements):
                violations.append({"field": tip_field, "reason": "anchor_violation"})
        elif tip_field in anchors.explicit_null_fields and actual_sizes is not None:
            violations.append({"field": tip_field, "reason": "anchor_violation"})
    for field in anchors.unsupported_fields:
        if not any(c.field == field and c.reason == "unsupported_requirement"
                   for c in intent.clarifications):
            violations.append({"field": field, "reason": "anchor_violation"})
    device_field = "hard_constraints.device_requirements"
    if anchors.pc_interface in {"trrs", "split", "unspecified", "unsupported"}:
        expected_key = {
            "trrs": "通用PC（3.5mm TRRS复合口）",
            "split": "通用PC（分离3.5mm音频/麦克风接口）",
        }.get(anchors.pc_interface)
        pc_devices = [d for d in intent.hard_constraints.device_requirements or [] if d.platform == "windows"]
        if not pc_devices or any(d.device_key != expected_key or d.transport != "wired_3_5mm"
                                 or not d.playback_required for d in pc_devices):
            violations.append({"field": device_field, "reason": "anchor_violation"})
        reason = {"unspecified": "missing_device", "unsupported": "unsupported_requirement"}.get(anchors.pc_interface)
        if reason and not any(c.field == device_field and c.reason == reason for c in intent.clarifications):
            violations.append({"field": device_field, "reason": "anchor_violation"})
    if anchors.foreign_currency and not any(
        c.field == "budget.currency" for c in intent.clarifications
    ):
        # 币种不明必须澄清，不得直接按人民币解析（RE31-06）
        violations.append({"field": "budget.currency", "reason": "anchor_violation"})
    if violations:
        raise IntentParseError(
            "invalid_output",
            "模型输出的意图与原话中的明确条件不一致（删除/放大/矛盾）",
            violations,
        )

def _merge_query_clarifications(intent: ShoppingIntent, anchors: QueryAnchors) -> ShoppingIntent:
    """原句中相互矛盾的显式要求不能由模型或规则静默选择。"""
    existing = {(c.field, c.reason) for c in intent.clarifications}
    extra = [Clarification(
        field=field, reason="conflicting_constraints",
        question="这项需求包含互相冲突的明确条件，请确认采用哪一项。",
    ) for field in anchors.conflicting_fields
        if (field, "conflicting_constraints") not in existing]
    extra.extend(Clarification(
        field=field,
        reason="unsupported_requirement",
        question="当前筛选只支持必须配套的耳塞套尺寸，不能表达排除或偏好某个尺寸，请确认是否有必须尺寸。",
    ) for field in anchors.unsupported_fields
        if (field, "unsupported_requirement") not in existing)
    pc_reason = {"unspecified": "missing_device", "unsupported": "unsupported_requirement"}.get(anchors.pc_interface)
    if pc_reason and ("hard_constraints.device_requirements", pc_reason) not in existing:
        extra.append(Clarification(
            field="hard_constraints.device_requirements", reason=pc_reason,
            question="电脑是单一3.5mm耳麦复合接口（TRRS），还是分离的耳机口与麦克风口？",
        ))
    if extra:
        return intent.model_copy(update={"clarifications": [*intent.clarifications, *extra]})
    return intent


# ---------------------------------------------------------------------------
# JSON 提取与类型严格化（S31-07/RE31-03/04）
# ---------------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"包含非法数值常量: {name}")


def _no_duplicate_keys(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"JSON 对象存在重复键: {key}")
        seen[key] = value
    return seen


#: 拒绝 NaN/Infinity + 拒绝重复键的解码器（解析根对象用）
_STRICT_DECODER = json.JSONDecoder(
    parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys
)
#: 标准解码器：仅用于探测"是否存在另一个 JSON 对象"（余文里的重复键
#: 对象也是对象，不能因为严格解码失败而漏判多根，RE31-03）
_LENIENT_DECODER = json.JSONDecoder()

#: 契约 §1：业务日期 YYYY-MM-DD（RE31-04：拒绝时间戳/带时间的 ISO）
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def extract_json_object(text: str) -> dict:
    """从模型文本中提取唯一 JSON 对象（S31-07/RE31-03）。

    协议：全文只允许一个根对象，可外包围栏代码块或说明文字。拒绝：
    数组根（含围栏内的 `[P]`，不得在数组里挑对象）、围栏外/尾部第二
    对象、根对象或其余对象含重复键、NaN/Infinity。
    """
    if not text or not text.strip():
        raise ValueError("模型响应为空")
    stripped = text.strip()

    root_position = None
    for index, char in enumerate(stripped):
        if char == "{":
            root_position = index
            break
        if char == "[":
            # 文字里也可能出现方括号：只有真能解码成数组才算数组根
            try:
                value, _end = _LENIENT_DECODER.raw_decode(stripped, index)
            except ValueError:
                continue
            if isinstance(value, list):
                raise ValueError("JSON 根不能是数组")
    if root_position is None:
        raise ValueError("未找到 JSON 对象")

    try:
        obj, end = _STRICT_DECODER.raw_decode(stripped, root_position)
    except ValueError as strict_error:
        # 严格失败但宽松可解 → 根对象本身非法（重复键/NaN），直接拒绝，
        # 不落入嵌套对象（RE31-03）
        try:
            _LENIENT_DECODER.raw_decode(stripped, root_position)
        except ValueError:
            raise strict_error from None
        raise ValueError(f"根对象含非法内容（重复键/NaN）: {strict_error}") from None

    remainder = stripped[end:]
    for match in re.finditer(r"\{", remainder):
        try:
            extra, _extra_end = _LENIENT_DECODER.raw_decode(remainder, match.start())
        except ValueError:
            continue
        if isinstance(extra, dict):
            raise ValueError("存在多个 JSON 对象")
    return obj


def _unwrap_annotation(annotation):
    """Annotated[X, ...] -> X（_AnnotatedAlias 用 __metadata__ 识别）。"""
    if hasattr(annotation, "__metadata__"):
        return annotation.__origin__
    return annotation


def _check_json_type(value, annotation, path: str) -> None:
    """按 JSON 语义检查标量/容器类型，堵住 Pydantic 宽松转换孔洞。

    明确拒绝：字符串数字当 int/float（"80000"）、布尔当数值（true→1）、
    字符串/整数当布尔（"true"/1→True）。date 字段要求 YYYY-MM-DD 且为
    有效日历日期（RE31-04：拒绝 "0"/时间戳/带时间的 ISO 字符串）。
    未知注解交回 Pydantic。
    """
    annotation = _unwrap_annotation(annotation)
    if value is None or annotation is type(None):
        return
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        for arg in args:
            try:
                _check_json_type(value, arg, path)
                return
            except ValueError:
                continue
        raise ValueError(f"{path}: 值类型与字段不符")
    if origin is typing.Literal:
        literal_values = typing.get_args(annotation)
        if literal_values and all(isinstance(v, bool) for v in literal_values):
            if not isinstance(value, bool):
                raise ValueError(f"{path}: 布尔字段收到非布尔值")
        elif literal_values and all(isinstance(v, str) for v in literal_values):
            if not isinstance(value, str):
                raise ValueError(f"{path}: 枚举字段收到非字符串值")
        return
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{path}: 字段需要数组")
        (item_annotation,) = typing.get_args(annotation) or (typing.Any,)
        for index, item in enumerate(value):
            _check_json_type(item, item_annotation, f"{path}[{index}]")
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, dict):
            raise ValueError(f"{path}: 字段需要对象")
        _ensure_json_types(value, annotation, path)
        return
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path}: 布尔字段收到非布尔值")
    elif annotation is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: 整数字段收到非整数（或布尔）值")
    elif annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: 数值字段收到非数值（或布尔）值")
    elif annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{path}: 字符串字段收到非字符串值")
    elif annotation is date:
        if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
            raise ValueError(f"{path}: 日期字段需要 YYYY-MM-DD 字符串")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{path}: 非法日期 {value}") from error


def _ensure_json_types(payload: dict, model_cls: type[BaseModel], path: str = "intent") -> None:
    for name, field in model_cls.model_fields.items():
        if name in payload:
            _check_json_type(payload[name], field.annotation, f"{path}.{name}")


def _require_complete_payload(payload: dict) -> None:
    """LLM 输出协议要求全部键在场（S31-01）：缺键=转译不完整，拒绝。

    内部直接构造 ShoppingIntent 不经过本检查，保留既有便利。
    """
    def _check(obj, model_cls, label) -> None:
        if not isinstance(obj, dict):
            raise ValueError(f"{label} 需要对象")
        missing = sorted(set(model_cls.model_fields) - set(obj))
        if missing:
            raise ValueError(f"{label} 缺少必需字段: {', '.join(missing)}")

    _check(payload, ShoppingIntent, "intent")
    _check(payload.get("budget"), Budget, "intent.budget")
    _check(payload.get("hard_constraints"), HardConstraints, "intent.hard_constraints")
    _check(payload.get("preferences"), Preferences, "intent.preferences")


def _pydantic_details(error: ValidationError) -> list[dict]:
    details = []
    for err in error.errors():
        parts = err.get("loc", ())
        if err.get("type") == "extra_forbidden":
            parts = parts[:-1]  # 未知模型键是自由文本；只报告其已知父字段。
        loc = ".".join(str(part) for part in parts)
        details.append({"field": loc or "intent", "reason": "invalid_intent_field"})
    return details


def _merge_conflict_clarifications(intent: ShoppingIntent) -> ShoppingIntent:
    """解析后立即跑 S2.1 冲突检测；模型已给出的同字段澄清不重复添加。"""
    existing = {(c.field, c.reason) for c in intent.clarifications}
    extra = [c for c in detect_intent_conflicts(intent) if (c.field, c.reason) not in existing]
    if extra:
        return intent.model_copy(
            update={"clarifications": [*intent.clarifications, *extra]}
        )
    return intent


def parse_intent_with_llm(query: str, harness: _HarnessLike) -> ShoppingIntent:
    """LLM 模式：一次 Harness 会话 → 服务端校验的 ShoppingIntent。"""
    prompt = build_intent_prompt(query)
    try:
        result = harness.run_intent_turn(prompt)
    except HarnessTimeoutError as error:
        raise IntentParseError("timeout", "需求解析超时，请重试") from error
    except HarnessUnavailableError as error:
        raise IntentParseError("unavailable", "意图解析服务不可用") from error
    if is_failed_finish_reason(result.finish_reason):
        # 双保险：适配层已拦，注入的替身也遵守同一终态契约（S31-04）
        raise IntentParseError(
            "unavailable",
            f"模型回合异常终止：{result.finish_reason}",
        )
    if isinstance(result.finish_reason, str) and result.finish_reason.strip().lower() in {
        "length", "max-tokens", "max_tokens",
    }:
        # 达到token上限时即使当前字符串恰好是完整JSON，也不能证明转译完整。
        raise IntentParseError(
            "invalid_output", "模型输出达到长度上限，需求转译未完整完成",
            [{"field": "query", "reason": "truncated_output"}],
        )

    try:
        payload = extract_json_object(result.final_response)
        _ensure_json_types(payload, ShoppingIntent)
        _require_complete_payload(payload)
    except ValueError as error:
        raise IntentParseError(
            "invalid_output", "模型响应不是可校验的意图 JSON",
            [{"field": "query", "reason": "unparseable_output"}],
        ) from error
    try:
        intent = ShoppingIntent.model_validate(payload)
    except ValidationError as error:
        raise IntentParseError(
            "invalid_output", "模型输出的意图未通过校验", _pydantic_details(error)
        ) from error
    # 明确条件的转译忠实性：删除/放大/矛盾原话条件即拒绝（S31-01/RE31-01）
    anchors = extract_query_anchors(query)
    _verify_against_anchors(intent, anchors)
    # 结构性安全不变量（schema 已强制，此处显式复核防回归）：
    # 库存检查不得被任何解析结果关闭。
    assert intent.hard_constraints.stock_required is True
    return _merge_conflict_clarifications(_merge_query_clarifications(intent, anchors))


# ---------------------------------------------------------------------------
# 规则演示模式（rule_based_demo）
# ---------------------------------------------------------------------------

_WEIGHT_RE = re.compile(
    r"(?:不超过\s*(?P<prefix>\d+(?:\.\d+)?)\s*(?:g|克|grams?)"
    r"|(?P<suffix>\d+(?:\.\d+)?)\s*(?:g|克|grams?)\s*(?:以内|之内|以下|内))"
)
_DEADLINE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]\s*(?:之前|前)")

#: 否定形态 → 排除形态（S31-08/RE31-07）：命中即只产 excluded，
#: 不与允许集假冲突；"不入耳"是排除入耳，不是开放耳要求
_NEGATED_FORM_FACTORS = (
    (re.compile(r"(?:不要|不想|不选|不接受|排除|别要?|非|不)入耳"), "in_ear"),
    (re.compile(r"(?:不要|不想|不选|不接受|排除|别要?|非|不)头戴"), "over_ear"),
    (re.compile(r"(?:不要|不想|不选|不接受|排除|别要?|非|不)开(?:放)?式"), "open_ear"),
    # "不堵耳道"按场景集 SC12“非入耳堵塞形态”翻译为排除入耳（S51-SPEC-01）
    (re.compile(r"不(?:堵|塞)(?:住)?(?:耳道|耳朵|耳洞)"), "in_ear"),
)

#: 相对/未给出确切日期的送达时间表达（S51-SPEC-01）：规则演示无法确定
#: 绝对截止日，按 SC15 验收行为先澄清日期与配送承诺口径，不静默丢弃。
_RELATIVE_DEADLINE_RE = re.compile(
    r"(?:[0-9一二两三四五六七八九十半几]+\s*天(?:之内|内|之后|后)"
    r"|明天|后天|大后天|截止前|赶在)"
)

#: 非耳机品类信号：命中且无任何耳机信号时请求澄清，不擅自改品类
_NON_HEADPHONE_HINTS = ("手机", "音箱", "音响", "键盘", "鼠标", "显示器", "电视盒子", "手表")
_HEADPHONE_HINTS = ("耳机", "耳塞", "耳麦", "头戴", "耳罩", "降噪", "真无线", "蓝牙耳机", "TWS")


def _to_cents(yuan_text: str) -> int:
    try:
        return int((Decimal(yuan_text) * 100).to_integral_value())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"金额无法换算为分: {yuan_text}") from error


@dataclass
class RuleBasedDemoParser:
    """确定性规则演示解析（显式标记 mode=rule_based_demo）。

    覆盖常见演示句式的子集，不是完整 NLU；但**已识别的输入必须正确
    处理**：否定只产排除条件、软需求只入偏好（EDGE04 不自动硬化）、
    外币请求澄清、非法日期请求澄清、明确重量硬上限绑定自身分句的
    口径（缺口径澄清，不跨口径换算）。解析不出的条件保持 null；
    冲突交由 S2.1 冲突检测。
    """

    now_year: int = 2026

    def parse(self, query: str) -> ShoppingIntent:
        hard: dict = {}
        prefs: dict = {}
        clarifications: list[Clarification] = []
        text = query.strip()
        segments = _split_segments(text)

        is_headphone = any(h in text for h in _HEADPHONE_HINTS)
        non_headphone = any(h in text for h in _NON_HEADPHONE_HINTS)
        category = "headphones" if is_headphone else None
        if not is_headphone and non_headphone:
            clarifications.append(
                Clarification(
                    field="category",
                    reason="unsupported_requirement",
                    question="本演示只支持耳机品类，请确认要买的耳机需求。",
                )
            )

        anchors = extract_query_anchors(text)
        features = boolean_evidence(text)
        budget_cents, foreign = anchors.budget_cents, anchors.foreign_currency
        if foreign:
            clarifications.append(
                Clarification(
                    field="budget.currency",
                    reason="unsupported_requirement",
                    question="本演示仅支持人民币预算，请提供人民币金额。",
                )
            )

        wireless = anchors.wireless
        if wireless is not None:
            hard["wireless"] = wireless
            if wireless is False:
                hard["allowed_transports"] = ["wired_3_5mm", "usb_audio"]
        elif features['wireless'].preferred:
            prefs["wireless"] = 0.6  # 软需求只入偏好（RE31-07）

        anc = anchors.anc
        if anc is not None:
            hard["active_noise_cancelling"] = anc
        elif features['anc'].preferred:
            prefs["anc"] = 0.6

        if anchors.microphone is not None:
            hard["microphone"] = anchors.microphone
        elif features["microphone"].preferred:
            prefs["call"] = 0.6
        if "多点" in text:
            hard["multipoint"] = True
        if re.search(r"开放式|开放耳|不堵(?:耳道|耳朵|耳洞)", text):
            if "open_ear" not in {
                factor for pattern, factor in _NEGATED_FORM_FACTORS if pattern.search(text)
            }:
                hard["open_ear"] = True
        if re.search(r"插上就能|插上能用|插上即可|不用充电|无需充电|不充电|不需要充电|免充电|不接受.{0,6}充电|无需供电", text):
            hard["requires_power"] = False

        excluded_forms = sorted(
            {factor for pattern, factor in _NEGATED_FORM_FACTORS if pattern.search(text)}
        )
        if excluded_forms:
            hard["excluded_form_factors"] = excluded_forms

        sub_categories: list[str] = []
        form_factors: list[str] = []
        if re.search(r"真无线|TWS", text, flags=re.IGNORECASE) and "in_ear" not in excluded_forms:
            sub_categories.append("true_wireless_earbuds")
            form_factors.append("in_ear")
        if re.search(r"游戏|电竞", text):
            sub_categories.append("gaming_headsets")
            prefs["gaming"] = 0.8
        if re.search(r"入耳式?|半入耳", text) and "in_ear" not in excluded_forms:
            form_factors.append("in_ear")
        if re.search(r"头戴式|头戴|耳罩式", text) and "over_ear" not in excluded_forms:
            form_factors.append("over_ear")
            sub_categories.append("over_ear_headphones")
        if sub_categories:
            hard["sub_categories"] = sorted(set(sub_categories))
        if form_factors:
            hard["allowed_form_factors"] = sorted(set(form_factors))

        battery_kind, hours, basis, anc_state = _battery_evidence(text, anc)
        if battery_kind == "hard":
            hard["battery_requirement"] = BatteryRequirement(
                min_hours=hours, basis=basis, anc_state=anc_state
            )
        elif battery_kind == "unsupported":
            clarifications.append(Clarification(
                field="hard_constraints.battery_requirement",
                reason="unsupported_requirement", question="请提供明确的续航下限，并说明是否为含盒口径。",
            ))
        elif battery_kind == "soft":
            prefs["battery"] = 0.8

        weight = _WEIGHT_RE.search(text)
        if weight:
            weight_segment = next(
                (seg for seg in segments if weight.group(0) and weight.group(0) in seg),
                "",
            )
            if re.search(r"单(?:只|耳|边)", weight_segment):
                basis = "single_earbud"
            elif re.search(r"含(?:充电)?盒", weight_segment):
                basis = "with_case"
            elif re.search(r"整副|一副|一对|两只|整机", weight_segment):
                basis = whole_weight_basis(form_factors, sub_categories)
            elif "over_ear" in form_factors and re.search(r"头戴|耳罩", weight_segment):
                basis = "headset"
            else:
                basis = None
            if basis is None:
                # 缺口径或整副品类不明时先澄清；排除的头戴不参与正向品类。
                clarifications.append(
                    Clarification(
                        field="hard_constraints.weight_requirement",
                        reason="missing_basis",
                        question="重量上限需要称量口径：单只耳塞、一副、整机还是含充电盒？",
                    )
                )
            grams = float(weight.group("prefix") or weight.group("suffix"))
            if grams <= 0 or not math.isfinite(grams):
                clarifications.append(Clarification(
                    field="hard_constraints.weight_requirement",
                    reason="unsupported_requirement", question="请提供大于零的重量上限。",
                ))
            elif basis:
                hard["weight_requirement"] = WeightRequirement(
                    max_grams=grams, basis=basis
                )
        elif re.search(r"轻便|轻巧|越轻|轻一点|尽量轻", text):
            prefs["weight"] = 0.6

        
        if re.search(r"防汗|防水", text):
            hard["sweat_use_allowed"] = True
        if re.search(r"防汗|防水|运动|健身|跑步", text):
            prefs.setdefault("fit_stability", 0.6)
        if re.search(r"(?:支持|允许|同意|可)开封(?:可退|退货|试戴)?|开封(?:可退|退货|试戴)|可试戴", text) \
                and not re.search(r"不(?:支持|允许|接受|可)?开封|开封(?:不可|不能|不)(?:可)?退", text):
            hard["opened_return_allowed"] = True
        if anchors.ear_tip_requirements:
            hard["ear_tip_sizes"] = list(anchors.ear_tip_requirements)
        if "全新" in text:
            hard["item_condition"] = "new"
        elif re.search(r"官翻|翻新", text):
            hard["item_condition"] = "refurbished"
        elif "二手" in text:
            hard["item_condition"] = "used"
        if re.search(r"同时连接\s*(?:两|2)\s*台|双设备|多设备同时", text):
            hard["min_active_devices"] = 2

        device = None
        device_mic = anchors.microphone is True
        if re.search(r"PS5|PlayStation", text, flags=re.IGNORECASE):
            device = DeviceRequirement(platform="ps5", playback_required=True, microphone_required=device_mic)
        elif re.search(r"电视|\bTV\b", text, flags=re.IGNORECASE):
            device = DeviceRequirement(platform="tv", playback_required=True, microphone_required=device_mic)
        elif re.search(r"手机", text):
            device = DeviceRequirement(platform="mobile", playback_required=True, microphone_required=device_mic)
        elif re.search(r"电脑|\bPC\b|Windows|笔记本", text, flags=re.IGNORECASE):
            if anchors.pc_interface != "unmentioned":
                device_key = {
                    "trrs": "通用PC（3.5mm TRRS复合口）",
                    "split": "通用PC（分离3.5mm音频/麦克风接口）",
                }.get(anchors.pc_interface)
                device = DeviceRequirement(
                    platform="windows", device_key=device_key,
                    transport="wired_3_5mm", playback_required=True,
                    microphone_required=device_mic,
                )
            else:
                device = DeviceRequirement(platform="windows", playback_required=True, microphone_required=device_mic)
        if device is not None:
            hard["device_requirements"] = [device]

        deadline = _DEADLINE_RE.search(text)
        if deadline:
            try:
                deadline_date = date(self.now_year, int(deadline.group(1)), int(deadline.group(2)))
            except ValueError:
                # 非法日期（如10月32日）：请求澄清，不抛原始异常（S31-08）
                deadline_date = None
                clarifications.append(
                    Clarification(
                        field="hard_constraints.delivery_requirement.deadline_date",
                        reason="unsupported_requirement",
                        question="请提供有效的送达截止日期（例如：10月30日前送到）。",
                    )
                )
            if deadline_date is not None:
                hard["delivery_requirement"] = {
                    "deadline_date": deadline_date,
                    "commitment_type": "guaranteed" if "保证" in text else None,
                }
        elif _RELATIVE_DEADLINE_RE.search(text):
            # SC15 验收行为：先澄清配送承诺口径；不能解析的截止不带约束返回成功推荐
            clarifications.append(
                Clarification(
                    field="hard_constraints.delivery_requirement.deadline_date",
                    reason="unsupported_requirement",
                    question="请提供具体的送达截止日期（例如：10月30日前送到），并说明是否接受预计到货。",
                )
            )

        # 偏好维度（0–1 重要度；规则演示给固定档位）
        if re.search(r"通勤|地铁|上班", text):
            prefs["commute"] = 0.8
        if re.search(r"音质|听感", text):
            prefs["sound_quality"] = 0.8
        if re.search(r"通话|开会", text):
            prefs["call"] = 0.7
        if re.search(r"舒适|戴久", text):
            prefs["comfort"] = 0.7
        if re.search(r"便宜|性价比|预算紧", text):
            prefs["price"] = 0.8
        if any(re.fullmatch(r"价格\s*最重要", segment) for segment in _split_segments(text)):
            prefs["price"] = 1.0
        if re.search(r"快递快|尽快到|急着用", text):
            prefs["shipping"] = 0.6

        intent = ShoppingIntent(
            category=category,
            hard_constraints=HardConstraints(**hard),
            preferences=Preferences(**prefs),
            clarifications=clarifications,
        )
        if budget_cents is not None:
            intent = intent.model_copy(
                update={"budget": Budget(max_cents=budget_cents)}
            )
        return _merge_conflict_clarifications(_merge_query_clarifications(intent, anchors))


# ---------------------------------------------------------------------------
# 模式解析与统一入口
# ---------------------------------------------------------------------------


def resolve_intent_mode(settings: Settings | None = None) -> str:
    """解析生效的意图模式（显式配置优先；未配置按密钥有无决定）。

    显式要求 llm 但缺供应商配置 → 503 LLM_UNAVAILABLE（契约 §7.5），
    不悄悄降级为规则模式。
    """
    settings = settings or load_settings()
    mode = settings.agent_intent_mode()
    if mode is None:
        return "llm" if settings.llm_api_key() else "rule_based_demo"
    if mode not in ("llm", "rule_based_demo"):
        raise IntentParseError(
            "unavailable",
            f"AGENT_INTENT_MODE 配置非法: {mode}",
            [{"field": "AGENT_INTENT_MODE", "reason": "invalid_config"}],
        )
    if mode == "llm" and settings.llm_api_key() is None:
        raise IntentParseError(
            "unavailable",
            "AGENT_INTENT_MODE=llm 但未配置 LLM_API_KEY",
            [{"field": "LLM_API_KEY", "reason": "missing_config"}],
        )
    return mode


def parse_intent(
    query: str,
    *,
    mode: str,
    harness: _HarnessLike | None = None,
) -> ShoppingIntent:
    """统一入口：按模式解析 query；query 本身的合法性在此前置校验。

    所有权（S31-03）：未注入 harness 时内部创建的客户端在返回前关闭；
    注入的客户端由调用方管理生命周期。
    """
    if not query or not query.strip():
        raise IntentParseError(
            "invalid_query", "请填写购物需求", [{"field": "query", "reason": "blank_query"}]
        )
    if len(query.strip()) > MAX_QUERY_LENGTH:
        raise IntentParseError(
            "invalid_query",
            f"购物需求超过 {MAX_QUERY_LENGTH} 字符",
            [{"field": "query", "reason": "too_long"}],
        )
    if mode == "llm":
        if harness is None:
            with ShoppingHarnessClient() as owned:
                return parse_intent_with_llm(query, owned)
        return parse_intent_with_llm(query, harness)
    if mode == "rule_based_demo":
        return RuleBasedDemoParser().parse(query)
    raise ValueError(f"未知意图模式: {mode}")
