"""Memory 语义层单元测试：两步写入、回读校验、title 生成、软删除、语义检索。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import respx

from cnb_agentic_memory import ApiError, CNBApiClient, Memory, MemoryError, normalize_title

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
    """调用方撰写的 title 原样保留（仅截断到上限）。"""
    title = normalize_title("PostgreSQL 分区表 pg_partman", "正文")
    assert title == "PostgreSQL 分区表 pg_partman"


def test_normalize_title_truncates_to_limit() -> None:
    title = normalize_title("超" * 200, "正文")
    assert title == "超" * 60  # 精确截断到 MAX_TITLE_CHARS


def test_normalize_title_falls_back_to_first_line() -> None:
    """未提供 title 时兜底为正文首个非空行（去 Markdown 标题符）。"""
    title = normalize_title(None, "# 标题行\n\n正文内容")
    assert title == "标题行"


def test_normalize_title_blank_content_fallback() -> None:
    """无有效行时使用 untitled memory 兜底；纯符号行如实保留（不做语义判断）。"""
    assert normalize_title(None, "") == "untitled memory"
    assert normalize_title(None, "!!!") == "!!!"


# ---- memory.write：两步写入 + 回读校验 ----


async def test_write_two_steps_and_verify(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        create = mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(9))
        labels = mock.post("/group/repo/-/issues/9/labels").respond(200, json=[{"id": "1", "name": "x"}])
        verify = mock.get("/group/repo/-/issues/9").respond(200, json=issue_payload(9, "内容"))
        result = await memory.write("内容", tags=["x"], category="db")

    assert create.called and labels.called and verify.called
    assert result.number == 9
    assert result.title == "内容"


async def test_write_creates_issue_without_labels_payload(
    client: CNBApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建请求体不应携带 labels（新标签会被服务端静默丢弃，类型层已锁死）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(1))
        mock.post("/group/repo/-/issues/1/labels").respond(200, json=[])
        mock.get("/group/repo/-/issues/1").respond(200, json=issue_payload(1, "内容"))
        await memory.write("内容", tags=["新标签"])

    payload = json.loads(route.calls.last.request.content)
    assert "labels" not in payload


async def test_write_rejects_empty_content(client: CNBApiClient) -> None:
    memory = Memory(client)
    with pytest.raises(MemoryError, match="不能为空"):
        await memory.write("   ")


async def test_write_verify_failure_raises(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """创建返回 201 但回读不一致 → 报写路径校验失败（静默失败形态）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(3))
        mock.post("/group/repo/-/issues/3/labels").respond(200, json=[])
        # 回读永远返回与期望不一致的 title
        mock.get("/group/repo/-/issues/3").respond(200, json=issue_payload(3, "mismatch"))
        with pytest.raises(MemoryError, match="回读校验失败"):
            await memory.write("内容", verify=True)


async def test_write_splits_long_content(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
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
    assert all(
        title and title.endswith(f"({i}/{counter['n']})") for i, title in created.items()
    )  # 每个分片 title 非空且带各自的 (i/n) 序号后缀
    assert all(len(t) <= 60 for t in created.values())  # title 含序号后缀仍不超上限


async def test_write_splits_single_long_line(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """无空行的超长单行（代码块/长文本）也能被按行拆分（评审意见：保底按行）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    long_content = "line\n" * 12000  # 无空行、约 60KB
    counter = {"n": 0}
    sizes: list[int] = []
    titles: dict[int, str] = {}

    def create_side_effect(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        payload = json.loads(request.content)
        sizes.append(len(payload["body"].encode("utf-8")))
        titles[counter["n"]] = payload["title"]
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    def get_side_effect(request: httpx.Request) -> httpx.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue_payload(number, titles[number]))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=create_side_effect)
        mock.post(path__regex=r"/group/repo/-/issues/\d+/labels").respond(200, json=[])
        mock.get(path__regex=r"/group/repo/-/issues/\d+").mock(side_effect=get_side_effect)
        await memory.write(long_content)

    assert counter["n"] > 1
    assert all(size <= 30000 for size in sizes)  # 每个分片都不超上限


async def test_write_splits_no_newline_hard_cut(
    client: CNBApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不含任何换行的连续串（长 URL/base64 等）按 UTF-8 字符边界硬切。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    long_content = "x" * 60000  # 无换行、无空行
    counter = {"n": 0}
    sizes: list[int] = []
    titles: dict[int, str] = {}

    def create_side_effect(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        payload = json.loads(request.content)
        sizes.append(len(payload["body"].encode("utf-8")))
        titles[counter["n"]] = payload["title"]
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    def get_side_effect(request: httpx.Request) -> httpx.Response:
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=issue_payload(number, titles[number]))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=create_side_effect)
        mock.post(path__regex=r"/group/repo/-/issues/\d+/labels").respond(200, json=[])
        mock.get(path__regex=r"/group/repo/-/issues/\d+").mock(side_effect=get_side_effect)
        await memory.write(long_content, verify=False)

    assert counter["n"] > 1
    assert all(size <= 30000 for size in sizes)
    assert sum(sizes) == 60000  # 无内容丢失


def test_split_hard_cut_utf8_boundary() -> None:
    """中文连续串硬切不产生半个字符（每片仍是合法 UTF-8）。"""
    from cnb_agentic_memory.memory import _split_body

    parts = _split_body("汉" * 20000)  # 每字 3 字节，总 60KB
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= 30000 for p in parts)
    assert "".join(parts) == "汉" * 20000  # 无内容丢失、无乱码


def test_split_lossless_with_blank_lines() -> None:
    """含空行分隔的内容拆分无损（评审 critical：分隔符不得被吞）。"""
    from cnb_agentic_memory.memory import _split_body

    content = "a" * 29990 + "\n\n" + "b" * 20
    parts = _split_body(content)
    assert "".join(parts) == content
    assert all(len(p.encode("utf-8")) <= 30000 for p in parts)


def test_split_fuzz_lossless() -> None:
    """随机混合正文 fuzz：段长跨过三级拆分阈值（含硬切），恒强无损恒有界。"""
    import random

    from cnb_agentic_memory.memory import _split_body

    rng = random.Random(42)
    split_count = 0
    for _ in range(60):
        segs = []
        for _ in range(rng.randint(1, 20)):
            kind = rng.random()
            n = rng.randint(1, 40000)  # 上限超 30000，确保硬切路径被覆盖
            if kind < 0.3:
                segs.append("a" * n)
            elif kind < 0.5:
                segs.append("汉" * (n // 3))
            elif kind < 0.7:
                segs.append("line\n" * (n // 5))
            else:
                segs.append("x" * n)
        content = "\n\n".join(segs)
        if rng.random() < 0.3:
            content += "\n\n"
        parts = _split_body(content)
        if len(parts) > 1:
            split_count += 1
        # 强无损：拼接恒等（含分隔符），纯空白分片不再被丢弃
        assert "".join(parts) == content
        assert all(len(p.encode("utf-8")) <= 30000 for p in parts)
    assert split_count > 0  # 用例必须真实进入拆分逻辑


async def test_search_network_error_suggests_fallback(client: CNBApiClient) -> None:
    """知识库网络异常（非 404）同样触发 MemoryError 降级提示（评审 warning）。"""
    import httpx as httpx_mod

    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").mock(side_effect=httpx_mod.ConnectError("net down"))
        with pytest.raises(MemoryError, match="keyword_search") as exc_info:
            await memory.search("查询")

    assert "ConnectError" in str(exc_info.value)  # 携带原始错误类型


def test_title_suffix_fits_limit_for_large_split_count() -> None:
    """分片数 >= 1000 时 (i/n) 后缀仍不破 60 字符上限（评审建议的回归守卫）。"""
    from cnb_agentic_memory.memory import MAX_TITLE_CHARS, normalize_title

    for n in (999, 1000, 12345):
        base = normalize_title("超" * 200, "正文")
        width = len(f" ({n}/{n})")
        assert len(base[: MAX_TITLE_CHARS - width] + f" ({n}/{n})") <= MAX_TITLE_CHARS


async def test_write_single_label_failure_reports_number(
    client: CNBApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主路径（单条）创建成功但补标签失败：MemoryError 携带已落盘编号（评审意见：与拆分路径标准一致）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(40))
        mock.post("/group/repo/-/issues/40/labels").respond(500, json={"errcode": 500, "errmsg": "boom"})
        with pytest.raises(MemoryError, match="#40") as exc_info:
            await memory.write("内容", title="t", tags=["x"])

    assert "ApiError" in str(exc_info.value)  # 携带原始错误摘要


async def test_write_partial_failure_reports_created(
    client: CNBApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多分片写入中途失败：MemoryError 携带已创建分片编号（评审意见：孤儿 Issue 可循迹）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    long_content = ("段落。\n\n" + "x" * 20000) * 3
    counter = {"n": 0}

    def create_side_effect(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] == 2:
            return httpx.Response(500, json={"errcode": 500, "errmsg": "boom"})
        payload = json.loads(request.content)
        return httpx.Response(201, json=issue_payload(counter["n"], payload["title"]))

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=create_side_effect)
        with pytest.raises(MemoryError, match="已完成 1/") as exc_info:
            await memory.write(long_content, verify=False)

    assert "#1" in str(exc_info.value)  # 已创建分片可循迹


async def test_write_category_prefix_idempotent(
    client: CNBApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """category 已带前缀不重复加；空 tag 被过滤（评审意见：防静默错误标签）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    captured: dict[str, object] = {}

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/group/repo/-/issues").mock(side_effect=echo_issue(30))

        def label_side_effect(request: httpx.Request) -> httpx.Response:
            captured["labels"] = json.loads(request.content)["labels"]
            return httpx.Response(200, json=[])

        mock.post("/group/repo/-/issues/30/labels").mock(side_effect=label_side_effect)
        mock.get("/group/repo/-/issues/30").respond(200, json=issue_payload(30, "t"))
        await memory.write("内容", title="t", tags=["", "  ", "ok", "x"], category="category:db")

    assert captured["labels"] == ["category:db", "ok", "x"]


# ---- memory.update / append / delete ----


async def test_update_body_and_verify(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").mock(side_effect=echo_issue(5))
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", body="新正文"))
        await memory.update(5, content="新正文")

    assert patch.called


async def test_update_labels_only(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        labels = mock.post("/group/repo/-/issues/5/labels").respond(200, json=[])
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t"))
        await memory.update(5, tags=["a"], category="db")

    assert labels.called  # 不应触发 PATCH


async def test_update_nothing_raises(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        # 未提供任何变更时直接拒绝，零网络调用（评审意见：空变更前置）。
        # 注意：断言必须在 mock 上下文内，call_count 才反映真实调用
        with pytest.raises(MemoryError, match="未指定任何变更"):
            await memory.update(5)

        assert mock.calls.call_count == 0  # 任何 HTTP 调用都算失败


async def test_update_title_verified(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """title 变更同样回读校验（评审意见：与 write 路径标准一致）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").mock(side_effect=echo_issue(5))
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "新标题"))
        await memory.update(5, title="新标题")

    payload = json.loads(patch.calls.last.request.content)
    assert payload["title"] == "新标题"


async def test_update_title_and_tags_same_call(client: CNBApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """title/content 与 tags 同次调用都生效（评审意见：分支不再吞标签更新）。"""
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)
    memory = Memory(client)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        patch = mock.patch("/group/repo/-/issues/5").mock(side_effect=echo_issue(5))
        labels = mock.post("/group/repo/-/issues/5/labels").respond(200, json=[])
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "新标题"))
        await memory.update(5, title="新标题", tags=["x"])

    assert patch.called and labels.called


async def test_append_and_verify(client: CNBApiClient) -> None:
    memory = Memory(client)
    comment = {"id": "c9", "body": "备注"}
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/group/repo/-/issues/5/comments").respond(201, json=comment)
        list_route = mock.get("/group/repo/-/issues/5/comments").respond(200, json=[comment])
        got = await memory.append(5, "备注")

    assert post.called
    assert got.id == "c9"
    # 回读按创建倒序取最新一页，评论超 100 条时新评论必在首页
    params = dict(list_route.calls.last.request.url.params)
    assert params["sort"] == "-created"


async def test_append_verify_failure(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/group/repo/-/issues/5/comments").respond(201, json={"id": "c9", "body": "备注"})
        mock.get("/group/repo/-/issues/5/comments").respond(200, json=[])
        with pytest.raises(MemoryError, match="评论未落盘"):
            await memory.append(5, "备注")


async def test_delete_soft_and_verify(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", state="closed"))
        mock.get("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t", state="closed"))
        await memory.delete(5)

    payload = json.loads(patch.calls.last.request.content)
    assert payload == {"state": "closed", "state_reason": "not_planned"}


async def test_restore_reopen(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        patch = mock.patch("/group/repo/-/issues/5").respond(200, json=issue_payload(5, "t"))
        await memory.restore(5)

    payload = json.loads(patch.calls.last.request.content)
    assert payload == {"state": "open", "state_reason": "reopened"}


# ---- memory.list / search ----


async def test_list_with_category_and_tags(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list(category="db", tags=["x"], limit=10)

    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "category:db,x"
    assert params["page_size"] == "10"


async def test_list_tags_deduped_and_trimmed(client: CNBApiClient) -> None:
    """tags 为普通标签：strip 空白、过滤空值、按内容去重。"""
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list(tags=["x", " x ", "x", "", "y"])

    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "x,y"


async def test_list_recent(client: CNBApiClient) -> None:
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await memory.list_recent(limit=3)

    params = dict(route.calls.last.request.url.params)
    assert params["page_size"] == "3"
    assert params["order_by"] == "-updated_at"


async def test_search_parses_number_and_filters_closed(client: CNBApiClient) -> None:
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


async def test_search_include_closed(client: CNBApiClient) -> None:
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


async def test_search_kb_unavailable_suggests_fallback(client: CNBApiClient) -> None:
    """知识库 404 → MemoryError 提示可改用 keyword_search。"""
    memory = Memory(client)
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base/query").respond(
            404, json={"errcode": 404, "errmsg": "知识库未启用"}
        )
        with pytest.raises(MemoryError, match="keyword_search") as exc_info:
            await memory.search("查询")

    assert isinstance(exc_info.value.__cause__, ApiError)


async def test_search_dedupes_repeated_numbers(client: CNBApiClient) -> None:
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
