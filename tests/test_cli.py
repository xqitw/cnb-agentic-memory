"""CLI 单元测试：typer CliRunner + respx mock，验证命令行为与错误出口。"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cam.cli import app

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
    for cmd in ("write", "get", "update", "append", "delete", "restore", "list", "recent", "search"):
        assert cmd in result.output


def test_write_outputs_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import respx

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")
    monkeypatch.setattr("cam.memory.VERIFY_INTERVAL_SECONDS", 0)

    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/g/r/-/issues").mock(side_effect=echo_issue(9))
        mock.post("/g/r/-/issues/9/labels").respond(200, json=[])
        mock.get("/g/r/-/issues/9").respond(200, json=issue_payload(9, "cam: 测试标题"))
        result = runner.invoke(app, ["write", "测试内容", "--title", "测试标题"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["number"] == 9
    assert data["title"] == "cam: 测试标题"
    assert "url" in data


def test_get_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues/7").respond(
            200,
            json={
                "number": "7",
                "title": "cam: t",
                "body": "正文",
                "state": "open",
                "labels": [{"name": "tag/x"}],
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )
        result = runner.invoke(app, ["get", "7"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["number"] == 7
    assert data["labels"] == ["tag/x"]


def test_api_error_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues/404").respond(404, json={"errcode": 404, "errmsg": "不存在"})
        result = runner.invoke(app, ["get", "404"])

    assert result.exit_code == 1
    assert "404" in result.output


def test_missing_repo_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAM_REPO", raising=False)
    monkeypatch.delenv("CAM_TOKEN", raising=False)
    result = runner.invoke(app, ["get", "1"])

    assert result.exit_code == 1


def test_list_state_bogus_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """--state 仅支持 open/closed（CNB API 不支持 all），非法值前置拒绝。"""
    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

    result = runner.invoke(app, ["list", "--state", "all"])

    assert result.exit_code == 2
    assert "仅支持 open/closed" in result.output


def test_write_split_outputs_all_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    """超长拆分时 CLI 输出全部分片编号（评审：循迹不漏片）。"""
    import respx

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

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

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/g/r/-/issues").respond(200, json=[])
        result = runner.invoke(app, ["list", "--limit", "500"])

    assert result.exit_code == 0
    params = dict(route.calls.last.request.url.params)
    assert params["page_size"] == "100"


def test_search_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import respx

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

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

    monkeypatch.setenv("CAM_TOKEN", "t")
    monkeypatch.setenv("CAM_REPO", "g/r")

    with respx.mock(base_url=BASE) as mock:
        mock.get("/g/r/-/issues").respond(200, json=[])
        result = runner.invoke(app, ["list", "--category", "db"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
