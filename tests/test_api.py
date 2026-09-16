"""CNBApiClient 单元测试：respx mock，覆盖 8 个端点与错误路径。"""

from __future__ import annotations

import traceback

import httpx
import pytest
import respx

from cnb_agentic_memory import ApiError, CNBApiClient
from cnb_agentic_memory.models import CreateCommentForm, CreateIssueForm, PatchIssueForm

BASE = "https://api.cnb.cool"
ISSUE_DETAIL = {
    "number": "7",
    "title": "PostgreSQL 分区表",
    "body": "正文",
    "state": "open",
    "labels": [{"id": "1", "name": "category:db", "color": "#fff"}],
    "comment_count": 0,
}


async def test_create_issue_sends_form_and_parses(client: CNBApiClient) -> None:
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
    assert issue.label_names == ["category:db"]


async def test_add_labels(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/group/repo/-/issues/7/labels").respond(200, json=[{"id": "2", "name": "x"}])
        labels = await client.add_labels(7, ["x"])

    assert route.called
    assert [lb.name for lb in labels] == ["x"]


async def test_get_issue(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/7").respond(200, json=ISSUE_DETAIL)
        issue = await client.get_issue(7)
    assert issue.title == "PostgreSQL 分区表"


async def test_update_issue_excludes_none(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.patch("/group/repo/-/issues/7").respond(200, json=ISSUE_DETAIL)
        await client.update_issue(7, PatchIssueForm(body="新正文"))

    import json

    payload = json.loads(route.calls.last.request.content)
    assert payload == {"body": "新正文"}


async def test_list_issues_labels_joined(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[ISSUE_DETAIL])
        issues = await client.list_issues(labels=["category:db", "x"])

    assert len(issues) == 1
    params = dict(route.calls.last.request.url.params)
    assert params["labels"] == "category:db,x"
    assert params["labels_operator"] == "contains_any"
    assert params["state"] == "open"


async def test_list_issues_without_labels_omits_params(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/issues").respond(200, json=[])
        await client.list_issues()

    params = dict(route.calls.last.request.url.params)
    assert "labels" not in params
    assert "keyword" not in params


async def test_create_and_list_comments(client: CNBApiClient) -> None:
    comment = {"id": "c1", "body": "备注"}
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/group/repo/-/issues/7/comments").respond(201, json=comment)
        got = await client.create_comment(7, CreateCommentForm(body="备注"))
        list_route = mock.get("/group/repo/-/issues/7/comments").respond(200, json=[comment])
        comments = await client.list_comments(7)

    assert got.id == "c1"
    assert [c.body for c in comments] == ["备注"]
    assert post.called and list_route.called


async def test_query_knowledge_base(client: CNBApiClient) -> None:
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


async def test_query_knowledge_base_score_threshold_passed(client: CNBApiClient) -> None:
    """score_threshold 传入时透传为查询参数。"""
    kb_item = {
        "score": 0.98,
        "chunk": "片段",
        "metadata": {"type": "issue", "path": "/group/repo/-/issues/7"},
    }
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/knowledge/base/query").respond(200, json=[kb_item])
        await client.query_knowledge_base("分区表", top_k=3, score_threshold=0.5)

    params = dict(route.calls.last.request.url.params)
    assert params["score_threshold"] == "0.5"


async def test_query_knowledge_base_omits_threshold_when_none(client: CNBApiClient) -> None:
    """score_threshold=None（默认）时不携带该参数（反向）。"""
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/group/repo/-/knowledge/base/query").respond(200, json=[])
        await client.query_knowledge_base("分区表")

    assert "score_threshold" not in dict(route.calls.last.request.url.params)


async def test_api_error_carries_body(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/404").respond(404, json={"errcode": 404, "errmsg": "issue 不存在"})
        with pytest.raises(ApiError) as exc_info:
            await client.get_issue(404)

    assert exc_info.value.status_code == 404
    assert "issue 不存在" in exc_info.value.message
    assert "404" in str(exc_info.value)


def test_missing_config_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置缺失在构造时前置报错（含可操作提示），而非请求时才失败。"""
    from cnb_agentic_memory import ConfigError

    # 显式空字符串因 or 语义会回落环境变量，先清除以保证测试环境无关
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_TOKEN", raising=False)
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_REPO", raising=False)

    with pytest.raises(ConfigError, match="CNB_AGENTIC_MEMORY_TOKEN"):
        CNBApiClient(token="", repo="g/r")
    with pytest.raises(ConfigError, match="CNB_AGENTIC_MEMORY_REPO"):
        CNBApiClient(token="t", repo="")


# 固定密码标记：断言其在任何错误文案/日志/repr 中零出现
SECRET_MARKER = "sup3r-secret-marker"

# #100 验收清单：畸形/凭据形态逐一作 base_url，全部应被构造期拒绝
CREDENTIAL_OR_MALFORMED_BASE_URLS = [
    "https://u:SECRET@h.cool",  # 正常形态含凭据
    '"https://u:SECRET@h.cool"',  # 带引号（.env/YAML 复制粘贴常见）
    "https：//u:SECRET@h.cool",  # 全角冒号
    "https:/u:SECRET@h.cool",  # 单斜杠
    "https://u:p@ss@h.cool",  # 密码含 @
    "https://u:cy1zZWNyZXQ=@h.cool",  # base64 风格口令
    "https://u:SECRET.h.cool",  # 漏 @ 手误（httpx 解析为非法端口，原文可入异常文案）
    "https://u:SECRET＠h.cool",  # 全角 @（同为非法端口形态）
    "https://u:p%40ss.host",  # 编码 @ 在口令（非法端口形态）
    "https://api.cnb.cool?token=SECRET",  # query 携带凭据（httpx 日志明文输出 URL）
    "https://api.cnb.cool?",  # 尾随空 ?：解析属性为空但字面存在，请求路径被吞进 query
    "https://h.cool#SECRET",  # fragment 携带凭据
    "https://xn--a",  # 畸形 A-label：host 属性抛 IDNA 异常，须在 try 内统一转 ConfigError
    "https://xn--SECRET-pw",  # 同上，且标签含凭据
    "https://xn--a?token=SECRET",  # 畸形 A-label × query 凭据：字面判据须前置于 host 求值
    "https://xn--SECRET-ta.cool",  # IDNA 文案解出明文标签的形态——回显复活时此用例必红
    "https:///u:SECRET@h.cool",  # 三斜杠空 host：userinfo 被吞进 path，原文随请求 URL 入日志
    "https:////u:SECRET@h.cool",  # 四斜杠同族
    "https:///",  # 纯空 host
    "//h.cool",  # 无 scheme
    "///h.cool",  # 无 scheme 多斜杠
    "h.cool",  # 裸主机名
]


@pytest.mark.parametrize("base_url", CREDENTIAL_OR_MALFORMED_BASE_URLS)
def test_credential_or_malformed_base_url_rejected_without_echo(
    base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """含凭据/畸形 base_url 构造期拒绝，且值不回显进任何错误文案与日志。"""
    from cnb_agentic_memory import ConfigError

    probe = base_url.replace("SECRET", SECRET_MARKER)
    with caplog.at_level("DEBUG"):
        with pytest.raises(ConfigError) as exc_info:
            CNBApiClient(token="t", repo="g/r", base_url=probe)
    # 值零回显：错误文案与捕获日志（含 httpx logger）均不得出现密码标记与原文
    assert SECRET_MARKER not in str(exc_info.value)
    assert probe not in str(exc_info.value)
    assert SECRET_MARKER not in caplog.text
    # 解析失败的原异常不链入：有隐式上下文链（裸 raise in except）必须已抑制
    # （__cause__ 对「删除 from None」无辨别力——评审实测隐式链的 traceback 带原文）
    assert exc_info.value.__context__ is None or exc_info.value.__suppress_context__ is True
    assert SECRET_MARKER not in "".join(traceback.format_exception(exc_info.value))
    # httpx 未被触达：凭据 URL 永不进入 httpx，其请求日志泄露面不可达
    assert "HTTP Request" not in caplog.text


@pytest.mark.parametrize(
    "base_url",
    [
        "HTTPS://api.cnb.cool",  # 大写 scheme（基线行为，不得因校验收窄回归）
        "https://api.cnb.cool/@user",  # path 中的 @ 不属 userinfo，不得误拒
        "https://h.cool/api",  # 带 path 的合法 origin
    ],
)
def test_valid_base_url_variants_accepted(base_url: str) -> None:
    """合法 base_url 形态（大写 scheme / path 含 @ / 带 path）构造期放行。"""
    client = CNBApiClient(token="t", repo="g/r", base_url=base_url)
    assert client.base_url == base_url.rstrip("/")


def test_repr_omits_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """repr 不含 base_url（含畸形形态在内的任何值都不回显），定位靠 repo/token 摘要。"""
    monkeypatch.delenv("CNB_AGENTIC_MEMORY_BASE_URL", raising=False)
    client = CNBApiClient(token="t", repo="g/r", base_url="https://api.example.com")
    assert "@" not in repr(client)
    assert "http" not in repr(client)
    assert "api.example.com" not in repr(client)
    assert "g/r" in repr(client) and "sha256:" in repr(client)


def test_credential_base_url_never_reaches_httpx(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """含凭据 base_url 被拒后，httpx 自身 INFO 日志（实测明文输出 URL 含密码）无任何请求行。"""
    from cnb_agentic_memory import ConfigError

    monkeypatch.delenv("CNB_AGENTIC_MEMORY_BASE_URL", raising=False)
    with caplog.at_level("INFO", logger="httpx"):
        with pytest.raises(ConfigError):
            CNBApiClient(token="t", repo="g/r", base_url=f"https://u:{SECRET_MARKER}@h.cool")
    assert "HTTP Request" not in caplog.text
    assert SECRET_MARKER not in caplog.text


async def test_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "env-token")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "env/repo")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_BASE_URL", "https://api.example.com/")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TIMEOUT", "5")
    client = CNBApiClient()
    assert client.token == "env-token"
    assert client.repo == "env/repo"
    assert client.base_url == "https://api.example.com"
    assert client.timeout == 5.0


def test_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TOKEN", "env-token")
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_REPO", "env/repo")
    client = CNBApiClient(token="x", repo="y")
    assert client.token == "x" and client.repo == "y"


def test_invalid_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """CNB_AGENTIC_MEMORY_TIMEOUT 非法值回落默认。"""
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TIMEOUT", "abc")
    client = CNBApiClient(token="t", repo="g/r")
    assert client.timeout == 30.0


def test_non_positive_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """CNB_AGENTIC_MEMORY_TIMEOUT=0/负值/inf 回落默认（0 在 httpx 语义=永不超时）。"""
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TIMEOUT", "0")
    assert CNBApiClient(token="t", repo="g/r").timeout == 30.0
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TIMEOUT", "-5")
    assert CNBApiClient(token="t", repo="g/r").timeout == 30.0
    monkeypatch.setenv("CNB_AGENTIC_MEMORY_TIMEOUT", "inf")
    assert CNBApiClient(token="t", repo="g/r").timeout == 30.0


async def test_non_json_2xx_raises_api_error(client: CNBApiClient) -> None:
    """2xx 但响应非 JSON（网关异常页等）→ ApiError 保留原文。"""
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/1").respond(200, text="<html>Bad Gateway Page</html>")
        with pytest.raises(ApiError) as exc_info:
            await client.get_issue(1)

    assert "Bad Gateway Page" in exc_info.value.message


async def test_timeout_enforced(client: CNBApiClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/1").mock(side_effect=httpx.ReadTimeout("timeout"))
        with pytest.raises(httpx.TimeoutException):
            await client.get_issue(1)


# ---- #99 边界用例（错误出口安全边界） ----


def test_api_error_message_truncated() -> None:
    """ApiError 构造级截断：500KB 上游异常页不回灌上下文（#99 ①）。"""
    from cnb_agentic_memory.api import API_ERROR_TEXT_LIMIT

    err = ApiError(502, "E" * 500_000)
    assert len(err.message) == API_ERROR_TEXT_LIMIT
    assert len(str(err)) < API_ERROR_TEXT_LIMIT + 100


async def test_non_2xx_body_truncated_not_replayed(client: CNBApiClient) -> None:
    """非 2xx 超长响应体经 ApiError 构造级截断（覆盖所有构造路径）。"""
    from cnb_agentic_memory.api import API_ERROR_TEXT_LIMIT

    with respx.mock(base_url=BASE) as mock:
        mock.get("/group/repo/-/issues/1").respond(502, text="X" * 500_000)
        with pytest.raises(ApiError) as exc_info:
            await client.get_issue(1)

    assert len(exc_info.value.message) == API_ERROR_TEXT_LIMIT


@pytest.mark.parametrize(
    "token",
    [
        "tok\nen\ntoken",  # CR/LF：请求头注入 + 凭据明文入日志
        "tok\ttoken",  # 制表符
        "toK\xa0n",  # 非打印空白
        "toK\u4e2d\u6587n",  # 非 ASCII：请求期 UnicodeEncodeError
    ],
)
def test_malformed_token_rejected_before_request(token: str) -> None:
    """非法 token 构造期即拒，不穿透到请求期（#99 ②）。"""
    from cnb_agentic_memory import ConfigError

    with pytest.raises(ConfigError, match="TOKEN 含非法字符"):
        CNBApiClient(token=token, repo="g/r")


@pytest.mark.parametrize(
    "repo",
    [
        "g/r\nx",  # CR/LF
        "g\tr",  # 制表符
        "g\xa0r",  # 非打印空白
    ],
)
def test_malformed_repo_rejected_before_request(repo: str) -> None:
    """含控制字符/非打印字符的 repo 构造期即拒（#99 ③）。"""
    from cnb_agentic_memory import ConfigError

    with pytest.raises(ConfigError, match="REPO 含换行/制表等控制字符"):
        CNBApiClient(token="t", repo=repo)


@pytest.mark.parametrize("repo", ["g r", "g?token=x", "g#frag"])
def test_repo_with_space_or_query_rejected(repo: str) -> None:
    """repo 空白 / ?/# 构造期拒绝（凭据形态文本入 httpx 日志的通道）。"""
    from cnb_agentic_memory import ConfigError

    with pytest.raises(ConfigError, match="含空白"):
        CNBApiClient(token="t", repo=repo)


@pytest.mark.parametrize("repo", ["g/r\u4e2d\u6587", "\u7ec4\u7ec7/\u8bb0\u5fc6\u5e93"])
def test_non_ascii_repo_accepted(repo: str) -> None:
    """非 ASCII repo 必须可构造：slug 含中文时 httpx 按规范百分号编码发出，
    构造期拒绝属功能回归（base 实测可正常发出请求）。"""
    client = CNBApiClient(token="t", repo=repo)
    assert client.repo == repo


@pytest.mark.parametrize(
    "base_url",
    ["https://h.cool:0", "https://h.cool:65536", "https://h.cool:99999"],
)
def test_out_of_range_port_rejected_at_construction(base_url: str) -> None:
    """端口越界构造期拒绝（#99 ③）：httpx 对越界端口构造期不抛错，
    会穿透到请求期以非 HTTPError 族异常失败。"""
    from cnb_agentic_memory import ConfigError

    with pytest.raises(ConfigError, match="端口越界"):
        CNBApiClient(token="t", repo="g/r", base_url=base_url)


def test_valid_port_accepted() -> None:
    """合法端口不误拒。"""
    client = CNBApiClient(token="t", repo="g/r", base_url="https://h.cool:8443")
    assert client.base_url.endswith(":8443")


def test_token_repo_error_no_value_echo() -> None:
    """token/repo 校验文案不回显原值（对齐 base_url 校验既有约定）。"""
    from cnb_agentic_memory import ConfigError

    marker = "sEcReT-tOkEn-9x"
    with pytest.raises(ConfigError) as exc_info:
        CNBApiClient(token=f"t{marker}\nt", repo="g/r")
    assert marker not in str(exc_info.value)

    repo_marker = "sEcReT-rePo-7z"
    with pytest.raises(ConfigError) as exc_info:
        CNBApiClient(token="t", repo=f"g{repo_marker}\nr")
    assert repo_marker not in str(exc_info.value)
