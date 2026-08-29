"""CLI 单元测试：typer CliRunner + respx mock，验证命令行为与错误出口。"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cnb_agentic_memory.cli import app

runner = CliRunner()
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


def echo_issue(number: int, state: str = "open"):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            201,
            json=issue_payload(number, payload.get("title", ""), body=payload.get("body", ""), state=state),
        )

    return handler


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "write",
        "get",
        "update",
        "append",
        "delete",
        "restore",
        "list",
        "recent",
        "search",
        "keyword",
    ):
        assert cmd in result.output


def test_write_outputs_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")
    monkeypatch.setattr("cnb_agentic_memory.memory.VERIFY_INTERVAL_SECONDS", 0)

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/g/r/-/issues").mock(side_effect=echo_issue(9))
        mock.post("/g/r/-/issues/9/labels").respond(200, json=[])
        mock.get("/g/r/-/issues/9").respond(200, json=issue_payload(9, "测试标题"))
        result = runner.invoke(app, ["write", "测试内容", "--title", "测试标题"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["number"] == 9
    assert data["title"] == "测试标题"


def test_get_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

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
        result = runner.invoke(app, ["get", "7"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["number"] == 7
    assert data["labels"] == ["x"]


def test_api_error_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues/404").respond(404, json={"errcode": 404, "errmsg": "不存在"})
        result = runner.invoke(app, ["get", "404"])

    assert result.exit_code == 1
    assert "404" in result.output


def test_missing_repo_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置缺失：退出码 2 + 可操作的友好提示（评审反馈：令牌缺失须有提示）。"""
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_REPO", raising=False)
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_TOKEN", raising=False)
    result = runner.invoke(app, ["get", "1"])

    assert result.exit_code == 2
    assert "CNB_AGENTIC_MEMORY_TOKEN" in result.output
    assert "CNB_AGENTIC_MEMORY_REPO" in result.output


def test_list_state_bogus_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """--state 仅支持 open/closed（CNB API 不支持 all），非法值前置拒绝。"""
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    result = runner.invoke(app, ["list", "--state", "all"])

    assert result.exit_code == 2
    assert "仅支持 open/closed" in result.output


def test_write_split_outputs_all_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """超长拆分时 CLI 输出全部分片编号（评审：循迹不漏片）。"""
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

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
        mock.post("/g/r/-/issues").mock(side_effect=create_side_effect)
        mock.post(path__regex=r"/g/r/-/issues/\d+/labels").respond(200, json=[])
        mock.get(path__regex=r"/g/r/-/issues/\d+").mock(side_effect=get_side_effect)
        result = runner.invoke(app, ["write", "段落。\n\n" + "x" * 40000, "--title", "t"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["parts"]) > 1  # 确实拆分了
    assert data["parts"][0]["number"] == data["number"]  # 首片即主编号


def test_list_limit_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """--limit 超过 100 被 clamp 到 100（服务端分页上限）。"""
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/g/r/-/issues").respond(200, json=[])
        result = runner.invoke(app, ["list", "--limit", "500"])

    assert result.exit_code == 0
    params = dict(route.calls.last.request.url.params)
    assert params["page_size"] == "100"


def test_search_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    kb_item = {
        "score": 0.9,
        "chunk": "片段",
        "metadata": {"path": "/g/r/-/issues/11", "type": "issue"},
    }
    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/knowledge/base/query").respond(200, json=[kb_item])
        mock.get("/g/r/-/issues/11").respond(
            200,
            json={
                "number": "11",
                "title": "记忆甲",
                "body": "",
                "state": "open",
                "labels": [],
                "comment_count": 0,
            },
        )
        result = runner.invoke(app, ["search", "查询"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["number"] == 11
    assert data[0]["score"] == pytest.approx(0.9)


def test_list_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues").respond(200, json=[])
        result = runner.invoke(app, ["list", "--category", "db"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_keyword_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """keyword 命令：透传 keyword 过滤，limit 钳制，默认仅查 open（复审：CLI 层直接用例）。"""
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    def issue(number: str, updated: str) -> dict:
        return {
            "number": number,
            "title": f"pg_partman {number}",
            "body": "",
            "state": "open",
            "labels": [{"name": "postgresql"}],
            "updated_at": updated,
        }

    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/g/r/-/issues").respond(
            200, json=[issue("3", "2026-01-03T00:00:00Z"), issue("1", "2026-01-01T00:00:00Z")]
        )
        result = runner.invoke(app, ["keyword", "pg_partman", "--limit", "500"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [i["number"] for i in data] == [3, 1]  # updated_at 降序（number 由模型解析为 int）
    params = dict(route.calls.last.request.url.params)
    assert params["keyword"] == "pg_partman"
    assert params["page_size"] == "100"  # limit 钳制到服务端分页上限
    assert "labels" not in params  # 专用记忆仓库，无需标签过滤
    assert params["state"] == "open"  # 默认仅查 open


def test_keyword_include_closed_queries_both_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """--include-closed：open/closed 双查合并去重（与 SDK include_closed 语义一致）。"""
    import respx

    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    def handler(request: httpx.Request) -> httpx.Response:
        state = dict(request.url.params).get("state")
        return httpx.Response(
            200,
            json=[
                {
                    "number": "5" if state == "open" else "7",
                    "title": "pg_partman",
                    "body": "",
                    "state": state,
                    "labels": [{"name": "x"}],
                    "updated_at": "2026-01-05T00:00:00Z" if state == "open" else "2026-01-07T00:00:00Z",
                }
            ],
        )

    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/g/r/-/issues").mock(side_effect=handler)
        result = runner.invoke(app, ["keyword", "pg_partman", "--include-closed"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [i["number"] for i in data] == [7, 5]  # closed 更新在前（updated_at 降序）
    assert route.call_count == 2  # open + closed 各查一次


def test_keyword_empty_query_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """空检索词前置拒绝（MemoryError → 退出码 1）。"""
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "t")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "g/r")

    result = runner.invoke(app, ["keyword", "   "])

    assert result.exit_code == 1
    assert "检索词不能为空" in result.output
