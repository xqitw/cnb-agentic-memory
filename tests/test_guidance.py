"""指导内容一致性锚点测试（AGENTS.md 防漂移红线的自动化部分）。

断言关键语义锚定短语存在于 MCP instructions 与关键工具描述中：
改动描述删除关键语义时本测试失败，防止多通道指导漂移。
事实唯一权威在 docs/ 与各通道文本，本测试只守护"必须出现"的下限。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from cnb_agentic_memory import mcp_server

# instructions 必须包含的原则锚点（恢复阶梯 / 向量库残留 / 知识库时延 / body 语义）
INSTRUCTIONS_ANCHORS = [
    "memory_update",
    "软删除",
    "知识库向量",
    "1~2 分钟",
    "memory_get",
]

# 工具名 → 描述必须包含的语义锚点
TOOL_DESCRIPTION_ANCHORS: dict[str, list[str]] = {
    "memory_delete": ["知识库向量", "memory_update"],
    "memory_update": ["勿删除重建"],
    "memory_write": ["逗号", "重复 write"],
    "memory_list": ["不回显正文", "memory_get"],
    "memory_keyword_search": ["不回显正文", "memory_get"],
}


def _tool_descriptions() -> dict[str, str]:
    """从 MCPServer 提取已注册工具的描述映射（name -> description）。"""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {tool.name: tool.description or "" for tool in tools}


def test_instructions_contains_principle_anchors() -> None:
    """server 级 instructions 必须携带核心原则锚点。"""
    instructions = mcp_server.mcp.instructions or ""
    for anchor in INSTRUCTIONS_ANCHORS:
        assert anchor in instructions, f"instructions 缺少原则锚点：{anchor}"


def test_tool_descriptions_contains_semantic_anchors() -> None:
    """关键工具描述必须包含语义锚点（防改动时删除关键语义）。"""
    descriptions = _tool_descriptions()
    for name, anchors in TOOL_DESCRIPTION_ANCHORS.items():
        assert name in descriptions, f"工具 {name} 未注册"
        for anchor in anchors:
            assert anchor in descriptions[name], f"{name} 描述缺少锚点：{anchor}"


def test_skill_md_contains_core_semantics() -> None:
    """SKILL.md（skill 通道）必须包含核心语义锚点（AGENTS.md 同步点 1）。"""
    skill = Path("skills/cnb-agentic-memory/SKILL.md").read_text(encoding="utf-8")
    for anchor in ["知识库向量", "update", "不回显正文", "逗号"]:
        assert anchor in skill, f"SKILL.md 缺少锚点：{anchor}"


def test_memory_error_contains_recovery_ladder() -> None:
    """语义层源码必须包含恢复阶梯锚点（AGENTS.md 同步点 4，静态断言）。"""
    memory_src = Path("src/cnb_agentic_memory/memory.py").read_text(encoding="utf-8")
    for anchor in ["恢复优先级：update 补齐/修正 > append 续写 > delete 废弃", "知识库向量"]:
        assert anchor in memory_src, f"memory.py 报错文案缺少锚点：{anchor}"


def test_cli_help_contains_new_semantics() -> None:
    """CLI help 文本（AGENTS.md 同步点 3）必须包含关键语义。"""
    cli_src = Path("src/cnb_agentic_memory/cli.py").read_text(encoding="utf-8")
    for anchor in ["拆分为多标签", "勿删除重建", "不回显正文"]:
        assert anchor in cli_src, f"CLI help 缺少锚点：{anchor}"


def test_normalize_title_strips_control_chars() -> None:
    """normalize_title 必须剔除控制字符（服务端不拦，工具必须拦）。"""
    from cnb_agentic_memory.memory import normalize_title

    dirty = "标题\x01含控制\x07符"
    cleaned = normalize_title(dirty, "正文")
    assert "\x01" not in cleaned and "\x07" not in cleaned
    assert "含控制" in cleaned and "符" in cleaned
    # 清洗后为空 → 回退兜底
    assert normalize_title("\x01\x07", "正文首行") == "正文首行"
    # 合法字符不受影响
    assert normalize_title("emoji😀与全角！", "x") == "emoji😀与全角！"


def test_delete_description_not_misleading() -> None:
    """memory_delete 不得再声称不再出现（实测软删除内容仍留知识库向量）。"""
    descriptions = _tool_descriptions()
    assert not re.search("不再出现", descriptions["memory_delete"]), (
        'memory_delete 描述含与实测矛盾的表述"不再出现"'
    )
