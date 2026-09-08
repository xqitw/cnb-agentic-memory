"""cnb-agentic-memory（CNB Issue 智能体记忆系统）：基于 CNB 平台的通用智能体记忆工具。

以 CNB Issue 为存储、知识库为语义检索，SDK / CLI / MCP / Skill 多形态对外服务。
一记忆 = 一 Issue，number 是记忆唯一标识。
"""

from importlib.metadata import PackageNotFoundError, version

from .api import ApiError, CNBApiClient, ConfigError
from .memory import Memory, MemoryRuleError, SearchResult, WriteResult, normalize_title
from .models import Comment, Issue, KbChunk, Label

try:
    # pyproject [project].version 为单一来源（手工硬编码会在发版时漂移，实测翻车）
    __version__ = version("cnb-agentic-memory")
except PackageNotFoundError:  # 源码直用未安装时兜底，保持可导入
    __version__ = "0+unknown"

__all__ = [
    "__version__",
    "ApiError",
    "CNBApiClient",
    "ConfigError",
    "Comment",
    "Issue",
    "KbChunk",
    "Label",
    "Memory",
    "MemoryRuleError",
    "normalize_title",
    "SearchResult",
    "WriteResult",
]
