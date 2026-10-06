"""Public Demo implementation."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from backend.services.explanation_service import (
    ExplanationReport,
    ExpressionSpec,
    STATEMENT_CATEGORIES,
)

#: 内部协议版本（交接文档建议名）
CATALOG_PROTOCOL_VERSION = "explanation-selection.v1"
CANONICAL_VARIANT_ID = "canonical"

#: 受控模板表：canonical 用于与确定性原文核对；其余为同命题异措辞。
#: 参数名即事实槽位，渲染时全部来自 ExpressionSpec.params。
FAMILY_TEMPLATES: dict[str, dict[str, str]] = {
    "budget_landed": {
        CANONICAL_VARIANT_ID: "报价{offer_id}到手价{landed_cents}分，满足{budget_cents}分预算上限。",
        "plain": "报价{offer_id}的到手价为{landed_cents}分，未超过{budget_cents}分预算上限。",
        "budget_first": "预算上限{budget_cents}分，报价{offer_id}到手价{landed_cents}分，仍在预算内。",
    },
    "unknown_dimension": {
        CANONICAL_VARIANT_ID: "{label}未知，按固定中性0.5计分（权重{weight_text}）。",
        "plain": "{label}信息缺失，以固定中性值0.5参与计分，权重{weight_text}。",
        # S32R3-01：0.5 是 normalized_score（0–1），不是对 0–100 总分的贡献
        # （contribution = 0.5×weight×100）；措辞必须写明"归一化值参与计分"，
        # 不得出现"0.5 分计入总分"一类口径。
        "reason_first": "因{label}未知，本维度以中性归一化值0.5参与计分（权重{weight_text}）。",
    },
    "opened_return_blocked": {
        CANONICAL_VARIANT_ID: "该报价不支持开封退货（政策来源：{policy_origin}）。",
        "plain": "开封使用后无法退货，政策依据为{policy_origin}。",
    },
}


@dataclass(frozen=True)
class CatalogVariant:
    variant_id: str
    text: str


@dataclass(frozen=True)
class CatalogItem:
    """一条语句的表达目录项：位置、原文与该条自己的合法版本集合。"""

    statement_id: str  # "r{rank}.{category}.{index}"，仅当前调用内有效
    rank: int
    category: str
    index: int
    family: str
    canonical: str
    variants: tuple[CatalogVariant, ...]  # 首个恒为 canonical

    def variant_text(self, variant_id: str) -> str | None:
        for variant in self.variants:
            if variant.variant_id == variant_id:
                return variant.text
        return None


@dataclass(frozen=True)
class ExpressionCatalog:
    catalog_id: str
    items: tuple[CatalogItem, ...]

    @property
    def statement_ids(self) -> frozenset[str]:
        return frozenset(item.statement_id for item in self.items)

    def item_by_id(self, statement_id: str) -> CatalogItem | None:
        for item in self.items:
            if item.statement_id == statement_id:
                return item
        return None

    def filtered(self, keys: set[tuple[int, str]]) -> "ExpressionCatalog":
        """只保留 (rank, category) 可见组内的项（裁剪级联用）。"""
        return replace(self, items=tuple(
            item for item in self.items if (item.rank, item.category) in keys))


def new_catalog_id() -> str:
    """单次解释调用唯一目录标识；跨调用复用会被校验层拒绝。"""
    return uuid.uuid4().hex[:16]


def _statement_id(rank: int, category: str, index: int) -> str:
    return f"r{rank}.{category}.{index}"


def _build_item(rank: int, category: str, index: int, text: str,
                spec: ExpressionSpec | None) -> CatalogItem:
    canonical_only = CatalogItem(
        statement_id=_statement_id(rank, category, index), rank=rank, category=category,
        index=index, family=spec.family if spec else "unsupported", canonical=text,
        variants=(CatalogVariant(CANONICAL_VARIANT_ID, text),),
    )
    if spec is None:
        return canonical_only
    templates = FAMILY_TEMPLATES.get(spec.family)
    if templates is None:
        return canonical_only
    try:
        rendered = {vid: template.format(**spec.params) for vid, template in templates.items()}
    except KeyError:
        return canonical_only
    if rendered.get(CANONICAL_VARIANT_ID) != text:
        # canonical 与确定性原文不一致 = 句型或参数漂移，降级为仅原文
        return canonical_only
    variants = [CatalogVariant(CANONICAL_VARIANT_ID, text)]
    for variant_id, variant_text in rendered.items():
        if variant_id == CANONICAL_VARIANT_ID:
            continue
        if not variant_text or variant_text == text:
            continue  # 异名同文版本无意义，直接不提供
        variants.append(CatalogVariant(variant_id, variant_text))
    return replace(canonical_only, variants=tuple(variants))


def build_catalog(report: ExplanationReport, catalog_id: str | None = None) -> ExpressionCatalog:
    """按原位置为报告每条语句建立表达目录（含无模板语句的原文项）。"""
    items: list[CatalogItem] = []
    for entry in report.recommendations:
        for category in STATEMENT_CATEGORIES:
            for index, statement in enumerate(getattr(entry, category)):
                spec = report.expression_specs.get((entry.rank, category, index))
                items.append(_build_item(entry.rank, category, index, statement.text, spec))
    return ExpressionCatalog(catalog_id or new_catalog_id(), tuple(items))
