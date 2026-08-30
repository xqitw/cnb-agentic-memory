"""MCP Server 独立启动入口（pyproject scripts 指向此处）。

MCP 依赖在 [mcp] extra 中，mcp_server 模块顶层即导入 mcp 包（无法延迟），
故入口与 mcp_server 分离：本模块延迟导入并捕获 ImportError，未安装 extra
时给出安装指引而非裸 traceback。
"""

from __future__ import annotations

import sys


def main() -> None:
    """MCP Server 启动入口（pyproject scripts 指向此处）。"""
    try:
        from .mcp_server import main as mcp_main
    except ImportError as err:
        print(
            f'缺少 MCP 依赖：{err}\n请安装：pip install "cnb-agentic-memory[mcp]"',
            file=sys.stderr,
        )
        raise SystemExit(2) from err
    mcp_main()


if __name__ == "__main__":
    main()
