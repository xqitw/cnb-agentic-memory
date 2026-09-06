"""MCP Server 实现（由独立入口 cnb-agentic-memory-mcp 启动），把 Memory 语义层注册为 MCP 工具。

设计约定：
- 薄适配层：工具与 Memory 方法一一对应，业务逻辑（两步写入/回读校验/
  title 不变量/超长拆分/软删除）全部在 SDK 层
- 工具描述内嵌使用指导（title 撰写规范等），供智能体理解调用方式
- 错误处理：ApiError/MemoryRuleError 转为带错误说明的结果文本（isError），
  不包装语义，智能体收到后自行决策重试或降级
- 配置优先级：请求头（X-CNB-Token/X-CNB-Repo/X-CNB-Base-URL，多用户共享部署时
  每请求覆盖）> CNB_AGENTIC_MEMORY_ 环境变量；stdio 下无请求头，自然回落环境变量
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from email.message import Message
from importlib.metadata import PackageNotFoundError, metadata
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from . import __version__
from .api import CNBApiClient, build_client_from_headers, env
from .memory import Memory, MemoryRuleError, SearchResult, WriteResult

_DIST_NAME = "cnb-agentic-memory"  # PyPI 发行名（pyproject [project].name 同源）


def _dist_meta() -> Message | None:
    """本包安装元数据（pyproject [project] 单一来源；未安装时兜底 None）。

    metadata() 声明返回 PackageMetadata 协议，但运行时实现是
    email.message.Message（get/get_all 语义可用），故按实际类型标注。
    """
    try:
        return cast(Message, metadata(_DIST_NAME))
    except PackageNotFoundError:
        return None


_META = _dist_meta()


def _homepage_url() -> str | None:
    """仓库主页（pyproject [project.urls] 单一来源）。

    hatchling 将 [project.urls] 全部写入 Project-URL 多值头（无独立
    Homepage 头），故按 "name, url" 格式解析；缺失时兜底 None。
    """
    if _META is None:
        return None
    for entry in _META.get_all("Project-URL") or []:
        name, _, url = entry.partition(",")
        if name.strip().lower() == "homepage":
            return url.strip() or None
    return None


def _meta_field(name: str) -> str | None:
    """读取单个元数据头（未安装或缺失/空值时兜底 None）。

    PackageMetadata 底层是 email.message.Message，缺失 key 返回 None
    而非抛 KeyError，故用 get() + 显式判空，避免 str(None) 得到 'None'。
    """
    if _META is None:
        return None
    value = _META.get(name)
    return str(value) if value else None


def _summary() -> str | None:
    """包描述（pyproject description 同源；未安装或缺失时兜底 None）。"""
    return _meta_field("Summary")


def _version() -> str:
    """包版本（安装元数据优先，未安装或缺失时兜底 __version__）。"""
    return _meta_field("Version") or __version__


mcp = MCPServer(
    _DIST_NAME,
    title="CNB Issue 智能体记忆系统",  # pyproject 无人类可读标题字段，无法单一来源
    description=_summary(),
    version=_version(),
    website_url=_homepage_url(),
    instructions=(
        "CNB 智能体记忆工具：写入、检索、管理跨会话记忆。核心原则：\n"
        "1. 修正/补充已有记忆一律用 memory_update / memory_append，"
        "不要删除重建——memory_delete 是软删除，内容仍留在知识库向量中"
        "（include_closed 可召回），仅用于真正废弃。\n"
        "2. 写入失败若返回已落盘分片编号，按错误信息中的建议处理："
        "update 补齐/修正，勿重复 write（会重复创建）。超长内容拆分为"
        "多条时按返回的 parts 逐条处理，勿只处理首条。\n"
        "3. 写入后能否立即被 memory_search 检索到取决于仓库知识库同步配置"
        "（实时或定时入库，如每小时/每日），且受网络、记忆数量影响，"
        "检索不到不是写入失败；需立即确认时用 memory_get 按 number 回查。\n"
        "4. memory_list / memory_keyword_search 不回显正文（body 为 null），"
        "需要全文用 memory_get。"
    ),
)


def _client(ctx: Context | None) -> CNBApiClient:
    """按本次请求构造 CNBApiClient：请求头配置优先，回落环境变量（见 api.build_client_from_headers）。

    MCP 框架对标注 Context 的参数自动注入请求上下文（不进入工具 Schema），
    ctx.headers 在 sse/streamable-http 下为该次 HTTP 请求头，stdio 下为 None
    （无请求头 → 配置回落环境变量，与历史行为一致）。
    """
    return build_client_from_headers(ctx.headers if ctx is not None else None)


def _write_out(result: WriteResult) -> dict:
    """WriteResult 的输出形状（parts 供超长拆分循迹）。"""
    return {
        "number": result.number,
        "title": result.title,
        "parts": [{"number": p.number, "title": p.title} for p in result.parts],
    }


def _search_out(results: list[SearchResult]) -> list[dict]:
    """检索结果的输出形状。"""
    return [
        {
            "score": r.score,
            "number": r.number,
            "title": r.title,
            "state": r.state,
            "chunk": r.chunk,
        }
        for r in results
    ]


def _issue_out(issue: Any, *, body_echo: bool = True) -> dict:
    """记忆（Issue）的输出形状。

    body_echo=False 用于 memory_list/memory_keyword_search：CNB list 接口
    不回显正文，输出 null（诚实表达"未回显，需 memory_get 获取"）而非
    空字符串（会被误解为"正文恰好是空的"）。
    """
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body if body_echo else None,
        "state": issue.state,
        "labels": issue.label_names,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


@mcp.tool(
    description=(
        "写入一条记忆。title 必须由你撰写：从内容提炼 3~8 个高区分度的关键词短语"
        "（keyword 标题检索只匹配 title，它决定记忆能否被找回），不要写长句或"
        "概括性描述；不传 title 则由工具兜底截取正文首行。tags 每个元素一个标签，"
        "不要在一个元素里拼逗号。超长内容自动拆分为多条，返回的 parts 含"
        "全部分片编号。写入失败若返回已落盘分片编号，按错误信息中的建议处理"
        "（update 补齐/修正），勿重复 write。写入后需立即确认用 memory_get "
        "按 number 回查（能否立即语义检索取决于仓库同步配置，可能是定时入库）。"
    )
)
async def memory_write(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    ctx: Context | None = None,
) -> str:
    """写入记忆。category 自动补 category: 前缀（CNB 分类约定），tags 为普通标签。

    部分成功（拆分场景部分分片已落盘后失败）时返回 JSON：error 字段
    携带已落盘分片编号，供智能体循迹处理孤儿分片。
    """
    try:
        async with _client(ctx) as client:
            result = await Memory(client).write(content, title=title, tags=tags, category=category)
            return json.dumps(_write_out(result), ensure_ascii=False)
    except MemoryRuleError as err:
        return json.dumps({"error": str(err)}, ensure_ascii=False)


@mcp.tool(description="按编号精确读取记忆原文（正文 Markdown）")
async def memory_get(number: int, ctx: Context | None = None) -> str:
    """读取记忆。"""
    async with _client(ctx) as client:
        issue = await Memory(client).get(number)
        return json.dumps(_issue_out(issue), ensure_ascii=False)


@mcp.tool(
    description=(
        "更新记忆（修正/补齐已有记忆的首选方式，勿删除重建）：content 为"
        "全量替换，单条上限 30KB（不支持自动拆分）；增量信息用"
        " memory_append 追加。title 规则同 memory_write，传纯空白视为"
        "未提供而忽略；tags/category 为追加语义。"
    )
)
async def memory_update(
    number: int,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    ctx: Context | None = None,
) -> str:
    """更新记忆。"""
    async with _client(ctx) as client:
        issue = await Memory(client).update(
            number, content=content, title=title, tags=tags, category=category
        )
        return json.dumps(_issue_out(issue), ensure_ascii=False)


@mcp.tool(description="向记忆追加一条更新记录（进知识库，可被语义检索）")
async def memory_append(number: int, note: str, ctx: Context | None = None) -> str:
    """追加更新记录。"""
    async with _client(ctx) as client:
        comment = await Memory(client).append(number, note)
        return json.dumps(
            {"id": comment.id, "body": comment.body, "created_at": comment.created_at},
            ensure_ascii=False,
        )


@mcp.tool(
    description=(
        "软删除记忆（可恢复）。仅从默认检索与列表中隐藏，内容仍留在"
        "知识库向量中（include_closed 可召回）——不是内容清除。"
        "修正/补充记忆请用 memory_update，本工具仅用于真正废弃。"
    )
)
async def memory_delete(number: int, ctx: Context | None = None) -> str:
    """软删除记忆。"""
    async with _client(ctx) as client:
        issue = await Memory(client).delete(number)
        return json.dumps({"number": issue.number, "state": issue.state}, ensure_ascii=False)


@mcp.tool(description="恢复软删除的记忆")
async def memory_restore(number: int, ctx: Context | None = None) -> str:
    """恢复记忆。"""
    async with _client(ctx) as client:
        issue = await Memory(client).restore(number)
        return json.dumps({"number": issue.number, "state": issue.state}, ensure_ascii=False)


@mcp.tool(
    description=(
        "按分类/标签过滤记忆列表（结构化过滤，不回显正文，需要全文用"
        " memory_get）。语义检索请用 memory_search，两者互补：list 适合"
        "按已知分类浏览，search 适合按内容模糊查找。"
    )
)
async def memory_list(
    category: str | None = None,
    tags: list[str] | None = None,
    state: str = "open",
    limit: int = 20,
    ctx: Context | None = None,
) -> str:
    """过滤记忆列表。state 仅支持 open/closed（CNB API 不支持 all）。"""
    if state not in ("open", "closed"):
        return json.dumps({"error": "state 仅支持 open/closed（CNB API 不支持 all）"}, ensure_ascii=False)
    async with _client(ctx) as client:
        issues = await Memory(client).list(
            category=category, tags=tags, state=state, limit=max(1, min(limit, 100))
        )
        return json.dumps([_issue_out(i, body_echo=False) for i in issues], ensure_ascii=False)


@mcp.tool(description="最近更新的记忆")
async def memory_list_recent(limit: int = 5, ctx: Context | None = None) -> str:
    """最近记忆。"""
    async with _client(ctx) as client:
        issues = await Memory(client).list_recent(limit=max(1, min(limit, 100)))
        return json.dumps([_issue_out(i) for i in issues], ensure_ascii=False)


@mcp.tool(
    description=(
        "语义检索记忆（知识库向量召回，按相关度排序）。适合按内容模糊查找，"
        "即使记不清确切用词也能命中；若记忆 title 中含有确切关键词（技术名词、"
        "编号），用 memory_keyword_search 更精准。知识库不可用时按错误提示处理。"
    )
)
async def memory_search(
    query: str,
    top_k: int = 5,
    include_closed: bool = False,
    ctx: Context | None = None,
) -> str:
    """语义检索记忆。"""
    async with _client(ctx) as client:
        results = await Memory(client).search(
            query, top_k=max(1, min(top_k, 100)), include_closed=include_closed
        )
        return json.dumps(_search_out(results), ensure_ascii=False)


@mcp.tool(
    description=(
        "关键词标题检索：仅匹配标题，无法检索正文（记忆仓库须为专用仓库，"
        "否则普通 Issue 会一并命中）。结果不回显正文（body 为 null），"
        "需要全文用 memory_get。"
        "当记忆 title 中含有确切关键词（技术名词、编号、命令）时比语义检索更精准。"
        "与 memory_search 并列的第二检索方法，按需选择。"
    )
)
async def memory_keyword_search(
    query: str,
    limit: int = 20,
    include_closed: bool = False,
    ctx: Context | None = None,
) -> str:
    """关键词标题检索记忆。"""
    async with _client(ctx) as client:
        issues = await Memory(client).keyword_search(
            query, limit=max(1, min(limit, 100)), include_closed=include_closed
        )
        return json.dumps([_issue_out(i, body_echo=False) for i in issues], ensure_ascii=False)


DEFAULT_PORT = 8000  # HTTP transport 默认端口

_TRANSPORTS = ("stdio", "sse", "streamable-http")

DEFAULT_HOST = "127.0.0.1"


def parse_host(value: str | None) -> str:
    """解析监听地址：键存在但值为空（容器编排常见）回落 127.0.0.1。

    空串透传 uvicorn 会绑定全部网卡（等效 0.0.0.0），却绕过通配安全提醒，
    故与 parse_transport/parse_port 同口径清洗。
    """
    stripped = (value or "").strip()
    return stripped or DEFAULT_HOST


def parse_allowed_hosts(value: str | None) -> list[str]:
    """解析额外 Host 白名单（逗号分隔，空/空白返回空列表）。

    用于反代保留真实 Host 域名的部署：把对外域名追加进 DNS rebinding 防护
    白名单，否则标准反代转发（proxy_set_header Host $host）会被 421 拒绝。
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def normalize_allowed_host(entry: str) -> str:
    """把 --allowed-host 条目归一化为「Host 基名」（IPv6 裹方括号）。

    纯域名 / ``host:*`` / ``host:port`` / ``[IPv6]`` 四种输入形态统一产出同一
    基名，由调用方按需生成 :* 端口通配与无端口精确两种白名单形态。
    ``host:port`` 必须先经 ``ipaddress`` 判别是否真 IPv6，否则裸 IPv6 带
    端口之外的 ``mem.example.com:8443`` 会被「含冒号即裹括号」误判成
    ``[mem.example.com:8443]`` 而永不匹配（复审致命项）。
    """
    value = entry.strip()
    if value.endswith(":*"):
        value = value[:-2]
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return f"[{value}]" if ipaddress.ip_address(value).version == 6 else value
    except ValueError:
        pass
    if ":" in value:
        host, _, port = value.rpartition(":")
        if port.isdigit() and host:
            return normalize_allowed_host(host)
    return value


def parse_transport(value: str | None) -> str:
    """解析传输协议：空白/大小写/下划线连字符笔误清洗，非法值回落 stdio。

    argparse choices 只校验命令行值、不校验 default，环境变量的异常值若不
    清洗会穿透到框架 MCPServer.run() 抛 ValueError 使服务启动即崩。
    """
    normalized = (value or "").strip().lower().replace("_", "-")
    return normalized if normalized in _TRANSPORTS else "stdio"


def parse_port(value: str | None) -> int:
    """解析端口：空/非法/越界回落默认 8000（与 api.parse_timeout 同口径，避免 int('') 崩启动）。

    仅供环境变量兜底使用；CLI 显式传参走 parse_port_strict，非法值直接报错。
    """
    try:
        port = int(value) if value else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def parse_port_strict(value: str) -> int:
    """CLI 端口严格校验：非法/越界直接报 argparse 错误退出（exit 2），不静默回落。

    环境变量的异常值静默回落是启动健壮性防御；用户显式敲错命令行参数则
    应立即暴露，静默改用 8000 会造成「服务起在了意外端口」的困惑。
    """
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口必须是整数：{value!r}") from None
    if not 0 < port < 65536:
        raise argparse.ArgumentTypeError(f"端口须在 1~65535 之间：{value!r}")
    return port


def main(argv: list[str] | None = None) -> None:
    """MCP Server 启动入口（由独立入口 cnb-agentic-memory-mcp 调用）。

    支持 stdio / sse / streamable-http 三种 transport（默认 stdio，与
    历史行为一致）：stdio 供客户端以子进程方式拉起；sse 与
    streamable-http 供远程接入，监听 --host/--port，路径由 MCP 框架
    固定（SSE: /sse + /messages/，streamable-http: /mcp）。
    CLI 参数优先，环境变量兜底（沿用 CNB_AGENTIC_MEMORY_ 前缀）。
    """
    parser = argparse.ArgumentParser(
        prog="cnb-agentic-memory-mcp",
        description="CNB Issue 智能体记忆 MCP Server",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=parse_transport(env("MCP_TRANSPORT")),
        help="传输协议（默认 stdio；环境变量 CNB_AGENTIC_MEMORY_MCP_TRANSPORT）",
    )
    parser.add_argument(
        "--host",
        type=parse_host,
        default=parse_host(env("MCP_HOST")),
        help="HTTP 监听地址，仅 sse/streamable-http 有效（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=parse_port_strict,
        default=parse_port(env("MCP_PORT")),
        help="HTTP 监听端口，仅 sse/streamable-http 有效（默认 8000）",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        # 拷贝一份再追加双保险：argparse 自 3.9 起 action="append" 会先拷贝
        # default 再追加（bpo-33519），本项目 requires-python >= 3.11 下不会
        # 原地修改；显式拷贝防御未来行为回退，多次 parse_args 互不污染
        default=list(parse_allowed_hosts(env("MCP_ALLOWED_HOSTS"))),
        help="DNS rebinding 防护额外放行的 Host 白名单（可多次传入，如反代转发的对外域名；环境变量 CNB_AGENTIC_MEMORY_MCP_ALLOWED_HOSTS，逗号分隔）",
    )
    args = parser.parse_args(argv)

    if args.transport != "stdio" and args.host in ("0.0.0.0", "::"):
        # HTTP 模式无内置鉴权，且框架对通配地址不自动开 DNS rebinding 防护；
        # 裸跑公网等于把凭据暴露给任意可达方，启动时显式提醒（不阻断）
        print(
            f"警告：HTTP transport 监听通配地址 {args.host} 且无内置鉴权，"
            "请务必置于反向代理/网关之后（访问控制 + HTTPS + DNS rebinding 防护）再对外暴露",
            file=sys.stderr,
        )

    if args.transport == "stdio":
        mcp.run()
    else:
        # sse / streamable-http：host/port 透传给 MCP 框架的 uvicorn 启动参数。
        # 框架仅对 localhost 自动开 DNS rebinding 防护，其他监听地址显式透传
        # TransportSecuritySettings 保持防护常开。白名单 = localhost 族 + 监听
        # 地址直连形式 + --allowed-host 追加项（反代保留真实 Host 的部署场景）。
        from mcp.server.transport_security import TransportSecuritySettings

        # 白名单形态统一生成（复审整改）：框架对 :* 通配的匹配要求 Host/Origin
        # 值带显式端口（startswith(base + ":")），而浏览器在默认端口（80/443）下
        # 不序列化端口（WHATWG origin 序列化），故每个基名同时生成 :* 端口通配
        # （非标准端口兜底）与无端口精确（默认端口场景）两种 Host 条目，恶意
        # 域名仍被精确匹配语义拒之门外，防护面未放宽。
        host_bases = dict.fromkeys(
            ["localhost", "127.0.0.1", "[::1]", "[::ffff:127.0.0.1]", normalize_allowed_host(args.host)]
        )
        extra_bases: list[str] = []
        for extra in args.allowed_host:
            base = normalize_allowed_host(extra)
            if base and base not in extra_bases:
                extra_bases.append(base)
        # Origin 口径：本机直连为纯 HTTP（uvicorn 无 TLS）单 scheme；--allowed-host
        # 是反代对外域名，反代入口多为 HTTPS（浏览器 Origin 带 https scheme），
        # 追加域名放行 http/https 双 scheme，均含通配与精确两形态
        allowed_hosts = (
            [f"{b}:*" for b in host_bases] + list(host_bases) + [f"{b}:*" for b in extra_bases] + extra_bases
        )
        allowed_origins = (
            [f"http://{b}" for b in host_bases]
            + [f"http://{b}:*" for b in host_bases]
            + [
                f"{scheme}://{b}{':*' if with_port else ''}"
                for b in extra_bases
                for with_port in (False, True)
                for scheme in ("http", "https")
            ]
        )

        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            transport_security=security,
        )


if __name__ == "__main__":
    main()
