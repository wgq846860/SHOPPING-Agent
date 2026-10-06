"""Public Demo implementation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from backend.ranking.offer_ranker import OfferScore, Recommendation
from backend.ranking.product_ranker import ProductScore
from backend.schemas.agent import ShoppingIntent
from backend.schemas.common import SWEAT_INDOOR_LIMITATION_CONDITIONS
from backend.services.candidate_service import CandidateSearch

EXPLANATION_VERSION = "explanation.v2"
MAX_RECOMMENDATIONS = 3

#: 解释语句类别（与 ExplainedRecommendation 字段一一对应；目录/格式化层共用）
STATEMENT_CATEGORIES = ("reason", "strengths", "weaknesses", "trade_offs")


@dataclass(frozen=True)
class ExpressionSpec:
    """S32R2 整改：一条确定性语句的最小内部句型描述（不进公开响应）。

    family 对应 expression_catalog 的受控模板；params 全部取自结构化事实
    对象（offer/budget/组件/政策），不从中文句子反推。build_catalog 以
    canonical 模板重渲染并核对原文，漂移时该项降级为仅原文。
    """

    family: str
    params: dict

#: 维度与单位的中文标签（展示用；数值口径以 ScoreComponent 为准）
DIMENSION_LABELS = {
    "price": "价格", "battery": "续航", "weight": "重量", "comfort": "舒适度",
    "anc": "主动降噪", "sound_quality": "音质", "call": "通话", "gaming": "游戏",
    "portability": "便携性", "commute": "通勤", "fit_stability": "佩戴稳定性",
    "wireless": "无线连接", "foldable": "可折叠",
    "shipping": "配送", "merchant_service": "商家服务", "returns": "退货政策",
    "warranty": "保修",
}
UNIT_LABELS = {
    "CNY_cent": "分", "day": "天", "month": "月", "g": "克", "h": "小时",
    "score_100": "分（0–100模拟）", "boolean": "",
}
#: 约定：含这些词的文本必须同时给出比较范围（本次…/该商品…）
SUPERLATIVE_MARKERS = ("最高", "最低", "最早", "最晚", "最便宜", "最贵", "最快", "最轻")


@dataclass(frozen=True)
class Comparison:
    other_product_id: str | None
    other_offer_id: str | None
    field: str
    difference: float  # 当前减对方
    unit: str | None
    scope: str


@dataclass(frozen=True)
class FactStatement:
    text: str
    fact_refs: list[str]
    comparison: Comparison | None = None


@dataclass
class ExplainedRecommendation:
    rank: int
    product_score: ProductScore
    recommended: OfferScore
    offer_scores: list[OfferScore]
    platform_steered: bool
    reason: list[FactStatement] = field(default_factory=list)
    strengths: list[FactStatement] = field(default_factory=list)
    weaknesses: list[FactStatement] = field(default_factory=list)
    trade_offs: list[FactStatement] = field(default_factory=list)


@dataclass
class DecisionSummary:
    candidate_count: int
    eligible_offer_count: int  # 按 offer_id 去重的合格报价数（S2A-06）
    eligible_plan_count: int   # 合格购买方案数（Offer×模式），独立字段独立命名
    returned_count: int
    comparison_scope: str
    message: str
    relaxations: list[str] = field(default_factory=list)


@dataclass
class ExplanationReport:
    recommendations: list[ExplainedRecommendation]
    decision_summary: DecisionSummary
    warnings: list[dict]
    algorithm_versions: dict[str, str]
    data_version: str | None
    explanation_version: str = EXPLANATION_VERSION
    #: S32R2：{(rank, category, index) -> ExpressionSpec}，仅供受控表达目录
    #: 使用；无 spec 的语句不支持改写，目录中只保留原文（不进公开响应）。
    expression_specs: dict[tuple[int, str, int], ExpressionSpec] = field(default_factory=dict)


def _label(dimension: str) -> str:
    return DIMENSION_LABELS.get(dimension, dimension)


def _unit(unit: str | None) -> str:
    return UNIT_LABELS.get(unit or "", unit or "")


def _offer_count(rec: Recommendation) -> int:
    """该商品合格报价数：按 offer_id 去重（同一报价多模式是一个报价，S2A-06）。"""
    return len({score.offer_id for score in rec.offer_scores})


def _component_statement(kind: str, component, score_value: float) -> FactStatement:
    raw = "" if component.raw_value is None else f"，原始值{component.raw_value}{_unit(component.unit)}"
    text = (f"{_label(component.dimension)}维度（{kind}侧）贡献{score_value:.2f}分"
            f"{raw}，权重{component.weight:.2f}。")
    return FactStatement(text, list(component.source_refs))


def _pair_statement(text: str, refs: list[str], *, other_product, other_offer, field_name,
                    difference, unit, scope) -> FactStatement:
    return FactStatement(text, refs, Comparison(other_product, other_offer, field_name,
                                                difference, unit, scope))


def _diff_word(difference: float, up: str, down: str) -> str:
    return up if difference > 0 else (down if difference < 0 else "相同")


def _reason_statements(rec: Recommendation, index: int, total: int,
                       intent: ShoppingIntent) -> tuple[list[FactStatement], dict[int, ExpressionSpec]]:
    score, offer = rec.product_score, rec.recommended
    statements: list[FactStatement] = []
    specs: dict[int, ExpressionSpec] = {}
    budget = intent.budget.max_cents
    if budget is not None:
        statements.append(FactStatement(
            f"报价{offer.offer_id}到手价{offer.landed}分，满足{budget}分预算上限。",
            [f"{offer.offer_id}.landed_price_cents", "intent.budget.max_cents"]))
        # S32R2：价格/预算字段绑定在结构化参数上，模板渲染不回读文本
        specs[0] = ExpressionSpec("budget_landed", {
            "offer_id": offer.offer_id, "landed_cents": offer.landed, "budget_cents": budget})
    offer_count = _offer_count(rec)
    if rec.platform_steered:
        # S2A-04（裁定3）：实验顺序按曝光分，适配陈述按 base_score；λ/μ 及两类贡献如实分报。
        statements.append(FactStatement(
            f"按本次实验排序位列第{index + 1}（曝光分{score.score:.2f}）；"
            f"用户适配分{score.base_score:.2f}（base_score）；"
            f"平台偏向λ={score.platform_bias}，贡献{score.bias_contribution:.2f}分；"
            f"意图扭曲μ={score.intent_distortion}，贡献{score.distortion_contribution:.2f}分。",
            [f"{score.product_id}.score_breakdown", f"{score.product_id}.base_score",
             f"{score.product_id}.bias_contribution", f"{score.product_id}.distortion_contribution"]))
    elif index == 0:
        statements.append(FactStatement(
            f"商品适配分{score.score:.2f}，为本次{total}个合格商品中最高。",
            [f"{score.product_id}.score_breakdown"]))
    else:
        statements.append(FactStatement(
            f"商品适配分{score.score:.2f}，位列本次{total}个合格商品第{index + 1}名。",
            [f"{score.product_id}.score_breakdown"]))
    # 报价选择依据按实际陈述（S2A-04/S2AR-02）："最高"按数值达到本商品合格方案的实际
    # 最高服务分判定（与 rank_offers 同分决胜口径一致）；排序第一条只是决胜结果，
    # 不是"唯一最高"的判据——所选报价并列最高时如实写"并列最高"。
    max_service = max((entry.score for entry in rec.offer_scores), default=None)
    is_service_best = max_service is not None and offer.score == max_service
    tied_plans = sum(1 for entry in rec.offer_scores if entry.score == max_service)
    if rec.platform_steered:
        if is_service_best and offer_count > 1:
            wording = "并列最高" if tied_plans > 1 else "最高"
            statements.append(FactStatement(
                f"该报价同时是该商品{offer_count}条合格报价中Offer服务分{wording}（{offer.score:.2f}）。",
                [f"{offer.offer_id}.score_breakdown"]))
        elif is_service_best:
            statements.append(FactStatement(
                f"为该商品唯一一条合格报价。",
                [f"{offer.offer_id}.offer_id"]))
        else:
            first = rec.offer_scores[0]
            statements.append(FactStatement(
                f"该报价按杠杆分选定，非该商品{offer_count}条合格报价中Offer服务分最高"
                f"（服务分最高为{first.offer_id}，{first.score:.2f}）。",
                [f"{offer.offer_id}.score", f"{first.offer_id}.score_breakdown"]))
    elif offer_count > 1:
        statements.append(FactStatement(
            f"在{score.product_id}的{offer_count}条合格报价中Offer服务分最高（{offer.score:.2f}）。",
            [f"{offer.offer_id}.score_breakdown"]))
    else:
        statements.append(FactStatement(
            f"为{score.product_id}唯一一条合格报价。",
            [f"{offer.offer_id}.offer_id"]))
    return statements, specs


def _strength_statements(rec: Recommendation) -> list[FactStatement]:
    statements: list[FactStatement] = []
    product_known = [c for c in rec.product_score.components if c.weight > 0 and not c.missing]
    for component in sorted(product_known, key=lambda c: (-c.contribution, c.dimension))[:2]:
        statements.append(_component_statement("商品", component, component.contribution))
    offer_known = [c for c in rec.recommended.components if c.weight > 0 and not c.missing]
    for component in sorted(offer_known, key=lambda c: (-c.contribution, c.dimension))[:1]:
        statements.append(_component_statement("报价", component, component.contribution))
    return statements


def _weakness_statements(rec: Recommendation) -> tuple[list[FactStatement], dict[int, ExpressionSpec]]:
    score, offer = rec.product_score, rec.recommended
    statements: list[FactStatement] = []
    specs: dict[int, ExpressionSpec] = {}
    missing = [c for c in score.components + offer.components if c.weight > 0 and c.missing]
    for component in missing:
        # 规格整缺（如 P002 无续航记录）的分量 source_refs 为空，回退指向记分组件本身
        refs = component.source_refs or [f"{score.product_id}.score_breakdown.{component.dimension}"]
        statements.append(FactStatement(
            f"{_label(component.dimension)}未知，按固定中性0.5计分（权重{component.weight:.2f}）。",
            refs))
        specs[len(statements) - 1] = ExpressionSpec("unknown_dimension", {
            "label": _label(component.dimension), "weight_text": f"{component.weight:.2f}"})
    policy = offer.plan.policies.return_policy
    if policy.opened_return_allowed is False:
        statements.append(FactStatement(
            f"该报价不支持开封退货（政策来源：{offer.plan.policies.policy_origin}）。",
            [f"{offer.offer_id}.effective_return_policy.opened_return_allowed"]))
        specs[len(statements) - 1] = ExpressionSpec("opened_return_blocked", {
            "policy_origin": offer.plan.policies.policy_origin})
    if not rec.platform_steered and len(rec.offer_scores) > 1:
        cheapest = min(rec.offer_scores, key=lambda s: (s.plan.landed, s.offer_id, s.mode_id))
        premium = offer.landed - cheapest.landed
        if premium > 0:
            statements.append(_pair_statement(
                f"到手价比该商品最低合格报价（{cheapest.offer_id}，{cheapest.landed}分）贵{premium}分。",
                [f"{offer.offer_id}.landed_price_cents", f"{cheapest.offer_id}.landed_price_cents"],
                other_product=score.product_id, other_offer=cheapest.offer_id,
                field_name="landed_price_cents", difference=premium, unit="CNY_cent",
                scope=f"该商品全部{_offer_count(rec)}条合格报价"))
    known = [c for c in score.components if c.weight > 0 and not c.missing]
    if len(known) >= 2:
        lowest = min(known, key=lambda c: (c.contribution, c.dimension))
        statements.append(FactStatement(
            f"{_label(lowest.dimension)}为该商品已评分维度中贡献最低（{lowest.contribution:.2f}分）。",
            list(lowest.source_refs)))
    # 裁定2：防汗声明带"仅室内训练"局限的整句通过硬要求，但解释须如实列出局限
    sweat = score.candidate.product.get("sweat_use_statement")
    if sweat is not None and sweat.get("conditions") == SWEAT_INDOOR_LIMITATION_CONDITIONS:
        statements.append(FactStatement(
            "防汗声明仅覆盖室内训练（厂商条件含训练后清洁），户外运动场景未获声明支持。",
            [f"{score.product_id}.sweat_use_statement.conditions"]))
    return statements, specs


def _spec_conditions(component) -> str:
    return component.condition_text or ""


def _specs_comparable(a, b, dimension: str) -> bool:
    """S2A-05：仅同 basis 且可比条件完全一致才可比较。

    续航还须同 ANC 状态、同模式；测试/范围条件须非空且整句相等——相同单位
    与 basis 不足以证明可比，不能仅凭非空条件认定两个条件相同。
    """
    if a.basis != b.basis:
        return False
    if dimension == "battery" and (a.anc_state != b.anc_state or a.mode_id != b.mode_id):
        return False
    return bool(_spec_conditions(a)) and _spec_conditions(a) == _spec_conditions(b)


def _spec_context(component, dimension: str) -> str:
    if dimension == "battery":
        return (f"口径{component.basis}、ANC状态{component.anc_state}、"
                f"模式{component.mode_id}、条件「{component.condition_text}」")
    return f"口径{component.basis}、范围「{component.condition_text}」"


def _trade_off_statements(current: ExplainedRecommendation,
                          others: list[ExplainedRecommendation]) -> list[FactStatement]:
    if not others:
        return [FactStatement("本次仅1个推荐，暂无商品间对比。", ["decision_summary.returned_count"])]
    statements: list[FactStatement] = []
    scope = f"本次{len(others) + 1}个推荐内"
    for other in others:
        cur, oth = current.recommended, other.recommended
        diff = cur.landed - oth.landed
        if diff == 0:
            price_text = f"到手价{cur.landed}分，与{other.product_score.product_id}（{oth.offer_id}）相同。"
        else:
            price_text = (f"到手价{cur.landed}分，比{other.product_score.product_id}"
                          f"（{oth.offer_id}，{oth.landed}分）{_diff_word(diff, '贵', '便宜')}{abs(diff)}分。")
        statements.append(_pair_statement(
            price_text,
            [f"{cur.offer_id}.landed_price_cents", f"{oth.offer_id}.landed_price_cents"],
            other_product=other.product_score.product_id, other_offer=oth.offer_id,
            field_name="landed_price_cents", difference=diff, unit="CNY_cent", scope=scope))
        # 适配陈述按用户适配分（base_score；正常模式下与排序分相等，实验模式下不随杠杆变动）
        score_diff = current.product_score.base_score - other.product_score.base_score
        if score_diff == 0:
            score_text = f"商品适配分与{other.product_score.product_id}相同（{current.product_score.base_score:.2f}）。"
        else:
            score_text = (f"商品适配分比{other.product_score.product_id}"
                          f"{_diff_word(score_diff, '高', '低')}{abs(score_diff):.2f}分。")
        statements.append(_pair_statement(
            score_text,
            [f"{current.product_score.product_id}.score_breakdown.base_score",
             f"{other.product_score.product_id}.score_breakdown.base_score"],
            other_product=other.product_score.product_id, other_offer=oth.offer_id,
            field_name="base_score", difference=score_diff, unit="score", scope=scope))
        for dimension, unit in (("weight", "g"), ("battery", "h")):
            a = next((c for c in current.product_score.components
                      if c.dimension == dimension and not c.missing), None)
            b = next((c for c in other.product_score.components
                      if c.dimension == dimension and not c.missing), None)
            if a is None or b is None:
                continue  # 未知不比较（契约：重量/续航须已知）
            if not _specs_comparable(a, b, dimension):
                # S2A-05：无共同协议——展示各自声明及条件，不直接算优劣差值
                statements.append(FactStatement(
                    f"{_label(dimension)}口径不可直接比较：本商品{a.raw_value}{_unit(a.unit)}"
                    f"（{_spec_context(a, dimension)}）；{other.product_score.product_id}为"
                    f"{b.raw_value}{_unit(b.unit)}（{_spec_context(b, dimension)}），条件不同，不比较优劣。",
                    list(a.source_refs) + list(b.source_refs)))
                continue
            value = a.raw_value - b.raw_value
            if dimension == "weight":
                better = "重" if value > 0 else "轻"
            else:
                better = "长" if value > 0 else "短"
            statements.append(_pair_statement(
                f"{_label(dimension)}{a.raw_value}{_unit(a.unit)}，比{other.product_score.product_id}"
                f"（{b.raw_value}{_unit(b.unit)}，同口径{a.basis}）{better}{abs(value)}{_unit(a.unit)}。",
                list(a.source_refs) + list(b.source_refs),
                other_product=other.product_score.product_id, other_offer=oth.offer_id,
                field_name=f"{dimension}.{a.basis}", difference=value, unit=unit, scope=scope))
        # S2A-07：到达早晚比较使用各自选中方案的 delivery_window.earliest
        # （由各自 observed_at + min_days 派生），不再比较无起算日的相对历时。
        cur_window, oth_window = cur.plan.delivery_window, oth.plan.delivery_window
        if cur_window.earliest and oth_window.earliest:
            days = (date.fromisoformat(cur_window.earliest)
                    - date.fromisoformat(oth_window.earliest)).days
            if days == 0:
                shipping_text = (f"最早送达日期与本次推荐内{other.product_score.product_id}（{oth.offer_id}）"
                                 f"相同（{cur_window.earliest}）。")
            else:
                shipping_text = (f"最早送达日期{cur_window.earliest}，比本次推荐内{other.product_score.product_id}"
                                 f"（{oth.offer_id}，{oth_window.earliest}）"
                                 f"{_diff_word(days, '晚', '早')}{abs(days)}天。")
            statements.append(_pair_statement(
                shipping_text,
                [f"{cur.offer_id}.delivery.min_days", f"{cur.offer_id}.delivery_window.observed_at",
                 f"{oth.offer_id}.delivery.min_days", f"{oth.offer_id}.delivery_window.observed_at"],
                other_product=other.product_score.product_id, other_offer=oth.offer_id,
                field_name="delivery_window.earliest", difference=days, unit="day", scope=scope))
    return statements


def _collect_warnings(recommendations: list[ExplainedRecommendation], insufficient: bool,
                      message: str, search: CandidateSearch | None) -> list[dict]:
    warnings: list[dict] = [{"code": "SIMULATED_DATA",
                             "message": "演示数据为模拟来源，分数与理由仅供流程验证。",
                             "field": None, "product_id": None}]
    if insufficient:
        warnings.append({"code": "INSUFFICIENT_RESULTS", "message": message,
                         "field": None, "product_id": None})
    seen: set[tuple] = set()
    for rec in recommendations:
        sources = [rec.product_score] + list(rec.offer_scores)
        for source in sources:
            for warning in source.warnings:
                key = (warning.get("code"), warning.get("product_id"), warning.get("field"))
                if warning.get("code") in ("MISSING_SOFT_METRIC", "UNSUPPORTED_PREFERENCE") and key not in seen:
                    seen.add(key)
                    warnings.append({"code": warning["code"], "message": warning.get("message", ""),
                                     "field": warning.get("field"), "product_id": warning.get("product_id")})
    if search is not None:
        for warning in search.warnings:
            if warning.get("code") == "STALE_DATA":
                warnings.append(dict(warning))
    return warnings


def build_explanations(recommendations: list[Recommendation], intent: ShoppingIntent, *,
                       search: CandidateSearch | None = None,
                       data_version: str | None = None) -> ExplanationReport:
    """Explain the ranked recommendations deterministically (top-3, ranked).

    recommendations must be the output of recommend() (one entry per candidate,
    product-ordered); candidate/offer counts are derived from it——报价数按
    offer_id 去重，方案数（Offer×模式）单独输出（S2A-06）。A
    needs_clarification search is rejected: clarification short-circuits
    ranking and must not produce recommendations (contract §7).
    """
    if search is not None and search.status != "ok":
        raise ValueError("needs_clarification 意图不进入排序与解释，先处理澄清。")
    candidate_count = len(recommendations)
    eligible_offer_count = len({score.offer_id for rec in recommendations
                                for score in rec.offer_scores})
    eligible_plan_count = sum(len(rec.offer_scores) for rec in recommendations)
    top = recommendations[:MAX_RECOMMENDATIONS]
    explained: list[ExplainedRecommendation] = []
    expression_specs: dict[tuple[int, str, int], ExpressionSpec] = {}
    for index, rec in enumerate(top):
        entry = ExplainedRecommendation(index + 1, rec.product_score, rec.recommended,
                                        rec.offer_scores, rec.platform_steered)
        entry.reason, reason_specs = _reason_statements(rec, index, candidate_count, intent)
        entry.strengths = _strength_statements(rec)
        entry.weaknesses, weakness_specs = _weakness_statements(rec)
        explained.append(entry)
        expression_specs.update(
            {(index + 1, "reason", local): spec for local, spec in reason_specs.items()})
        expression_specs.update(
            {(index + 1, "weaknesses", local): spec for local, spec in weakness_specs.items()})
    others_by_rank = {e.rank: [o for o in explained if o.rank != e.rank] for e in explained}
    for entry in explained:
        entry.trade_offs = _trade_off_statements(entry, others_by_rank[entry.rank])
    if candidate_count == 0:
        message = "没有符合硬条件的商品"
    elif candidate_count < MAX_RECOMMENDATIONS:
        message = f"只有{candidate_count}个合格商品"
    else:
        message = f"{candidate_count}个合格商品，返回前{len(top)}个"
    summary = DecisionSummary(
        candidate_count, eligible_offer_count, eligible_plan_count, len(top),
        f"本次全部{candidate_count}个合格商品和{eligible_offer_count}条报价", message, [])
    versions: dict[str, str] = {}
    if top:
        versions = {"product": top[0].product_score.algorithm_version,
                    "offer": top[0].recommended.algorithm_version,
                    "lever": top[0].product_score.lever_version}
    warnings = _collect_warnings(explained, 0 < len(top) < MAX_RECOMMENDATIONS, message, search)
    return ExplanationReport(explained, summary, warnings, versions, data_version,
                             expression_specs=expression_specs)
