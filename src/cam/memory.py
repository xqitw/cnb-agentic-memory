"""记忆语义层：在 API 薄封装之上实现记忆业务规则。

沉淀的架构红线（均来自实测确认）：
- 两步写入：创建 Issue 后必须单独补打标签（创建接口对新标签静默丢弃）
- 写路径回读校验：所有写操作完成后 GET 回读确认（CNB API 存在静默失败形态）
- title 由调用方（智能体）撰写，工具只保证不变量：前缀、长度上限、非空兜底
- 软删除：PATCH state=closed + state_reason=not_planned，可 reopen 恢复
- 超长拆分：单条记忆超过 MAX_BODY_BYTES 自动拆为多条，用 title 关联
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .api import ApiError, CnbApiClient
from .models import Comment, CreateCommentForm, CreateIssueForm, Issue, KbChunk, PatchIssueForm

if TYPE_CHECKING:
    from builtins import list as _List  # noqa: UP035  # 避免与 Memory.list 方法名冲突

# 软删除 / 生命周期状态约定
STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_REASON_NOT_PLANNED = "not_planned"
STATE_REASON_REOPENED = "reopened"

# 分类与标签命名空间约定
CATEGORY_PREFIX = "category/"
TAG_PREFIX = "tag/"

# 单条记忆正文字节上限（35KB 实测下界留出余量，超长自动拆分）
MAX_BODY_BYTES = 30_000
# title 长度上限（keyword 只搜标题，title 是标题检索的唯一入口，超长无意义）
MAX_TITLE_CHARS = 60
# title 命名空间前缀（cam: <关键词短语>，便于识别 cam 管理的 Issue）
TITLE_PREFIX = "cam: "
# 回读校验重试次数与间隔（CNB API 存在静默失败形态，写后必须 GET 确认）
VERIFY_RETRIES = 3
VERIFY_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class WriteResult:
    """memory_write 的返回：记忆编号与 Web 地址。"""

    number: int
    title: str
    url: str


@dataclass(frozen=True)
class SearchResult:
    """memory_search 的返回：语义召回 + 补齐元信息。"""

    score: float
    chunk: str
    number: int
    title: str
    state: str
    url: str


class MemoryError(Exception):
    """记忆语义层错误（在 ApiError 之上，表达业务规则失败）。"""


def normalize_title(title: str | None, content: str) -> str:
    """规范化 title，保证两条不变量：cam: 前缀 + 长度上限。

    title 撰写权在调用方（智能体）：应提炼高区分度关键词短语，
    keyword 检索只匹配标题。未提供时兜底为正文首行截取——
    不做语义提炼，只保证与内容相关且长度有界。
    """
    raw = title.strip() if title and title.strip() else _first_line(content)
    body = f"{TITLE_PREFIX}{raw}"[:MAX_TITLE_CHARS].strip()
    return body if len(body) > len(TITLE_PREFIX) else f"{TITLE_PREFIX}untitled memory"


def _first_line(content: str) -> str:
    """取正文首个非空行作为兜底 title 素材。"""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()  # 去除 Markdown 标题符
        if stripped:
            return stripped
    return "untitled memory"


def _split_body(content: str) -> _List[str]:
    """超长正文拆分为不超过 MAX_BODY_BYTES 的多条（优先空行段落，保底按行）。

    单段落超限时二次按行拆分，无空行的超长单行（代码块/长文本）同样覆盖。
    """
    if len(content.encode("utf-8")) <= MAX_BODY_BYTES:
        return [content]

    # 第一轮：按空行（Markdown 段落）聚合
    parts: _List[str] = []
    buf = ""
    for para in content.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if buf and len(candidate.encode("utf-8")) > MAX_BODY_BYTES:
            parts.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        parts.append(buf)

    # 第二轮：仍超限的片段按行拆（覆盖无空行的超长单行/代码块）
    bounded: _List[str] = []
    for part in parts:
        if len(part.encode("utf-8")) <= MAX_BODY_BYTES:
            bounded.append(part)
            continue
        chunk = ""
        for line in part.splitlines(keepends=True):
            if chunk and len((chunk + line).encode("utf-8")) > MAX_BODY_BYTES:
                bounded.append(chunk)
                chunk = line
            else:
                chunk += line
        if chunk:
            bounded.append(chunk)
    return bounded or [content]


class Memory:
    """记忆操作门面（一记忆 = 一 Issue，number 即记忆唯一标识）。"""

    def __init__(self, client: CnbApiClient) -> None:
        self.client = client

    # ---- 内部工具 ----

    def _url(self, number: int) -> str:
        return self.client.web_url(number)

    @staticmethod
    def _normalize_labels(tags: list[str] | None, category: str | None) -> _List[str]:
        """统一标签归一化：tags 补 tag/ 前缀，category 补 category/ 前缀（幂等）。

        已带任一命名空间前缀的值原样保留；空值过滤，避免产生空标签。
        """
        namespaced_prefixes = (CATEGORY_PREFIX, TAG_PREFIX)
        labels: _List[str] = []
        stripped_category = (category or "").strip()
        if stripped_category:
            labels.append(
                stripped_category
                if stripped_category.startswith(namespaced_prefixes)
                else f"{CATEGORY_PREFIX}{stripped_category}"
            )
        for tag in tags or []:
            stripped = tag.strip()
            if not stripped:
                continue
            labels.append(stripped if stripped.startswith(namespaced_prefixes) else f"{TAG_PREFIX}{stripped}")
        return labels

    async def _verify(self, number: int, *, field: str, expect: str) -> None:
        """写路径回读校验：GET 确认关键字段已落盘（架构红线）。

        CNB API 存在静默失败形态，短重试后仍不一致才报错。
        """
        actual: object = None
        for attempt in range(VERIFY_RETRIES):
            issue = await self.client.get_issue(number)
            actual = getattr(issue, field)
            if actual == expect:
                return
            if attempt < VERIFY_RETRIES - 1:
                await asyncio.sleep(VERIFY_INTERVAL_SECONDS)  # 异步等待，不阻塞事件循环
        raise MemoryError(
            f"写路径回读校验失败：issue #{number} 的 {field} 与期望不一致（期望 {expect!r}，实际 {actual!r}）"
        )

    # ---- 记忆操作 ----

    async def write(
        self,
        content: str,
        *,
        title: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        verify: bool = True,
    ) -> WriteResult:
        """写入一条记忆（两步写入 + 回读校验）。

        - title 由调用方撰写（提炼高区分度关键词短语），未提供时兜底为正文首行截取；
          工具保证不变量：cam: 前缀 + 长度上限（keyword 检索只匹配标题）
        - 创建后补打标签（两步写入红线）
        - 超长内容自动拆分为多条，title 带 (i/n) 后缀关联
        - verify=False 可跳过回读校验（仅测试用）
        - 拆分中途失败时抛出 MemoryError，携带已创建分片编号，便于循迹清理
        """
        if not content.strip():
            raise MemoryError("记忆内容不能为空")
        base_title = normalize_title(title, content)
        parts = _split_body(content)
        all_labels = self._normalize_labels(tags, category)
        created: _List[WriteResult] = []

        # 拆分时给 (i/n) 后缀预留预算，保证总长不超上限
        title_budget = MAX_TITLE_CHARS - len(" (999/999)")
        base_title = base_title[:title_budget] if len(parts) > 1 else base_title

        try:
            for index, part in enumerate(parts):
                suffix = f" ({index + 1}/{len(parts)})" if len(parts) > 1 else ""
                final_title = base_title + suffix
                # 两步写入：创建时不传 labels（新标签会被静默丢弃），创建后单独补打
                issue = await self.client.create_issue(CreateIssueForm(title=final_title, body=part))
                if all_labels:
                    await self.client.add_labels(issue.number, all_labels)
                if verify:
                    await self._verify(issue.number, field="title", expect=final_title)
                created.append(WriteResult(issue.number, final_title, self._url(issue.number)))
        except Exception as err:
            if created:  # 拆分场景下已有分片落盘，必须让调用方可循迹
                done = ", ".join(f"#{r.number}" for r in created)
                raise MemoryError(
                    f"写入拆分分片时失败（已完成 {len(created)}/{len(parts)}：{done}），"
                    f"失败分片未创建；已创建分片可按需软删除清理"
                ) from err
            raise
        return created[0]

    async def update(
        self,
        number: int,
        *,
        content: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
        category: str | None = None,
        verify: bool = True,
    ) -> Issue:
        """更新记忆正文/标题/标签（PATCH + 补打标签 + 回读校验）。

        - title 变更同样回读校验（与 write 路径标准一致）
        - tags/category 为追加语义：补打标签不影响已有标签（受 CNB 标签模型决定）
        - 各变更项独立生效，同次调用可同时更新正文/标题/标签
        """
        form = PatchIssueForm()
        if content is not None:
            if not content.strip():
                raise MemoryError("记忆内容不能为空")
            form.body = content
        if title is not None:
            form.title = normalize_title(title, content or title)

        if form.model_dump(exclude_none=True):
            await self.client.update_issue(number, form)

        labels = self._normalize_labels(tags, category)
        if labels:
            await self.client.add_labels(number, labels)

        if verify and form.body is not None:
            new_body = form.body
            await self._verify(number, field="body", expect=new_body)
        if verify and form.title is not None:
            new_title = form.title
            await self._verify(number, field="title", expect=new_title)

        if not form.model_dump(exclude_none=True) and not labels:
            raise MemoryError("update 未指定任何变更（content/title/tags/category 至少一项）")
        return await self.client.get_issue(number)

    async def append(self, number: int, note: str, *, verify: bool = True) -> Comment:
        """追加更新记录（评论进知识库，可被语义检索）。"""
        if not note.strip():
            raise MemoryError("追加内容不能为空")
        comment = await self.client.create_comment(number, CreateCommentForm(body=note))
        if verify:
            # 按创建倒序取最新一页，新评论必在首页（评论超 100 条时不误报）
            comments = await self.client.list_comments(number, sort="-created")
            if not any(c.id == comment.id for c in comments):
                raise MemoryError(f"写路径回读校验失败：issue #{number} 评论未落盘")
        return comment

    async def delete(self, number: int, *, verify: bool = True) -> Issue:
        """软删除记忆（state=closed + not_planned，可 reopen 恢复；无硬删除接口）。"""
        deleted = await self.client.update_issue(
            number, PatchIssueForm(state=STATE_CLOSED, state_reason=STATE_REASON_NOT_PLANNED)
        )
        if verify:
            issue = await self.client.get_issue(number)
            if issue.state != STATE_CLOSED:
                raise MemoryError(f"写路径回读校验失败：issue #{number} 未进入 closed 状态")
        return deleted

    async def restore(self, number: int) -> Issue:
        """恢复软删除的记忆（reopen）。"""
        return await self.client.update_issue(
            number, PatchIssueForm(state=STATE_OPEN, state_reason=STATE_REASON_REOPENED)
        )

    async def get(self, number: int) -> Issue:
        """精确读取记忆原文。"""
        return await self.client.get_issue(number)

    async def list(
        self,
        *,
        category: str | None = None,
        tags: list[str] | None = None,
        state: str = STATE_OPEN,
        limit: int = 20,
    ) -> _List[Issue]:
        """按分类/标签过滤记忆列表（结构化过滤与语义检索解耦）。"""
        labels = self._normalize_labels(tags, category)
        return await self.client.list_issues(
            state=state, labels=labels or None, order_by="-updated_at", page_size=limit
        )

    async def list_recent(self, limit: int = 5) -> _List[Issue]:
        """最近更新的记忆（一次请求）。"""
        return await self.client.list_issues(state=STATE_OPEN, order_by="-updated_at", page_size=limit)

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        include_closed: bool = False,
    ) -> _List[SearchResult]:
        """语义检索（主通道：知识库向量召回 → 解析 number → 回读原文补齐元信息）。

        - 知识库不可用（404）时抛出 MemoryError，由调用方决定是否降级为
          client.list_issues(keyword=...) 标题检索（降级通道）
        - include_closed=False 时过滤掉已软删除的记忆
        - 单个命中回读失败（如已删除）跳过该条，不中断整体检索
        """
        try:
            chunks: _List[KbChunk] = await self.client.query_knowledge_base(query, top_k=top_k)
        except ApiError as err:
            raise MemoryError(
                f"知识库检索失败（{err.status_code}）。"
                f"可降级为标题检索：client.list_issues(keyword=...)，"
                f"注意知识库需先配置 .cnb.yml 流水线才会建立"
            ) from err

        results: _List[SearchResult] = []
        seen_numbers: set[int] = set()
        for chunk in chunks:
            number = chunk.number
            if number is None or number in seen_numbers:
                continue
            seen_numbers.add(number)
            try:
                issue = await self.client.get_issue(number)
            except ApiError:
                continue  # 命中已不可读（被清理等），跳过不中断整体检索
            if not include_closed and issue.state != STATE_OPEN:
                continue
            results.append(
                SearchResult(
                    score=chunk.score,
                    chunk=chunk.chunk,
                    number=number,
                    title=issue.title,
                    state=issue.state,
                    url=self._url(number),
                )
            )
        return results
