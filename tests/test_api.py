"""CnbApiClient 单元测试：respx mock，覆盖 9 个端点与错误路径。"""

from __future__ import annotations

import httpx
import pytest
import respx

from cam import ApiError, CnbApiClient
from cam.models import CreateCommentForm, CreateIssueForm, PatchIssueForm

BASE = "https://api.cnb.cool"
ISSUE_DETAIL = {
    "number": "7",
    "title": "cam: PostgreSQL 分区表",
    "body": "正文",
    "state": "open",
    "labels": [{"id": "1", "name": "category/db", "color": "#fff"}],
    "comment_count": 0,
}


async def test_create_issue_sends_form_and_parses(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/group/repo/-/issues").respond(201, json=ISSUE_DETAIL)
        issue = await client.create_issue(CreateIssueForm(title="t", body="b"))

    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["accept"] == "application/json"
    import json

    payload = json.loads(request.content)
    assert payload == {"title": "t", "body": "b"}
    assert issue.number == 7
    assert issue.label_names == ["category/db"]


async def test_add_labels(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/group/repo/-/issues/7/labels").respond(200, json=[{"id": "2", "name": "tag/x"}])
        labels = await client.add_labels(7, ["tag/x"])

    assert route.called
    assert [lb.name for lb in labels] == ["tag/x"]


async def test_get_issue(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/7").respond(200, json=ISSUE_DETAIL)
        issue = await client.get_issue(7)
    assert issue.title == "cam: PostgreSQL 分区表"


async def test_update_issue_excludes_none(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.patch("/group/repo/-/issues/7").respond(200, json=ISSUE_DETAIL)
        await client.update_issue(7, PatchIssueForm(body="新正文"))

    import json

    payload = json.loads(route.calls.last.request.content)
    assert payload == {"body": "新正文"}


async def test_list_issues_labels_joined(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[ISSUE_DETAIL])
        issues = await client.list_issues(labels=["category/db", "tag/x"])

    assert len(issues) == 1
    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "category/db,tag/x"
    assert params["labels_operator"] == "contains_any"
    assert params["state"] == "open"


async def test_list_issues_without_labels_omits_params(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await client.list_issues()

    params = dict(route.calls.last.request.url.params)
    assert "labels" not in params
    assert "keyword" not in params


async def test_create_and_list_comments(client: CnbApiClient) -> None:
    comment = {"id": "c1", "body": "备注"}
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/group/repo/-/issues/7/comments").respond(201, json=comment)
        got = await client.create_comment(7, CreateCommentForm(body="备注"))
        list_route = mock.get("/group/repo/-/issues/7/comments").respond(200, json=[comment])
        comments = await client.list_comments(7)

    assert got.id == "c1"
    assert [c.body for c in comments] == ["备注"]
    assert post.called and list_route.called


async def test_query_knowledge_base(client: CnbApiClient) -> None:
    kb_item = {
        "score": 0.98,
        "chunk": "片段",
        "metadata": {
            "type": "issue",
            "path": "/group/repo/-/issues/7",
            "url": "https://cnb.cool/group/repo/-/issues/7",
        },
    }
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/knowledge/base/query").respond(200, json=[kb_item])
        chunks = await client.query_knowledge_base("分区表", top_k=3)

    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "分区表"
    assert params["top_k"] == "3"
    assert chunks[0].number == 7
    assert chunks[0].url == "https://cnb.cool/group/repo/-/issues/7"


async def test_get_knowledge_base(client: CnbApiClient) -> None:
    kb = {"id": "kb1", "issue_sync_enabled": True, "statistics": {"count": 3}}
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/knowledge/base").respond(200, json=kb)
        status = await client.get_knowledge_base()
    assert status.issue_sync_enabled is True


async def test_api_error_carries_body(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/404").respond(404, json={"errcode": 404, "errmsg": "issue 不存在"})
        with pytest.raises(ApiError) as exc_info:
            await client.get_issue(404)

    assert exc_info.value.status_code == 404
    assert "issue 不存在" in exc_info.value.message
    assert "404" in str(exc_info.value)


async def test_missing_repo_raises_value_error() -> None:
    async with CnbApiClient(token="t", repo="") as client:
        with pytest.raises(ValueError, match="CAM_REPO"):
            await client.get_issue(1)


async def test_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_TOKEN", "env-token")
    monkeypatch.setenv("CAM_REPO", "env/repo")
    monkeypatch.setenv("CAM_BASE_URL", "https://api.example.com/")
    monkeypatch.setenv("CAM_TIMEOUT", "5")
    client = CnbApiClient()
    assert client.token == "env-token"
    assert client.repo == "env/repo"
    assert client.base_url == "https://api.example.com"
    assert client.timeout == 5.0


def test_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_TOKEN", "env-token")
    monkeypatch.setenv("CAM_REPO", "env/repo")
    client = CnbApiClient(token="x", repo="y")
    assert client.token == "x" and client.repo == "y"


def test_web_url(client: CnbApiClient) -> None:
    assert client.web_url(7) == "https://cnb.cool/group/repo/-/issues/7"


async def test_timeout_enforced(client: CnbApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/1").mock(side_effect=httpx.ReadTimeout("timeout"))
        with pytest.raises(httpx.TimeoutException):
            await client.get_issue(1)
