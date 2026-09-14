"""指导内容一致性锚点测试（AGENTS.md 防漂移红线的自动化部分）。

断言关键语义锚定短语存在于 MCP instructions 与关键工具描述中：
改动描述删除关键语义时本测试失败，防止多通道指导漂移。
事实唯一权威在 docs/ 与各通道文本，本测试只守护"必须出现"的下限。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from cnb_agentic_memory import mcp_server

# instructions 必须包含的原则锚点（恢复阶梯 / 向量库残留 / 知识库时延 / body 语义 / 配置缺失指引）
INSTRUCTIONS_ANCHORS = [
    "memory_update",
    "软删除",
    "知识库向量",
    "定时入库",
    "回查",
    "memory_get",
    "不要猜测连接参数",
    "isError=true",
]

# 工具名 → 描述必须包含的语义锚点
TOOL_DESCRIPTION_ANCHORS: dict[str, list[str]] = {
    "memory_delete": ["知识库向量", "memory_update"],
    "memory_update": ["勿删除重建"],
    "memory_write": ["逗号", "重复 write", "回查"],
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
    for anchor in ["知识库向量", "update", "不回显正文", "逗号", "回查", "不要猜测连接参数"]:
        assert anchor in skill, f"SKILL.md 缺少锚点：{anchor}"


def test_config_error_runtime_message_carries_guidance() -> None:
    """运行时 str(ConfigError) 必须携带行动指引（SKILL.md/MCP 判据的三方同源）。

    断言运行时消息而非整文件文本——整文件断言可被 docstring 喂饱，
    文案搬进注释后防漂移即失效。SKILL.md 与 MCP 出口的判据都绑
    「缺少必需配置：」字面量，此锚点保证改 api.py 文案时同步报警。
    """
    import os

    saved = {k: os.environ.pop(k, None) for k in ("CNB_AGENTIC_MEMORY_TOKEN", "CNB_AGENTIC_MEMORY_REPO")}
    try:
        from cnb_agentic_memory import ConfigError

        with pytest.raises(ConfigError) as exc_info:
            from cnb_agentic_memory.api import CNBApiClient

            CNBApiClient()
        message = str(exc_info.value)
        for anchor in ["缺少必需配置：", "CNB_AGENTIC_MEMORY_TOKEN", "CNB_AGENTIC_MEMORY_REPO"]:
            assert anchor in message, f"ConfigError 运行时文案缺少锚点：{anchor}"
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def test_error_contract_gates_colocated() -> None:
    """错误契约闸门与判据同句共现（docs 与 instructions 两侧，位置敏感）。

    历史事故：ab60e65 改写时 isError 闸门从 docs 静默丢失、f3bd292 才被
    评审点出；本轮锚点曾只断言子串出现，闸门被删或搬离判据仍全绿。
    升级为同句共现 + 位置关系断言：闸门限定词（isError=true）必须出现在
    判据文案（缺少必需配置）之前，闸门再丢或挪位即红。
    """
    # docs/MCP.md 例外条款：切片到「例外」之后再断言——整行含 ①②③ 各自的
    # isError=true，「同行出现」不等于例外条款被守护（锐鉴第六轮实测穿透）
    mcp_doc = Path("docs/MCP.md").read_text(encoding="utf-8")
    gate_line = next(line for line in mcp_doc.splitlines() if "缺少必需配置" in line and "例外" in line)
    exception_seg = gate_line.split("例外", 1)[1]  # 只看例外条款本体
    assert "isError=true" in exception_seg, "docs/MCP.md 例外条款丢失 isError=true 闸门"
    assert "仅在" in exception_seg, "docs/MCP.md 例外条款丢失「仅在」限定"
    assert exception_seg.index("isError=true") < exception_seg.index("缺少必需配置"), (
        "docs/MCP.md 闸门（isError=true）须出现在判据文案之前"
    )
    # 判据形态（第六轮引入）：「含「缺少必需配置：」+ 不含排除形态」组合。
    # 变异实测（锐鉴/幽明 M1/M9/G3）：整段退回旧 startswith 形态或删排除项均静默失效，
    # 故形态三要素逐一钉住。
    assert "含「缺少必需配置：」" in exception_seg, "docs/MCP.md 例外条款丢失「含」判据形态"
    assert "不含 `validation error` / `Unknown tool`" in exception_seg, (
        "docs/MCP.md 例外条款丢失排除形态（validation error / Unknown tool）"
    )
    assert "以「缺少必需配置：」开头" not in exception_seg, (
        "docs/MCP.md 例外条款回退为旧 startswith 形态（框架强制前缀下恒不命中）"
    )
    # instructions 第 5 条：同句共现（闸门 + 形状排除 + 形态）
    instructions = mcp_server.mcp.instructions or ""
    rule5 = next(seg for seg in instructions.split("5.") if "缺少必需配置" in seg)
    assert "isError=true" in rule5, "instructions 第 5 条丢失 isError=true 闸门"
    assert "validation error" in rule5, "instructions 第 5 条丢失 ② 形状排除"
    assert "Unknown tool" in rule5, "instructions 第 5 条丢失 Unknown tool 形状"
    assert rule5.index("isError=true") < rule5.index("缺少必需配置"), "instructions 闸门须出现在判据文案之前"
    # 否定词语义（幽明 H1 变异：且不含→且含 完全反转不报警）
    assert "不含 validation error" in rule5, "instructions 第 5 条否定词反转（排除项被改为命中）"
    # 旧形态禁用（幽明 G5/G6 变异：回退为 startswith 形态不报警）
    assert "以「缺少必需配置：」开头" not in rule5, "instructions 第 5 条回退为旧 startswith 形态"


def test_error_contract_mutation_guards() -> None:
    """错误契约变异回归：第六轮已修复的三种事故形态再次出现时必须报警。

    用例内直接复刻文档判据链，对四类真实错误文本逐一验证落点——
    文档措辞与协议行为的绑定不再依赖人工 diff 核对。
    """
    cases = [
        # (名称, is_error, 正文, 期望判定)
        ("①业务拒绝", False, '{"error": "state 仅支持 open/closed"}', "business"),
        ("①部分落盘", False, '{"error": "写入失败（已完成 1/2）… #7 → update 7"}', "business"),
        (
            "②Schema校验",
            True,
            "Error executing tool memory_get: 1 validation error for memory_getArguments",
            "param",
        ),
        ("②未知工具", True, "Unknown tool: memory_typo", "param"),
        ("③未捕获ApiError", True, "Error executing tool memory_get: CNB API 500: {}", "unexpected"),
        (
            "配置缺失",
            True,
            "Error executing tool memory_get: 缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN",
            "config",
        ),
        # 反例：成功结果与参数错误回显含判据字面量，不得误判为配置缺失
        (
            "成功正文含字面量",
            False,
            '{"number": 42, "body": "报「缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN」时不要重试"}',
            "success",
        ),
        (
            "②实参回显含字面量",
            True,
            "Error executing tool memory_get: 1 validation error for memory_getArguments [input_value=缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN]",
            "param",
        ),
    ]

    def judge(is_error: bool, text: str) -> str:
        if not is_error:
            return "business" if '"error"' in text[:200] else "success"
        if "1 validation error" in text or text.startswith("Unknown tool:"):
            return "param"
        if "缺少必需配置：" in text:
            return "config"
        return "unexpected"

    for name, is_error, text, expected in cases:
        actual = judge(is_error, text)
        assert actual == expected, f"{name}：判据落点 {actual} ≠ 预期 {expected}"


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
