# Cnb Agentic Memory (CAM)

基于 [CNB](https://cnb.cool) 平台的通用智能体记忆工具：以 Issue 为存储、知识库为语义检索，把 CNB 的 Issue、知识库、检索能力组合成一个开箱即用的智能体记忆层。

- **一记忆 = 一 Issue**：Issue `number` 是记忆唯一标识，免自定义 id / 免并发仲裁
- **零 git 依赖**：全部走 CNB Open API，无 git 二进制 / 无本地 clone
- **多形态**：SDK（Python）→ CLI（`cam`）→ MCP Server（`cam-mcp`）→ Agent Skill，按需取用
- **实测背书**：语义召回 0.98+、两步写入、写后回读校验等架构红线全部来自 PoC 实测

## 安装

```bash
uv add cnb-agentic-memory             # SDK + CLI
uv add "cnb-agentic-memory[mcp]"       # 含 MCP Server（计划中）
```

## 快速开始（SDK）

```python
import asyncio

from cam import CnbApiClient, Memory


async def main() -> None:
    async with CnbApiClient(token="<token>", repo="group/memory") as client:
        memory = Memory(client)

        # 写入：两步写入 + 回读校验；title 由调用方撰写（建议提炼关键词短语，
        # keyword 检索只匹配标题），未提供时兜底为正文首行截取
        result = await memory.write(
            "PostgreSQL 分区表使用 pg_partman 解决慢查询，按月分区",
            title="PostgreSQL 分区表 pg_partman",
            tags=["postgresql"],
            category="db",
        )
        print(result.number, result.url)

        # 语义检索（需仓库配置 knowledge:update 流水线，写入后约 1~2 分钟可检索）
        hits = await memory.search("分区表 慢查询")
        for hit in hits:
            print(hit.score, hit.number, hit.title)


asyncio.run(main())
```

配置支持 `CAM_` 前缀环境变量：`CAM_TOKEN`、`CAM_REPO`、`CAM_BASE_URL`、`CAM_TIMEOUT`（详见 [docs/API.md](docs/API.md)）。

## 使用形态

| 形态 | 用法 | 文档 | 状态 |
| --- | --- | --- | --- |
| Python SDK | `from cam import CnbApiClient, Memory` | [docs/API.md](docs/API.md) | ✅ 已实现 |
| CLI | `cam --help` | [docs/CLI.md](docs/CLI.md) | ✅ 已实现 |
| MCP Server | `cam-mcp` | docs/MCP.md | 计划中 |
| Agent Skill | `npx skills add ...` | skills/ | 计划中 |

## 文档

- [SDK API 参考](docs/API.md) — 配置、错误处理、记忆语义层设计约定、记忆仓库前置条件
- [CLI 参考](docs/CLI.md) — 命令清单、配置、错误处理

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
