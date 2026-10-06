"""Agent 完整 API（S3.3）：POST /api/agent-shopping（api.v0.1 §7）。

契约边界：
- 请求体只接受 `{"query": string}` 对象：缺少、null、非字符串、额外键、
  非 object 体均由 Pydantic strict(extra=forbid) + 统一框架包装拒绝
  （422 VALIDATION_ERROR）；去空白后 1–2000 字符由意图解析层校验
  （§7.5 blank_query / too_long）。
- 模式：AGENT_INTENT_MODE 显式配置优先；llm 无密钥 → 503
  LLM_UNAVAILABLE，不悄悄降级为规则演示（§7.5）。
- 业务状态 200：needs_clarification / no_results / partial / ok；
  意图解析失败 502/504/503 错误信封，不捏造意图返回成功。
- Harness 客户端在 app.state 惰性创建、跨请求复用（SDK 进程设计），
  每次解析/工具/解释回合为独立 session；应用关闭时统一回收（main.py
  lifespan），回合超时由适配层回收重建（S31-02）。

本文件不含密钥、Prompt 与排序逻辑：解析/过滤/排序/解释全部复用
backend/agent 与 backend/services 的已验证入口。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from backend.agent.harness_client import ShoppingHarnessClient
from backend.agent.intent_parser import IntentParseError, resolve_intent_mode
from backend.agent.shopping_agent import run_agent_query
from backend.api.errors import (
    ApiError,
    DataUnavailable,
    make_meta,
    request_id,
)
from backend.api.products import _now, _open_conn
from backend.services.errors import DataError

router = APIRouter(tags=["agent"])


class AgentShoppingRequest(BaseModel):
    """契约 §7：仅接受 {"query": string}；未知额外键一律 422。"""

    model_config = ConfigDict(extra="forbid")

    query: str


def _harness_for(app, server=None) -> ShoppingHarnessClient:
    """app 级 Harness 客户端：惰性创建、复用 runtime；测试可预注入替身
    （app.state.agent_harness），注入对象的生命周期由注入方管理。"""
    with app.state.agent_harness_lock:
        existing = getattr(app.state, "agent_harness", None)
        if existing is not None:
            return existing
        settings = app.state.settings
        # Uvicorn 的实际监听端口可能由 --port 覆盖；只采用ASGI服务端
        # 回环地址，绝不使用客户端Host头或模型参数决定桥接目的地。
        if server and server[0] == "127.0.0.1" and isinstance(server[1], int):
            settings = replace(settings, host="127.0.0.1", port=server[1])
        client = ShoppingHarnessClient(settings)
        app.state.agent_harness = client
        return client


@router.post("/api/agent-shopping")
async def agent_shopping(request: Request, body: AgentShoppingRequest) -> JSONResponse:
    settings = request.app.state.settings
    try:
        mode = resolve_intent_mode(settings)
    except IntentParseError as exc:
        raise ApiError(
            exc.api_status, exc.api_code, exc.message,
            details=exc.details, retryable=exc.retryable,
        ) from exc

    if mode == "rule_based_demo":
        return await run_in_threadpool(_run_query, request, body, mode)
    if settings.agent_bridge_token() is None:
        raise ApiError(503, "LLM_UNAVAILABLE", "完整购物服务未启用工具桥",
                       details=[{"field": "AGENT_BRIDGE_TOKEN", "reason": "missing_configuration"}])
    # 等待在事件循环中发生，避免占满同步线程池后工具桥无线程可用。
    lock = request.app.state.agent_request_lock
    try:
        await asyncio.wait_for(lock.acquire(), timeout=settings.llm_timeout_seconds() or 60)
    except TimeoutError as exc:
        raise ApiError(504, "LLM_TIMEOUT", "购物服务繁忙，请重试", retryable=True) from exc
    try:
        return await run_in_threadpool(_run_query, request, body, mode)
    finally:
        lock.release()


def _run_query(request: Request, body: AgentShoppingRequest, mode: str) -> JSONResponse:
    settings = request.app.state.settings
    conn = _open_conn(request)
    try:
        harness = _harness_for(request.app, request.scope.get("server")) if mode == "llm" else None
        now = _now(request)
        result = run_agent_query(
            conn,
            body.query,
            mode=mode,
            now=now,
            harness=harness,
            decision_runner=(lambda intent: request.app.state.decision_records.run(intent, now, harness))
            if mode == "llm" else None,
        )
        payload = dict(result.body)
        payload["meta"] = make_meta(settings, request_id(), result.data_version)
        return JSONResponse(payload)
    except IntentParseError as exc:
        raise ApiError(
            exc.api_status, exc.api_code, exc.message,
            details=exc.details, retryable=exc.retryable,
        ) from exc
    except DataError as exc:
        raise DataUnavailable(str(exc), field="data") from exc
    finally:
        conn.close()
