"""cnb-agentic-memory：基于 CNB 平台的通用智能体记忆工具。

以 CNB Issue 为存储、知识库为语义检索，SDK / CLI / MCP / Skill 多形态对外服务。
一记忆 = 一 Issue，number 是记忆唯一标识。
"""

from .api import ApiError, CNBApiClient, ConfigError
from .memory import Memory, MemoryRuleError, SearchResult, WriteResult, normalize_title
from .models import Comment, Issue, KbChunk, Label

__version__ = "2.0.1"

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
