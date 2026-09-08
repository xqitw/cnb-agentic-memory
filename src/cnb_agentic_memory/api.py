"""CNB Open API 薄封装（httpx 异步客户端）。

设计约定（均经实测确认）：
- 仅封装 8 个端点，无重试/限流/Provider 抽象，错误原样抛给调用方（智能体自行决策重试）
- 非 2xx 抛 ApiError，响应体原样保留
- 所有请求必须带 Accept: application/json，否则服务端返回 406（实测踩坑）
- 配置优先级：显式参数 > CNB_AGENTIC_MEMORY_ 前缀环境变量 > 默认值
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

import httpx

from .models import (
    Comment,
    CreateCommentForm,
    CreateIssueForm,
    Issue,
    KbChunk,
    Label,
    PatchIssueForm,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.cnb.cool"
DEFAULT_TIMEOUT = 30.0


def env(name: str, default: str | None = None) -> str | None:
    """读取 CNB_AGENTIC_MEMORY_ 前缀环境变量（如 CNB_AGENTIC_MEMORY_TOKEN / CNB_AGENTIC_MEMORY_REPO）。"""
    return os.environ.get(f"CNB_AGENTIC_MEMORY_{name}", default)


def parse_timeout(value: str | None) -> float:
    """解析超时秒数：非法值、非正值、inf/nan 均回落默认（0 在 httpx 语义=永不超时）。"""
    try:
        timeout = float(value) if value else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT
    return timeout if 0 < timeout < float("inf") else DEFAULT_TIMEOUT


class ConfigError(Exception):
    """配置缺失/非法（token/repo 等），SDK 与 CLI 据此给出可操作的友好提示。"""


class ApiError(Exception):
    """CNB API 错误（响应体原样保留，由调用方决定后续处理）。

    - status_code：HTTP 状态码
    - message：服务端响应原文（如 {"errcode":404,"errmsg":"..."}）
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"CNB API {status_code}: {message}")


def _first_header(lowered: dict[str, list[str]], name: str) -> str | None:
    """取同名头首个非空值（与 Starlette Headers.get() 首值语义一致，消除重复头二义）。"""
    values = lowered.get(name) or []
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def resolve_overrides_from_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """从请求头提取每请求配置覆盖（多用户共享部署场景）。

    支持的头（大小写不敏感）：

    - ``X-CNB-Token``：CNB API Token（覆盖 CNB_AGENTIC_MEMORY_TOKEN）
    - ``X-CNB-Repo``：记忆仓库 slug（覆盖 CNB_AGENTIC_MEMORY_REPO）
    - ``X-CNB-Base-URL``：API 地址（覆盖 CNB_AGENTIC_MEMORY_BASE_URL）

    仅认 ``X-CNB-`` 完整头名，不提供裸 ``token``/``repo`` 等别名：
    通用头名易与其他代理/网关注入的头冲突，意外进入头覆盖模式。

    安全约定（全有或全无）：凭据头 ``X-CNB-Token`` 与 ``X-CNB-Repo`` 必须同时
    出现才启用头覆盖模式，否则一律忽略全部头——防止「头只改 base_url」时
    服务端环境变量的 token/repo 被发送到调用方指定的任意主机（凭据外泄与
    内网探测面）。空值/空白值视为未提供。

    headers 为 None（stdio）或未启用头模式时返回空 dict，配置回落到
    环境变量（原行为不变）。
    """
    if not headers:
        return {}
    # 保留同名头全部值（首值语义在 _first_header 中统一），大小写不敏感
    lowered: dict[str, list[str]] = {}
    for k, v in headers.items():
        lowered.setdefault(k.lower(), []).append(v)
    token = _first_header(lowered, "x-cnb-token")
    repo = _first_header(lowered, "x-cnb-repo")
    if not (token and repo):
        # 凭据不齐：拒绝进入头模式，避免部分回落组合出危险配置
        return {}
    overrides: dict[str, str] = {"token": token, "repo": repo}
    base_url = _first_header(lowered, "x-cnb-base-url")
    if base_url:
        overrides["base_url"] = base_url
    return overrides


def build_client_from_headers(headers: Mapping[str, str] | None) -> CNBApiClient:
    """按请求头构造客户端：头覆盖优先，其余配置回落环境变量（原行为）。"""
    overrides = resolve_overrides_from_headers(headers)
    if not overrides:
        return CNBApiClient()
    return CNBApiClient(
        token=overrides.get("token"),
        repo=overrides.get("repo"),
        base_url=overrides.get("base_url"),
    )


class SharedClientPool:
    """按配置键缓存的 CNBApiClient 池（MCP 工具层专用，SDK 用户不受影响）。

    #83 建议第 5 条：HTTP 共享部署下每个工具调用新建客户端（TLS 握手）开销
    可观。按配置键缓存复用连接池。

    生命周期与边界（复审阻塞项整改）：

    - **条目保活复用**：acquire/release 仅做同步字典计数，引用归零不关
      连接——串行工具调用主路径每次 acquire 均命中缓存；关闭统一交显式
      aclose()（进程退出/测试清理）。
    - **绑定事件循环**：绑定发生在首次入池成功时并固定；此后异 loop
      请求不走池（临时客户端直建直关 + logging 告警一次）——httpx 连接池
      与创建它的 loop 绑定，跨 loop 复用已关连接必炸（RuntimeError）。
      MCP server 进程单 loop 主场景不受影响。
    - **有界淘汰**：条目数超 MAX_ENTRIES 时优先淘汰引用为 0 的最旧条目
      （LRU 触碰序；无 0 引用则放弃淘汰，不关正在使用的连接）。注意
      淘汰仅由**新键 acquire** 驱动：归零条目自身不会主动移出，异键
      突发未再触发 acquire 时超限条目会滞留（有界承诺在此边界内成立）。
    - **凭据不进键明文**：token 以 sha256 摘要参与键（crash/dump 不暴露）。

    acquire 含一次可挂起的淘汰 close；release 对池内条目为纯同步计数、
    对异 loop 临时客户端含一次 close 挂起（仅有的两个挂起点）——
    事件循环单线程内天然互斥，无需锁（模块级 asyncio.Lock 是多 loop
    宿主死锁源，已移除）。
    """

    #: 池条目上限：实际键空间（本机 1 + 每用户 1）远小于此，超限即异常部署。
    #: 注意淘汰仅由新键 acquire 驱动，归零条目滞留期内在用引用不占淘汰名额
    MAX_ENTRIES = 32

    def __init__(self) -> None:
        self._clients: OrderedDict[tuple[str, str, str, str], tuple[CNBApiClient, int]] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_warned = False

    @staticmethod
    def _token_digest(token: str) -> str:
        """token 的 sha256 摘要（前 16 字符）：键唯一性够用且不落明文。"""
        return hashlib.sha256(token.encode()).hexdigest()[:16]

    def _key(
        self,
        token: str | None,
        repo: str | None,
        base_url: str | None,
        timeout: float | None,
    ) -> tuple[str, str, str, str]:
        """配置键：token 走摘要，repo/base_url/timeout 明文（非敏感）。"""
        resolved_timeout = timeout if timeout is not None else parse_timeout(env("TIMEOUT"))
        return (
            self._token_digest((token or env("TOKEN") or "").strip()),
            (repo or env("REPO") or "").strip(),
            (base_url or env("BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            str(resolved_timeout),
        )

    async def _evict_if_full(self) -> None:
        """条目超上限时淘汰最旧的 0 引用条目（LRU）；无空闲则放弃不阻塞。"""
        while len(self._clients) >= self.MAX_ENTRIES:
            evictable = next((k for k, (_, refs) in self._clients.items() if refs == 0), None)
            if evictable is None:
                return  # 全部在用：宁可超限也不关正在使用的连接
            client, _ = self._clients.pop(evictable)
            await client.close()

    async def acquire(
        self,
        token: str | None = None,
        repo: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> CNBApiClient:
        """取键相同的缓存客户端（引用 +1），无则构造并缓存。

        异 loop 请求不走池：直接返回临时客户端（release 时识别关闭），
        不共享、不缓存，避免跨 loop 复用已关连接（RuntimeError）。
        """
        loop = asyncio.get_running_loop()
        # 已绑定 loop 的存活探活：临时 loop 首请求劫持绑定后，
        # loop 结束 → is_closed() → 解绑重绑到当前 loop，劫持可自愈而非永久。
        # 重绑时池内条目全部属已死 loop（连接已随 loop 死亡），一并作废清空。
        if self._loop is not None and self._loop.is_closed():
            logger.warning("SharedClientPool 绑定的事件循环已关闭，解绑并重建绑定（原缓存连接随 loop 失效）")
            self._clients.clear()
            self._loop = None
        # 绑定与入池绑定：只有确认走缓存路径才允许设置 _loop——防止进程首个
        # 请求来自临时 loop（管理探针等）把池劫持到即将结束的 loop 上
        if self._loop is not None and self._loop is not loop:
            if not self._loop_warned:
                self._loop_warned = True
                logger.warning(
                    "SharedClientPool 检测到跨事件循环访问，该请求不享受连接复用"
                    "（httpx 客户端与创建它的 loop 绑定）；本池随首次使用的 loop 固定。"
                )
            client = CNBApiClient(token=token, repo=repo, base_url=base_url, timeout=timeout)
            client._pool_temporary = True  # noqa: SLF001 — 池内部标记
            return client

        key = self._key(token, repo, base_url, timeout)
        await self._evict_if_full()
        entry = self._clients.get(key)
        if entry is not None:
            client, refs = entry
            self._clients[key] = (client, refs + 1)
            self._clients.move_to_end(key)  # LRU 触碰
            return client
        client = CNBApiClient(token=token, repo=repo, base_url=base_url, timeout=timeout)
        self._clients[key] = (client, 1)
        if self._loop is None:
            self._loop = loop
        return client

    async def release(self, client: CNBApiClient) -> None:
        """引用 -1；归零不关（条目保活复用），关闭统一走 aclose()。

        池内条目路径为纯同步计数（事件循环单线程内「查找-计数」不可被
        其他协程打断）；异 loop 临时客户端在此直接关闭——这是本方法
        唯一的 await 挂起点。
        """
        if getattr(client, "_pool_temporary", False):
            await client.close()
            return
        for key, (cached, refs) in self._clients.items():
            if cached is client:
                self._clients[key] = (cached, max(refs - 1, 0))
                return

    async def aclose(self) -> None:
        """关闭全部缓存客户端并清空（进程退出/测试清理用；显式调用）。"""
        for client, _ in self._clients.values():
            try:
                await client.close()
            except RuntimeError:  # 异 loop 连接：Event loop is closed
                # 静默吞没无留痕会掩盖排查线索：debug 级留痕（不升 warning，
                # 异 loop 已死连接本就该关，属预期清理分支）
                logger.debug("aclose 跳过异 loop 已关闭连接：%r", client, exc_info=True)
        self._clients.clear()


class CNBApiClient:
    """CNB Open API 异步客户端（8 个端点的薄封装）。

    用法::

        async with CNBApiClient(token="...", repo="group/repo") as client:
            issue = await client.get_issue(1)
    """

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.token = (token or env("TOKEN") or "").strip()
        self.repo = (repo or env("REPO") or "").strip()
        self.base_url = (base_url or env("BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else parse_timeout(env("TIMEOUT"))
        self._validate_config()
        self._client: httpx.AsyncClient | None = None
        # 池内部标记：SharedClientPool 对异 loop 请求建的临时客户端（不入池，release 即关）
        self._pool_temporary: bool = False

    # ---- 生命周期 ----

    @property
    def client(self) -> httpx.AsyncClient:
        """懒创建的 httpx 异步客户端（统一认证与 Accept 头）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=self.timeout,
                # 显式跟随重定向（默认 False 会让 3xx 落入"响应非 JSON"分支，
                # 抛出误导性 ApiError；实测 CNB API 当前无重定向场景，此处为健壮性预留）
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> CNBApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        """收敛 repr：token 仅呈现摘要前 8 字符——池条目长期驻留进程内，
        dump/诊断输出不得携带明文凭据（#83 建议第 5 条复审建议）。"""
        token_head = hashlib.sha256(self.token.encode()).hexdigest()[:8] if self.token else "<empty>"
        return (
            f"CNBApiClient(repo={self.repo!r}, base_url={self.base_url!r}, "
            f"token=sha256:{token_head}, timeout={self.timeout!r})"
        )

    # ---- 内部 ----

    def _validate_config(self) -> None:
        """构造时前置校验配置完整性，给出可操作的提示（而非请求时才炸）。"""
        missing = []
        if not self.token:
            missing.append("CNB_AGENTIC_MEMORY_TOKEN（CNB API 令牌）")
        if not self.repo:
            missing.append("CNB_AGENTIC_MEMORY_REPO（记忆仓库 slug，如 group/memory）")
        if missing:
            raise ConfigError("缺少必需配置：" + "、".join(missing))

    def _path(self, suffix: str) -> str:
        """拼接 API 路径：/{repo}/-/{suffix}。"""
        return f"/{self.repo}/-/{suffix.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """发请求；非 2xx 抛 ApiError（响应体原样保留），成功返回 JSON。"""
        resp = await self.client.request(method, path, params=params, json=json_body)
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, resp.text)
        try:
            return resp.json()
        except ValueError as err:
            # 2xx 但响应非 JSON（网关异常页等）：保留原文抛错，不掩盖真实响应
            raise ApiError(resp.status_code, f"响应非 JSON：{resp.text[:500]}") from err

    # ---- Issue 端点（纯 CRUD，记忆语义见 memory.py）----

    async def create_issue(self, form: CreateIssueForm) -> Issue:
        """创建 Issue（POST /{-}/issues），标签走 add_labels 两步写入。"""
        data = await self._request("POST", self._path("issues"), json_body=form.model_dump(exclude_none=True))
        return Issue.model_validate(data)

    async def add_labels(self, number: int, labels: list[str]) -> list[Label]:
        """补打标签（POST /{-}/issues/{number}/labels），可自动创建不存在的标签。"""
        data = await self._request(
            "POST", self._path(f"issues/{number}/labels"), json_body={"labels": labels}
        )
        return [Label.model_validate(item) for item in data]

    async def get_issue(self, number: int) -> Issue:
        """读取 Issue（GET /{-}/issues/{number}）。"""
        return Issue.model_validate(await self._request("GET", self._path(f"issues/{number}")))

    async def update_issue(self, number: int, form: PatchIssueForm) -> Issue:
        """更新 Issue（PATCH /{-}/issues/{number}），None 字段不发送。"""
        data = await self._request(
            "PATCH",
            self._path(f"issues/{number}"),
            json_body=form.model_dump(exclude_none=True),
        )
        return Issue.model_validate(data)

    async def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        labels_operator: str = "contains_any",
        keyword: str | None = None,
        order_by: str = "-updated_at",
        page: int = 1,
        page_size: int = 100,
    ) -> list[Issue]:
        """列出 Issue（GET /{-}/issues），服务端分页上限 100/页。

        keyword 只匹配标题（实测两轮确认）；labels 为空时不传过滤参数。
        """
        params: dict[str, Any] = {
            "state": state,
            "order_by": order_by,
            "page": page,
            "page_size": page_size,
        }
        if labels:
            params["labels"] = ",".join(labels)
            params["labels_operator"] = labels_operator
        if keyword:
            params["keyword"] = keyword
        data = await self._request("GET", self._path("issues"), params=params)
        return [Issue.model_validate(item) for item in data]

    async def create_comment(self, number: int, form: CreateCommentForm) -> Comment:
        """追加评论（POST /{-}/issues/{number}/comments）。"""
        data = await self._request(
            "POST",
            self._path(f"issues/{number}/comments"),
            json_body=form.model_dump(exclude_none=True),
        )
        return Comment.model_validate(data)

    async def list_comments(
        self, number: int, *, sort: str = "created", page: int = 1, page_size: int = 100
    ) -> list[Comment]:
        """列出评论（GET /{-}/issues/{number}/comments），sort 支持 created/-created/updated/-updated。"""
        data = await self._request(
            "GET",
            self._path(f"issues/{number}/comments"),
            params={"sort": sort, "page": page, "page_size": page_size},
        )
        return [Comment.model_validate(item) for item in data]

    # ---- 知识库端点 ----

    async def query_knowledge_base(
        self, query: str, *, top_k: int = 5, score_threshold: float | None = None
    ) -> list[KbChunk]:
        """知识库语义检索（GET /{-}/knowledge/base/query），主检索通道。"""
        params: dict[str, Any] = {"query": query, "top_k": top_k}
        if score_threshold is not None:
            params["score_threshold"] = score_threshold
        data = await self._request("GET", self._path("knowledge/base/query"), params=params)
        return [KbChunk.model_validate(item) for item in data]
