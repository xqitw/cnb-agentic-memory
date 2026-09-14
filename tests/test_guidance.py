"""指导内容一致性锚点测试（AGENTS.md 防漂移红线的自动化部分）。

断言关键语义锚定短语存在于 MCP instructions 与关键工具描述中：
改动描述删除关键语义时本测试失败，防止多通道指导漂移。
事实唯一权威在 docs/ 与各通道文本，本测试只守护"必须出现"的下限。
"""

from __future__ import annotations

import asyncio
import json
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
    # 主语正向断言（幽明第八轮变异：含→不含 整句反转，判据后果完全倒置仍全绿）
    # 匹配穿透 markdown 加粗；前导字符为「不」即判主语否定方向反转
    import re as _re

    m = _re.search(r"(.)(含)([^。]{0,4})「缺少必需配置：」", exception_seg)
    assert m is not None, "docs/MCP.md 例外条款判据主语丢失（不再是「含」）"
    assert m.group(1) != "不", "docs/MCP.md 例外条款主语否定方向反转（含→不含）"
    # instructions 第 5 条：同句共现（闸门 + 形状排除 + 形态）
    instructions = mcp_server.mcp.instructions or ""
    rule5 = next(seg for seg in instructions.split("5.") if "缺少必需配置" in seg)
    assert "isError=true" in rule5, "instructions 第 5 条丢失 isError=true 闸门"
    assert "validation error" in rule5, "instructions 第 5 条丢失 ② 形状排除"
    assert "Unknown tool" in rule5, "instructions 第 5 条丢失 Unknown tool 形状"
    assert rule5.index("isError=true") < rule5.index("缺少必需配置"), "instructions 闸门须出现在判据文案之前"
    # 否定词语义（幽明 H1 变异：且不含→且含 完全反转不报警）
    assert "不含 validation error" in rule5, "instructions 第 5 条否定词反转（排除项被改为命中）"
    # 主语正向断言（幽明第八轮变异：整句反转不报警）
    m5 = _re.search(r"(.)(含)([^。]{0,4})「缺少必需配置：」", rule5)
    assert m5 is not None, "instructions 第 5 条判据主语丢失"
    assert m5.group(1) != "不", "instructions 第 5 条主语否定方向反转（含→不含）"
    # 排除项完整字面量（幽明第八轮变异：Unknown tool → Unknown toolX 弱化仍通过）
    assert "Unknown tool:" in rule5, "instructions 第 5 条排除项被加尾缀弱化"
    # 旧形态禁用（幽明 G5/G6 变异：回退为 startswith 形态不报警）
    assert "以「缺少必需配置：」开头" not in rule5, "instructions 第 5 条回退为旧 startswith 形态"


def test_error_contract_mutation_guards() -> None:
    """错误契约变异回归：文档措辞与协议行为的绑定由本用例锁定。

    判据词（排除形态/判据字面量）从 docs/MCP.md 与 instructions 的实际文本
    抽取构造——文档措辞变更而用例未同步即红（真绑定，非测试内硬编码复刻）。
    错误文本样本取自协议层真实形态：③ 无明细（框架吞掉，仅服务端日志）、
    ② 单/复数 validation error、Unknown tool 无前缀。
    """
    # 判据词从文档实际文本构造（幽明第八轮：judge 硬编码与文档零连接是假绑定）
    mcp_doc = Path("docs/MCP.md").read_text(encoding="utf-8")
    gate_line = next(line for line in mcp_doc.splitlines() if "缺少必需配置" in line and "例外" in line)
    exception_seg = gate_line.split("例外", 1)[1]
    instructions = mcp_server.mcp.instructions or ""
    rule5 = next(seg for seg in instructions.split("5.") if "缺少必需配置" in seg)

    # 排除形态词：例外条款与 instructions 第 5 条都要求「不含 validation error / Unknown tool」
    config_marker = "缺少必需配置："  # 配置缺失判据字面量（与 api.py _validate_config 同源）
    param_markers = ("validation error", "Unknown tool:")  # ② 识别形态（宽口径，单复数通吃）
    for marker in (config_marker,) + param_markers:
        assert marker in exception_seg or marker in rule5, f"判据词 {marker!r} 已从文档/instructions 消失"

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
        (
            "②多字段校验",
            True,
            "Error executing tool memory_write: 2 validation errors for memory_writeArguments",
            "param",
        ),
        ("②未知工具", True, "Unknown tool: memory_typo", "param"),
        # ③ 真实形态：无明细（框架 tools/base.py:210 吞掉异常文本，仅服务端日志）
        ("③未捕获异常", True, "Error executing tool memory_get", "unexpected"),
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
        # ① 顶层键判据反例：error 键在 JSON 靠后位置（窗口子串查找会误判，须按顶层键）
        (
            "①error键靠后",
            False,
            '{"number": 42, "title": "t", "body": "x", "error": "state 仅支持 open/closed"}',
            "business",
        ),
    ]

    def judge(is_error: bool, text: str) -> str:
        if not is_error:
            # ① 判据与文档同源：顶层 JSON 的 error 键（非窗口子串查找）
            try:
                return "business" if "error" in json.loads(text) else "success"
            except ValueError:
                return "success"
        for marker in param_markers:  # ② 宽口径（单/复数/未知工具），词表与文档同源
            if marker in text:
                return "param"
        if config_marker in text:
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
