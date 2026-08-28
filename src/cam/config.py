"""统一配置：CAM_ 前缀环境变量，供 SDK / CLI / MCP 各形态共用。

环境变量清单（全部可选，代码内显式参数优先）：
- CAM_TOKEN     CNB API Token（需 repo-issue:rw + repo-code:r）
- CAM_REPO      记忆仓库 slug，如 group/memory
- CAM_BASE_URL  API 地址，默认 https://api.cnb.cool
- CAM_TIMEOUT   请求超时秒数，默认 30
"""

from __future__ import annotations

from dataclasses import dataclass

from .api import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, env, parse_timeout


@dataclass(frozen=True)
class Config:
    """cam 全局配置（环境变量快照）。"""

    token: str = ""
    repo: str = ""
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> Config:
        """从 CAM_ 前缀环境变量构建配置。"""
        return cls(
            token=env("TOKEN") or "",
            repo=env("REPO") or "",
            base_url=(env("BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            timeout=parse_timeout(env("TIMEOUT")),
        )
