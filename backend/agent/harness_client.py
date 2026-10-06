"""DeepSeek Harness SDK 适配模块（S3.1；S31 整改版）。

统一管理 runtime 生命周期、购物 profile、会话与超时（s3.md S3.1 第 1 条）：
- 每次意图解析使用独立 session（SDK `run()` 每次新建 session-<uuid>），
  不跨请求混合需求；
- **整体回合截止（S31-02）**：已安装 SDK 在 prompt RPC 应答之后的事件
  等待（NotificationSubscription.next()）没有超时，request_timeout 只
  覆盖 RPC。本适配把整次 `run()` 放入单工作线程并施加截止时间；超时即
  关闭 SDK 回收 runtime 子进程（阻塞的等待随传输关闭而失败），下一次
  调用重新启动，不留下无限运行的 Agent；
- **错误终态（S31-04）**：SDK 可能正常返回 RunResult 但 finish_reason
  标记失败/取消——按终态先判失败再交给文本校验；
- **provider 如实（S31-10）**：DeepSeek与MiMo分开记录供应商；MiMo通过
  已核验的Chat Completions协议复用deepseek-official适配器，并覆盖思考
  与输出上限。其他未知provider仍明确拒绝；
- 超时/异常在本层翻译为稳定类型：初始化与请求超时均归超时（S31-11，
  映射 504），其余归不可用（映射 503），不透传供应商报错原文；
- 购物 profile 从官方 sdk-minimal 模板派生，叠加 `harness/shopping.patch.yml`
  （关闭 shell/文件/子进程/日志上传）与工具插件插入（可选）。工具桥地址
  固定派生自服务监听地址，模型不可指定（deepseek_harness_integration.md §4）。

模型协议适配边界（同文档 §5）：MiMo必须显式提供地址和模型，不回退
DeepSeek默认值；配置与实际兼容证据单独记录，密钥只经环境传给runtime。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.config import PROJECT_ROOT, Settings, load_settings

#: 未配置 LLM_TIMEOUT_SECONDS 时的默认请求超时（秒），兼作整体回合截止
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
#: runtime 初始化（进程启动 + initialize）超时（秒）
INITIALIZE_TIMEOUT_SECONDS = 30.0

#: 购物 profile 的静态叠加层与工具插件（项目内固定文件）
SHOPPING_PATCH_PATH = PROJECT_ROOT / "harness" / "shopping.patch.yml"
SHOPPING_TOOLS_PLUGIN_PATH = PROJECT_ROOT / "harness" / "shopping-tools.cjs"
SHOPPING_TOOLS_PLUGIN_ID = "agentic-shopping-tools"
SHOPPING_PROFILE_TEMPLATE = "sdk-minimal"

#: 供应商名与实际协议引擎分开；未知协议仍拒绝（S31-10）。
MIMO_PROVIDERS = frozenset({"xiaomi-mimo", "mimo"})
SUPPORTED_ADAPTER_PROVIDERS = frozenset({"deepseek-official", "deepseek", *MIMO_PROVIDERS})
MIMO_MAX_OUTPUT_TOKENS = 4096


def _uses_mimo(settings: Settings) -> bool:
    return (settings.llm_provider() or "").lower() in MIMO_PROVIDERS


def _model_for(settings: Settings) -> str:
    if _uses_mimo(settings):
        missing = [name for name, value in (("LLM_BASE_URL", settings.llm_base_url()),
                                            ("LLM_MODEL", settings.llm_model())) if not value]
        if missing:
            raise HarnessUnavailableError("MiMo配置缺少 " + ", ".join(missing))
    return settings.llm_model() or "deepseek-v4-flash"

#: finish_reason 中的失败/取消终态：先于文本校验判失败（S31-04）。
#: 其余非停机终态（如截断）交由文本/JSON 校验如实拒绝。
TURN_FAILURE_FINISH_REASONS = frozenset(
    {"error", "cancelled", "canceled", "timeout", "aborted", "abort", "crash"}
)


class HarnessError(Exception):
    """本层错误基类（与 SDK 的 deepseek_harness.HarnessError 区分）。"""


class HarnessTimeoutError(HarnessError):
    """初始化/请求/整体回合超时（映射 504 LLM_TIMEOUT）。"""


class HarnessUnavailableError(HarnessError):
    """运行时启动失败、上游不可用、协议错误或回合异常终止（映射 503）。"""


class HarnessTransportError(HarnessUnavailableError):
    """run() 期间抛出异常（传输关闭/进程死亡等）：持有的 SDK 实例已失效。

    与"终态失败但进程仍健康"区分（RE31-08）：前者调用后回收本客户端，
    下一次调用重建 runtime；后者保留实例继续复用。
    """


def is_failed_finish_reason(finish_reason: str | None) -> bool:
    """终态是否为失败/取消（S31-04；意图解析层与适配层共用同一判定）。"""
    return (
        isinstance(finish_reason, str)
        and finish_reason.strip().lower() in TURN_FAILURE_FINISH_REASONS
    )


@dataclass(frozen=True)
class HarnessTurnResult:
    """一次会话回合的结果；final_response 为模型最终文本。"""

    session_id: str
    final_response: str
    finish_reason: str | None


class _SessionLike(Protocol):
    def run(self, input: str) -> object: ...


class _SdkLike(Protocol):
    """SDK 最小表面（DeepSeekHarness 满足；测试注入伪实现）。"""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def start_session(self) -> _SessionLike: ...


@dataclass(frozen=True)
class HarnessRuntimeConfig:
    """传给 SDK 的运行配置快照（诊断用；不含密钥值）。

    provider 是用户在 LLM_PROVIDER 里填的服务商标识；adapter 是实际
    走的协议适配器——两者分开记录，不用用户标识冒充适配能力（S31-10）。
    """

    provider: str | None
    adapter: str
    model: str
    base_url: str | None
    api_key_present: bool
    request_timeout_seconds: float
    tools_enabled: bool
    bridge_origin: str | None
    profile: str
    dsh_home: Path


def compose_shopping_overlay(
    out_path: Path,
    *,
    bridge_origin: str | None,
    bridge_token: str | None,
    plugin_config_extra: dict | None = None,
    mimo_base_url: str | None = None,
) -> Path:
    """组合购物 profile 叠加层：静态禁用清单 +（凭据就绪时）工具插件插入。

    bridge_origin/bridge_token 为空时不插入工具插件——意图解析本身不需要
    工具，组合工具在 S3.3 的完整链路才必经。plugin_config_extra 仅供
    离线验证脚本注入自检配置（如 selfTest），正常运行不传。
    """
    static = SHOPPING_PATCH_PATH.read_text(encoding="utf-8")
    parts = [static.rstrip("\n")]
    if mimo_base_url:
        # 固定SDK的Config使用字符串thinking，wire序列化为{type:disabled}。
        # MiMo禁思考避免工具历史依赖reasoning_content；保留模板的密钥环境绑定。
        parts.append(
            "\n- id: llm-deepseek\n"
            "  config:\n"
            "    apiKeyEnv: DEEPSEEK_API_KEY\n"
            f"    baseURL: {json.dumps(mimo_base_url)}\n"
            "    thinking: disabled\n"
            '    reasoningEffort: "off"\n'
            f"    maxTokens: {MIMO_MAX_OUTPUT_TOKENS}\n"
            "    defaultContextWindow: 1000000\n"
            "    streamIdleTimeoutMs: 60000\n"
            "- id: plugin-package-inventory-deepseek\n"
            "  disabled: true\n"
        )
    if bridge_origin and bridge_token:
        plugin = SHOPPING_TOOLS_PLUGIN_PATH.resolve().as_posix()
        extra_lines = "".join(
            f"        {key}: {json.dumps(value)}\n" for key, value in (plugin_config_extra or {}).items()
        )
        parts.append(
            "\n# 动态插入：本地购物工具插件（地址固定派生自服务监听配置）\n"
            "- insert:\n"
            f"    - id: {SHOPPING_TOOLS_PLUGIN_ID}\n"
            f"      name: {json.dumps(plugin)}\n"
            "      config:\n"
            f"        bridgeOrigin: {json.dumps(bridge_origin)}\n"
            f"        bridgeToken: {json.dumps(bridge_token)}\n"
            f"{extra_lines}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return out_path


def bootstrap_shopping_profile(settings: Settings, *, env_extra: dict[str, str] | None = None) -> None:
    """在隔离 DSH_HOME 内从官方模板初始化购物 profile（幂等）。

    仅落盘模板配置，不产生模型请求；与 scripts/verify_harness.py 同一
    机制（S0.3 已验）。
    """
    from deepseek_harness_runtime import RUNTIME_MODE_ENV_VAR, bundled_runtime_path

    env = {
        **os.environ,
        "DSH_HOME": str(settings.harness_home),
        RUNTIME_MODE_ENV_VAR: "exe",
        **(env_extra or {}),
    }
    command = [str(bundled_runtime_path()), "--profile", settings.harness_profile]
    package = settings.harness_home / "profiles" / settings.harness_profile / "package.json"
    if not package.is_file():
        # CLI只允许新profile使用from-default；已有profile重复传会exit1。
        command.extend(["--from-default-profile", SHOPPING_PROFILE_TEMPLATE])
    command.append("--dump-default-config")
    proc = subprocess.run(
        command,
        env=env,
        cwd=str(settings.project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise HarnessUnavailableError(
            f"购物 profile 初始化失败（exit={proc.returncode}）：{proc.stderr[-500:]}"
        )


def _default_sdk_factory(settings: Settings, overlay: Path, timeout: float, env: dict[str, str]) -> _SdkLike:
    """构造真实 DeepSeekHarness SDK 实例（延迟导入，便于离线测试）。"""
    from deepseek_harness import DeepSeekHarness

    mimo_options = {"max_tokens": MIMO_MAX_OUTPUT_TOKENS, "reasoning_effort": "off"} if _uses_mimo(settings) else {}
    return DeepSeekHarness(
        cwd=str(settings.project_root),
        dsh_home=str(settings.harness_home),
        profile=settings.harness_profile,
        patches=(str(overlay),),
        env=env,
        base_url=settings.llm_base_url(),
        api_key=settings.llm_api_key(),
        model=_model_for(settings),
        initialize_timeout_seconds=INITIALIZE_TIMEOUT_SECONDS,
        request_timeout_seconds=timeout,
        shutdown_timeout_seconds=10.0,
        **mimo_options,
    )


class ShoppingHarnessClient:
    """意图解析用的 Harness 会话客户端。

    首次调用 run_intent_turn 时惰性启动 runtime，进程跨请求复用（SDK 设计）；
    close() 幂等回收 runtime 与回合工作线程。线程模型：FastAPI 同步端点内
    串行使用（单工作线程足够，串行也保证了请求间不并发共享会话）。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sdk_factory=None,
        overlay_dir: Path | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._sdk_factory = sdk_factory or _default_sdk_factory
        self._overlay_dir = overlay_dir or (
            self._settings.project_root / ".runtime" / "agent-harness"
        )
        self._sdk: _SdkLike | None = None
        self._runtime_config: HarnessRuntimeConfig | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._turn_lock = threading.RLock()

    # -- 生命周期 -----------------------------------------------------------

    def __enter__(self) -> "ShoppingHarnessClient":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        """幂等回收：SDK runtime 与回合工作线程。"""
        with self._turn_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception:  # noqa: BLE001 — 关闭路径不因回收失败中断调用方
                pass
            self._sdk = None
        if self._executor is not None:
            # wait=False：阻塞中的回合线程在 SDK 关闭后自行结束，
            # 不让回收路径卡在等待上（S31-02/S31-03）。
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    @property
    def started(self) -> bool:
        return self._sdk is not None

    def runtime_config(self) -> HarnessRuntimeConfig:
        """当前（或启动时将生效的）运行配置快照；不包含密钥值。"""
        if self._runtime_config is not None:
            return self._runtime_config
        settings = self._settings
        timeout = settings.llm_timeout_seconds() or DEFAULT_REQUEST_TIMEOUT_SECONDS
        token = settings.agent_bridge_token()
        self._runtime_config = HarnessRuntimeConfig(
            provider=settings.llm_provider(),
            adapter="deepseek-official",
            model=_model_for(settings),
            base_url=settings.llm_base_url(),
            api_key_present=settings.llm_api_key() is not None,
            request_timeout_seconds=timeout,
            tools_enabled=token is not None,
            bridge_origin=settings.agent_bridge_origin if token else None,
            profile=settings.harness_profile,
            dsh_home=settings.harness_home,
        )
        return self._runtime_config

    # -- 会话 ---------------------------------------------------------------

    def run_intent_turn(self, prompt: str) -> HarnessTurnResult:
        """执行一次意图解析回合。

        整次 SDK run() 在专用线程执行，受 request_timeout_seconds 的整体
        截止约束：超时先关闭本客户端（回收 runtime 子进程，阻塞线程随
        传输关闭结束），再抛 HarnessTimeoutError；下一次调用重新启动。
        """
        deadline = self.runtime_config().request_timeout_seconds
        expires = time.monotonic() + deadline
        # 在取得所有权前不捕获 SDK、不提交任务；等待超时不能关闭别人的回合。
        if not self._turn_lock.acquire(timeout=deadline):
            raise HarnessTimeoutError("等待 Harness 回合超时，请重试")
        try:
            if time.monotonic() >= expires:
                raise HarnessTimeoutError("等待 Harness 回合超时，请重试")
            sdk = self._ensure_started()
            # SDK.run() 会自动 start。先在所有权锁内创建会话，工作线程仅
            # Session.run()；已领取但尚未执行的旧线程不能重新启动关闭的SDK。
            session = sdk.start_session()
            remaining = expires - time.monotonic()
            if remaining <= 0:
                self._close_locked()
                raise HarnessTimeoutError("Harness 初始化超过回合截止，已回收 runtime")
            future = self._turn_executor().submit(self._run_sdk_turn, session, prompt)
            try:
                return future.result(timeout=remaining)
            except FutureTimeoutError:
                self._close_locked()
                raise HarnessTimeoutError(
                    f"意图解析回合超过 {deadline} 秒未结束，已回收 Harness runtime"
                ) from None
            except (HarnessTimeoutError, HarnessTransportError):
                self._close_locked()
                raise
        finally:
            self._turn_lock.release()

    # -- 内部 ---------------------------------------------------------------

    def _turn_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="harness-intent-turn"
            )
        return self._executor

    def _run_sdk_turn(self, session: _SessionLike, prompt: str) -> HarnessTurnResult:
        try:
            result = session.run(prompt)
        except TimeoutError as error:
            raise HarnessTimeoutError(f"意图解析超时：{error}") from error
        except Exception as error:  # noqa: BLE001 — 传输/进程死亡：实例失效（RE31-08）
            raise HarnessTransportError(
                f"Harness 会话失败：{type(error).__name__}"
            ) from error
        finish_reason = getattr(result, "finish_reason", None)
        if is_failed_finish_reason(finish_reason):
            # 终态失败但 runtime 进程仍健康：保留实例，只判本回合失败
            raise HarnessUnavailableError(f"模型回合异常终止：{finish_reason}")
        return HarnessTurnResult(
            session_id=str(getattr(result, "session_id", "")),
            final_response=str(getattr(result, "final_response", "") or ""),
            finish_reason=finish_reason,
        )

    def _ensure_started(self) -> _SdkLike:
        if self._sdk is not None:
            return self._sdk
        settings = self._settings
        provider = settings.llm_provider()
        if provider and provider.strip().lower() not in SUPPORTED_ADAPTER_PROVIDERS:
            raise HarnessUnavailableError(
                f"LLM_PROVIDER={provider!r} 尚无已验证的模型协议适配器"
                f"（当前仅支持 {sorted(SUPPORTED_ADAPTER_PROVIDERS)}）；"
                "请按 docs/deepseek_harness_integration.md §5 完成薄适配并实测后再启用"
            )
        timeout = self.runtime_config().request_timeout_seconds
        sdk = None
        try:
            bootstrap_shopping_profile(settings)
            overlay = compose_shopping_overlay(
                self._overlay_dir / "shopping.overlay.yml",
                bridge_origin=settings.agent_bridge_origin
                if settings.agent_bridge_token()
                else None,
                bridge_token=settings.agent_bridge_token(),
                mimo_base_url=settings.llm_base_url() if _uses_mimo(settings) else None,
            )
            env = {**os.environ}
            sdk = self._sdk_factory(settings, overlay, timeout, env)
            sdk.start()
        except HarnessError:
            if sdk is not None:
                sdk.close()
            raise
        except (TimeoutError, subprocess.TimeoutExpired) as error:
            if sdk is not None:
                sdk.close()
            # 初始化超时同样是超时（S31-11），与请求超时一致映射 504
            raise HarnessTimeoutError(
                f"Harness runtime 初始化超时：{type(error).__name__}"
            ) from error
        except Exception as error:  # noqa: BLE001 — 启动失败统一归不可用
            if sdk is not None:
                sdk.close()
            raise HarnessUnavailableError(
                f"Harness runtime 启动失败：{type(error).__name__}"
            ) from error
        self._sdk = sdk
        return sdk
