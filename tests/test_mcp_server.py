"""MCP Server 单元测试：工具注册、title 指导内嵌、JSON 输出形状、错误处理。"""

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest
import respx
from mcp.server.mcpserver.context import Context

import cnb_agentic_memory.mcp_server as mcp_server
from cnb_agentic_memory import __version__
from cnb_agentic_memory.mcp_server import _DIST_NAME, mcp

BASE = "https://api.cnb.cool"


def issue_payload(number: int, title: str, body: str = "", state: str = "open") -> dict:
    return {
        "number": str(number),
        "title": title,
        "body": body,
        "state": state,
        "labels": [],
        "comment_count": 0,
    }


def _tool_names() -> list[str]:
    return [t.name for t in mcp._tool_manager.list_tools()]


def test_ten_tools_registered() -> None:
    """10 个记忆操作全部注册为 MCP 工具（含关键词标题检索）。"""
    assert set(_tool_names()) == {
        "memory_write",
        "memory_get",
        "memory_update",
        "memory_append",
        "memory_delete",
        "memory_restore",
        "memory_list",
        "memory_list_recent",
        "memory_search",
        "memory_keyword_search",
    }


def test_server_metadata() -> None:
    """serverInfo 元数据完整，且与包安装元数据（pyproject）同源。"""
    from importlib.metadata import metadata

    assert mcp.name == _DIST_NAME
    assert mcp.title == "CNB Issue 智能体记忆系统"
    assert mcp.description == str(metadata(_DIST_NAME)["Summary"])
    assert mcp.version == str(metadata(_DIST_NAME)["Version"])
    assert mcp.website_url == "https://cnb.cool/xqitw/cnb-agentic-memory"


def test_meta_field_missing_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """元数据头缺失时兜底 None/__version__，而非字符串 'None'。

    PackageMetadata 底层是 email.message.Message，缺失 key 返回 None
    而非抛 KeyError（评审发现：原 except KeyError 为死代码）。
    """
    from email.message import Message

    monkeypatch.setattr(mcp_server, "_META", Message())
    assert mcp_server._meta_field("Summary") is None
    assert mcp_server._meta_field("Version") is None
    assert mcp_server._summary() is None
    assert mcp_server._version() == __version__


def test_tool_descriptions_embed_title_guidance() -> None:
    """title 撰写指导必须内嵌在 memory_write 工具描述（评审要求）。"""
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_write")
    assert "关键词" in tool.description
    assert "keyword" in tool.description.lower()


def test_tool_descriptions_note_append_semantics() -> None:
    """memory_update 描述须区分全量替换与追加语义（防误用）。"""
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_update")
    assert "全量替换" in tool.description
    assert "memory_append" in tool.description


def test_memory_write_returns_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_write 工具返回 JSON，超长拆分时含全部分片（评审：循迹不漏片）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_write")
    counter = {"n": 0}
    titles: dict[int, str] = {}

    def create_side_effect(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        payload = json.loads(request.content)
        titles[counter["n"]] = payload["title"]
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    def get_side_effect(request: httpx.Request) -> httpx.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue_payload(number, titles[number]))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/g/r/-/issues").mock(side_effect=create_side_effect)
        mock.post(path__regex=r"/g/r/-/issues/\d+/labels").respond(200, json=[])
        mock.get(path__regex=r"/g/r/-/issues/\d+").mock(side_effect=get_side_effect)
        result = asyncio.run(tool.fn(content="段落。\n\n" + "x" * 40000, title="t"))

    data = json.loads(result)
    assert data["number"] == 1
    assert len(data["parts"]) > 1  # 已拆分且全部分片可循迹


def test_memory_get_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_get 返回记忆 JSON。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_get")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues/7").respond(
            200,
            json={
                "number": "7",
                "title": "t",
                "body": "正文",
                "state": "open",
                "labels": [{"name": "x"}],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
        result = asyncio.run(tool.fn(number=7))

    data = json.loads(result)
    assert data["number"] == 7
    assert data["labels"] == ["x"]


def test_memory_get_api_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """API 错误在工具函数内抛出（由 MCP 框架转为 isError 结果），不吞不包装。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_get")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues/404").respond(404, json={"errcode": 404, "errmsg": "不存在"})
        with pytest.raises(Exception, match="404"):
            asyncio.run(tool.fn(number=404))


def test_memory_write_partial_success_transparent(monkeypatch):
    """拆分部分成功：MemoryRuleError 的循迹信息透传给智能体（评审 warning）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_write")
    counter = {"n": 0}
    titles = {}

    def create_side_effect(request):
        counter["n"] += 1
        if counter["n"] == 2:
            return httpx.Response(500, json={"errcode": 500, "errmsg": "boom"})
        payload = json.loads(request.content)
        titles[counter["n"]] = payload["title"]
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    def get_side_effect(request):
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue_payload(number, titles.get(number, "t")))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/g/r/-/issues").mock(side_effect=create_side_effect)
        mock.get(path__regex=r"/g/r/-/issues/\d+").mock(side_effect=get_side_effect)
        result = asyncio.run(tool.fn(content=("段落。" + chr(10) * 2 + "x" * 40000) * 3, title="t"))

    data = json.loads(result)
    assert "error" in data
    assert "#1" in data["error"]


def test_memory_list_state_invalid_rejected(monkeypatch):
    """state 非法值前置拒绝（评审 warning：不再打到服务端吃 4xx）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_list")
    result = asyncio.run(tool.fn(category=None, tags=None, state="all", limit=20))

    data = json.loads(result)
    assert "open/closed" in data["error"]


def test_memory_list_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_list 正常路径返回记忆数组（state 校验已由另一用例覆盖）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_list")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues").respond(200, json=[issue_payload(5, "分类记忆")])
        result = asyncio.run(tool.fn(category="db", tags=None, state="open", limit=10))

    data = json.loads(result)
    assert data[0]["number"] == 5


def test_memory_search_returns_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_search 返回语义召回形状（score/chunk/number/title/state）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_search")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/knowledge/base/query").respond(
            200,
            json=[
                {
                    "score": 0.99,
                    "chunk": "命中片段",
                    "metadata": {"path": "/g/r/-/issues/11", "type": "issue"},
                }
            ],
        )
        mock.get("/g/r/-/issues/11").respond(200, json=issue_payload(11, "命中记忆"))
        result = asyncio.run(tool.fn(query="查询", top_k=3))

    data = json.loads(result)
    assert data[0]["score"] == 0.99
    assert data[0]["chunk"] == "命中片段"
    assert data[0]["number"] == 11
    assert data[0]["title"] == "命中记忆"
    assert data[0]["state"] == "open"


def test_memory_update_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_update 返回更新后的记忆 JSON（含 labels 形状）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_update")
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.patch("/g/r/-/issues/9").respond(200, json=issue_payload(9, "新标题"))
        mock.post("/g/r/-/issues/9/labels").respond(200, json=[])
        mock.get("/g/r/-/issues/9").respond(200, json=issue_payload(9, "新标题"))
        result = asyncio.run(tool.fn(number=9, title="新标题"))

    data = json.loads(result)
    assert data["number"] == 9
    assert data["title"] == "新标题"
    assert data["labels"] == []  # _issue_out 输出 labels 字段（_LenientModel 空列表）


def test_memory_append_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_append 返回评论 JSON（id/body/created_at）。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_append")
    with respx.mock(base_url=BASE) as mock:
        mock.post("/g/r/-/issues/9/comments").respond(
            200, json={"id": "c1", "body": "补充内容", "created_at": "2026-01-01T00:00:00Z"}
        )
        mock.get("/g/r/-/issues/9/comments").respond(
            200, json=[{"id": "c1", "body": "补充内容", "created_at": "2026-01-01T00:00:00Z"}]
        )
        result = asyncio.run(tool.fn(number=9, note="补充内容"))

    data = json.loads(result)
    assert data["id"] == "c1"
    assert data["body"] == "补充内容"


def test_memory_delete_and_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_delete 软删除、memory_restore 恢复，均返回 number/state。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    delete_tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_delete")
    restore_tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_restore")
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.patch("/g/r/-/issues/9").respond(200, json=issue_payload(9, "t", state="closed"))
        mock.get("/g/r/-/issues/9").respond(200, json=issue_payload(9, "t", state="closed"))
        data = json.loads(asyncio.run(delete_tool.fn(number=9)))
    assert data == {"number": 9, "state": "closed"}

    with respx.mock(base_url=BASE) as mock:
        mock.patch("/g/r/-/issues/9").respond(200, json=issue_payload(9, "t", state="open"))
        data = json.loads(asyncio.run(restore_tool.fn(number=9)))
    assert data == {"number": 9, "state": "open"}


def test_memory_list_recent_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory_list_recent 返回记忆数组。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_list_recent")
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues").respond(200, json=[issue_payload(3, "最近")])
        result = asyncio.run(tool.fn(limit=5))

    data = json.loads(result)
    assert data[0]["number"] == 3


def test_keyword_search_basic(monkeypatch):
    """关键词标题检索：两态合并去重、按 updated_at 降序。"""
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_keyword_search")

    def issue(number, updated):
        return {
            "number": str(number),
            "title": "kw " + str(number),
            "body": "",
            "state": "open",
            "labels": [{"name": "x"}],
            "comment_count": 0,
            "updated_at": updated,
        }

    open_items = [issue(5, "2026-01-05T00:00:00Z"), issue(9, "2026-01-09T00:00:00Z")]
    closed_items = [issue(7, "2026-01-07T00:00:00Z")]

    def make_handler(items):
        def handler(request):
            return httpx.Response(200, json=items)

        return handler

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues").mock(
            side_effect=lambda request: (
                httpx.Response(200, json=open_items)
                if dict(request.url.params).get("state") == "open"
                else httpx.Response(200, json=closed_items)
            )
        )
        result = asyncio.run(tool.fn(query="kw", include_closed=True))

    data = json.loads(result)
    assert [i["number"] for i in data] == [9, 7, 5]  # updated_at 降序


def test_keyword_search_rejects_empty(monkeypatch):
    import asyncio

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_keyword_search")
    # _tool_guard 统一出口：MemoryRuleError 转为 {"error": ...} JSON 结果文本（客户端可读修复指引）
    result = asyncio.run(tool.fn(query="   "))
    assert "检索词不能为空" in result


def test_main_transport_cli_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """--transport CLI 参数优先于环境变量；非法 transport 报 SystemExit。"""
    calls: list[tuple] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_TRANSPORT", "sse")

    # CLI 参数优先：环境变量是 sse，CLI 指定 streamable-http
    mcp_server.main(["--transport", "streamable-http", "--port", "9123"])
    assert calls[-1] == (
        (),
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 9123,
            "transport_security": calls[-1][1]["transport_security"],
        },
    )

    # 仅环境变量时生效（transport_security 为防护透传，断言防护开启即可）
    mcp_server.main([])
    assert calls[-1][1]["transport"] == "sse"
    assert calls[-1][1]["port"] == 8000
    assert calls[-1][1]["transport_security"].enable_dns_rebinding_protection is True

    # 无参数无环境变量 = stdio（历史默认行为，无参调用）
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_MCP_TRANSPORT")
    mcp_server.main([])
    assert calls[-1] == ((), {})

    # 非法值由 argparse 拒绝（exit 2）
    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--transport", "ws"])
    assert exc_info.value.code == 2


# ---- 每请求配置（多用户共享部署）----


def test_resolve_overrides_from_headers() -> None:
    """X-CNB-* 头提取每请求覆盖；大小写不敏感；空值/无关头忽略。"""
    from cnb_agentic_memory.api import resolve_overrides_from_headers

    # 全量头（Starlette Headers 风格，小写）
    assert resolve_overrides_from_headers(
        {"x-cnb-token": " t1 ", "x-cnb-repo": "g/r", "x-cnb-base-url": "https://x.example"}
    ) == {"token": "t1", "repo": "g/r", "base_url": "https://x.example"}
    # 原始大小写形式
    assert resolve_overrides_from_headers({"X-CNB-Token": "t2", "X-CNB-Repo": "g/r2"}) == {
        "token": "t2",
        "repo": "g/r2",
    }
    # 空值/纯空白视为未提供
    assert resolve_overrides_from_headers({"x-cnb-token": "  "}) == {}
    # 无关头不进入覆盖
    assert resolve_overrides_from_headers({"authorization": "Bearer x", "x-other": "y"}) == {}
    # None（stdio）回落空覆盖
    assert resolve_overrides_from_headers(None) == {}


def test_build_client_from_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """头覆盖优先，未覆盖项回落环境变量；无头时与 CNBApiClient() 等价。"""
    from cnb_agentic_memory.api import build_client_from_headers

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "env-token")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/env-repo")

    # 无头 → 纯环境变量（历史行为）
    client = build_client_from_headers(None)
    assert client.token == "env-token"
    assert client.repo == "g/env-repo"

    # 头覆盖 token/repo，base_url 仍回落环境变量
    client = build_client_from_headers({"x-cnb-token": "hdr-token", "x-cnb-repo": "g/hdr-repo"})
    assert client.token == "hdr-token"
    assert client.repo == "g/hdr-repo"

    # 空白头值不覆盖（回落环境变量）
    client = build_client_from_headers({"x-cnb-token": "   "})
    assert client.token == "env-token"


def test_tools_use_request_header_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 场景：工具调用经 ctx.headers 使用请求头里的 token/repo（用户间互不影响）。"""
    import asyncio

    from cnb_agentic_memory.models import Issue

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "env-token")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/env-repo")

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_get")
    seen: list[str] = []

    class FakeContext:
        """模拟 MCP Context：仅暴露 headers 属性（_client 只读 headers）。"""

        def __init__(self, headers: dict) -> None:
            self.headers = headers

    async def fake_get_issue(self, number: int):
        seen.append(self.token)
        seen.append(self.repo)
        # Memory.get 返回 Issue 模型（_issue_out 按属性访问）
        return Issue.model_validate(issue_payload(number, "t"))

    monkeypatch.setattr("cnb_agentic_memory.api.CNBApiClient.get_issue", fake_get_issue)

    # 带凭据头：使用头中的 token/repo
    ctx_hdr = FakeContext({"x-cnb-token": "hdr-token", "x-cnb-repo": "g/hdr-repo"})
    data = json.loads(asyncio.run(tool.fn(number=1, ctx=ctx_hdr)))
    assert data["number"] == 1
    assert seen[-2:] == ["hdr-token", "g/hdr-repo"]

    # 不带凭据头：回落环境变量
    ctx_bare = FakeContext({})
    asyncio.run(tool.fn(number=1, ctx=ctx_bare))
    assert seen[-2:] == ["env-token", "g/env-repo"]

    # stdio（ctx=None）：回落环境变量
    asyncio.run(tool.fn(number=1, ctx=None))
    assert seen[-2:] == ["env-token", "g/env-repo"]


def test_resolve_overrides_requires_full_credentials() -> None:
    """安全约定（阻塞项修复）：token+repo 齐备才启用头覆盖，env 凭据不外泄。"""
    from cnb_agentic_memory.api import resolve_overrides_from_headers

    # 只带 base_url头：拒绝头模式（env token/repo 不会发往头的 base_url）
    assert resolve_overrides_from_headers({"x-cnb-base-url": "http://evil"}) == {}
    # 只带 token：同样拒绝（repo 回落 env 会把 env repo 发往头 token 对应平台）
    assert resolve_overrides_from_headers({"x-cnb-token": "t"}) == {}
    # token+repo 齐备：启用，base_url 可选覆盖
    assert resolve_overrides_from_headers(
        {"x-cnb-token": "t", "x-cnb-repo": "g/r", "x-cnb-base-url": "http://x"}
    ) == {"token": "t", "repo": "g/r", "base_url": "http://x"}
    # token+repo 齐备但不带 base_url
    assert resolve_overrides_from_headers({"x-cnb-token": "t", "x-cnb-repo": "g/r"}) == {
        "token": "t",
        "repo": "g/r",
    }


def test_resolve_overrides_duplicate_header_first_value() -> None:
    """重复同名头取首值（与 Starlette Headers.get() 语义一致）。"""
    from collections.abc import Mapping
    from typing import Any

    from cnb_agentic_memory.api import resolve_overrides_from_headers

    class MultiMapping(Mapping):  # 模拟 Starlette Headers：同名头产出多个键值对
        def __init__(self, pairs: list[tuple[str, str]]) -> None:
            self._pairs = pairs

        def __getitem__(self, key: str) -> Any:
            for k, v in self._pairs:
                if k == key:
                    return v
            raise KeyError(key)

        def __iter__(self):
            return iter(dict(self._pairs))

        def __len__(self) -> int:
            return len(dict(self._pairs))

        def items(self):
            return iter(self._pairs)

    dup = MultiMapping([("x-cnb-token", "t1"), ("x-cnb-token", "t2"), ("x-cnb-repo", "g/r")])
    assert resolve_overrides_from_headers(dup) == {"token": "t1", "repo": "g/r"}


def test_parse_port_lenient() -> None:
    """端口宽松解析：空/非法/越界回落 8000（阻塞评审建议2）。"""
    from cnb_agentic_memory.mcp_server import parse_port

    assert parse_port(None) == 8000
    assert parse_port("") == 8000
    assert parse_port("abc") == 8000
    assert parse_port("0") == 8000
    assert parse_port("70000") == 8000
    assert parse_port("9000") == 9000


def test_main_port_env_empty_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_PORT 为空串/非法值时进程不崩，回落 8000。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_PORT", "")
    mcp_server.main(["--transport", "streamable-http"])
    assert calls[-1]["port"] == 8000


def test_parse_transport_lenient() -> None:
    """transport 环境变量宽松解析：笔误清洗，非法回落 stdio（复审 warning）。"""
    from cnb_agentic_memory.mcp_server import parse_transport

    assert parse_transport(None) == "stdio"
    assert parse_transport("") == "stdio"
    assert parse_transport(" sse") == "sse"
    assert parse_transport("SSE") == "sse"
    assert parse_transport("streamable_http") == "streamable-http"
    assert parse_transport("ws") == "stdio"


def test_main_transport_env_invalid_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP_TRANSPORT 异常值不崩启动：空白/大小写/下划线清洗后生效，非法回落 stdio。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_TRANSPORT", " sse")
    mcp_server.main([])
    assert calls[-1]["transport"] == "sse"
    assert calls[-1]["host"] == "127.0.0.1"
    assert calls[-1]["port"] == 8000
    assert calls[-1]["transport_security"].enable_dns_rebinding_protection is True

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_TRANSPORT", "streamable_http")
    mcp_server.main([])
    assert calls[-1]["transport"] == "streamable-http"

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_TRANSPORT", "ws")
    mcp_server.main([])
    assert calls[-1] == {}  # 非法回落 stdio（无参调用）


def test_main_wildcard_host_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """HTTP transport 监听通配地址时向 stderr 打安全提醒（复审建议2），stdio/本机地址不提醒。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
    captured = capsys.readouterr()
    assert "反向代理" in captured.err

    mcp_server.main(["--transport", "streamable-http", "--host", "127.0.0.1"])
    captured = capsys.readouterr()
    assert "反向代理" not in captured.err

    mcp_server.main([])  # stdio 不提醒
    captured = capsys.readouterr()
    assert "反向代理" not in captured.err


def test_parse_host_lenient() -> None:
    """host 宽松解析：空值/空白回落 127.0.0.1（复审 warning1：MCP_HOST 空串绑定全网卡且警告失效）。"""
    from cnb_agentic_memory.mcp_server import parse_host

    assert parse_host(None) == "127.0.0.1"
    assert parse_host("") == "127.0.0.1"
    assert parse_host("  ") == "127.0.0.1"
    assert parse_host("0.0.0.0") == "0.0.0.0"


def test_main_empty_host_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """MCP_HOST 键存在值为空：回落 127.0.0.1 且不触发通配警告（复审复现场景）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_HOST", "")
    mcp_server.main(["--transport", "streamable-http"])
    captured = capsys.readouterr()
    assert calls[-1]["host"] == "127.0.0.1"
    assert "反向代理" not in captured.err  # 非 0.0.0.0，不误报


def test_dns_rebinding_protection_passed_on_wildcard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """通配监听时透传 TransportSecuritySettings（复审 info：防护与警告对齐）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
    captured = capsys.readouterr()
    security = calls[-1]["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in security.allowed_hosts
    assert "0.0.0.0:*" in security.allowed_hosts
    assert "反向代理" in captured.err  # 通配警告保留

    # 本机监听同样透传（防护常开），但不触发通配警告
    mcp_server.main(["--transport", "streamable-http", "--host", "127.0.0.1"])
    captured = capsys.readouterr()
    assert calls[-1]["transport_security"].enable_dns_rebinding_protection is True
    assert "反向代理" not in captured.err


def test_env_var_names_documented_correctly() -> None:
    """守护：help 文本中的环境变量名与 env() 实际读取一致（复审 warning2 防回归）。"""
    import inspect

    src = inspect.getsource(mcp_server.main)
    # env() 自动加前缀：源码用短名，实际读取的完整变量名 = 前缀 + 短名，与 help/docs 声明一致
    assert 'env("MCP_TRANSPORT")' in src
    assert 'env("MCP_HOST")' in src
    assert 'env("MCP_PORT")' in src
    # help 文本向用户展示完整变量名
    assert "CNB_AGENTIC_MEMORY_MCP_TRANSPORT" in src
    docs = open("docs/MCP.md").read()
    assert "CNB_AGENTIC_MEMORY_MCP_TRANSPORT" in docs
    assert "CNB_AGENTIC_MEMORY_MCP_HOST" in docs
    assert "CNB_AGENTIC_MEMORY_MCP_PORT" in docs


def test_security_settings_origin_and_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """防护白名单：Origin 随 Host 同源生成（复审 warning1）；IPv6 监听加方括号（warning2）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    mcp_server.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
    sec = calls[-1]["transport_security"]
    assert sec.enable_dns_rebinding_protection is True
    # Origin 白名单随 Host 白名单同源生成（否则浏览器同源请求 403）
    assert "http://127.0.0.1:*" in sec.allowed_origins
    assert "http://localhost:*" in sec.allowed_origins

    # 非通配 IPv6 监听：模式须带方括号（RFC 3986 Host 头格式）
    mcp_server.main(["--transport", "streamable-http", "--host", "2001:db8::1"])
    sec6 = calls[-1]["transport_security"]
    assert "[2001:db8::1]:*" in sec6.allowed_hosts
    assert "http://[2001:db8::1]:*" in sec6.allowed_origins


def test_whitelist_fixed_no_extension_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """白名单固定为 localhost 族 + 监听地址（#85 裁剪）：--allowed-host 已移除，未知域名被拒。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    # CLI --allowed-host 参数已移除：argparse 直接报错 exit 2
    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--transport", "streamable-http", "--allowed-host", "mem.example.com"])
    assert exc_info.value.code == 2

    # env 白名单入口已移除：设置也不再生效，白名单仍只有固定条目
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_ALLOWED_HOSTS", "mem.example.com")
    mcp_server.main(["--transport", "streamable-http"])
    captured = capsys.readouterr()
    # 废弃 env 显式告警（非阻塞行级意见：静默失效会让反代部署升级后全量 421 无提示）
    assert "已随 --allowed-host 移除" in captured.err
    # 示例文案完整：锁定 Origin 空值引号示例不回退（不论塌陷机理，输出形态必须如此）
    assert 'proxy_set_header Origin "";' in captured.err
    sec = calls[-1]["transport_security"]
    assert not any("example.com" in h for h in sec.allowed_hosts)
    assert not any("example.com" in o for o in sec.allowed_origins)
    # 固定条目齐全：localhost 族 + 监听地址，双形态（:* 通配 + 无端口精确）
    assert "localhost:*" in sec.allowed_hosts
    assert "localhost" in sec.allowed_hosts
    assert "127.0.0.1:*" in sec.allowed_hosts
    assert "[::1]:*" in sec.allowed_hosts
    assert "http://localhost:*" in sec.allowed_origins
    assert "http://127.0.0.1" in sec.allowed_origins


def test_whitelist_host_base() -> None:
    """监听地址转白名单基名：裸 IPv6 裹方括号，域名/IPv4 原样（#85 裁剪后仅此一职责）。"""
    from cnb_agentic_memory.mcp_server import whitelist_host_base

    assert whitelist_host_base("myhost") == "myhost"
    assert whitelist_host_base("127.0.0.1") == "127.0.0.1"
    assert whitelist_host_base("::1") == "[::1]"
    assert whitelist_host_base("2001:db8::1") == "[2001:db8::1]"
    assert whitelist_host_base("[::1]") == "[::1]"  # 已带壳（防御性，正常入口不会传入）


def test_resolve_overrides_rejects_bare_header_names() -> None:
    """仅认 X-CNB-* 完整头名：裸 token/repo/base-url 别名一律忽略（防通用头名冲突）。"""
    from cnb_agentic_memory.api import resolve_overrides_from_headers

    # 裸别名即使 token/repo 齐全也不进入头覆盖模式
    assert resolve_overrides_from_headers({"token": "t", "repo": "g/r"}) == {}
    # 裸别名不与 X-CNB-* 头混用（base-url 别名无效）
    assert resolve_overrides_from_headers(
        {"x-cnb-token": "t", "x-cnb-repo": "g/r", "base-url": "https://x.example"}
    ) == {"token": "t", "repo": "g/r"}


def test_main_invalid_cli_port_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 显式传非法端口直接报错退出（exit 2），不静默回落 8000。"""
    for bad in ("abc", "0", "70000"):
        with pytest.raises(SystemExit) as exc_info:
            mcp_server.main(["--transport", "streamable-http", "--port", bad])
        assert exc_info.value.code == 2


def test_malformed_host_cli_errors_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """畸形 --host：CLI 显式传参 exit 2；env 兜底 stderr 告警回落 127.0.0.1，不崩启动。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    # CLI 显式传错立即暴露（与 parse_port_strict 同理）
    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--transport", "streamable-http", "--host", "myhost:abc"])
    assert exc_info.value.code == 2

    # env 注入畸形值（含全角冒号）：告警回落默认地址，服务照常启动（复审阻塞项）
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_HOST", "127.0.0.1：8443")
    mcp_server.main(["--transport", "streamable-http"])
    captured = capsys.readouterr()
    assert "无法解析" in captured.err
    assert calls[-1]["host"] == "127.0.0.1"


def test_validate_listen_host_accepts_valid() -> None:
    """监听地址校验：域名/IPv4/IPv6/[IPv6] 合法，方括号形态返回裸地址（端口段拒绝，端口由 --port 指定）。"""
    from cnb_agentic_memory.mcp_server import validate_listen_host

    assert validate_listen_host("myhost") == "myhost"
    assert validate_listen_host("127.0.0.1") == "127.0.0.1"
    assert validate_listen_host("0.0.0.0") == "0.0.0.0"
    assert validate_listen_host("::1") == "::1"
    assert validate_listen_host("::") == "::"
    assert validate_listen_host("::ffff:127.0.0.1") == "::ffff:127.0.0.1"
    assert validate_listen_host("[::1]") == "::1"


def test_validate_listen_host_rejects_invalid() -> None:
    """监听地址校验：host:port 合并/全角冒号/畸形括号/域名裹括号/IPv4 裹括号全部拒绝。"""
    import pytest

    from cnb_agentic_memory.mcp_server import validate_listen_host

    bads = [
        "myhost:8000",  # host:port 合并形态（复审阻塞项主场景）
        "0.0.0.0:8000",  # env 通道混入白名单的场景
        "myhost:abc",
        "127.0.0.1：8443",  # 全角冒号
        "[::1",  # 方括号不完整
        "[::1]:",  # 方括号后空端口
        "[]",
        "[::1]:abc",
        "a:b:c",
        ":8000",
        "[myhost]",  # 域名裹方括号
        "[127.0.0.1]",  # IPv4 裹方括号（RFC 3986 方括号仅用于 IPv6）
        "[::1]x",
        "[::1]:8443",  # 带壳带端口：端口段不静默丢弃（复审 warning），端口用 --port
        "[2001:db8::1]:8443",
    ]
    for bad in bads:
        with pytest.raises(ValueError):
            validate_listen_host(bad)
    with pytest.raises(ValueError):
        validate_listen_host("")


def test_main_host_port_merged_cli_errors_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """host:port 合并形态端到端：CLI exit 2；env 告警回落且基名不混入白名单（复审阻塞项）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    # CLI：--host myhost:8000（把端口并进 host 的最常见敲错）报 argparse 错误 exit 2
    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--transport", "streamable-http", "--host", "myhost:8000"])
    assert exc_info.value.code == 2

    # env：MCP_HOST=0.0.0.0:8000（容器编排端口映射常见写法）告警回落 127.0.0.1，
    # 0.0.0.0 基名不混入白名单（旧实现会把白名单防护打到失守）
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_HOST", "0.0.0.0:8000")
    mcp_server.main(["--transport", "streamable-http"])
    captured = capsys.readouterr()
    assert "无法解析" in captured.err
    assert calls[-1]["host"] == "127.0.0.1"
    security = calls[-1]["transport_security"]
    assert not any("0.0.0.0" in h for h in security.allowed_hosts)

    # env 带壳 IPv6 带端口：端口段不再静默丢弃，告警回落（复审 warning：
    # --host [::1]:9000 原先实际落 8000 且 env 通道无告警）
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_MCP_HOST", "[::1]:8000")
    mcp_server.main(["--transport", "streamable-http"])
    captured = capsys.readouterr()
    assert "无法解析" in captured.err
    assert calls[-1]["host"] == "127.0.0.1"
    security = calls[-1]["transport_security"]
    assert not any("[::1]:8000" in h for h in security.allowed_hosts)


def test_main_bracketed_ipv6_host_stripped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 带壳 IPv6 成功路径 main 级锚定：[::1] 剥壳为裸 ::1 透传，白名单基名正确（复审致命项回归 + info 补对称用例）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    mcp_server.main(["--transport", "streamable-http", "--host", "[::1]"])
    assert calls[-1]["host"] == "::1"
    security = calls[-1]["transport_security"]
    # 白名单基名用裸地址生成，无带壳残留
    assert "[::1]:*" in security.allowed_hosts
    assert "[::1]" in security.allowed_hosts
    assert not any(h.startswith("[::1]:") and h != "[::1]:*" for h in security.allowed_hosts)

    # CLI 带壳带端口：报 argparse 错误 exit 2（端口由 --port 指定，复审 warning）
    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main(["--transport", "streamable-http", "--host", "[::1]:8000"])
    assert exc_info.value.code == 2


class _FakeCtx:
    """最小 Context 替身：仅提供 _client 用到的 headers 属性。"""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers


def _ctx_of(headers: dict[str, str] | None) -> Context:
    """_FakeCtx → Context 的类型收口（测试替身只实现 _client 实际读取的属性）。"""
    return cast(Context, _FakeCtx(headers))


def test_require_headers_rejects_anonymous_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """--require-headers 开启：HTTP 无凭据头请求被拒（MemoryRuleError），凭据齐全放行（#83 建议第 2 条）。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    mcp_server.main(["--transport", "streamable-http", "--require-headers"])
    assert mcp_server._require_headers is True

    # 无凭据头：拒绝且不回落环境变量凭据（_client 现为 asynccontextmanager，进入 with 才触发校验）
    import asyncio

    async def _rejected():
        async with mcp_server._client(_ctx_of({"user-agent": "anonymous"})):
            pass

    with pytest.raises(mcp_server.MemoryRuleError, match="强制凭据头"):
        asyncio.run(_rejected())

    # 凭据头齐全：正常放行（池 acquire 成功）
    import asyncio

    async def _allowed():
        async with mcp_server._client(_ctx_of({"x-cnb-token": "t", "x-cnb-repo": "g/r"})) as c:
            return c

    client = asyncio.run(_allowed())
    assert client is not None

    # 拒绝走 _tool_guard 统一出口：调用方拿到可读修复指引 JSON，而非笼统框架异常（复审阻塞项）
    import asyncio

    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "memory_get")
    result = asyncio.run(tool.fn(number=1, ctx=_ctx_of({"user-agent": "anonymous"})))
    assert "强制凭据头" in result and "X-CNB-Token" in result


def test_require_headers_stdio_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio 无请求头是常态：开关开启下回落环境变量，不自杀。

    覆盖 stdio 真实形态（复审阻塞项）：框架对带 Context 形参的工具无条件
    注入 ctx（恒非 None）但 headers 为 None——_client(None) 手工直调形态
    绕开了误伤分支，必须以 _ctx_of(None)（ctx 非 None + headers=None）锚定。
    """
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))
    # 回落路径构造 CNBApiClient 需要服务端凭据 env 完整（CI 无真实凭据，显式注入）
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    mcp_server.main(["--transport", "stdio", "--require-headers"])
    assert mcp_server._require_headers is True
    # stdio 真实形态：ctx 非 None + headers=None → 不校验凭据
    import asyncio

    async def _stdio():
        async with mcp_server._client(_ctx_of(None)) as c:
            return c

    client = asyncio.run(_stdio())
    assert client is not None


def test_require_headers_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关闭（兼容旧行为）：无开关时无凭据头请求照常回落环境变量；非法 env 值回落关闭，truthy 开启。"""
    calls: list[dict] = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **kw: calls.append(kw))

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    mcp_server.main(["--transport", "streamable-http"])
    assert mcp_server._require_headers is False
    client = mcp_server._client(_ctx_of({"user-agent": "anonymous"}))
    assert client is not None

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REQUIRE_HEADERS", "not-a-bool")
    mcp_server.main(["--transport", "streamable-http"])
    assert mcp_server._require_headers is False

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REQUIRE_HEADERS", "1")
    mcp_server.main(["--transport", "streamable-http"])
    assert mcp_server._require_headers is True


def test_shared_client_pool_reuse_and_refcount(monkeypatch: pytest.MonkeyPatch) -> None:
    """共享池：同键复用实例（#83 第 5 条），引用计数归零才真正关闭（并发关闭语义）。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()

    async def scenario():
        c1 = await pool.acquire()
        c2 = await pool.acquire()
        assert c1 is c2  # 同键复用同一实例（免每请求 TLS 握手）
        assert len(pool._clients) == 1

        # A 还在用（refs=2），B 的 async with 先退出：引用 -1 条目保留
        await pool.release(c1)
        assert len(pool._clients) == 1
        assert pool._clients[next(iter(pool._clients))][1] == 1

        # 引用归零：条目保活复用（复审阻塞项：归零即关使串行主路径每次
        # 缓存未命中，TLS 重握手）——关闭统一走显式 aclose()
        await pool.release(c2)
        assert len(pool._clients) == 1
        assert pool._clients[next(iter(pool._clients))][1] == 0

        # 归零后再 acquire：同实例复用（条目保活的直接收益）
        c3 = await pool.acquire()
        assert c3 is c1
        await pool.aclose()
        assert len(pool._clients) == 0

    asyncio.run(scenario())


def test_shared_client_pool_distinct_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同配置键（如每请求头覆盖的 token/repo）各自独立缓存实例。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()

    async def scenario():
        a = await pool.acquire()
        b = await pool.acquire(token="other", repo="x/y")
        assert a is not b
        # 键含 timeout：不同超时不共享底层 client
        c = await pool.acquire(timeout=1.0)
        assert c is not a
        await pool.release(a)
        await pool.release(b)
        await pool.release(c)
        await pool.aclose()

    asyncio.run(scenario())


def test_shared_client_pool_serial_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """串行主路径复用（幽明阻塞项实测场景）：连续多次 acquire/release 得到同一实例。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()

    async def scenario():
        seen = []
        for _ in range(3):
            c = await pool.acquire()
            seen.append(c)
            await pool.release(c)
        # 条目保活：串行每次 acquire 命中缓存，三连调用同一实例（免 TLS 重握手）
        assert seen[0] is seen[1] is seen[2]
        await pool.aclose()

    asyncio.run(scenario())


def test_shared_client_pool_token_not_in_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """token 走 sha256 摘要进键（锐鉴阻塞项）：明文凭据不进程级驻留。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()

    async def scenario():
        await pool.acquire(token="secret-token-value")
        dumped = repr(pool._clients)
        assert "secret-token-value" not in dumped
        await pool.release(next(iter(pool._clients.values()))[0])
        await pool.aclose()

    asyncio.run(scenario())


def test_shared_client_pool_cross_loop_isolated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """跨事件循环访问不走池（临时客户端直建直关 + 告警一次），不炸已关连接（幽明阻塞项整改）。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()

    async def acquire_in_loop():
        return await pool.acquire()

    # loop1：绑定池
    asyncio.run(acquire_in_loop())

    # loop2：异 loop 请求 → 临时客户端（不入池），logging 告警一次（锐鉴遗留项：
    # print 在 server 进程 stderr 未接流时丢失，统一走 logger）
    import logging as logging_mod

    c2 = asyncio.run(acquire_in_loop())
    assert any("跨事件循环" in r.message for r in caplog.records if r.levelno == logging_mod.WARNING)
    assert len(pool._clients) == 1  # 临时客户端未入池
    # 临时客户端已由 release 关闭
    assert c2._client is None
    # 告警只发一次
    caplog.clear()
    asyncio.run(acquire_in_loop())
    assert not any("跨事件循环" in r.message for r in caplog.records)

    # loop1 的原客户端用 aclose 收尾（不能在 loop2 close loop1 的连接，直接清池）
    pool._clients.clear()


def test_shared_client_pool_bounded_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    """有界淘汰：条目超 MAX_ENTRIES 时淘汰最旧 0 引用条目；全在用则不阻塞（幽明阻塞项整改）。"""
    import asyncio

    from cnb_agentic_memory.api import SharedClientPool

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    pool = SharedClientPool()
    pool.MAX_ENTRIES = 3

    async def scenario():
        # 制造 3 个不同键条目（均 0 引用后）
        clients = []
        for i in range(3):
            c = await pool.acquire(repo=f"g/r{i}")
            clients.append(c)
        for c in clients:
            await pool.release(c)
        assert len(pool._clients) == 3

        # 第 4 个键触发淘汰：最旧的 0 引用条目被逐出
        await pool.acquire(repo="g/r-new")
        assert len(pool._clients) == 3
        keys = list(pool._clients.keys())
        assert all("g/r0" not in k for k in keys)  # 最旧者被淘汰

        # 全部在用（refs>0）时不淘汰：宁超限不关正在用的连接
        pool._clients.clear()
        busy = []
        for i in range(3):
            c = await pool.acquire(repo=f"g/b{i}")
            busy.append(c)
        # 3 条全部 refs=1（在用）：第 4 键 acquire 时无 0 引用可淘汰 → 不淘汰
        await pool.acquire(repo="g/b-extra")
        assert len(pool._clients) == 4  # 超限保留，未误伤在用条目
        for c in busy:
            await pool.release(c)
        await pool.aclose()

    asyncio.run(scenario())
