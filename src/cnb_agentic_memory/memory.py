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
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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

# 分页大小边界（CNB 服务端分页上限 100/页，超出被服务端拒绝）
MAX_PAGE_SIZE = 100

# 标签字符白名单与长度限制（实测 CNB 400 errcode 2000063 报错原文沉淀：
# "只允许汉字、字母、数字或者小数点(.)、下划线(_)、冒号(:)、中划线(-)、
# 正斜杠(/)、反斜杠(\)、全角符号以及中间空格（首尾不能为空格），
# 长度必须在1到50个字符之间"；实测确认按 UTF-8 字节计数，cam-test
# #78/#79：50 ASCII 通过、30 汉字（60 字节）被拒）。写前预检用，
# 不合法直接报错，避免 Issue 已落盘后标签步骤才失败产生孤儿分片
LABEL_MAX_BYTES = 50
# 白名单显式字符类（不用 \\w：其 Unicode 语义会放行拉丁扩展字母等
# 未实测字符，预检口径必须与服务端实测严格一致）：
# 汉字 U+4E00-9FFF、CJK 标点 U+3000-303F、全角字符 U+FF00-FFEF、
# ASCII 字母数字与实测允许的符号 . : - / \\ 及空格（下划线显式列出，
# 对齐报错原文"下划线(_)"）
# 实测补充（cam-test #80/#81）：CNB 报错原文的"全角符号"不含省略号
# …(U+2026) 与破折号 —(U+2014)，服务端拒绝，故不入白名单
_LABEL_ALLOWED = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefA-Za-z0-9_.:\-/\\ ]+")


def validate_label(label: str) -> str | None:
    """校验单个标签是否满足 CNB 字符白名单，不合法时返回原因，合法返回 None。

    白名单来自 CNB 400 报错原文（见模块常量注释），写前预检使用：
    在发起任何写请求之前拦下不合法标签，从源头避免孤儿分片。
    """
    if not label or not label.strip():
        return "标签不能为空"
    # 实测确认按 UTF-8 字节计数（30 汉字=30 字符=60 字节被服务端拒绝，
    # 50 ASCII=50 字节通过，cam-test #78/#79）
    label_bytes = len(label.encode("utf-8"))
    if label_bytes > LABEL_MAX_BYTES:
        return f"标签长度 {label_bytes} 字节超过上限 {LABEL_MAX_BYTES} 字节（UTF-8 计数，1 汉字/全角字符占 3 字节）"
    if label != label.strip():
        return "标签首尾不能包含空格"
    if not _LABEL_ALLOWED.fullmatch(label):
        return "标签含不允许的字符（只允许汉字、字母、数字、_.: - / \\ 、全角符号及中间空格）"
    return None


def clamp_page_size(value: int) -> int:
    """统一钳制分页参数：下限 1，上限服务端分页上限。

    SDK 与 CLI / MCP 入口层的钳制口径一致（入口层钳制保留，双层防护不冲突）。
    """
    return max(1, min(value, MAX_PAGE_SIZE))


def _updated_at_sort_key(issue: Issue) -> tuple[int, str]:
    """keyword_search 合并去重后的时序排序键（#51）。

    CNB 当前统一返回 UTC Z 后缀，但时区表示不能依赖（混入 +08:00 等
    偏移时字符串比较会错序）：优先解析为 datetime（aware，跨时区可比）。
    naive 时间戳（无时区后缀）显式按 UTC 解释——CNB 事实标准，且不受
    部署机 TZ 影响；仅畸形格式回退 (0, 原字符串) 保持兼容且不抛错。
    """
    raw = issue.updated_at or ""
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return (0, raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (1, f"{dt.timestamp():020.6f}")
    return (0, raw)


@dataclass(frozen=True)
class WriteResult:
    """memory_write 的返回：主分片定位 + 全部分片信息。

    超长拆分时 parts 含全部分片（number/title），供调用方循迹；
    单分片时 parts 为空元组（主字段即唯一分片）。
    """

    number: int
    title: str
    parts: tuple[WriteResult, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    """memory_search 的返回：语义召回 + 补齐元信息。"""

    score: float
    chunk: str
    number: int
    title: str
    state: str


class MemoryError(Exception):
    """记忆语义层错误（在 ApiError 之上，表达业务规则失败）。"""


# 控制字符（C0 除 	 外与 DEL）：实测 CNB 不拒绝，落盘会污染 keyword 分词、
# 展示与知识库向量，工具层必须清洗（服务端不拦 = 工具必须拦）
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_title(title: str | None, content: str) -> str:
    """规范化 title，保证三条不变量：无控制字符 + 非空 + 长度上限。

    title 撰写权在调用方（智能体）：应提炼高区分度关键词短语，
    keyword 检索只匹配标题。未提供时兜底为正文首行截取——
    不做语义提炼，只保证与内容相关且长度有界。
    """
    raw = title.strip() if title and title.strip() else _first_line(content)
    raw = _CONTROL_CHARS.sub("", raw).strip()
    # 清洗后为空（title 全为控制字符）→ 回退正文首行再清洗
    if not raw:
        raw = _CONTROL_CHARS.sub("", _first_line(content))
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

    @staticmethod
    def _normalize_labels(tags: list[str] | None, category: str | None) -> list[str]:
        """统一标签归一化：category 自动补 category: 前缀（幂等），
        tags 为普通标签原样保留。

        - 逗号拆分：单元素内含半角/全角逗号时拆为多个标签（智能体按
          自然直觉传 "a,b" 是高频行为，实测会整条写入失败）
        - 空值过滤，避免产生空标签；重复值去重，避免重复打标
        - 首尾空白 strip（CNB 标签首尾不允许空格）
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
            # 逗号拆分：半角/全角逗号均视为分隔符（空格不拆，保留多词标签）
            for piece in re.split(r"[,，]", tag):
                stripped = piece.strip()
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

    @staticmethod
    def _precheck_labels(labels: list[str]) -> None:
        """写前预检：任何标签不合法直接抛 MemoryError，不发任何 API 请求。

        实测 CNB 对标签有字符白名单与长度限制（400 errcode 2000063），
        若在 create_issue 落盘后才因标签被拒，会产生孤儿分片。
        fail-fast 把这类失败拦在发生之前。
        """
        reasons = {label: validate_label(label) for label in labels}
        bad = {label: reason for label, reason in reasons.items() if reason}
        if bad:
            detail = "；".join(f"{label!r}：{reason}" for label, reason in bad.items())
            raise MemoryError(f"标签不合法（未发起任何写请求）：{detail}")

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
        self._precheck_labels(all_labels)
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
                created.append(WriteResult(issue.number, final_title))
                if all_labels:
                    await self.client.add_labels(issue.number, all_labels)
                if verify:
                    await self._verify(issue.number, field="title", expect=final_title)
        except Exception as err:
            if created:  # 已有分片落盘（含落盘但无标签），必须让调用方可循迹
                health = await self._health_check_shards(created, parts, all_labels)
                reason = f"{type(err).__name__}: {err}"
                raise MemoryError(
                    f"写入失败（已完成 {len(created)}/{len(parts)}）。"
                    f"已落盘分片体检：\n{health}"
                    "恢复优先级：update 补齐/修正 > append 续写 > delete 废弃（最后手段，"
                    "软删除内容仍留在知识库向量中）；切勿重复 write（会重复创建）。"
                    f"原始错误：{reason}"
                ) from err
            raise
        # 多分片时 parts 携带全部分片供循迹；单分片时主字段即唯一分片
        return (
            created[0]
            if len(created) == 1
            else WriteResult(created[0].number, created[0].title, tuple(created))
        )

    async def _health_check_shards(
        self,
        created: _List[WriteResult],
        parts: _List[str],
        expect_labels: _List[str] | None = None,
    ) -> str:
        """失败时对已落盘分片逐个体检：回读确认正文完整性与标签状态。

        把"编号已知但状态不明"的孤儿分片变成可直接执行的行动建议；
        单片回读失败不中断整体体检（该片标注"状态未知，请 get 确认"）。
        created 与 parts 按创建顺序一一对应（顺序创建，任一片失败即中断，
        created 恒为 parts 前缀），索引比对无错位。
        """
        lines: list[str] = []
        expect_set = set(expect_labels or [])
        for index, result in enumerate(created):
            try:
                issue = await self.client.get_issue(result.number)
                # get_issue 走单条详情接口（实测回显完整正文，与 list 接口
                # 不回显正文的结论区分，见 docs/ 实测记录），比对可靠
                body_ok = issue.body == parts[index]
                # 集合比对区分"全部已打"与"部分缺失"（add_labels 批量
                # 部分失败场景）；expect_labels 为空时仅判断是否打标
                missing = expect_set - set(issue.label_names) if expect_set else set()
                labels_ok = not missing if expect_set else bool(issue.label_names)
                state_desc = "正文完整" if body_ok else "正文与期望不一致"
                if expect_set and missing:
                    state_desc += f"、标签缺失（缺 {sorted(missing)}）"
                else:
                    state_desc += "、标签已打" if labels_ok else "、标签缺失"
                lines.append(
                    f"  #{result.number} ({index + 1}/{len(parts)}) {state_desc}"
                    f" → 建议：update {result.number} 修正/补齐"
                )
            except Exception as check_err:  # noqa: BLE001  # 体检失败不中断其余分片
                lines.append(
                    f"  #{result.number} ({index + 1}/{len(parts)}) "
                    f"状态未知（回读失败：{type(check_err).__name__}）"
                    f" → 建议：get {result.number} 人工确认"
                )
        return "\n".join(lines)

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
        if content is not None and _byte_len(content) > MAX_BODY_BYTES:
            raise MemoryError(
                f"update 正文 {_byte_len(content)} 字节超过单条上限 {MAX_BODY_BYTES}"
                "（update 是全量替换，不支持自动拆分）。"
                "建议：压缩正文，或 get 原文后按主题拆分，"
                "增量信息用 append 追加（进知识库可被检索）"
            )
        form = PatchIssueForm()
        if content is not None:
            form.body = content
        if title is not None:
            form.title = normalize_title(title, content or title)

        has_form_changes = bool(form.model_dump(exclude_none=True))
        labels = self._normalize_labels(tags, category)
        if not has_form_changes and not labels:
            raise MemoryError("update 未指定任何变更（content/title/tags/category 至少一项）")
        self._precheck_labels(labels)

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
            state=state,
            labels=labels or None,
            order_by="-updated_at",
            page_size=clamp_page_size(limit),
        )

    async def list_recent(self, limit: int = 5) -> _List[Issue]:
        """最近更新的记忆（一次请求）。"""
        return await self.client.list_issues(
            state=STATE_OPEN, order_by="-updated_at", page_size=clamp_page_size(limit)
        )

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
                page_size=clamp_page_size(limit),
            )
            for issue in issues:
                seen.setdefault(issue.number, issue)
        return sorted(seen.values(), key=_updated_at_sort_key, reverse=True)[:limit]

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
            chunks: _List[KbChunk] = await self.client.query_knowledge_base(
                query, top_k=clamp_page_size(top_k)
            )
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
                )
            )
        return results
