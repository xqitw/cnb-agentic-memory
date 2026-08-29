# CNB Agentic Memory

[![Latest Release](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/release)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/release.link)
[![PyPI](https://img.shields.io/pypi/v/cnb-agentic-memory.svg)](https://pypi.org/project/cnb-agentic-memory/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org)
![CI](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/git/latest/ci/pipeline-as-code?branch=main)
![git-clone-yyds](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/git/latest/ci/git-clone-yyds)
[![Star](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/star)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/star.link)
[![Fork](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/fork)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/fork.link)

基于 [CNB](https://cnb.cool) 平台的通用智能体记忆工具：以 Issue 为存储、知识库为语义检索，把 CNB 的 Issue、知识库、检索能力组合成一个开箱即用的智能体记忆层。

- **一记忆 = 一 Issue**：Issue `number` 是记忆唯一标识，免自定义 id / 免并发仲裁
- **零 git 依赖**：全部走 CNB Open API，无 git 二进制 / 无本地 clone
- **多形态**：SDK（Python）→ CLI（`cnb-agentic-memory`）→ MCP Server（`cnb-agentic-memory-mcp`）→ Agent Skill，按需取用
- **实测背书**：语义召回在 PoC 实测样例中可达 0.98+；两步写入、写后回读校验等架构红线均来自实测

## 安装

```bash
uv add cnb-agentic-memory             # SDK + CLI
uv add "cnb-agentic-memory[mcp]"       # 含 MCP Server

# 或用 pip
pip install "cnb-agentic-memory[mcp]"

# MCP Server 启动（stdio，接入 Claude Desktop / CNB AI 助手等 MCP 客户端；需 [mcp] extra）
cnb-agentic-memory mcp
```

## 快速开始（SDK）

> **前置条件**：记忆仓库须为**专用仓库**（仓库中全部 Issue 均为记忆，不与普通 Issue 混用）；语义检索还需在仓库配置知识库同步流水线，详见 [docs/API.md](docs/API.md#记忆仓库前置条件)。

```python
import asyncio

from cnb_agentic_memory import CNBApiClient, Memory


async def main() -> None:
    async with CNBApiClient(token="<token>", repo="group/memory") as client:
        memory = Memory(client)

        # 写入：两步写入 + 回读校验；title 由调用方撰写（建议提炼关键词短语，
        # keyword 检索只匹配标题），未提供时兜底为正文首行截取
        result = await memory.write(
            "PostgreSQL 分区表使用 pg_partman 解决慢查询，按月分区",
            title="PostgreSQL 分区表 pg_partman",
            tags=["postgresql"],
            category="db",
        )
        print(result.number, result.title)

        # 语义检索（需仓库配置 knowledge:update 流水线，写入后约 1~2 分钟可检索）
        hits = await memory.search("分区表 慢查询")
        for hit in hits:
            print(hit.score, hit.number, hit.title)

        # 关键词标题检索（与语义检索并列，仅匹配 title，无需知识库；
        # title 含确切关键词时更精准）
        issues = await memory.keyword_search("pg_partman", limit=10)
        for issue in issues:
            print(issue.number, issue.title)


asyncio.run(main())
```

配置支持 `CNB_AGENTIC_MEMORY_` 前缀环境变量：`CNB_AGENTIC_MEMORY_TOKEN`、`CNB_AGENTIC_MEMORY_REPO`、`CNB_AGENTIC_MEMORY_BASE_URL`、`CNB_AGENTIC_MEMORY_TIMEOUT`（详见 [docs/API.md](docs/API.md)）。

## 使用形态

| 形态 | 用法 | 文档 | 状态 |
| --- | --- | --- | --- |
| Python SDK | `from cnb_agentic_memory import CNBApiClient, Memory` | [docs/API.md](docs/API.md) | ✅ 已实现 |
| CLI | `cnb-agentic-memory --help` | [docs/CLI.md](docs/CLI.md) | ✅ 已实现 |
| MCP Server | `cnb-agentic-memory-mcp` | [docs/MCP.md](docs/MCP.md) | ✅ 已实现 |
| Agent Skill | `npx skills add ...` | [skills/cnb-agentic-memory](skills/cnb-agentic-memory/SKILL.md) | ✅ 已实现 |

## 文档

- [SDK API 参考](docs/API.md) — 配置、错误处理、记忆语义层设计约定、记忆仓库前置条件
- [CLI 参考](docs/CLI.md) — 命令清单、配置、错误处理
- [MCP Server 参考](docs/MCP.md) — 接入配置、工具清单、智能体使用指导
- [Agent Skill](skills/cnb-agentic-memory/SKILL.md) — 智能体记忆系统使用技能

## 开发

```bash
uv sync --extra dev                    # 安装依赖
uv run ruff check src/ tests/          # 代码风格
uv run ruff format --check src/ tests/ # 格式
uv run mypy src tests                  # 类型检查
uv run pytest -v                       # 单元测试（respx mock，不依赖网络）
npx markdownlint-cli2 '**/*.md'        # 文档校验
```

提交信息使用中文 Conventional Commits 风格（feat/fix/docs/refactor/test/chore），开发在短生命周期分支，main 通过 PR 合并。参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
