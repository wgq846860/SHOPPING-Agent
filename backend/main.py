"""Public Demo implementation."""

from __future__ import annotations

import uuid
import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from backend import __version__
from backend.api.agent import router as agent_router
from backend.api.errors import install_error_handlers
from backend.api.internal import router as internal_router
from backend.api.products import router as products_router
from backend.config import Settings, load_settings

#: 页面路由表：URL 路径 -> frontend/ 下的文件。顺序即导航顺序。
PAGES: dict[str, str] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/product.html": "product.html",
    "/agent-shopping.html": "agent-shopping.html",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """应用关闭时回收 Agent Harness runtime（若有被启动过）。"""
    try:
        yield
    finally:
        harness = getattr(application.state, "agent_harness", None)
        if harness is not None:
            await run_in_threadpool(harness.close)
            application.state.agent_harness = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。测试可以传入自定义 settings（例如临时目录）。"""
    settings = settings or load_settings()

    application = FastAPI(
        title="Agentic Shopping Demo",
        description=(
            "耳机品类的 Agentic Shopping 演示后端。当前阶段 S3.3："
            "Agent 完整 API（POST /api/agent-shopping）已接入——意图解析"
            "（llm/rule_based_demo）、硬约束过滤、商品/Offer 排序与事实"
            "解释在同一请求内串联；传统购物数据接口见 S1.3。"
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    application.state.settings = settings
    from backend.agent.decision_records import DecisionRecords

    application.state.decision_records = DecisionRecords()
    application.state.agent_harness_lock = threading.Lock()
    application.state.agent_request_lock = asyncio.Lock()

    # ---- 统一错误信封（api.v0.1 §1.2） -------------------------------------
    install_error_handlers(application, _data_version)

    # ---- 本地演示站点：静态资源同样禁止缓存，改完 CSS/JS 刷新即生效 ----------
    @application.middleware("http")
    async def _no_store_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---- 静态资源：/static/css/*、/static/js/* -------------------------------
    if settings.frontend_dir.is_dir():
        application.mount(
            "/static",
            StaticFiles(directory=str(settings.frontend_dir)),
            name="static",
        )

    # ---- 页面路由 -----------------------------------------------------------
    for url_path, filename in PAGES.items():
        application.get(url_path, include_in_schema=False)(
            _make_page_handler(filename)
        )

    # ---- 健康检查 -----------------------------------------------------------
    @application.get("/api/health", tags=["system"])
    def health(request: Request) -> JSONResponse:
        """如实上报当前能力边界。

        `checks` 里每一项都对应下一步骤，S0.3 阶段预期多为 false；
        前端与验收脚本据此判断"能用什么、不能用什么"，而不是靠猜。
        """
        current: Settings = request.app.state.settings
        database_ready = current.db_ready()
        # Agent 就绪按实际意图模式如实上报（S3.3）：llm=密钥就绪可调模型；
        # 规则演示/配置非法都不是 Harness 就绪，但服务本身可用。
        from backend.agent.intent_parser import IntentParseError, resolve_intent_mode

        try:
            agent_mode = resolve_intent_mode(current)
        except IntentParseError as exc:
            agent_mode = f"invalid:{exc.message}"
        harness_ready = agent_mode == "llm" and current.agent_bridge_token() is not None

        payload = {
            "status": "ok",
            "service": "agentic-shopping-demo",
            "stage": "S3.3",
            "version": __version__,
            "request_id": f"req_{uuid.uuid4().hex[:12]}",
            "generated_at": _now_iso(),
            "meta": {
                "api_version": current.api_version,
                "schema_version": current.schema_version,
                "simulated": current.simulated,
                "data_version": None,
            },
            "checks": {
                "static_frontend": {
                    "ok": current.static_ok(),
                    "detail": (
                        "frontend/ 下页面与静态资源齐全"
                        if current.static_ok()
                        else f"缺失：{', '.join(current.missing_static())}"
                    ),
                },
                "database": {
                    "ok": database_ready,
                    "detail": (
                        f"已找到 {current.db_path.name}（查询与推荐接口已接入）"
                        if database_ready
                        else "尚未创建（运行 scripts/init_database.py 导入数据批次）"
                    ),
                },
                "ranking_engine": {
                    "ok": database_ready,
                    "detail": "硬约束过滤与商品/Offer两类排序已接入（S2）；无库时不可用",
                },
                "agent_harness": {
                    "ok": harness_ready,
                    "detail": (
                        "llm 模式已就绪（实际调用用户配置的模型）"
                        if harness_ready
                        else "llm 模式工具桥未启用（需配置 AGENT_BRIDGE_TOKEN）"
                        if agent_mode == "llm"
                        else f"当前模式：{agent_mode}（rule_based_demo 规则演示可离线使用）"
                    ),
                },
            },
            "endpoints": {
                "implemented": [
                    "GET /api/health",
                    "GET /api/products",
                    "GET /api/products/{id}",
                    "GET /api/products/{id}/offers",
                    "GET /api/merchants/{id}",
                    "POST /api/agent-shopping",
                ],
                "planned": [],
            },
        }
        return JSONResponse(payload)

    # ---- 传统购物数据接口（S1.3） ------------------------------------------
    # 必须注册在 /api/{rest:path} 兜底路由之前，否则会被 501 拦截。
    application.include_router(products_router)

    # ---- Agent 工具桥（S3.1）：仅限本地 Harness 工具插件调用 ---------------
    # 令牌校验在 backend/api/internal.py；未配置 AGENT_BRIDGE_TOKEN 时
    # 端点统一返回 503（桥关闭），不暴露任何业务数据。
    application.include_router(internal_router)

    # ---- Agent 完整 API（S3.3）：POST /api/agent-shopping -------------------
    application.include_router(agent_router)

    # ---- 未实现的 API：给出明确 501，而不是 404 或假数据 ---------------------
    @application.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def api_not_implemented(rest: str) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "code": "not_implemented",
                    "message": (
                        f"/api/{rest} 尚未实现；当前可用接口为 GET /api/health、"
                        "四个传统购物数据接口（products/offers/merchants）与 "
                        "POST /api/agent-shopping。"
                    ),
                    "details": {
                        "stage": "S3.3",
                        "planned_owner": "S4（前端联调）、S5（正式数据与评测）",
                    },
                },
                "meta": {
                    "api_version": settings.api_version,
                    "schema_version": settings.schema_version,
                    "simulated": settings.simulated,
                    "request_id": f"req_{uuid.uuid4().hex[:12]}",
                    "generated_at": _now_iso(),
                },
            },
        )

    return application


def _data_version(application: FastAPI) -> str | None:
    """错误信封里的批次号尽力而为：取不到为 null，不让错误处理再抛错。"""
    current: Settings = application.state.settings
    if not current.db_path.is_file():
        return None
    try:
        from backend.database.db import connect, get_manifest

        conn = connect(current.db_path)
        try:
            manifest = get_manifest(conn)
            return manifest["data_version"] if manifest else None
        finally:
            conn.close()
    except Exception:
        return None


def _make_page_handler(filename: str):
    """为每个页面生成独立处理器，避免闭包捕获到循环变量。"""

    def handler(request: Request) -> FileResponse:
        current: Settings = request.app.state.settings
        target = current.frontend_dir / filename
        if not target.is_file():
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "static_missing",
                        "message": f"前端文件缺失：{target}",
                    }
                },
            )
        # 本地演示站点：禁止缓存，改完 HTML/JS 刷新即可生效。
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    handler.__name__ = f"page_{filename.replace('.', '_')}"
    return handler


app = create_app()
