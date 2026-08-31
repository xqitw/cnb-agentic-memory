"""cnb-agentic-memory 命令行入口：薄封装 Memory 语义层。

设计约定：
- 输出 JSON（--json 或默认）便于智能体消费，人类可读格式仅 list/search 展示用
- 错误透传：ApiError/MemoryRuleError 输出到 stderr 并以非零码退出，不包装语义
- 配置统一走 CNB_AGENTIC_MEMORY_ 环境变量（与 SDK/MCP 一致），命令行参数可覆盖
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import typer

from . import __version__
from .api import ApiError, CNBApiClient, ConfigError, env
from .memory import STATE_CLOSED, STATE_OPEN, Memory, MemoryRuleError

app = typer.Typer(
    name="cnb-agentic-memory",
    help="CNB Issue 智能体记忆系统：基于 CNB 平台的通用智能体记忆工具",
    no_args_is_help=False,  # 无参数时由 callback 显示 help，避免拦截 --version
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="显示版本号并退出",
    ),
) -> None:
    """cnb-agentic-memory：跨会话记忆的写入、检索与管理。"""
    if version:
        typer.echo(f"cnb-agentic-memory {__version__}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def _execute(coro_factory: Any) -> Any:
    """同步入口：构造客户端、运行协程、统一错误出口。

    coro_factory 接收 Memory 实例并返回协程。
    """

    async def runner() -> Any:
        async with CNBApiClient() as client:
            memory = Memory(client)
            return await coro_factory(memory)

    try:
        return asyncio.run(runner())
    except ConfigError as err:
        typer.echo(f"配置错误：{err}", err=True)
        typer.echo("请设置环境变量后重试（配置说明见 docs/CLI.md）", err=True)
        raise typer.Exit(2) from err
    except ApiError as err:
        typer.echo(f"API 错误（{err.status_code}）：{err.message}", err=True)
        raise typer.Exit(1) from err
    except MemoryRuleError as err:
        typer.echo(f"错误：{err}", err=True)
        raise typer.Exit(1) from err
    except httpx.HTTPError as err:
        typer.echo(f"网络错误：{type(err).__name__}: {err}", err=True)
        typer.echo("请检查网络连接后重试", err=True)
        raise typer.Exit(1) from err
    except Exception as err:
        # 未预期异常：默认友好一行；CNB_AGENTIC_MEMORY_DEBUG=1 时抛出完整 traceback 定位代码缺陷
        typer.echo(f"内部错误：{type(err).__name__}: {err}", err=True)
        if env("DEBUG"):
            raise
        typer.echo("如需查看完整堆栈，请设置 CNB_AGENTIC_MEMORY_DEBUG=1 后重试", err=True)
        raise typer.Exit(70) from err


def _dump(data: Any) -> None:
    """JSON 输出（ensure_ascii=False，中文可读）。"""
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _issue_out(issue: Any, *, body_echo: bool = True) -> dict:
    """Issue 的 JSON 输出形状。

    body_echo=False 用于 list/keyword：CNB list 接口不回显正文，
    输出 null（诚实表达"未回显，需 get 获取"）而非空字符串（会被
    误解为"正文恰好是空的"）。
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


@app.command()
def write(
    content: str = typer.Argument(..., help="记忆正文（Markdown）"),
    title: str = typer.Option(
        None,
        "--title",
        "-t",
        help="标题：提炼高区分度关键词短语（keyword 检索只匹配标题）；不传则兜底为正文首行截取",
    ),
    tag: list[str] = typer.Option(
        None, "--tag", help="标签，可多次传入；单值内逗号会拆分为多标签（如 --tag 'a,b'）"
    ),
    category: str = typer.Option(None, "--category", "-c", help="分类（自动补 category: 前缀）"),
) -> None:
    """写入一条记忆（两步写入 + 回读校验；超长自动拆分）。"""

    def run(memory: Memory) -> Any:
        return memory.write(content, title=title, tags=tag, category=category)

    result = _execute(run)
    _dump(
        {
            "number": result.number,
            "title": result.title,
            "parts": [{"number": p.number, "title": p.title} for p in result.parts],
        }
    )


@app.command()
def get(number: int = typer.Argument(..., help="记忆编号")) -> None:
    """精确读取记忆原文。"""

    def run(memory: Memory) -> Any:
        return memory.get(number)

    issue = _execute(run)
    _dump(_issue_out(issue))


@app.command()
def update(
    number: int = typer.Argument(..., help="记忆编号"),
    content: str = typer.Option(None, "--content", help="新正文（全量替换）"),
    title: str = typer.Option(None, "--title", "-t", help="新标题（工具保证不变量）"),
    tag: list[str] = typer.Option(None, "--tag", help="追加标签，可多次传入；单值内逗号会拆分为多标签"),
    category: str = typer.Option(None, "--category", help="追加分类"),
) -> None:
    """更新记忆（修正/补齐已有记忆的首选方式，勿删除重建）。"""

    def run(memory: Memory) -> Any:
        return memory.update(number, content=content, title=title, tags=tag, category=category)

    issue = _execute(run)
    _dump(_issue_out(issue))


@app.command()
def append(
    number: int = typer.Argument(..., help="记忆编号"),
    note: str = typer.Argument(..., help="追加的更新记录内容"),
) -> None:
    """追加更新记录（评论，进知识库可被语义检索）。"""

    def run(memory: Memory) -> Any:
        return memory.append(number, note)

    comment = _execute(run)
    _dump({"id": comment.id, "body": comment.body, "created_at": comment.created_at})


@app.command()
def delete(number: int = typer.Argument(..., help="记忆编号")) -> None:
    """软删除记忆（仅默认检索隐藏，内容仍留知识库向量；真正废弃才用）。"""

    def run(memory: Memory) -> Any:
        return memory.delete(number)

    issue = _execute(run)
    _dump({"number": issue.number, "state": issue.state})


@app.command()
def restore(number: int = typer.Argument(..., help="记忆编号")) -> None:
    """恢复软删除的记忆。"""

    def run(memory: Memory) -> Any:
        return memory.restore(number)

    issue = _execute(run)
    _dump({"number": issue.number, "state": issue.state})


@app.command("list")
def list_cmd(
    category: str = typer.Option(None, "--category", "-c", help="按分类过滤"),
    tag: list[str] = typer.Option(None, "--tag", help="按标签过滤，可多次传入；单值内逗号会拆分为多标签"),
    state: str = typer.Option("open", "--state", help="生命周期状态过滤：open/closed（CNB API 不支持 all）"),
    limit: int = typer.Option(20, "--limit", "-l", help="返回条数上限（1~100）"),
) -> None:
    """按分类/标签过滤记忆列表（不回显正文，需全文用 get <编号>）。"""
    if state not in (STATE_OPEN, STATE_CLOSED):
        typer.echo("错误：--state 仅支持 open/closed", err=True)
        raise typer.Exit(2)
    limit = max(1, min(limit, 100))  # 服务端分页上限 100/页

    def run(memory: Memory) -> Any:
        return memory.list(category=category, tags=tag, state=state, limit=limit)

    issues = _execute(run)
    _dump([_issue_out(i, body_echo=False) for i in issues])


@app.command()
def recent(
    limit: int = typer.Option(5, "--limit", "-l", help="返回条数上限（1~100）"),
) -> None:
    """最近更新的记忆（不回显正文，需全文用 get <编号>）。"""
    limit = max(1, min(limit, 100))

    def run(memory: Memory) -> Any:
        return memory.list_recent(limit=limit)

    issues = _execute(run)
    _dump([_issue_out(i, body_echo=False) for i in issues])


@app.command()
def search(
    query: str = typer.Argument(..., help="语义检索查询词"),
    top_k: int = typer.Option(5, "--top-k", "-k", help="返回条数上限（1~100）"),
    include_closed: bool = typer.Option(False, "--include-closed", help="包含已软删除的记忆"),
) -> None:
    """语义检索（知识库向量召回，按内容模糊查找；与 keyword 命令并列）。"""
    top_k = max(1, min(top_k, 100))

    def run(memory: Memory) -> Any:
        return memory.search(query, top_k=top_k, include_closed=include_closed)

    results = _execute(run)
    _dump(
        [
            {
                "score": r.score,
                "number": r.number,
                "title": r.title,
                "state": r.state,
                "chunk": r.chunk,
            }
            for r in results
        ]
    )


@app.command()
def keyword(
    query: str = typer.Argument(..., help="标题关键词（CNB keyword 检索只匹配标题，无法检索正文）"),
    limit: int = typer.Option(20, "--limit", "-l", help="返回条数上限（1~100）"),
    include_closed: bool = typer.Option(False, "--include-closed", help="包含已软删除的记忆"),
) -> None:
    """关键词标题检索（只回显标题元信息，需全文用 get <编号>）。"""
    limit = max(1, min(limit, 100))

    def run(memory: Memory) -> Any:
        return memory.keyword_search(query, limit=limit, include_closed=include_closed)

    issues = _execute(run)
    _dump([_issue_out(i, body_echo=False) for i in issues])


def main() -> None:
    """CLI 入口（pyproject scripts 指向此处）。"""
    app()


if __name__ == "__main__":
    main()
