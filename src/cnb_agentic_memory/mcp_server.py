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
import functools
import ipaddress
import json
import sys
from email.message import Message
from importlib.metadata import PackageNotFoundError, metadata
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context

from . import __version__
from .api import CNBApiClient, build_client_from_headers, env, resolve_overrides_from_headers
from .memory import Memory, MemoryRuleError, SearchResult, WriteResult

_DIST_NAME = "cnb-agentic-memory"  # PyPI 发行名（pyproject [project].name 同源）

# --require-headers 开启后，HTTP 模式下凭据头不齐的请求在工具入口即拒绝，
# 不再回落服务端环境变量凭据（#83 建议第 2 条：杜绝共享部署下的匿名调用）。
# stdio 下无请求头是常态，开关不生效（否则自断）；env 值语义 1/true/yes/on 开。
_REQUIRE_HEADERS_TRUTHY = frozenset({"1", "true", "yes", "on"})

# 运行时开关状态：main 解析 --require-headers 后设置；模块级变量而非工具参数——
# 工具签名不得携带与调用语义无关的部署开关（会污染 MCP Schema）
_require_headers: bool = False


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

    MCP 框架对标注 Context 的参数自动注入请求上下文（不进入工具 Schema）：
    sse/streamable-http 下 ctx.headers 为该次 HTTP 请求头；stdio 下 ctx 仍被
    无条件注入（恒非 None）但 headers 为 None——故凭据校验以「有无请求头」
    判传输，不能用 ctx is not None（复审阻塞项：会误杀 stdio 全部工具）。

    --require-headers 开启时（HTTP 共享部署强制多用户隔离），凭据头不齐的
    请求直接拒绝，不回落服务端环境变量凭据——杜绝匿名调用间接使用
    CNB_AGENTIC_MEMORY_TOKEN。stdio 下开关不生效（无请求头是常态）。
    """
    headers = ctx.headers if ctx is not None else None
    if _require_headers and headers is not None:
        # 仅约束 HTTP 传输：stdio 无请求头是常态，不适用本开关
        overrides = resolve_overrides_from_headers(headers)
        if not overrides:
            raise MemoryRuleError(
                "本服务已启用 --require-headers（强制凭据头）：请在请求头中同时提供 "
                "X-CNB-Token 与 X-CNB-Repo（HTTP 共享部署的多用户隔离要求），"
                "匿名请求不再回落服务端环境变量凭据。"
            )
    return build_client_from_headers(headers)


def _tool_guard(fn):
    """工具统一错误出口：MemoryRuleError 转为 {"error": ...} JSON 结果文本。

    不加此出口，MemoryRuleError 穿透无捕获的工具被框架包成笼统的
    "Error executing tool ..."（复审阻塞项）：调用方拿不到修复指引，
    且每次匿名探测都打 ERROR 级故障栈。--require-headers 的拒绝
    （凭据头不齐）是可预期的业务拒绝，与 memory_write 既有错误形状
    同源，客户端收到后自行决策补凭据头重试。
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except MemoryRuleError as err:
            return json.dumps({"error": str(err)}, ensure_ascii=False)

    return wrapper


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
@_tool_guard
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
@_tool_guard
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
@_tool_guard
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
@_tool_guard
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
@_tool_guard
async def memory_delete(number: int, ctx: Context | None = None) -> str:
    """软删除记忆。"""
    async with _client(ctx) as client:
        issue = await Memory(client).delete(number)
        return json.dumps({"number": issue.number, "state": issue.state}, ensure_ascii=False)


@mcp.tool(description="恢复软删除的记忆")
@_tool_guard
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
@_tool_guard
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
@_tool_guard
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
@_tool_guard
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
@_tool_guard
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


def validate_listen_host(value: str) -> str:
    """校验监听地址（--host），返回清洗后的值；非法 raise ValueError。

    监听地址与白名单条目是两种语义：前者要的是整体合法的
    地址（域名 / IPv4 / IPv6 / [IPv6]），不允许 host:port 合并形态——端口由
    --port 单独指定，合并形态会被 uvicorn 原样透传 getaddrinfo 失败、启动
    即崩；按监听地址语义独立判别（复审阻塞项）：

    1. 先 `ipaddress.ip_address()` 判裸 IP（含 `::1`、`0.0.0.0`、`::`）——
       判别顺序必须先于拆分，否则 `::1` 会被 rpartition 误拆成 `::` + `1`；
    2. `[IPv6]` 剥方括号后同样按裸 IP 校验，返回裸 IPv6（getaddrinfo 不认
       方括号形态，uvicorn 需裸地址）；
    3. 其余含冒号值 `rpartition(":")` 拆分：端口段纯数字即视为 host:port
       合并形态，拒绝；其他含冒号形态一并拒绝（域名不含冒号）。

    全角冒号、方括号不完整、空串均拒绝。
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("监听地址为空")
    if "：" in stripped:
        raise ValueError("含全角冒号，请改用半角")
    candidate = stripped
    bracketed = candidate.startswith("[")
    if bracketed:
        # [IPv6] 与 [IPv6]:port：以 "]" 为界剥壳与端口，再按裸 IP 校验
        inner, sep, tail = candidate[1:].partition("]")
        if not sep or (tail and not tail.startswith(":")):
            raise ValueError("方括号 IPv6 不完整（形如 [::1]）")
        if tail.count(":") > 1 or (tail.startswith(":") and not tail[1:].isdigit()):
            raise ValueError("方括号后只允许跟一个数字端口（形如 [::1]:8000）")
        if len(tail) > 1:
            # [IPv6]:port 的端口段不静默丢弃：监听端口由 --port 指定，静默改用
            # 别的端口会让「服务起在意外地址」且 env 通道无告警（复审 warning）
            raise ValueError(f"方括号形态不接受端口号（{stripped!r}），端口请用 --port 指定")
        candidate = inner
        if not candidate:
            raise ValueError("方括号内为空")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        ip = None
    if ip is not None:
        if bracketed and ip.version != 6:
            # 方括号只用于 IPv6（RFC 3986），[127.0.0.1] 这类 IPv4 裹括号非法
            raise ValueError("方括号内须为 IPv6 地址，IPv4 不裹方括号")
        return candidate
    if ":" in candidate:
        host, _, port = candidate.rpartition(":")
        if port.isdigit() and host:
            raise ValueError(f"监听地址不接受 host:port 合并形态（{stripped!r}），端口请用 --port 指定")
        raise ValueError("IPv6 或含冒号地址不完整")
    if bracketed:
        # 方括号只用于 IPv6（RFC 3986），[myhost] 这类域名裹括号非法
        raise ValueError("方括号内须为合法 IPv6 地址")
    return candidate


def parse_host(value: str | None) -> str:
    """CLI --host 解析：按监听地址语义校验，空值回落 127.0.0.1，畸形报 argparse 错误（exit 2）。

    空串透传 uvicorn 会绑定全部网卡（等效 0.0.0.0），却绕过通配安全提醒，
    故与 parse_transport/parse_port 同口径清洗。host:port 合并形态（把端口
    并进 host 的常见敲错）按监听地址语义拒绝，不复用白名单归一化（复审
    阻塞项：normalize 判其合法，CLI 不报错但 uvicorn 启动即崩，env 通道
    还会把剥壳基名混入白名单）。环境变量兜底走 parse_host_env，告警回落
    不崩启动。
    """
    stripped = (value or "").strip()
    if not stripped:
        # 空值是「未指定」语义：回落默认地址（沿用既有行为），不算畸形
        return DEFAULT_HOST
    try:
        # 取校验返回值而非原值：[IPv6]/[IPv6]:port 剥壳为裸地址，带壳原值
        # 直传 uvicorn 会被当作主机名 sock.bind 即崩（复审致命项）
        return validate_listen_host(stripped)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"监听地址无法解析：{stripped!r}（{err}）") from None


def parse_host_env(value: str | None) -> str:
    """环境变量 MCP_HOST 兜底解析：畸形值 stderr 告警回落 127.0.0.1，不崩启动。

    env default 不经 argparse type 校验，畸形值若不清洗会穿透到 mcp.run
    裸 traceback 崩启动（复审阻塞项）；CLI 显式传错则直接报错退出。
    """
    stripped = (value or "").strip()
    if not stripped:
        # 空值是「未指定」语义：回落默认地址（test_main_empty_host_env_falls_back 锚定），不告警
        return DEFAULT_HOST
    try:
        # 取校验返回值而非原值：剥壳语义与 CLI 通道同口径（复审致命项）
        return validate_listen_host(stripped)
    except ValueError as err:
        print(
            f"警告：CNB_AGENTIC_MEMORY_MCP_HOST {stripped!r} 无法解析（{err}），回落 {DEFAULT_HOST}",
            file=sys.stderr,
        )
        return DEFAULT_HOST


def whitelist_host_base(host: str) -> str:
    """把监听地址转为白名单「Host 基名」：裸 IPv6 裹方括号（RFC 3986），其余原样。

    仅服务 --host 的白名单生成这一个职责；原 --allowed-host 条目的多形态
    归一化（host:port / host:* 剥壳等）随参数裁剪一并移除（#85）。
    调用前 host 已过 validate_listen_host，此处只做形态转换，不校验。
    """
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


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
        default=parse_host_env(env("MCP_HOST")),
        help="HTTP 监听地址，仅 sse/streamable-http 有效（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--port",
        type=parse_port_strict,
        default=parse_port(env("MCP_PORT")),
        help="HTTP 监听端口，仅 sse/streamable-http 有效（默认 8000）",
    )
    parser.add_argument(
        "--require-headers",
        action="store_true",
        # 布尔开关无 default 解析问题：env 缺省关闭（兼容旧行为），truthy 值开启
        default=(env("REQUIRE_HEADERS") or "").strip().lower() in _REQUIRE_HEADERS_TRUTHY,
        help="强制要求凭据头：HTTP 模式下凭据头（X-CNB-Token/X-CNB-Repo）不齐的请求直接拒绝，不回落服务端环境变量凭据（多用户共享部署防匿名调用；stdio 不受影响；环境变量 CNB_AGENTIC_MEMORY_REQUIRE_HEADERS=1）",
    )
    args = parser.parse_args(argv)

    global _require_headers
    _require_headers = args.require_headers

    # #85 裁剪 --allowed-host 后的废弃提示：存量部署的该 env 会被无声吞掉，
    # 反代保留真实 Host 的场景升级后全量 421 且无告警——显式提醒迁移路径
    if env("MCP_ALLOWED_HOSTS"):
        print(
            "警告：CNB_AGENTIC_MEMORY_MCP_ALLOWED_HOSTS 已随 --allowed-host 移除（#85），"
            "本次启动被忽略；反代部署请改写 Host/Origin（如 proxy_set_header Host localhost; "
            'proxy_set_header Origin "";）或由代理层完成白名单校验',
            file=sys.stderr,
        )

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
        # TransportSecuritySettings 保持防护常开。白名单固定为 localhost 族 +
        # 监听地址直连形式，不提供扩展入口：对外部署一律置于反代之后，访问
        # 控制与 Host 白名单属代理层职责（扩展白名单入口已按 #85 裁剪——其
        # 输入形态 × 匹配语义矩阵的维护成本远超防御价值，见 PR !84 八轮复审）。
        from mcp.server.transport_security import TransportSecuritySettings

        # 框架对 :* 通配的匹配要求 Host/Origin 值带显式端口（startswith(base + ":")），
        # 而浏览器在默认端口（80/443）下不序列化端口（WHATWG origin 序列化），故每个
        # 基名同时生成 :* 端口通配（非标准端口兜底）与无端口精确（默认端口场景）两种
        # Host 条目，恶意域名仍被精确匹配语义拒之门外。
        host_bases = dict.fromkeys(
            ["localhost", "127.0.0.1", "[::1]", "[::ffff:127.0.0.1]", whitelist_host_base(args.host)]
        )
        # Origin 口径：本机直连为纯 HTTP（uvicorn 无 TLS）单 scheme，含通配与精确两形态
        allowed_hosts = [f"{b}:*" for b in host_bases] + list(host_bases)
        allowed_origins = [f"http://{b}" for b in host_bases] + [f"http://{b}:*" for b in host_bases]

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
