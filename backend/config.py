"""配置层（S0.3 最小版本）。

设计边界：
- 只放**非敏感**的运行参数（路径、监听地址、版本标识）。
- 任何密钥（LLM API Key 等）一律不写在这里、不设默认值、不进代码库；
  S3 接入时通过环境变量或 `.env`（见将来的 `.env.example`）注入，
  由 `load_settings()` 从环境变量读取，缺失即报错而不是回退到明文默认值。
- 本模块不依赖 FastAPI，方便之后被 scripts/ 与 tests/ 直接复用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# backend/config.py -> backend/ -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

FRONTEND_DIR = PROJECT_ROOT / "frontend"
DATA_DIR = PROJECT_ROOT / "data"
DB_DIR = DATA_DIR / "db"

# ---------------------------------------------------------------------------
# 版本标识：与 docs/api_contract.md / docs/data_dictionary.md 的版本号对应。
# 前端与 API 响应里的 meta 块都引用这两个值，避免各自硬编码。
# ---------------------------------------------------------------------------
API_VERSION = "api.v0.1"
SCHEMA_VERSION = "commerce.v0.1"

#: 默认目的地，用于到手价（landed price）推导；见 api_contract.md 的 landed_price.v1。
DEFAULT_DESTINATION_REGION = "CN-SH"

#: 本阶段演示数据均为模拟数据，健康检查与后续 API 的 meta.simulated 都据此置位。
SIMULATED_DATA = True


@dataclass(frozen=True)
class Settings:
    """运行期配置。全部为只读，避免运行时被意外改写。"""

    # --- 路径 ---
    project_root: Path = PROJECT_ROOT
    frontend_dir: Path = FRONTEND_DIR
    data_dir: Path = DATA_DIR

    # --- 本地服务 ---
    # 只监听回环地址：本 Demo 无鉴权、无账号体系，不应暴露到局域网。
    host: str = "127.0.0.1"
    port: int = 8000

    # --- 版本 ---
    api_version: str = API_VERSION
    schema_version: str = SCHEMA_VERSION
    destination_region: str = DEFAULT_DESTINATION_REGION
    simulated: bool = SIMULATED_DATA

    # --- 数据库（S1 才会真正创建文件） ---
    #: 主库与测试库分离，避免测试污染演示数据。
    db_filename: str = "shopping.sqlite3"
    test_db_filename: str = "shopping_test.sqlite3"

    # --- Agent 层（S3 才接入真实调用）---
    #: 当前未接入，健康检查据此如实上报。
    harness_enabled: bool = False
    harness_profile: str = "shopping"
    #: Harness 运行时 home 目录名（相对 project_root）：与用户个人编程 Agent
    #: 配置隔离；由 S3.1 适配模块在首次启动前初始化。
    harness_home_name: str = ".runtime/agent-harness/home"
    #: 模型相关配置统一使用 LLM_* 环境变量名（见研发计划"本地部署与自定义 API"
    #: 一节），.env.example 提供字段示例。密钥只从环境变量读取；未设置时为
    #: None，绝不写默认明文。S3 的 Harness 适配器负责按供应商文档消费这些值。
    llm_provider_env: str = "LLM_PROVIDER"
    llm_base_url_env: str = "LLM_BASE_URL"
    llm_api_key_env: str = "LLM_API_KEY"
    llm_model_env: str = "LLM_MODEL"
    llm_timeout_env: str = "LLM_TIMEOUT_SECONDS"

    extra: dict = field(default_factory=dict)

    # -- 派生路径 ---------------------------------------------------------
    @property
    def db_dir(self) -> Path:
        """库目录从 data_dir 派生：隔离 data_dir 即隔离整个数据子树。"""
        return self.data_dir / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / self.db_filename

    @property
    def test_db_path(self) -> Path:
        return self.db_dir / self.test_db_filename

    # -- 模型配置读取（不提供默认值，缺失即 None） -------------------------
    def _env(self, name: str) -> str | None:
        value = os.environ.get(name)
        return value.strip() if value and value.strip() else None

    def llm_provider(self) -> str | None:
        return self._env(self.llm_provider_env)

    def llm_base_url(self) -> str | None:
        return self._env(self.llm_base_url_env)

    def llm_api_key(self) -> str | None:
        """返回 API Key；未配置时返回 None，调用方必须显式处理缺失。"""
        return self._env(self.llm_api_key_env)

    def llm_model(self) -> str | None:
        return self._env(self.llm_model_env)

    def llm_timeout_seconds(self) -> float | None:
        raw = self._env(self.llm_timeout_env)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # -- Agent 模式与工具桥（S3.1；均为非敏感配置） -------------------------
    #: 意图解析模式（契约 §7.2 mode）：AGENT_INTENT_MODE 显式取
    #: llm / rule_based_demo；未设置时按"有密钥用 llm、无密钥用规则演示"
    #: 解析。规则模式必须明确显示，不得因解析失败悄悄切换。
    agent_mode_env: str = "AGENT_INTENT_MODE"

    #: 工具桥内部凭据：Node 工具插件调用 /api/internal/tools/* 时携带，
    #: 双端从同一环境变量读取；未设置时桥端点与工具注入都关闭。
    agent_bridge_token_env: str = "AGENT_BRIDGE_TOKEN"

    def agent_intent_mode(self) -> str | None:
        return self._env(self.agent_mode_env)

    def agent_bridge_token(self) -> str | None:
        return self._env(self.agent_bridge_token_env)

    @property
    def harness_home(self) -> Path:
        """隔离的 Harness home（项目内 .runtime/，不碰用户全局配置）。"""
        return self.project_root / self.harness_home_name

    @property
    def agent_bridge_origin(self) -> str:
        """工具桥固定本地地址：由服务监听地址派生，模型不可指定。"""
        return f"http://{self.host}:{self.port}"

    # -- 自检 -------------------------------------------------------------
    def static_ok(self) -> bool:
        """静态资源是否就位（三个页面 + 样式 + 脚本）。"""
        required = (
            "index.html",
            "product.html",
            "agent-shopping.html",
            "css/main.css",
            "js/app.js",
            "js/agent.js",
        )
        return all((self.frontend_dir / name).is_file() for name in required)

    def missing_static(self) -> list[str]:
        required = (
            "index.html",
            "product.html",
            "agent-shopping.html",
            "css/main.css",
            "js/app.js",
            "js/agent.js",
        )
        return [name for name in required if not (self.frontend_dir / name).is_file()]

    def db_ready(self) -> bool:
        """数据库文件是否已存在。S0.3 阶段预期为 False。"""
        return self.db_path.is_file()


def load_settings(**overrides) -> Settings:
    """构造配置。允许用关键字覆盖默认值，便于测试注入临时路径。

    首次调用时若项目根存在 `.env`（参照 `.env.example` 建立），先把其中
    未在进程环境中出现的变量注入 os.environ（不覆盖已有环境变量）。
    这样密钥与本地差异只留在 `.env`，代码与示例文件不含真实值。
    依赖 python-dotenv；未安装时静默跳过，测试与 CI 不受影响。
    """
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        _DOTENV_LOADED = True
        try:
            from dotenv import load_dotenv
        except ImportError:
            pass
        else:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
    if not overrides:
        return Settings()
    base = Settings()
    return Settings(**{**base.__dict__, **overrides})


_DOTENV_LOADED = False
