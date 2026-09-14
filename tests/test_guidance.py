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
    """错误契约闸门与判据以「规范短语整段」锁定（docs 与 instructions 两侧）。

    历史事故链：裸子串断言被加尾缀/删槽位/换口径穿透（第八、九轮评审
    实测 Unknown tool→Unknown toolX、含→不包含、validation error→
    ValidationError 均不报警），故断言一律取规范短语整段而非裸子串。
    """
    import re as _re

    # docs/MCP.md 例外条款：切片到「例外」之后再断言（同行出现 ≠ 条款被守护）
    mcp_doc = Path("docs/MCP.md").read_text(encoding="utf-8")
    gate_line = next(line for line in mcp_doc.splitlines() if "缺少必需配置" in line and "例外" in line)
    exception_seg = gate_line.split("例外", 1)[1]
    assert "isError=true" in exception_seg, "docs/MCP.md 例外条款丢失 isError=true 闸门"
    assert "仅在" in exception_seg, "docs/MCP.md 例外条款丢失「仅在」限定"
    assert exception_seg.index("isError=true") < exception_seg.index("缺少必需配置"), (
        "docs/MCP.md 闸门（isError=true）须出现在判据文案之前"
    )
    # 判据主语精确字面量（第十轮：正则前导字符法被 **不**含/不包含/且→或 同族穿透）
    assert "且正文**含「缺少必需配置：」" in exception_seg, "docs/MCP.md 例外条款判据主语丢失或方向反转"
    assert "不含 `validation error` / `Unknown tool`" in exception_seg, (
        "docs/MCP.md 例外条款丢失排除形态（validation error / Unknown tool）"
    )
    assert "以「缺少必需配置：」开头" not in exception_seg, (
        "docs/MCP.md 例外条款回退为旧 startswith 形态（框架强制前缀下恒不命中）"
    )
    # ①②③ 正反向示例句对称锚点（幽明第九轮条 4：同类方向锚只在例外段建了）
    clause = gate_line  # 条款本体（单行，含 ①②③ 与例外）
    assert "③ 无明细" in clause, "docs/MCP.md ③ 明细方向反转（真实形态为无明细）"
    assert "顶层 JSON 的 `error` 键" in clause, "docs/MCP.md ① 判据口径被改（须为顶层 JSON 的 error 键）"
    assert "正文含 `validation error`" in clause, "docs/MCP.md ② 正向判据口径被改"
    assert "② 的明细在正文里，③ 无明细" in clause, "docs/MCP.md ②③ 明细区分句丢失"
    # ①②③ 正反向示例句对称锚点（幽明第九轮条 4：同类方向锚只在例外段建了）
    clause = gate_line  # 条款本体（单行，含 ①②③ 与例外）
    assert "③ 无明细" in clause, "docs/MCP.md ③ 明细方向反转（真实形态为无明细）"
    assert "顶层 JSON 的 `error` 键" in clause, "docs/MCP.md ① 判据口径被改（须为顶层 JSON 的 error 键）"
    assert "正文含 `validation error`" in clause, "docs/MCP.md ② 正向判据口径被改"
    assert "② 的明细在正文里，③ 无明细" in clause, "docs/MCP.md ②③ 明细区分句丢失"
    # ② 正向判据口径锚点（第九轮：换口径 ValidationError / 改计数不报警）
    assert "正文含 `validation error`" in mcp_doc, (
        "docs/MCP.md ② 正向判据口径被改（须为宽口径 validation error，单复数通吃）"
    )
    # ① 判据口径整段锚点（第十轮锐鉴：顶层 JSON error 键 改成 文本子串查找不报警）
    assert "顶层 JSON 的 `error` 键" in mcp_doc, (
        "docs/MCP.md ① 判据口径被改（须为顶层 JSON 的 error 键，非子串查找）"
    )
    # docs ③ 无明细锚点（第十轮锐鉴：无明细 改 含明细 方向反转不报警）
    assert "③ 无明细" in mcp_doc, "docs/MCP.md ③ 明细方向反转（真实形态为无明细）"
    # instructions 第 5 条：定位加固（split("5.") 在「五、」时退化为全文——
    # 改用正则锚定行首编号，退化即断言失败而非静默变绿）
    instructions = mcp_server.mcp.instructions or ""
    seg5 = _re.search(r"5\.[^\n]*", instructions)
    assert seg5 is not None, "instructions 第 5 条定位失败（编号缺失）"
    rule5 = seg5.group(0)
    assert "缺少必需配置" in rule5, "instructions 第 5 条判据丢失"
    assert "isError=true" in rule5, "instructions 第 5 条丢失 isError=true 闸门"
    assert rule5.index("isError=true") < rule5.index("缺少必需配置"), "instructions 闸门须出现在判据文案之前"
    # 判据主语精确字面量（第十轮：同 docs 侧，堵同族否定写法与连接词放宽）
    assert "且正文含「缺少必需配置：」" in rule5, "instructions 第 5 条判据主语丢失或方向反转"
    assert "且不含 validation error" in rule5, "instructions 第 5 条否定词反转（排除项被改为命中）"
    # 说明句口径词对称锚点（幽明第九轮条 3：docs 同语义句有锚、instructions 说明句无锚）
    assert "validation error" in rule5 and "单数或复数" in rule5, (
        "instructions 第 5 条说明句丢失宽口径口径词（单数或复数）"
    )
    # 说明句口径词对称锚点（幽明第九轮条 3：docs 同语义句有锚、instructions 说明句无锚）
    assert "validation error" in rule5 and "单数或复数" in rule5, (
        "instructions 第 5 条说明句丢失宽口径口径词（单数或复数）"
    )
    # 排除条款整句锚点（第十轮：rule5 里 Unknown tool: 出现 2 次，裸子串被说明文字喂饱）
    assert "且不含 validation error / Unknown tool: 时" in rule5, "instructions 第 5 条排除条款被删或弱化"
    # 宽口径措辞锚点（第九轮：单数或复数 被撤销即回退单数硬计数）
    assert "单数或复数" in rule5, "instructions 第 5 条丢失宽口径措辞（单数或复数）"
    # 末句方向锚点（第十轮锐鉴：isError=false 改 isError=true 方向反转不报警）
    assert "isError=false 的成功结果" in rule5, (
        "instructions 第 5 条末句方向反转（须为 isError=false 的成功结果）"
    )
    # 旧形态禁用
    assert "以「缺少必需配置：」开头" not in rule5, "instructions 第 5 条回退为旧 startswith 形态"


def test_error_contract_mutation_guards() -> None:
    """错误契约变异回归：文档措辞与协议行为的绑定由本用例锁定。

    判据词（排除形态/判据字面量）从 docs/MCP.md 与 instructions 的实际文本
    抽取构造——文档措辞变更而用例未同步即红（真绑定，非测试内硬编码复刻）。
    错误文本样本取自协议层真实形态：③ 无明细（框架吞掉，仅服务端日志）、
    ② 单/复数 validation error、Unknown tool 无前缀。
    """
    # 判据词从文档实际文本构造（真绑定：文档改措辞而本用例未同步即红）
    mcp_doc = Path("docs/MCP.md").read_text(encoding="utf-8")
    gate_line = next(line for line in mcp_doc.splitlines() if "缺少必需配置" in line and "例外" in line)
    exception_seg = gate_line.split("例外", 1)[1]
    instructions = mcp_server.mcp.instructions or ""
    # 第 5 条定位：取「5.」出现的行并要求行内含判据——避免全文首个 5. 的误报面
    rule5 = next(
        line for line in instructions.splitlines() if line.startswith("5.") and "缺少必需配置" in line
    )

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
        # ① 顶层键判据反例：非顶层嵌套（子串/递归查找两类退化形态均误判 business，
        # 只有 json.loads 顶层键判据能正确判 success——第十轮：75 字符样本在 200 窗口内无判别力）
        ("①error非顶层", False, '{"data": {"error": "state 仅支持 open/closed"}}', "success"),
    ]

    # 样本守护（幽明第九轮：样本集自身无守卫，删样本/组合回退全绿）——
    # 按用例名精确取值，逐条断言存在 + 判别性形态；10/10 全覆盖，删任何一条或削形态即红
    by_name = {name: text for name, _, text, _ in cases}
    expected_shapes = {
        "①业务拒绝": ('{"error"', None),
        "①部分落盘": ('{"error"', None),
        "②Schema校验": ("1 validation error", None),
        "②多字段校验": ("2 validation errors", None),
        "②未知工具": ("Unknown tool:", None),
        "③未捕获异常": ("Error executing tool memory_get", "validation error"),  # 无明细
        "配置缺失": ("缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN", None),
        "成功正文含字面量": ("缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN", None),
        "②实参回显含字面量": ("1 validation error", None),
        "①error非顶层": ('{"data"', None),
    }
    for name, (must_contain, must_not_contain) in expected_shapes.items():
        assert name in by_name, f"判据链样本 {name!r} 被删——样本集守护失效"
        text = by_name[name]
        assert must_contain in text, f"样本 {name!r} 形态漂移：丢失 {must_contain!r}"
        if must_not_contain:
            assert must_not_contain not in text, f"样本 {name!r} 形态漂移：不应含 {must_not_contain!r}"

    # 判据优先级锁定（幽明：删②实参回显样本+倒置 config/param 顺序可全绿）——
    # ② 排除形态必须先于配置缺失判据被检验（顺序语义即契约）；judge 分支顺序与之同源
    overlapping = "1 validation error [input_value=缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN]"
    assert param_first(overlapping) == "param"
    pure_config = "缺少必需配置：CNB_AGENTIC_MEMORY_TOKEN（CNB API 令牌）"
    assert param_first(pure_config) == "config"

    for name, is_error, text, expected in cases:
        actual = judge(is_error, text)
        assert actual == expected, f"{name}：判据落点 {actual} ≠ 预期 {expected}"


def param_first(text: str) -> str:
    """judge 的②/config 分支顺序语义复刻（②先于 config）。"""
    for marker in ("validation error", "Unknown tool:"):
        if marker in text:
            return "param"
    if "缺少必需配置：" in text:
        return "config"
    return "unexpected"


def judge(is_error: bool, text: str) -> str:
    """判据链复刻（与 param_first 顺序同源）：① 顶层键 → ② 宽口径 → config。"""
    if not is_error:
        try:
            return "business" if "error" in json.loads(text) else "success"
        except ValueError:
            return "success"
    return param_first(text)


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
