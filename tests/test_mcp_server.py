"""MCP Server 单元测试：工具注册、title 指导内嵌、JSON 输出形状、错误处理。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

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
    with pytest.raises(Exception, match="检索词不能为空"):
        asyncio.run(tool.fn(query="   "))
