"""MCP Server 实现（由 CLI 子命令 cnb-agentic-memory mcp 启动），把 Memory 语义层注册为 MCP 工具。

设计约定：
- 薄适配层：工具与 Memory 方法一一对应，业务逻辑（两步写入/回读校验/
  title 不变量/超长拆分/软删除）全部在 SDK 层
- 工具描述内嵌使用指导（title 撰写规范等），供智能体理解调用方式
- 错误处理：ApiError/MemoryError 转为带错误说明的结果文本（isError），
  不包装语义，智能体收到后自行决策重试或降级
- 配置统一 CNB_AGENTIC_MEMORY_ 环境变量（CNB_AGENTIC_MEMORY_TOKEN/CNB_AGENTIC_MEMORY_REPO/CNB_AGENTIC_MEMORY_BASE_URL/CNB_AGENTIC_MEMORY_TIMEOUT）
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .api import CNBApiClient
from .memory import Memory, MemoryError, SearchResult, WriteResult

mcp = MCPServer(
    "cnb-agentic-memory",
    instructions=(
        "CNB 智能体记忆工具：写入、检索、管理跨会话记忆。核心原则：\n"
        "1. 修正/补充已有记忆一律用 memory_update / memory_append，"
        "不要删除重建——memory_delete 是软删除，内容仍留在知识库向量中"
        "（include_closed 可召回），仅用于真正废弃。\n"
        "2. 写入失败若返回已落盘分片编号，按错误信息中的建议处理："
        "update 补齐/修正，勿重复 write（会重复创建）。超长内容拆分为"
        "多条时按返回的 parts 逐条处理，勿只处理首条。\n"
        "3. 刚写入的记忆需 1~2 分钟知识库同步才能被 memory_search 检索到，"
        "这是平台预期不是故障；精确确认用 memory_get。\n"
        "4. memory_list / memory_keyword_search 不回显正文（body 为 null），"
        "需要全文用 memory_get。"
    ),
)


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
        "（update 补齐/修正），勿重复 write。"
    )
)
async def memory_write(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
) -> str:
    """写入记忆。category 自动补 category: 前缀（CNB 分类约定），tags 为普通标签。

    部分成功（拆分场景部分分片已落盘后失败）时返回 JSON：error 字段
    携带已落盘分片编号，供智能体循迹处理孤儿分片。
    """
    try:
        async with CNBApiClient() as client:
            result = await Memory(client).write(content, title=title, tags=tags, category=category)
            return json.dumps(_write_out(result), ensure_ascii=False)
    except MemoryError as err:
        return json.dumps({"error": str(err)}, ensure_ascii=False)


@mcp.tool(description="按编号精确读取记忆原文（正文 Markdown）")
async def memory_get(number: int) -> str:
    """读取记忆。"""
    async with CNBApiClient() as client:
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
) -> str:
    """更新记忆。"""
    async with CNBApiClient() as client:
        issue = await Memory(client).update(
            number, content=content, title=title, tags=tags, category=category
        )
        return json.dumps(_issue_out(issue), ensure_ascii=False)


@mcp.tool(description="向记忆追加一条更新记录（进知识库，可被语义检索）")
async def memory_append(number: int, note: str) -> str:
    """追加更新记录。"""
    async with CNBApiClient() as client:
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
async def memory_delete(number: int) -> str:
    """软删除记忆。"""
    async with CNBApiClient() as client:
        issue = await Memory(client).delete(number)
        return json.dumps({"number": issue.number, "state": issue.state}, ensure_ascii=False)


@mcp.tool(description="恢复软删除的记忆")
async def memory_restore(number: int) -> str:
    """恢复记忆。"""
    async with CNBApiClient() as client:
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
) -> str:
    """过滤记忆列表。state 仅支持 open/closed（CNB API 不支持 all）。"""
    if state not in ("open", "closed"):
        return json.dumps({"error": "state 仅支持 open/closed（CNB API 不支持 all）"}, ensure_ascii=False)
    async with CNBApiClient() as client:
        issues = await Memory(client).list(
            category=category, tags=tags, state=state, limit=max(1, min(limit, 100))
        )
        return json.dumps([_issue_out(i, body_echo=False) for i in issues], ensure_ascii=False)


@mcp.tool(description="最近更新的记忆")
async def memory_list_recent(limit: int = 5) -> str:
    """最近记忆。"""
    async with CNBApiClient() as client:
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
) -> str:
    """语义检索记忆。"""
    async with CNBApiClient() as client:
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
) -> str:
    """关键词标题检索记忆。"""
    async with CNBApiClient() as client:
        issues = await Memory(client).keyword_search(
            query, limit=max(1, min(limit, 100)), include_closed=include_closed
        )
        return json.dumps([_issue_out(i, body_echo=False) for i in issues], ensure_ascii=False)


def main() -> None:
    """MCP Server 启动入口（由 CLI 子命令 cnb-agentic-memory mcp 调用）。"""
    mcp.run()


if __name__ == "__main__":
    main()
