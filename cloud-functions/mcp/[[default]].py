"""EdgeOne Pages Python 运行时适配层：streamable-http 形态承载 MCP Server（#91）。

EO 运行时把函数目录前缀（/mcp）剥掉后把请求交给本 ASGI 应用，MCP 客户端连接
https://<project>.edgeone.app/mcp。Serverless 短执行模型（上限 120s）不适配 SSE
长连接与有状态 session，故 json_response + stateless_http 双开。

环境变量（EO 控制台配置，值须 ≤500 字节）：
- CNB_AGENTIC_MEMORY_REQUIRE_HEADERS=1：强制凭据头（多用户共享部署防匿名调用，
  凭据一律由调用方经 X-CNB-Token / X-CNB-Repo 头传递，服务端不持有）
- CNB_AGENTIC_MEMORY_MCP_PUBLIC_HOST：对外域名基名（如 cam.xqitw.cool），作
  Origin 白名单
- CNB_AGENTIC_MEMORY_MCP_INTERNAL_HOST：EO 内部源站域名（实测 Host 头被改写为
  pages-*.qcloudteo.com 形态，#91），作 Host 白名单；平台侧变更该域名时须同步
  更新（表现为请求 421）

两 HOST 变量缺任一则防护不启用并打警告（仅限测试部署）；EO 边缘按 Host 路由，
恶意 Host 在平台层已被拒（418），函数内 Host 校验锁内部源站、Origin 校验防
浏览器跨源。部署步骤与实测结论回填见 docs/EdgeOne.md。
"""

import warnings
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cnb_agentic_memory.api import env
from cnb_agentic_memory.mcp_server import build_transport_security, configure_require_headers, mcp

# require-headers 激活必须显式调用：适配层不经 main()，argparse default 的 env
# 兜底在此路径不生效，开关恒 False（幽明 #91 实测）——语义与 CLI 完全同源
configure_require_headers()

_public_host = env("MCP_PUBLIC_HOST")
_internal_host = env("MCP_INTERNAL_HOST")
if _public_host and _internal_host:
    # EO 为 HTTPS 平台，浏览器/客户端 Origin 序列化为 https，白名单 scheme 取 https；
    # Host 与 Origin 基名分离（#91 实测：Host 被改写为内部源站，Origin 原样透传）
    _transport_security = build_transport_security(
        _internal_host, origin_bases=(_public_host,), origin_schemes=("https",)
    )
else:
    # 测试部署兜底：两 HOST 变量缺任一时防护不启用（框架不校验 Host/Origin）。
    # 生产部署必须配置，否则对外端点暴露于浏览器跨源面——warnings.warn 收口
    # （默认打印一次至 stderr，EO 采为函数日志），不用裸 print
    warnings.warn(
        "未配齐 CNB_AGENTIC_MEMORY_MCP_PUBLIC_HOST / CNB_AGENTIC_MEMORY_MCP_INTERNAL_HOST，"
        "Host/Origin 校验未启用；生产部署必须配置（PUBLIC_HOST=对外域名，"
        "INTERNAL_HOST=EO 内部源站域名，见 docs/EdgeOne.md）",
        RuntimeWarning,
        stacklevel=2,
    )
    _transport_security = None

# EO 剥掉函数目录前缀后 MCP 端点落在函数根路径，故 streamable_http_path 取 "/"。
# 注意：mount 进 FastAPI 后子应用 lifespan 不会自动执行，session manager 未启动
# 即抛 "Task group is not initialized"——须在 FastAPI lifespan 中手动进入子应用
# 的 lifespan_context（MCP SDK 官方挂载模式）
_mcp_asgi = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    transport_security=_transport_security,
    # transport_security=None 分支（两 HOST 变量缺任一）时 host 必须传非 localhost 值：
    # 框架对 localhost 族默认自动开 DNS rebinding 防护（allowed_hosts 仅本机族），
    # EO 转发的内部源站 Host 必被 421；传非 localhost 使框架不做任何 Host/Origin 校验
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
