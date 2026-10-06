"""Agentic Shopping Demo 后端包。

当前阶段：S0.3（可运行环境，部分完成）。
本包目前只提供最小 HTTP 服务：静态前端资源 + `GET /api/health`。
数据库层（S1）、排序层（S2）、Agent 层（S3）尚未实现。
"""

__all__ = ["__version__"]

# 与 docs/api_contract.md 的 api_version 对应，用于 /api/health 自报版本。
__version__ = "0.1.0"
