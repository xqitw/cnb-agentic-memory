"""Memory 语义层单元测试：两步写入、回读校验、title 生成、软删除、语义检索。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import respx

from cam import ApiError, CnbApiClient, Memory, MemoryError, normalize_title

BASE = "https://api.cnb.cool"


def issue_payload(number: int, title: str, body: str = "", state: str = "open") -> dict:
    """构造 IssueDetail 形状的响应。"""
    return {
        "number": str(number),
        "title": title,
        "body": body,
        "state": state,
        "labels": [],
        "comment_count": 0,
    }


def echo_issue(number: int, state: str = "open") -> Callable[[httpx.Request], httpx.Response]:
    """创建/更新请求的回显 handler：按请求体 title/body 构造响应。

    模拟真实服务端（创建返回带 title 的 IssueDetail），供回读校验比对。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            201,
            json=issue_payload(
                number,
                payload.get("title", ""),
                body=payload.get("body", ""),
                state=state,
            ),
        )

    return handler


# ---- title 规范化（撰写权在调用方，工具只保证不变量）----


def test_normalize_title_keeps_caller_title() -> None:
    """调用方撰写的 title 原样保留（仅加前缀）。"""
    title = normalize_title("PostgreSQL 分区表 pg_partman", "正文")
    assert title == "cam: PostgreSQL 分区表 pg_partman"


def test_normalize_title_truncates_to_limit() -> None:
    title = normalize_title("超" * 200, "正文")
    assert len(title) <= 60
    assert title.startswith("cam: ")


def test_normalize_title_falls_back_to_first_line() -> None:
    """未提供 title 时兜底为正文首个非空行（去 Markdown 标题符）。"""
    title = normalize_title(None, "# 标题行\n\n正文内容")
    assert title == "cam: 标题行"


def test_normalize_title_blank_content_fallback() -> None:
    """无有效行时使用 untitled memory 兜底；纯符号行如实保留（不做语义判断）。"""
    assert normalize_title(None, "") == "cam: untitled memory"
    assert normalize_title(None, "!!!") == "cam: !!!"


# ---- memory.write：两步写入 + 回读校验 ----


async def test_write_two_steps_and_verify(client: CnbApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        create = mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(9))
        labels = mock.post("/group/repo/-/issues/9/labels").respond(200, json=[{"id": "1", "name": "tag/x"}])
        verify = mock.get("/group/repo/-/issues/9").respond(200, json=issue_payload(9, "cam: 内容"))
        result = await memory.write("内容", tags=["x"], category="db")

    assert create.called and labels.called and verify.called
    assert result.number == 9
    assert result.title == "cam: 内容"
    assert result.url == "https://cnb.cool/group/repo/-/issues/9"


async def test_write_creates_issue_without_labels_payload(
    client: CnbApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建请求体不应携带 labels（新标签会被服务端静默丢弃，类型层已锁死）。"""
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(1))
        mock.post("/group/repo/-/issues/1/labels").respond(200, json=[])
        mock.get("/group/repo/-/issues/1").respond(200, json=issue_payload(1, "cam: 内容"))
        await memory.write("内容", tags=["新标签"])

    payload = json.loads(route.calls.last.request.content)
    assert "labels" not in payload


async def test_write_rejects_empty_content(client: CnbApiClient) -> None:
    memory = Memory(client)
    with pytest.raises(MemoryError, match="不能为空"):
        await memory.write("   ")


async def test_write_verify_failure_raises(client: CnbApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """创建返回 201 但回读不一致 → 报写路径校验失败（静默失败形态）。"""
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(3))
        mock.post("/group/repo/-/issues/3/labels").respond(200, json=[])
        # 回读永远返回与期望不一致的 title
        mock.get("/group/repo/-/issues/3").respond(200, json=issue_payload(3, "mismatch"))
        with pytest.raises(MemoryError, match="回读校验失败"):
            await memory.write("内容", verify=True)


async def test_write_splits_long_content(client: CnbApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    long_content = ("段落。\n\n" + "x" * 20000) * 3
    created: dict[int, str] = {}
    counter = {"n": 0}

    def create_side_effect(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        payload = json.loads(request.content)
        created[counter["n"]] = payload["title"]
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    def get_side_effect(request: httpx.Request) -> httpx.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue_payload(number, created[number]))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=create_side_effect)
        mock.post(path__regex=r"/group/repo/-/issues/\d+/labels").respond(200, json=[])
        mock.get(path__regex=r"/group/repo/-/issues/\d+").mock(side_effect=get_side_effect)
        result = await memory.write(long_content)

    assert counter["n"] > 1  # 被拆成多条
    assert result.title.endswith(f"(1/{counter['n']})")
    assert all(title.startswith("cam: ") for title in created.values())


# ---- memory.update / append / delete ----


async def test_update_body_and_verify(client: CnbApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").mock(side_effect=echo_issue(5))
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", body="新正文"))
        await memory.update(5, content="新正文")

    assert patch.called


async def test_update_labels_only(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        labels = mock.post("/group/repo/-/issues/5/labels").respond(200, json=[])
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t"))
        await memory.update(5, tags=["a"], category="db")

    assert labels.called  # 不应触发 PATCH


async def test_update_nothing_raises(client: CnbApiClient) -> None:
    memory = Memory(client)
    with pytest.raises(MemoryError, match="未指定任何变更"):
        await memory.update(5)


async def test_append_and_verify(client: CnbApiClient) -> None:
    memory = Memory(client)
    comment = {"id": "c9", "body": "备注"}
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/group/repo/-/issues/5/comments").respond(201, json=comment)
        mock.get("/group/repo/-/issues/5/comments").respond(200, json=[comment])
        got = await memory.append(5, "备注")

    assert post.called
    assert got.id == "c9"


async def test_append_verify_failure(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/group/repo/-/issues/5/comments").respond(201, json={"id": "c9", "body": "备注"})
        mock.get("/group/repo/-/issues/5/comments").respond(200, json=[])
        with pytest.raises(MemoryError, match="评论未落盘"):
            await memory.append(5, "备注")


async def test_delete_soft_and_verify(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", state="closed"))
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", state="closed"))
        await memory.delete(5)

    payload = json.loads(patch.calls.last.request.content)
    assert payload == {"state": "closed", "state_reason": "not_planned"}


async def test_restore_reopen(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t"))
        await memory.restore(5)

    payload = json.loads(patch.calls.last.request.content)
    assert payload == {"state": "open", "state_reason": "reopened"}


# ---- memory.list / search ----


async def test_list_with_category_and_tags(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list(category="db", tags=["x"], limit=10)

    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "category/db,tag/x"
    assert params["page_size"] == "10"


async def test_list_tags_prefix_idempotent(client: CnbApiClient) -> None:
    """已带命名空间前缀的 tags 不重复加前缀。"""
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list(tags=["tag/already", "bare"])

    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "tag/already,tag/bare"


async def test_list_recent(client: CnbApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list_recent(limit=3)

    params = dict(route.calls.last.request.url.params)
    assert params["page_size"] == "3"
    assert params["order_by"] == "-updated_at"


async def test_search_parses_number_and_filters_closed(client: CnbApiClient) -> None:
    memory = Memory(client)
    kb_items = [
        {
            "score": 0.99,
            "chunk": "片段1",
            "metadata": {"path": "/group/repo/-/issues/11", "type": "issue"},
        },
        {
            "score": 0.95,
            "chunk": "片段2",
            "metadata": {"path": "/group/repo/-/issues/12", "type": "issue"},
        },
    ]
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").respond(200, json=kb_items)
        mock.get("/group/repo/-/issues/11").respond(200, json=issue_payload(11, "记忆甲"))
        mock.get("/group/repo/-/issues/12").respond(200, json=issue_payload(12, "记忆乙", state="closed"))
        results = await memory.search("查询")

    assert len(results) == 1  # closed 被过滤
    first = results[0]
    assert first.number == 11
    assert first.title == "记忆甲"
    assert first.state == "open"


async def test_search_include_closed(client: CnbApiClient) -> None:
    memory = Memory(client)
    kb_items = [
        {
            "score": 0.95,
            "chunk": "片段",
            "metadata": {"path": "/group/repo/-/issues/12"},
        }
    ]
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").respond(200, json=kb_items)
        mock.get("/group/repo/-/issues/12").respond(200, json=issue_payload(12, "记忆乙", state="closed"))
        results = await memory.search("查询", include_closed=True)

    assert len(results) == 1
    closed_hit = results[0]
    assert closed_hit.state == "closed"


async def test_search_kb_unavailable_suggests_fallback(client: CnbApiClient) -> None:
    """知识库 404 → MemoryError 提示降级通道。"""
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").respond(
            404, json={"errcode": 404, "errmsg": "知识库未启用"}
        )
        with pytest.raises(MemoryError, match="降级") as exc_info:
            await memory.search("查询")

    assert isinstance(exc_info.value.__cause__, ApiError)


async def test_search_dedupes_repeated_numbers(client: CnbApiClient) -> None:
    memory = Memory(client)
    kb_items = [
        {"score": 0.9, "chunk": "a", "metadata": {"path": "/group/repo/-/issues/11"}},
        {"score": 0.8, "chunk": "b", "metadata": {"path": "/group/repo/-/issues/11"}},
    ]
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").respond(200, json=kb_items)
        mock.get("/group/repo/-/issues/11").respond(200, json=issue_payload(11, "甲"))
        results = await memory.search("查询")

    assert len(results) == 1
