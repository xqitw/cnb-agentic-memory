"""记忆语义层：在 API 薄封装之上实现记忆业务规则。

沉淀的架构红线（均来自实测确认）：
- 两步写入：创建 Issue 后必须单独补打标签（创建接口对新标签静默丢弃）
- 写路径回读校验：所有写操作完成后 GET 回读确认（CNB API 存在静默失败形态）
- title 由调用方（智能体）撰写，工具只保证不变量：长度上限、非空兜底
- 软删除：PATCH state=closed + state_reason=not_planned，可 reopen 恢复
- 超长拆分：单条记忆超过 MAX_BODY_BYTES 自动拆为多条，用 title 关联；
  拆分强无损（"".join(parts) == content 恒成立），分隔符不丢不粘
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from .api import ApiError, CNBApiClient
from .models import Comment, CreateCommentForm, CreateIssueForm, Issue, KbChunk, PatchIssueForm

if TYPE_CHECKING:
    from builtins import list as _List  # noqa: UP035  # 避免与 Memory.list 方法名冲突

# 软删除 / 生命周期状态约定
STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_REASON_NOT_PLANNED = "not_planned"
STATE_REASON_REOPENED = "reopened"

# 分类标签前缀（对齐 CNB 平台约定：分类形如 category:xxx，选择器中单选）
CATEGORY_PREFIX = "category:"

# 单条记忆正文字节上限（35KB 实测下界留出余量，超长自动拆分）
MAX_BODY_BYTES = 30_000
# title 长度上限（keyword 只搜标题，title 是标题检索的唯一入口，超长无意义）
MAX_TITLE_CHARS = 60
# 回读校验重试次数与间隔（CNB API 存在静默失败形态，写后必须 GET 确认）
VERIFY_RETRIES = 3
VERIFY_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class WriteResult:
    """memory_write 的返回：主分片定位 + 全部分片信息。

    超长拆分时 parts 含全部分片（number/title/url），供调用方循迹；
    单分片时 parts 为空元组（主字段即唯一分片）。
    """

    number: int
    title: str
    url: str
    parts: tuple[WriteResult, ...] = ()


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
    """规范化 title，保证两条不变量：非空 + 长度上限。

    title 撰写权在调用方（智能体）：应提炼高区分度关键词短语，
    keyword 检索只匹配标题。未提供时兜底为正文首行截取——
    不做语义提炼，只保证与内容相关且长度有界。
    """
    raw = title.strip() if title and title.strip() else _first_line(content)
    body = raw[:MAX_TITLE_CHARS].strip()
    return body or "untitled memory"


def _first_line(content: str) -> str:
    """取正文首个非空行作为兜底 title 素材。"""
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()  # 去除 Markdown 标题符
        if stripped:
            return stripped
    return "untitled memory"


def _byte_len(text: str) -> int:
    """UTF-8 字节长度。"""
    return len(text.encode("utf-8"))


def _accumulate(units: list[str], limit: int = MAX_BODY_BYTES) -> list[str]:
    """把单元（段落/行）聚合为不超过 limit 字节的片段。

    分隔符随单元保留（单元自带换行），聚合为纯拼接，保证拼接无损。
    纯空白 unit 无条件并入当前 buf（不触发切分）——它是前后内容的
    分隔符，单独成片再丢弃会把两段内容粘成一个 token（有损合并）。
    字节计数器增量判断，避免对递增 buffer 反复全量编码（线性时间）。
    """
    parts: list[str] = []
    buf = ""
    buf_bytes = 0
    for unit in units:
        unit_bytes = len(unit.encode("utf-8"))
        if buf and buf_bytes + unit_bytes > limit and unit.strip():
            # 仅当 unit 含内容时才允许切分；纯空白 unit 恒并入当前 buf
            parts.append(buf)
            buf = unit
            buf_bytes = unit_bytes
        else:
            buf += unit
            buf_bytes += unit_bytes
    if buf:
        parts.append(buf)
    return parts


def _hard_cut(part: str) -> list[str]:
    """无分隔符的连续串按 UTF-8 字符边界硬切（码点切片不切半个字符）。

    增量字节计数器单趟扫描：内存 O(输出)，避免前缀表的 O(n) int 列表放大。
    """
    pieces: list[str] = []
    buf = ""
    buf_bytes = 0
    for ch in part:
        ch_bytes = len(ch.encode("utf-8"))
        if buf and buf_bytes + ch_bytes > MAX_BODY_BYTES:
            pieces.append(buf)
            buf = ch
            buf_bytes = ch_bytes
        else:
            buf += ch
            buf_bytes += ch_bytes
    if buf:
        pieces.append(buf)
    return pieces


def _split_body(content: str) -> list[str]:
    """超长正文拆分为不超过 MAX_BODY_BYTES 的多条。

    三级拆分点：空行段落 → 单行 → 硬切（UTF-8 字符边界，覆盖长 URL、
    base64、minified JSON 等不含换行的连续串）。
    强无损红线："".join(parts) == content 恒成立（分隔符随单元保留、
    纯空白 unit 不切分，任何分片都不会被丢弃）。
    """
    if _byte_len(content) <= MAX_BODY_BYTES:
        return [content]

    # 第一轮：按空行（Markdown 段落）聚合，空行分隔符归属后段
    paragraphs: list[str] = []
    for index, para in enumerate(content.split("\n\n")):
        paragraphs.append(para if index == 0 else f"\n\n{para}")
    parts = _accumulate(paragraphs)

    # 第二轮：仍超限的片段按行拆（覆盖无空行的超长多行文本/代码块）。
    # 行自带行尾换行（keepends），同为纯拼接
    bounded: list[str] = []
    for part in parts:
        if _byte_len(part) <= MAX_BODY_BYTES:
            bounded.append(part)
            continue
        lines = part.splitlines(keepends=True)
        bounded.extend(_accumulate(lines))

    # 第三轮：仍超限的片段为不含换行的连续串，硬切
    final: list[str] = []
    for part in bounded:
        if _byte_len(part) <= MAX_BODY_BYTES:
            final.append(part)
            continue
        final.extend(_hard_cut(part))
    return final


class Memory:
    """记忆操作门面（一记忆 = 一 Issue，number 即记忆唯一标识）。"""

    def __init__(self, client: CNBApiClient) -> None:
        self.client = client

    # ---- 内部工具 ----

    def _url(self, number: int) -> str:
        return self.client.web_url(number)

    @staticmethod
    def _normalize_labels(tags: list[str] | None, category: str | None) -> list[str]:
        """统一标签归一化：category 自动补 category: 前缀（幂等），
        tags 为普通标签原样保留。

        空值过滤，避免产生空标签；重复值去重，避免重复打标。
        """
        labels: list[str] = []
        stripped_category = (category or "").strip()
        if stripped_category:
            labels.append(
                stripped_category
                if stripped_category.startswith(CATEGORY_PREFIX)
                else f"{CATEGORY_PREFIX}{stripped_category}"
            )
        for tag in tags or []:
            stripped = tag.strip()
            if stripped and stripped not in labels:
                labels.append(stripped)
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
          工具保证不变量：非空 + 长度上限（keyword 检索只匹配标题）
        - 创建后补打标签（两步写入红线）
        - 超长内容自动拆分为多条，title 带 (i/n) 后缀关联；拆分无损
        - verify=False 可跳过回读校验（仅测试用）
        - 任何一步失败时抛出 MemoryError，携带已落盘分片编号与原始错误摘要，
          便于循迹清理（Issue 创建成功即记为已落盘，与后续步骤失败无关）
        """
        if not content.strip():
            raise MemoryError("记忆内容不能为空")
        base_title = normalize_title(title, content)
        parts = _split_body(content)
        all_labels = self._normalize_labels(tags, category)
        created: _List[WriteResult] = []

        # 拆分时按实际分片数算 (i/n) 后缀宽度并预留，保证总长不超上限
        if len(parts) > 1:
            suffix_width = len(f" ({len(parts)}/{len(parts)})")
            base_title = base_title[: MAX_TITLE_CHARS - suffix_width]

        try:
            for index, part in enumerate(parts):
                suffix = f" ({index + 1}/{len(parts)})" if len(parts) > 1 else ""
                final_title = base_title + suffix
                # 两步写入：创建时不传 labels（新标签会被静默丢弃），创建后单独补打。
                # create_issue 成功即视为该分片已落盘，后续任何失败都可循迹
                issue = await self.client.create_issue(CreateIssueForm(title=final_title, body=part))
                created.append(WriteResult(issue.number, final_title, self._url(issue.number)))
                if all_labels:
                    await self.client.add_labels(issue.number, all_labels)
                if verify:
                    await self._verify(issue.number, field="title", expect=final_title)
        except Exception as err:
            if created:  # 已有分片落盘（含落盘但无标签），必须让调用方可循迹
                done = ", ".join(f"#{r.number}" for r in created)
                reason = f"{type(err).__name__}: {err}"
                raise MemoryError(
                    f"写入失败（已完成 {len(created)}/{len(parts)}：{done}），"
                    f"已落盘分片可按需软删除清理；原始错误：{reason}"
                ) from err
            raise
        # 多分片时 parts 携带全部分片供循迹；单分片时主字段即唯一分片
        return (
            created[0]
            if len(created) == 1
            else WriteResult(created[0].number, created[0].title, created[0].url, tuple(created))
        )

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
        if content is not None and not content.strip():
            raise MemoryError("记忆内容不能为空")
        form = PatchIssueForm()
        if content is not None:
            form.body = content
        if title is not None:
            form.title = normalize_title(title, content or title)

        has_form_changes = bool(form.model_dump(exclude_none=True))
        labels = self._normalize_labels(tags, category)
        if not has_form_changes and not labels:
            raise MemoryError("update 未指定任何变更（content/title/tags/category 至少一项）")

        if has_form_changes:
            await self.client.update_issue(number, form)
        if labels:
            await self.client.add_labels(number, labels)

        if verify and form.body is not None:
            new_body = form.body
            await self._verify(number, field="body", expect=new_body)
        if verify and form.title is not None:
            new_title = form.title
            await self._verify(number, field="title", expect=new_title)

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

    async def keyword_search(
        self, query: str, *, limit: int = 20, include_closed: bool = False
    ) -> _List[Issue]:
        """关键词标题检索（与语义检索并列的第二检索方法，按需选择）。

        - 仅匹配标题（CNB keyword 检索特性），无法检索正文——title 是
          关键词摘要正因此关键
        - 记忆仓库为专用仓库（全部 Issue 均为记忆），无需标签过滤
        - CNB state 不支持 all，分 open/closed 各查一次后合并（按 number 去重，
          按 updated_at 降序，与列表/语义检索的时序语义一致）
        - include_closed=False（默认）仅返回 open 记忆
        """
        if not query.strip():
            raise MemoryError("检索词不能为空")
        seen: dict[int, Issue] = {}
        states = (STATE_OPEN,) if not include_closed else (STATE_OPEN, STATE_CLOSED)
        for state in states:
            issues = await self.client.list_issues(
                state=state,
                keyword=query,
                order_by="-updated_at",
                page_size=max(limit, 1),
            )
            for issue in issues:
                seen.setdefault(issue.number, issue)
        return sorted(seen.values(), key=lambda i: i.updated_at or "", reverse=True)[:limit]

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        include_closed: bool = False,
    ) -> _List[SearchResult]:
        """语义检索（知识库向量召回 → 解析 number → 回读原文补齐元信息）。

        - 知识库不可用（404/网络异常）时抛出 MemoryError，错误信息提示
          可改用 keyword_search（关键词标题检索，与语义检索并列的第二方法）
        - include_closed=False 时过滤掉已软删除的记忆
        - 单个命中回读失败（如已删除/网络异常）跳过该条，不中断整体检索
        """
        try:
            chunks: _List[KbChunk] = await self.client.query_knowledge_base(query, top_k=top_k)
        except (ApiError, httpx.HTTPError) as err:
            reason = f"{type(err).__name__}: {err}"
            raise MemoryError(
                f"知识库检索失败（{reason}）。"
                f"可改用 keyword_search 按标题关键词检索，"
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
            except (ApiError, httpx.HTTPError):
                continue  # 命中不可读（被清理/网络异常等），跳过不中断整体检索
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
