"""cnb-agentic-memory（cam）：基于 CNB 平台的通用智能体记忆工具。

以 CNB Issue 为存储、知识库为语义检索，SDK / CLI / MCP / Skill 多形态对外服务。
一记忆 = 一 Issue，number 是记忆唯一标识。
"""

from .api import ApiError, CnbApiClient
from .config import Config
from .memory import Memory, MemoryError, SearchResult, WriteResult, normalize_title
from .models import Comment, Issue, KbChunk, Label

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "ApiError",
    "CnbApiClient",
    "Comment",
    "Config",
    "Issue",
    "KbChunk",
    "Label",
    "Memory",
    "MemoryError",
    "normalize_title",
    "SearchResult",
    "WriteResult",
]
