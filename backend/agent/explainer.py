"""Public Demo implementation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from backend.agent.harness_client import (
    HarnessTimeoutError,
    HarnessUnavailableError,
    HarnessTurnResult,
    is_failed_finish_reason,
)
from backend.agent.intent_parser import extract_json_object
from backend.agent.prompts import build_explanation_prompt
from backend.ranking.offer_ranker import Recommendation
from backend.schemas.agent import ShoppingIntent
from backend.services.explanation_service import (
    ExplainedRecommendation,
    ExplanationReport,
    FactStatement,
    STATEMENT_CATEGORIES,
)
from backend.services.expression_catalog import CANONICAL_VARIANT_ID
from backend.services.formatting_service import (
    DEFAULT_CONTEXT_CHAR_CAP,
    DEFAULT_MAX_CANDIDATES,
    ExplanationContext,
    format_context,
)

#: 达到输出上限的终态：即使文本恰好完整也无法证明选择完整，直接回退
TRUNCATED_FINISH_REASONS = frozenset({"length", "max-tokens", "max_tokens"})


class _HarnessLike(Protocol):
    def run_intent_turn(self, prompt: str) -> HarnessTurnResult: ...


@dataclass(frozen=True)
class ExplanationOutcome:
    """一次语言解释的结果：可能替换语句的报告 + 附加警告。

    warnings 仅含本层新增项（如 EXPLANATION_FALLBACK），S3.3 组装响应时
    与报告自带警告合并；used_llm=False 且 fallback_reason=None 表示没有
    可解释对象（零候选），并非失败。
    """

    report: ExplanationReport
    used_llm: bool
    warnings: tuple[dict, ...]
    finish_reason: str | None
    fallback_reason: str | None


def _fallback(report: ExplanationReport, reason: str, *,
              finish_reason: str | None = None) -> ExplanationOutcome:
    warning = {
        "code": "EXPLANATION_FALLBACK",
        "message": "模型语言解释不可用，已回退确定性解释。",
        "field": None,
        "product_id": None,
    }
    return ExplanationOutcome(
        report=report, used_llm=False, warnings=(warning,),
        finish_reason=finish_reason, fallback_reason=reason,
    )


# ---------------------------------------------------------------------------
# 选择协议校验：目录归属，不接受任何模型文本
# ---------------------------------------------------------------------------


def _validate_selections(
    payload,
    context: ExplanationContext,
) -> tuple[dict[str, str] | None, list[str]]:
    """校验模型选择；通过时返回 statement_id -> variant_id 映射。"""
    violations: list[str] = []
    if not isinstance(payload, dict):
        return None, ["selection_root_not_object"]
    if set(payload) != {"catalog_id", "selections"}:
        return None, ["unexpected_top_level_keys"]
    catalog = context.catalog
    if payload["catalog_id"] != catalog.catalog_id:
        return None, ["stale_catalog_id"]
    selections = payload["selections"]
    if not isinstance(selections, list):
        return None, ["selections_not_list"]
    chosen: dict[str, str] = {}
    for entry in selections:
        if not isinstance(entry, dict) or set(entry) != {"statement_id", "variant_id"}:
            return None, violations + ["selection_shape"]
        statement_id, variant_id = entry["statement_id"], entry["variant_id"]
        if not isinstance(statement_id, str) or not statement_id:
            return None, violations + ["statement_id_not_string"]
        if not isinstance(variant_id, str) or not variant_id:
            return None, violations + ["variant_id_not_string"]
        if statement_id in chosen:
            return None, violations + ["duplicated_statement"]
        item = catalog.item_by_id(statement_id)
        if item is None:
            return None, violations + ["unknown_statement"]
        if item.variant_text(variant_id) is None:
            # 版本存在性按条目归属判定：别条语句的合法版本同样拒绝
            return None, violations + ["foreign_variant"]
        chosen[statement_id] = variant_id
    missing = sorted(catalog.statement_ids - set(chosen))
    if missing:
        return None, violations + ["incomplete_coverage"]
    return chosen, violations


def _rebuild_report(report: ExplanationReport, context: ExplanationContext,
                    chosen: dict[str, str]) -> tuple[ExplanationReport, int]:
    """按原位置套用所选版本文本；引用与比较元数据原样继承。

    改写条数按"最终文本与该条 canonical 实际不同"计（不按非 canonical
    的 ID 数量计），全为原文时由调用方按 zero_rewrites 回退。
    """
    catalog = context.catalog
    rebuilt: list[ExplainedRecommendation] = []
    replaced = 0
    for entry in report.recommendations:
        fields: dict[str, list[FactStatement]] = {}
        for category in STATEMENT_CATEGORIES:
            statements = []
            for index, source in enumerate(getattr(entry, category)):
                item = catalog.item_by_id(f"r{entry.rank}.{category}.{index}")
                text = source.text
                if item is not None:
                    variant_id = chosen.get(item.statement_id, CANONICAL_VARIANT_ID)
                    selected = item.variant_text(variant_id)
                    if selected is not None:
                        text = selected
                if text != source.text:
                    replaced += 1
                statements.append(FactStatement(text, source.fact_refs, source.comparison))
            fields[category] = statements
        rebuilt.append(replace(entry, **fields))
    return replace(report, recommendations=rebuilt), replaced


def explain_recommendations(
    report: ExplanationReport,
    intent: ShoppingIntent,
    ranked_candidates: list[Recommendation],
    harness: _HarnessLike,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    char_cap: int = DEFAULT_CONTEXT_CHAR_CAP,
) -> ExplanationOutcome:
    """S3.2 完整入口：压缩上下文 → 模型选择表达 → 目录归属校验 → 回退/套用。

    report 必须是 ranked_candidates 的 S2.4 解释报告（S3.3 已构建）。
    本函数对推荐事实只读：失败路径返回原报告 + EXPLANATION_FALLBACK。

    目录单次边界（S32R3-02）：上下文与目录在每次调用内部重新构建，
    catalog_id 一次性——同一份模型响应在后续调用（新目录）必然
    stale_catalog_id 回退，不存在可复用的注入入口。调用方需要检视
    目录时自行 format_context（仅用于观察，不参与校验）。
    """
    context = format_context(
        intent, ranked_candidates, report=report,
        max_candidates=max_candidates, char_cap=char_cap,
    )
    if not report.recommendations:
        # 零候选：没有可解释对象，不产生模型调用，也不是回退失败
        return ExplanationOutcome(report, False, (), None, None)
    if context.overflow or not context.snippets:
        return _fallback(report, "context_overflow")
    if not context.has_variant_items():
        # 目录没有任何非原文表达：跳过语言改写，不计为改写成功
        return _fallback(report, "no_variant_expressions")
    prompt = build_explanation_prompt(context.rendered)
    try:
        result = harness.run_intent_turn(prompt)
    except HarnessTimeoutError:
        return _fallback(report, "timeout")
    except HarnessUnavailableError as error:
        return _fallback(report, f"unavailable:{type(error).__name__}")
    if is_failed_finish_reason(result.finish_reason):
        return _fallback(report, f"model_turn_failed:{result.finish_reason}",
                         finish_reason=result.finish_reason)
    if isinstance(result.finish_reason, str) and result.finish_reason.strip().lower() in TRUNCATED_FINISH_REASONS:
        return _fallback(report, "truncated_output", finish_reason=result.finish_reason)
    try:
        payload = extract_json_object(result.final_response)
    except (ValueError, RecursionError):
        # RecursionError：超深嵌套 JSON 在解析器递归上限处失败，同样回退
        return _fallback(report, "unparseable_output", finish_reason=result.finish_reason)
    chosen, violations = _validate_selections(payload, context)
    if chosen is None:
        return _fallback(report, "grounding_violation:" + ";".join(violations),
                         finish_reason=result.finish_reason)
    rebuilt, replaced = _rebuild_report(report, context, chosen)
    if replaced == 0:
        return _fallback(report, "zero_rewrites", finish_reason=result.finish_reason)
    return ExplanationOutcome(
        report=rebuilt,
        used_llm=True,
        warnings=(),
        finish_reason=result.finish_reason,
        fallback_reason=None,
    )
