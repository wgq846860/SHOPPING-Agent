"""Public Demo implementation."""
from __future__ import annotations

import json
from dataclasses import dataclass

from backend.ranking.offer_ranker import OfferScore, Recommendation
from backend.ranking.product_ranker import ProductScore
from backend.schemas.agent import ShoppingIntent
from backend.services.explanation_service import (
    ExplanationReport,
    STATEMENT_CATEGORIES,
    build_explanations,
)
from backend.services.expression_catalog import ExpressionCatalog, build_catalog

#: 兼容出口：类别元组唯一事实源在 explanation_service（S32R2 整改）
__all__ = [
    "DEFAULT_MAX_CANDIDATES", "DEFAULT_CONTEXT_CHAR_CAP", "FLOOR_CANDIDATES",
    "MAX_MAX_CANDIDATES", "MIN_MAX_CANDIDATES", "STATEMENT_CATEGORIES",
    "ExplanationContext", "format_context",
]

#: s3.md S3.2：传入候选上限 10–20（默认取下界，压缩优先）
MIN_MAX_CANDIDATES = 10
MAX_MAX_CANDIDATES = 20
DEFAULT_MAX_CANDIDATES = 10
#: 上下文渲染字符上限（含意图摘要 + 候选事实 + 受控表达目录）。
#: 实测 10 个候选（各 5–9 个评分分量）约 28–36k 字符，取 32k 让典型
#: 场景完整保留；更重的候选按级联裁剪，上限仍是硬边界。
DEFAULT_CONTEXT_CHAR_CAP = 32000
#: 截断级联中优先保留的候选数（解释对象是前 3 名）
FLOOR_CANDIDATES = 3


@dataclass(frozen=True)
class IntentFact:
    """给模型看的意图摘要：只保留已表达的条件，未表达不占上下文。"""

    category: str | None
    budget_max_cents: int | None
    expressed_hard_constraints: dict
    expressed_preferences: dict[str, float]


@dataclass(frozen=True)
class ComponentFact:
    """单个评分贡献：missing=true 时 raw_value 为 None（未知保持未知）。"""

    side: str  # "product" | "offer"
    dimension: str
    raw_value: object
    unit: str | None
    basis: str | None
    weight: float
    contribution: float
    missing: bool
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class OfferFact:
    """推荐报价（后端已定，模型不得更换）：价格、库存、配送与售后事实。"""

    offer_id: str
    merchant_id: str
    merchant_name: str | None
    mode_id: str | None
    landed_price_cents: int | None
    stock: int | None
    item_condition: str | None
    delivery_earliest: str | None
    delivery_latest: str | None
    delivery_commitment: str | None
    return_days: int | None
    opened_return_allowed: bool | None
    return_shipping_fee_cents: int | None
    warranty_months: int | None


@dataclass(frozen=True)
class CandidateFact:
    """一个已排序候选：排名、商品身份、推荐 Offer 与两组评分贡献。"""

    rank: int
    product_id: str
    brand: str | None
    product_name: str | None
    sub_category: str | None
    form_factor: str | None
    platform_steered: bool
    score: float
    base_score: float
    lever: dict | None  
    offer: OfferFact
    components: tuple[ComponentFact, ...]


@dataclass(frozen=True)
class SnippetFact:
    """S2.4 确定性事实片段：模型改写的唯一依据，一条对一条不可增删。

    fact_refs/has_comparison 是后端身份元数据，改写结果按索引原样继承
    （S32R-01：引用与比较方向不由模型输出决定）。
    """

    rank: int
    category: str
    text: str
    fact_refs: tuple[str, ...]
    has_comparison: bool


@dataclass(frozen=True)
class ExplanationContext:
    """format_context 的产物：压缩后的可渲染上下文、可见目录与截断状态。"""

    intent: IntentFact
    decision: dict
    candidates: tuple[CandidateFact, ...]
    snippets: tuple[SnippetFact, ...]
    catalog: ExpressionCatalog  # 可见目录（随裁剪级联过滤；catalog_id 一次性）
    max_candidates: int
    char_cap: int
    rendered: str            # 送入提示词的文本（始终是合法 JSON；overflow 时可能超上限）
    truncated: bool          # 候选或片段组被丢弃
    overflow: bool           # 连最小上下文（1候选+0片段）都放不下：禁止发起模型调用
    dropped_candidate_count: int
    total_candidates: int

    def snippet_counts(self) -> dict[tuple[int, str], int]:
        """(rank, 类别) -> 模型可见的片段数（整组丢弃后为 0）。"""
        counts: dict[tuple[int, str], int] = {}
        for snippet in self.snippets:
            counts[(snippet.rank, snippet.category)] = (
                counts.get((snippet.rank, snippet.category), 0) + 1
            )
        return counts

    def has_variant_items(self) -> bool:
        """可见目录中是否存在提供非原文版本的语句（无则跳过语言改写）。"""
        return any(len(item.variants) > 1 for item in self.catalog.items)


def _intent_fact(intent: ShoppingIntent) -> IntentFact:
    hard = intent.hard_constraints.model_dump(mode="json", exclude_none=True)
    preferences = intent.preferences.model_dump()
    return IntentFact(
        category=intent.category,
        budget_max_cents=intent.budget.max_cents,
        expressed_hard_constraints=hard,
        expressed_preferences={key: value for key, value in preferences.items() if value > 0},
    )


def _offer_fact(offer: OfferScore) -> OfferFact:
    plan = offer.plan
    policy = plan.policies.return_policy
    warranty = plan.policies.warranty
    merchant_name = plan.merchant.get("merchant_name")
    return OfferFact(
        offer_id=offer.offer_id,
        merchant_id=offer.merchant_id,
        merchant_name=merchant_name if isinstance(merchant_name, str) else None,
        mode_id=offer.mode_id,
        landed_price_cents=plan.landed,
        stock=plan.offer.get("stock"),
        item_condition=plan.offer.get("item_condition"),
        delivery_earliest=plan.delivery_window.earliest,
        delivery_latest=plan.delivery_window.latest,
        delivery_commitment=plan.delivery_window.commitment_type,
        return_days=policy.days,
        opened_return_allowed=policy.opened_return_allowed,
        return_shipping_fee_cents=policy.return_shipping_fee_cents,
        warranty_months=warranty.months,
    )


def _component_facts(score: ProductScore, offer: OfferScore) -> tuple[ComponentFact, ...]:
    """两组贡献：只保留权重>0 的维度（含 missing，未知必须可见）。"""

    def to_fact(side: str, component) -> ComponentFact:
        return ComponentFact(
            side=side,
            dimension=component.dimension,
            raw_value=None if component.missing else component.raw_value,
            unit=component.unit,
            basis=component.basis,
            weight=component.weight,
            contribution=component.contribution,
            missing=component.missing,
            source_refs=tuple(component.source_refs),
        )

    facts = [to_fact("product", c) for c in score.components if c.weight > 0]
    facts += [to_fact("offer", c) for c in offer.components if c.weight > 0]
    return tuple(facts)


def _candidate_fact(rec: Recommendation, rank: int) -> CandidateFact:
    score, offer = rec.product_score, rec.recommended
    product = score.candidate.product
    lever = None
    if rec.platform_steered:
        lever = {
            "platform_bias": score.platform_bias,
            "intent_distortion": score.intent_distortion,
            "bias_contribution": score.bias_contribution,
            "distortion_contribution": score.distortion_contribution,
        }
    return CandidateFact(
        rank=rank,
        product_id=score.product_id,
        brand=product.get("brand"),
        product_name=product.get("product_name"),
        sub_category=product.get("sub_category"),
        form_factor=product.get("form_factor"),
        platform_steered=rec.platform_steered,
        score=score.score,
        base_score=score.base_score,
        lever=lever,
        offer=_offer_fact(offer),
        components=_component_facts(score, offer),
    )


def _snippet_facts(report: ExplanationReport) -> tuple[SnippetFact, ...]:
    """按 rank → reason/strengths/weaknesses/trade_offs 顺序展平。

    展平顺序即整组丢弃的顺序：从尾部丢弃意味着先牺牲靠后 rank 的
    trade_offs，最后才是 rank1 的 reason——解释的核心事实最后才让位。
    """
    snippets: list[SnippetFact] = []
    for entry in report.recommendations:
        for category in STATEMENT_CATEGORIES:
            for statement in getattr(entry, category):
                snippets.append(SnippetFact(
                    rank=entry.rank, category=category, text=statement.text,
                    fact_refs=tuple(statement.fact_refs),
                    has_comparison=statement.comparison is not None,
                ))
    return tuple(snippets)


def _render(intent: IntentFact, decision: dict, candidates: tuple[CandidateFact, ...],
            snippets: tuple[SnippetFact, ...], catalog: ExpressionCatalog) -> str:
    payload = {
        "intent": {
            "category": intent.category,
            "budget_max_cents": intent.budget_max_cents,
            "expressed_hard_constraints": intent.expressed_hard_constraints,
            "expressed_preferences": intent.expressed_preferences,
        },
        "decision": decision,
        "candidates": [
            {
                "rank": c.rank, "product_id": c.product_id, "brand": c.brand,
                "product_name": c.product_name, "sub_category": c.sub_category,
                "form_factor": c.form_factor, "platform_steered": c.platform_steered,
                "score": c.score, "base_score": c.base_score, "lever": c.lever,
                "offer": {
                    "offer_id": c.offer.offer_id, "merchant_id": c.offer.merchant_id,
                    "merchant_name": c.offer.merchant_name, "mode_id": c.offer.mode_id,
                    "landed_price_cents": c.offer.landed_price_cents,
                    "stock": c.offer.stock, "item_condition": c.offer.item_condition,
                    "delivery_earliest": c.offer.delivery_earliest,
                    "delivery_latest": c.offer.delivery_latest,
                    "delivery_commitment": c.offer.delivery_commitment,
                    "return_days": c.offer.return_days,
                    "opened_return_allowed": c.offer.opened_return_allowed,
                    "return_shipping_fee_cents": c.offer.return_shipping_fee_cents,
                    "warranty_months": c.offer.warranty_months,
                },
                "score_components": [
                    {
                        "side": comp.side, "dimension": comp.dimension,
                        "raw_value": comp.raw_value, "unit": comp.unit,
                        "basis": comp.basis, "weight": comp.weight,
                        "contribution": comp.contribution, "missing": comp.missing,
                        "source_refs": list(comp.source_refs),
                    }
                    for comp in c.components
                ],
            }
            for c in candidates
        ],
        # S32R2：受控表达目录取代自由事实片段——模型只见候选措辞并选择版本
        "selection_catalog": {
            "catalog_id": catalog.catalog_id,
            "items": [
                {
                    "statement_id": item.statement_id,
                    "rank": item.rank, "category": item.category,
                    "variants": [
                        {"variant_id": variant.variant_id, "text": variant.text}
                        for variant in item.variants
                    ],
                }
                for item in catalog.items
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decision_dict(report: ExplanationReport) -> dict:
    summary = report.decision_summary
    return {
        "candidate_count": summary.candidate_count,
        "eligible_offer_count": summary.eligible_offer_count,
        "eligible_plan_count": summary.eligible_plan_count,
        "returned_count": summary.returned_count,
        "comparison_scope": summary.comparison_scope,
        "message": summary.message,
    }


def _drop_last_snippet_group(snippets: list[SnippetFact]) -> list[SnippetFact]:
    """整组丢弃最后一个 (rank, 类别) 的全部片段（保持结构完整）。"""
    if not snippets:
        return snippets
    last_key = (snippets[-1].rank, snippets[-1].category)
    cut = 0
    for index in range(len(snippets) - 1, -1, -1):
        if (snippets[index].rank, snippets[index].category) != last_key:
            cut = index + 1
            break
    return snippets[:cut]


def _visible_keys(snippets: list[SnippetFact]) -> set[tuple[int, str]]:
    return {(snippet.rank, snippet.category) for snippet in snippets}


def format_context(
    intent: ShoppingIntent,
    ranked_candidates: list[Recommendation],
    *,
    report: ExplanationReport | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    char_cap: int = DEFAULT_CONTEXT_CHAR_CAP,
) -> ExplanationContext:
    """把已排序候选压缩成 LLM 解释上下文（确定性，无模型调用）。

    ranked_candidates 必须是 recommend() 的输出（排序即名次）。report
    可传入已构建的 S2.4 解释报告；缺省时内部构建一次，事实片段与最终
    回退同源。max_candidates 只接受 10–20；char_cap 必须 > 0。返回的
    rendered 始终是合法 JSON；overflow=True 表示最小上下文也放不下，
    调用方必须跳过模型调用（S32R 观察项整改）。
    """
    if not MIN_MAX_CANDIDATES <= max_candidates <= MAX_MAX_CANDIDATES:
        raise ValueError(
            f"max_candidates 只允许 {MIN_MAX_CANDIDATES}–{MAX_MAX_CANDIDATES}：{max_candidates}"
        )
    if char_cap <= 0:
        raise ValueError(f"char_cap 必须为正整数：{char_cap}")
    if report is None:
        report = build_explanations(ranked_candidates, intent)

    total = len(ranked_candidates)
    kept = list(
        _candidate_fact(rec, rank)
        for rank, rec in enumerate(ranked_candidates[:max_candidates], start=1)
    )
    snippets = list(_snippet_facts(report))
    intent_fact = _intent_fact(intent)
    decision = _decision_dict(report)
    catalog = build_catalog(report)

    def _current_render() -> str:
        visible = catalog.filtered(_visible_keys(snippets))
        return _render(intent_fact, decision, tuple(kept), tuple(snippets), visible)

    truncated = False
    dropped = 0
    floor = min(FLOOR_CANDIDATES, len(kept))
    rendered = _current_render()
    while len(rendered) > char_cap and len(kept) > floor:
        kept.pop()
        dropped += 1
        truncated = True
        rendered = _current_render()
    while len(rendered) > char_cap and snippets:
        snippets = _drop_last_snippet_group(snippets)
        truncated = True
        rendered = _current_render()
    while len(rendered) > char_cap and len(kept) > 1:
        kept.pop()
        dropped += 1
        truncated = True
        rendered = _current_render()
    overflow = len(rendered) > char_cap

    return ExplanationContext(
        intent=intent_fact,
        decision=decision,
        candidates=tuple(kept),
        snippets=tuple(snippets),
        catalog=catalog.filtered(_visible_keys(snippets)),
        max_candidates=max_candidates,
        char_cap=char_cap,
        rendered=rendered,
        truncated=truncated,
        overflow=overflow,
        dropped_candidate_count=dropped,
        total_candidates=total,
    )
