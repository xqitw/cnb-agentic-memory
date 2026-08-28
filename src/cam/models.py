"""CNB API 数据模型（自建，只建 cam 消费的字段）。

建模原则：cam 是记忆系统，不是通用 Issue 客户端——字段以"是否参与记忆的
生命周期或检索语义"为准入，CNB 的展示层字段（优先级/颜色/计数等）一律不收。
策略：extra="ignore" 宽容未知字段——上游加字段不炸。

字段描述会进入 MCP 工具参数的 JSON Schema（P3）与 CLI 帮助文本（P2），
是对智能体/用户的接口文档，必须随字段维护。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _CamModel(BaseModel):
    """模型基类：宽容未知字段。"""

    model_config = ConfigDict(extra="ignore")


class Label(_CamModel):
    """Issue 标签（分类与标签命名空间约定的载体）。"""

    name: str = Field(default="", description="标签名，如 category/db、tag/x")


class Issue(_CamModel):
    """记忆（一记忆 = 一 Issue，number 是记忆唯一标识）。"""

    number: int = Field(description="记忆唯一标识（仓库内自增 Issue 编号）")
    title: str = Field(default="", description="标题，由工具生成的关键词摘要（cam: 前缀）")
    body: str = Field(default="", description="记忆正文（Markdown）")
    state: str = Field(default="open", description="生命周期状态：open=有效，closed=已软删除")
    labels: list[Label] = Field(default_factory=list, description="标签列表（分类与命名空间约定）")
    created_at: str | None = Field(default=None, description="写入时间（ISO 8601）")
    updated_at: str | None = Field(default=None, description="最近更新时间（ISO 8601）")

    @property
    def label_names(self) -> list[str]:
        """标签名列表（如 ["category/xxx", "tag/yyy"]）。"""
        return [lb.name for lb in self.labels]


class Comment(_CamModel):
    """追加更新记录（评论，进知识库可被语义检索）。"""

    id: str = Field(default="", description="记录 ID（服务端生成）")
    body: str = Field(default="", description="追加的更新记录内容")
    created_at: str | None = Field(default=None, description="追加时间（ISO 8601）")


class CreateIssueForm(_CamModel):
    """创建 Issue 请求体（POST /{-}/issues）。

    刻意不设 labels 字段：创建接口对不存在的标签静默丢弃（实测确认），
    标签一律通过 add_labels 两步写入，在类型层杜绝误用。
    """

    title: str = Field(description="标题，工具生成的关键词摘要（keyword 检索只匹配标题）")
    body: str = Field(default="", description="记忆正文（Markdown，建议单条 ≤30KB，超长自动拆分）")


class PatchIssueForm(_CamModel):
    """更新 Issue 请求体（PATCH /{-}/issues/{number}），None 字段不发送。

    state_reason 是生命周期内部契约：软删除/恢复由 memory 层自动设置，
    不作为调用方接口暴露。
    """

    title: str | None = Field(default=None, description="新标题（工具生成，一般不手动指定）")
    body: str | None = Field(default=None, description="新正文（全量替换，非增量追加）")
    state: str | None = Field(default=None, description="目标状态：open/closed")
    state_reason: str | None = Field(
        default=None,
        description="状态流转原因（内部契约）：软删除 not_planned、恢复 reopened，由 memory 层自动设置",
    )


class CreateCommentForm(_CamModel):
    """创建追加记录请求体（POST /{-}/issues/{number}/comments）。"""

    body: str = Field(description="追加的更新记录内容（进知识库，可被语义检索）")


class KbChunk(_CamModel):
    """知识库检索单条结果（GET /{-}/knowledge/base/query）。"""

    score: float = Field(default=0.0, description="语义相关度得分（0~1，实测主通道 0.98+）")
    chunk: str = Field(default="", description="命中切片原文（记忆标题/正文/追加记录片段）")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="切片元数据：type/path/url 等，path 形如 /{group}/{repo}/-/issues/{number}",
    )

    @property
    def number(self) -> int | None:
        """从 metadata.path（形如 /{group}/{repo}/-/issues/{number}）解析记忆编号。"""
        path = str(self.metadata.get("path", ""))
        marker = "/-/issues/"
        if marker in path:
            tail = path.rsplit(marker, 1)[1]
            if tail.isdigit():
                return int(tail)
        return None

    @property
    def url(self) -> str | None:
        """记忆 Issue 的 Web 地址（缺失时为 None）。"""
        url = self.metadata.get("url")
        return str(url) if url else None


class KnowledgeBase(_CamModel):
    """知识库状态（用于 memory_search 的降级判定）。"""

    id: str | None = Field(default=None, description="知识库 ID")
    issue_sync_enabled: bool = Field(
        default=False,
        description="是否启用记忆自动同步（未启用时检索不可用，需配置 .cnb.yml 流水线）",
    )
