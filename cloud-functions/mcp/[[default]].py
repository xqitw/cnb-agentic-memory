"""EdgeOne Pages Python 运行时适配层：streamable-http 形态承载 MCP Server（#91）。

EO 运行时把函数目录前缀（/mcp）剥掉后把请求交给本 ASGI 应用，MCP 客户端连接
https://<对外域名>/mcp。Serverless 短执行模型（上限 120s）不适配 SSE 长连接与
有状态 session，故 json_response + stateless_http 双开。

环境变量（EO 控制台配置，值须 ≤500 字节）：
- CNB_AGENTIC_MEMORY_REQUIRE_HEADERS=1：强制凭据头（共享部署必配）。凭据一律由
  调用方经 X-CNB-Token / X-CNB-Repo 头传递，服务端不持有

SDK 的 Host/Origin 传输层校验在 EO 形态下不启用，防护职责分层（#91 实测）：
- Host：EO 边缘按 Host 路由（路由键），恶意域名在平台层即 418，到不了函数；
  而函数收到的 Host 被改写为平台内部源站域名（pages-*.qcloudteo.com 形态），
  动态不可预知，SDK 白名单无法稳定配置（SDK 无「只校验 Origin」的粒度）
- 匿名/跨源滥用：REQUIRE_HEADERS 门禁在工具入口拒绝无凭据头请求（服务端零
  凭据可被间接使用），实质阻断 DNS rebinding / 跨源攻击的资产面

部署步骤与实测结论回填见 docs/EdgeOne.md。
"""

import warnings
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cnb_agentic_memory.mcp_server import configure_require_headers, mcp

# require-headers 激活必须显式调用：适配层不经 main()，argparse default 的 env
# 兜底在此路径不生效，开关恒 False（幽明 #91 实测）——语义与 CLI 完全同源。
# 启动告警落 EO 函数日志：门禁关闭时共享部署匿名可调全部工具（幽明 #91），
# 漏配 env 不能零痕迹；请求期门禁由 ensure_require_headers 每请求惰性求解
if not configure_require_headers():
    warnings.warn(
        "CNB_AGENTIC_MEMORY_REQUIRE_HEADERS 未启用（当前值非 1/true/yes/on）："
        "共享部署必须配置为 1 强制凭据头，否则匿名请求可调用全部记忆工具",
        RuntimeWarning,
        stacklevel=2,
    )

_mcp_asgi = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    # transport_security=None 且 host 传非 localhost 值：SDK 对 localhost 族会
    # 自动开 DNS rebinding 防护（allowed_hosts 仅本机族），EO 转发的内部源站
    # Host 必被 421——防护不启用见模块 docstring 的职责分层说明
    transport_security=None,
    host="edgeone-pages",
)


@asynccontextmanager
async def _mcp_lifespan(_app: FastAPI):
    async with _mcp_asgi.router.lifespan_context(_mcp_asgi):
        yield


app = FastAPI(
    title="CNB Issue 智能体记忆 MCP Server",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_mcp_lifespan,
)


@app.api_route(
    "/",
    methods=["GET", "PUT", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def _reject_non_mcp_methods(_request: Request) -> JSONResponse:
    # stateless + json_response 形态无服务器推送流：SDK 对 GET /mcp 不回 405 而是
    # 挂起一条无 session、无内容、无人关闭的 SSE 长连接，白耗 maxDuration（120s）
    # 执行配额还可能被平台记为异常长连接（幽明 #91 暗裂二）——外层直接拦截。
    # POST（工具调用）与 DELETE（会话终结语义）仍透传子应用
    return JSONResponse(
        {"error": "method not allowed：本端点仅接受 POST（MCP JSON-RPC）"},
        status_code=405,
    )


app.mount("/", _mcp_asgi)
