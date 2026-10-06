"""有限原句证据：只绑定已支持的功能强度/极性和金额币种。

供规则演示与 LLM 校验共同消费；不推断商品事实，也不执行排序。
明确的无需/偏好与没有识别出条件分开，冲突留给调用方请求澄清。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Literal


_CLAUSES = re.compile(r"[，。；！？,;!?\n]+")
_FEATURE = re.compile(
    r"主动降噪|降噪|ANC|无线|蓝牙|TWS|有线|麦克风|带麦(?:克风)?|麦(?!克风)|能语音|语音|拾音",
    re.IGNORECASE,
)
_TIP_SENTENCES = re.compile(r"[。；！？.!?\n]+")
_TIP_NOUN = re.compile(r"耳塞套|耳帽|耳套")
_TIP_SIZE = re.compile(r"(?P<size>小号|小码|中号|中码|大号|大码)(?:(?:的|可替换|替换)?(?:耳塞套|耳帽|耳套))?")
_TIP_NEGATIVE_SUFFIX = re.compile(r"\s*(?:不要|不接受|不想要|排除|拒绝|别要?|不带)")
_CUES = {
    "无需": "optional", "不需要": "optional", "不要求": "optional",
    "无所谓": "optional", "不强求": "optional",
    "接受": "optional", "可以接受": "optional",
    "最好": "soft", "尽量": "soft", "优先": "soft",
    "更希望": "soft", "倾向": "soft",
    "不要": "negative", "不带": "negative", "没有": "negative",
    "不想": "negative", "不选": "negative", "不接受": "negative",
    "拒绝": "negative", "排除": "negative", "别": "negative",
    "非": "negative", "无": "negative", "不": "negative",
    "必须": "hard", "一定要": "hard", "只要": "hard",
    "只用": "hard", "需要": "hard", "要": "hard", "用": "hard",
}
_LEADING_CUE = re.compile(
    r"(" + "|".join(sorted(_CUES, key=len, reverse=True))
    + r")\s*(?:附带|带有|开启|带|有|用|能|配有|提供)?\s*$"
)
_JOIN = re.compile(r"\s*(?:(?:的)?(?:耳机|功能))?\s*(?:和|与|及|以及|并且|并|且|、|也)?\s*(?:有|带|开启)?\s*")
_OPTIONAL_SUFFIX = re.compile(r"\s*(?:功能)?\s*(?:也可以|也行|都行|无所谓|不强求|不是必须)")
_SOFT_SUFFIX = re.compile(r"\s*(?:的耳机|耳机|功能)?\s*(?:都)?(?:尽量|最好|优先)")
_TIP_REFERENCE_BRIDGE = re.compile(
    r"\s*(?:(?:是|为|的|附带|带有|带|有|尺寸|规格|小号|小码|中号|中码|大号|大码"
    r"|但|和|与|及|以及|都|可替换|替换|不是必须|"
    + "|".join(sorted(_CUES, key=len, reverse=True)) + r"|[，,、])\s*)*"
)
_TIP_ELLIPSIS_SUFFIX = re.compile(
    r"\s*(?:(?:都)?(?:不要|不接受|不想要|排除|拒绝|别要?|不带|无所谓|不强求|不是必须"
    r"|必须|尽量|最好)|[，,、]|$)"
)
_TIP_GROUP_JOIN = re.compile(
    r"\s*(?:(?:和|与|及|以及|并且|并|且|、)\s*)?(?:附带|带有|配有)?\s*"
)
_TIP_GROUP_SUFFIX = re.compile(
    r"\s*(?:都)?(?:是|为)?(?P<cue>"
    + "|".join(sorted({*_CUES, "不是必须", "也可以", "也行", "都行"}, key=len, reverse=True))
    + r")(?=\s*(?:附带|带有|配有|配套|提供|有|带|的|[，,、]|$))"
)
_MIC_PREFIX = re.compile(r"(?:麦克风|麦|通话|拾音|语音|ENC|mic)$", re.IGNORECASE)
_MIC_SUFFIX = re.compile(r"^(?:麦克风|麦|通话|拾音|语音|ENC|mic)", re.IGNORECASE)


@dataclass(frozen=True)
class BooleanEvidence:
    value: bool | None = None
    expressed: bool = False
    preferred: bool = False
    conflicting: bool = False


def boolean_evidence(text: str) -> dict[str, BooleanEvidence]:
    """修饰词绑定同一需求组；显式的新修饰词开启新组。

    组内仅经已知并列连接或直接相邻传播，不能把后面的“尽量轻”
    追溯到前面的必须功能；末尾“都行/无所谓”作用于所属并列组。
    """
    mentions: dict[str, list[tuple[str, bool]]] = {
        "wireless": [], "anc": [], "microphone": [],
    }
    for clause in _CLAUSES.split(text):
        matches = list(_FEATURE.finditer(clause))
        groups: list[list[tuple[str, bool, str]]] = []
        previous = None
        strength = "hard"
        for index, match in enumerate(matches):
            token = match.group().lower()
            if token in {"主动降噪", "降噪", "anc"}:
                field = "anc"
            elif token in {"无线", "蓝牙", "tws", "有线"}:
                field = "wireless"
            else:
                field = "microphone"
            before = clause[:match.start()]
            after = clause[match.end():]
            if field == "anc" and (_MIC_PREFIX.search(before) or _MIC_SUFFIX.match(after)):
                continue
            bridge = clause[previous.end():match.start()] if previous else before
            cue = _LEADING_CUE.search(bridge)
            connected = previous is not None and cue is None and _JOIN.fullmatch(bridge) is not None
            if cue:
                strength = _CUES[cue.group(1)]
            elif not connected:
                strength = "hard"
            if not connected:
                groups.append([])
            polarity = token != "有线"
            if strength == "negative":
                polarity = not polarity
            groups[-1].append((field, polarity, strength))
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
            suffix = clause[match.end():next_start]
            if _OPTIONAL_SUFFIX.match(suffix):
                # 不强制/可以接受不会把“最好”变成禁止，也不虚构偏好。
                groups[-1] = [(f, p, "soft" if s == "soft" else "optional")
                              for f, p, s in groups[-1]]
                strength = "optional"
            elif _SOFT_SUFFIX.match(suffix):
                groups[-1] = [(f, p, "soft") for f, p, _ in groups[-1]]
                strength = "soft"
            previous = match
        for group in groups:
            for field, polarity, kind in group:
                mentions[field].append((kind, polarity))
    result = {}
    for field, items in mentions.items():
        required = {p for kind, p in items if kind in {"hard", "negative"}}
        # 保留原有有线冲突记录中的 false，同时明确禁止执行待澄清意图。
        value = False if False in required else True if True in required else None
        result[field] = BooleanEvidence(value, bool(items),
            any(kind == "soft" and polarity for kind, polarity in items), len(required) > 1)
    return result


def ear_tip_evidence(text: str) -> dict[str, BooleanEvidence]:
    """先绑定完整尺寸名词组，再将前后修饰作用于该组的每个成员。

    名词可在并列组末尾或开头；省略指代仍须经有限桥接绑定耳塞套，
    收纳盒等其他对象会截断组。显式新修饰开启新组，不能跨组传播。
    """
    mentions: dict[str, list[tuple[str, bool]]] = {"S": [], "M": [], "L": []}
    for sentence in _TIP_SENTENCES.split(text):
        if not _TIP_NOUN.search(sentence):
            continue
        matches = list(_TIP_SIZE.finditer(sentence))
        nouns = list(_TIP_NOUN.finditer(sentence))
        groups: list[list[re.Match]] = []
        for index, match in enumerate(matches):
            if not _TIP_NOUN.search(match.group()):
                next_match = matches[index + 1] if index + 1 < len(matches) else None
                coordinated = next_match is not None and _TIP_GROUP_JOIN.fullmatch(
                    sentence[match.end():next_match.start()]) is not None
                # 裸尺寸必须接组成员或本尺寸修饰，不能借邻项名词绑定其他对象。
                if not coordinated and not _TIP_ELLIPSIS_SUFFIX.match(sentence[match.end():]):
                    continue
            if (groups and _TIP_GROUP_JOIN.fullmatch(
                    sentence[groups[-1][-1].end():match.start()])):
                groups[-1].append(match)
            else:
                groups.append([match])
        for index, group in enumerate(groups):
            start, end = group[0].start(), group[-1].end()
            if not any(_TIP_NOUN.search(match.group()) for match in group):
                preceding = [noun for noun in nouns if noun.end() <= start]
                if (not preceding
                        or not _TIP_REFERENCE_BRIDGE.fullmatch(sentence[preceding[-1].end():start])
                        or not _TIP_ELLIPSIS_SUFFIX.match(sentence[end:])):
                    continue

            cue = _LEADING_CUE.search(sentence[:start])
            strength = _CUES[cue.group(1)] if cue else "hard"
            next_start = groups[index + 1][0].start() if index + 1 < len(groups) else len(sentence)
            suffix = _TIP_GROUP_SUFFIX.match(sentence[end:next_start])
            if suffix:
                token = suffix.group("cue")
                strength = ("optional" if token in {"不是必须", "也可以", "也行", "都行"}
                            else _CUES[token])
            for match in group:
                size = match.group("size")
                canonical = "S" if size in {"小号", "小码"} else "M" if size in {"中号", "中码"} else "L"
                mentions[canonical].append((strength, strength != "negative"))

    result = {}
    for size, items in mentions.items():
        required = {polarity for kind, polarity in items if kind in {"hard", "negative"}}
        value = False if False in required else True if True in required else None
        result[size] = BooleanEvidence(
            value=value,
            expressed=bool(items),
            preferred=any(kind == "soft" and polarity for kind, polarity in items),
            conflicting=len(required) > 1,
        )
    return result


PC35mmInterface = Literal["unmentioned", "unspecified", "trrs", "split", "conflicting", "unsupported"]
_PC = re.compile(r"电脑|\bPC\b|Windows|笔记本", re.IGNORECASE)
_PORT_35MM = re.compile(r"3\.5\s*(?:mm|毫米)", re.IGNORECASE)
_PORT_KIND = re.compile(
    r"TRRS(?:\s*(?:的\s*)?(?:耳麦)?复合(?:接口|口))?|(?:耳麦)?复合(?:接口|口)|分离(?:的|式)?"
    r"(?:\s*(?:3\.5\s*(?:mm|毫米)\s*)?"
    r"(?:(?:耳机|音频)(?:输出口|口)?\s*(?:和|与|及|/)\s*麦克风(?:输入口|接口|口)"
    r"|(?:音频|麦克风)?接口|口))?",
    re.IGNORECASE,
)
_PORT_CUE = re.compile(
    r"(" + "|".join(sorted(_CUES, key=len, reverse=True))
    + r")\s*(?:3\.5\s*(?:mm|毫米)\s*)?(?:为|是)?\s*(?:3\.5\s*(?:mm|毫米)\s*)?$",
    re.IGNORECASE,
)
_PC_SENTENCES = re.compile(r"[。；！？;!?\n]+")
_PC_PRODUCT_OWNER = re.compile(
    r"(?:商品|耳机|耳麦)(?!复合(?:接口|口)|(?:输出|输入)?口)\s*(?:的\s*)?"
    r"(?:接头|插头|接口|参数|使用|采用|具备|配有|是|为|有|TRRS)"
    r"|(?:是|为|属于)\s*(?:商品|耳机|耳麦)(?:的\s*(?:接头|插头|接口|参数))?"
    r"|(?:(?:" + "|".join(sorted(_CUES, key=len, reverse=True)) + r")\s*(?:带|有|要)?\s*)?"
    r"(?:" + _PORT_KIND.pattern + r")\s*(?:插头|接头|接口)?\s*(?:的\s*)?"
    r"(?:商品|耳机|耳麦)(?!复合(?:接口|口)|(?:输出|输入)?口)",
    re.IGNORECASE,
)
_PC_DEVICE_OWNER = re.compile(
    r"(?:电脑|\bPC\b|Windows|笔记本)\s*(?:的\s*)?"
    r"(?:有|只有|具备|配有|使用|采用|是|为|接口|口型|3\.5)"
    r"|(?:是|为|属于)\s*(?:我(?:的)?|这台|目标)?\s*(?:电脑|\bPC\b|Windows|笔记本)"
    r"|(?:有|具备|配有)[^，。；！？,;!?\n]*(?:的)?(?:电脑|\bPC\b|Windows|笔记本)",
    re.IGNORECASE,
)
_PC_NEW_DESCRIPTION = (
    r"(?:卖家说\s*)?(?:" + _PC_DEVICE_OWNER.pattern + "|" + _PC_PRODUCT_OWNER.pattern
    + r"|(?:3\.5\s*(?:mm|毫米)\s*)?(?:" + _PORT_KIND.pattern
    + r")\s*(?:插头|接头|接口|口型)?\s*(?:是|为|属于)\s*"
    + r"(?:(?:我(?:的)?|这台|目标)?\s*(?:电脑|\bPC\b|Windows|笔记本)|商品|耳机|耳麦))"
)
_PC_DESCRIPTIONS = re.compile(
    r"[，,]+|但是|不过|然后|同时|但|而|且|(?:以及|和|与|及|并)(?=\s*(?:"
    + _PC_NEW_DESCRIPTION + r"))",
    re.IGNORECASE,
)
_PC_PORT_PREDICATE = re.compile(
    r"^\s*(?:(?:音频|麦克风|3\.5\s*(?:mm|毫米))?接口|口型|输出口|输入口)\s*(?:是|为|有)",
    re.IGNORECASE,
)
_PC_IDENTITY_PREDICATE = re.compile(r"^\s*(?:是|为|有|具备)\s*$")
_PC_NEGATIVE_SUFFIX = re.compile(r"\s*(?:接口|插头|接头)?\s*(?:都)?(?:不要|不接受|不想要|排除|拒绝|别要?|不带)")
_PC_OPTIONAL_SUFFIX = re.compile(r"\s*(?:接口|插头|接头)?\s*(?:都)?(?:也可以|也行|都行|无所谓|不强求|不是必须)")
_PC_SOFT_SUFFIX = re.compile(r"\s*(?:接口|插头|接头)?\s*(?:都)?(?:尽量|最好|优先)")
_PC_PORT_GROUP_JOIN = re.compile(
    r"\s*(?:接口|插头|接头|口)?\s*(?:以及|并且|和|与|及|并|且|、)\s*"
    r"(?:3\.5\s*(?:mm|毫米)\s*)?",
    re.IGNORECASE,
)
_PC_UNKNOWN_PORT = re.compile(r"还是|不知道|不确定|可能")


def _pc_port_strength(description: str, match: re.Match, group_end: int | None = None) -> str:
    cue = _PORT_CUE.search(description[:match.start()])
    strength = _CUES[cue.group(1)] if cue else "hard"
    suffix = description[match.end() if group_end is None else group_end:]
    if _PC_OPTIONAL_SUFFIX.match(suffix):
        return "optional"
    if _PC_NEGATIVE_SUFFIX.match(suffix):
        return "negative"
    if _PC_SOFT_SUFFIX.match(suffix):
        return "soft"
    return strength


def _pc_port_groups(description: str, matches: list[re.Match]) -> list[list[re.Match]]:
    groups: list[list[re.Match]] = []
    for match in matches:
        if groups and _PC_PORT_GROUP_JOIN.fullmatch(
                description[groups[-1][-1].end():match.start()]):
            groups[-1].append(match)
        else:
            groups.append([match])
    return groups


def _pc_port_continuation(description: str, groups: list[list[re.Match]]) -> bool:
    """省略主体须仍是明确口型谓语/修饰，裸TRRS描述不继承电脑主体。"""
    if _PC_PORT_PREDICATE.match(description):
        return True
    if not groups:
        return False
    first = groups[0][0]
    before = description[:first.start()]
    return bool(
        _PORT_CUE.search(before)
        or _PC_IDENTITY_PREDICATE.fullmatch(before)
        or _PC_UNKNOWN_PORT.search(before)
        or _pc_port_strength(description, first, groups[0][-1].end()) != "hard"
    )


def pc_35mm_interface(text: str) -> PC35mmInterface:
    """先确认完整插头描述归属，再共享并列口型组的前后修饰。"""
    if not _PC.search(text):
        return "unmentioned"
    positive: set[str] = set()
    negative: set[str] = set()
    expressed = False
    shape_confirmed = False
    uncertain = False
    for sentence in _PC_SENTENCES.split(text):
        owner = "unknown"
        for description in _PC_DESCRIPTIONS.split(sentence):
            matches = list(_PORT_KIND.finditer(description))
            groups = _pc_port_groups(description, matches)
            # 整个正序/倒装描述属于商品时，不先采TRRS前半句再截断。
            # 商品提及电脑作为连接对象也不提供电脑口型。
            if _PC_PRODUCT_OWNER.search(description):
                owner = "product"
                continue
            if _PC_DEVICE_OWNER.search(description):
                owner = "pc"
            elif owner != "pc" or not _pc_port_continuation(description, groups):
                if matches:
                    owner = "unknown"
                continue
            if _PORT_35MM.search(description) and re.search(r"接口|输出口|输入口|复合口|TRRS", description, re.IGNORECASE):
                shape_confirmed = True
            if _PC_UNKNOWN_PORT.search(description) and matches:
                uncertain = True
                continue
            for group in groups:
                strength = _pc_port_strength(description, group[0], group[-1].end())
                for match in group:
                    expressed = True
                    kind = "split" if match.group().startswith("分离") else "trrs"
                    if strength == "negative":
                        negative.add(kind)
                    elif strength == "hard":
                        positive.add(kind)
    if not shape_confirmed:
        return "unmentioned"
    if uncertain:
        return "unspecified"
    if len(positive) > 1 or positive & negative:
        return "conflicting"
    if positive:
        return "split" if "split" in positive else "trrs"
    return "unsupported" if expressed else "unspecified"

_NUMBER = r"\d+(?:\.\d+)?"
_FOREIGN = r"美元|美金|欧元|英镑|日元|港币|港圆|加元|澳元|卢布|USD|EUR|JPY|GBP|HKD|CAD|AUD|\$|＄"
_FOREIGN_AMOUNT = re.compile(
    rf"(?:(?:{_FOREIGN})\s*(?:预算|报价)?\s*{_NUMBER}|{_NUMBER}\s*(?:{_FOREIGN}))", re.IGNORECASE
)
_FOREIGN_AFTER = re.compile(rf"\s*(?:{_FOREIGN})", re.IGNORECASE)
_FOREIGN_BEFORE = re.compile(rf"(?:{_FOREIGN})\s*(?:预算|报价)?\s*$", re.IGNORECASE)
_AMOUNT_EXCLUDED_BEFORE = re.compile(r"(?:不要|不买|不收|不选|排除|拒绝|别)\s*$")
_AMOUNT_EXCLUDED_AFTER = re.compile(r"\s*(?:的)?(?:报价|价格)?\s*(?:不要|不收|不选|排除|拒绝)")
_RANGE = re.compile(r"\d+\s*(?:到|至|~|～|－|–|—|-)\s*\d+")
_NON_MONEY = re.compile(r"\s*(?:克|g|公斤|kg|斤|小时|h|天|日|分钟|秒|台|个|件|只|副|人|年|月|次|度|mAh|db|分贝|欧|Ω|赫兹|hz)", re.IGNORECASE)
_BUDGETS = (
    re.compile(rf"({_NUMBER})\s*(?:元|块)?\s*(?:以内|之内|以下|内)"),
    re.compile(rf"不超过\s*({_NUMBER})"),
    re.compile(rf"预算\s*({_NUMBER})"),
)
_BUDGET_REPLACEMENT = re.compile(
    rf"(?:预算\s*)?(?:改为|改成|调整为|改到)\s*({_NUMBER})\s*(?:元|块)"
)


@dataclass(frozen=True)
class BudgetEvidence:
    max_cents: int | None = None
    foreign: bool = False
    conflicting: bool = False


def budget_evidence(text: str) -> BudgetEvidence:
    """外币必须属于完整金额表达；非金额阈值不能遮挡另一项预算。"""
    amounts: set[int] = set()
    foreign = False
    for clause in _CLAUSES.split(text):
        for amount in _FOREIGN_AMOUNT.finditer(clause):
            if (_AMOUNT_EXCLUDED_BEFORE.search(clause[:amount.start()])
                    or _AMOUNT_EXCLUDED_AFTER.match(clause[amount.end():])):
                continue
            foreign = True
        ranges = list(_RANGE.finditer(clause))
        found: dict[int, tuple[int, bool]] = {}
        for pattern in (*_BUDGETS, _BUDGET_REPLACEMENT):
            for match in pattern.finditer(clause):
                start, end = match.span(1)
                if any(r.start() <= start < r.end() for r in ranges):
                    continue  # 区间下界/上界交给语言解析，不以其中一个值作锚点。
                after = clause[end:]
                before = clause[:start]
                if (_FOREIGN_AFTER.match(after) or _FOREIGN_BEFORE.search(before)
                        or _NON_MONEY.match(after)):
                    continue
                try:
                    cents = int((Decimal(match.group(1)) * 100).to_integral_value())
                    replacing = pattern is _BUDGET_REPLACEMENT
                    previous = found.get(start)
                    found[start] = (cents, replacing or bool(previous and previous[1]))
                except (InvalidOperation, ValueError):
                    continue
        for _, (cents, replacing) in sorted(found.items()):
            if replacing:
                amounts.clear()
            amounts.add(cents)
    return BudgetEvidence(next(iter(amounts)) if len(amounts) == 1 else None,
                          foreign, len(amounts) > 1)


def whole_weight_basis(form_factors: list[str], sub_categories: list[str]) -> str | None:
    """整副口径使用已确认的正向品类；排除词不参与这个选择。"""
    headset = "over_ear" in form_factors or "gaming_headsets" in sub_categories
    earbuds = "in_ear" in form_factors or any(
        c in {"true_wireless_earbuds", "wireless_earbuds"} for c in sub_categories)
    if headset == earbuds:
        return None
    return "headset" if headset else "pair"
