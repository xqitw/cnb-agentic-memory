# CNB Agentic Memory

[![Latest Release](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/release)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/release.link)
[![PyPI](https://img.shields.io/pypi/v/cnb-agentic-memory.svg)](https://pypi.org/project/cnb-agentic-memory/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org)
![CI](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/git/latest/ci/pipeline-as-code?branch=main)
![git-clone-yyds](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/git/latest/ci/git-clone-yyds)
[![Star](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/star)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/star.link)
[![Fork](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/fork)](https://cnb.cool/xqitw/cnb-agentic-memory/-/badge/fork.link)

基于 [CNB](https://cnb.cool) 平台的智能体记忆工具：以 Issue 为存储、知识库为语义检索，把 CNB 的 Issue、知识库、检索能力组合成一个开箱即用的记忆层。

## 功能特性

- **一记忆 = 一 Issue** — Issue `number` 即记忆唯一标识，免自定义 id / 免并发仲裁
- **双通道检索** — 知识库语义检索（按内容模糊查找）+ 关键词标题检索（无需知识库），按需选路
- **结构化组织** — 分类（`category:xxx`，CNB 平台约定）与标签过滤，适合按已知维度浏览
- **多形态** — Python SDK / CLI / MCP Server / Agent Skill，同一语义层按需取用
- **实测背书** — 两步写入、写后回读校验、超长拆分等架构红线全部来自 PoC 实测

## 前置准备

开始使用前需要准备三件事：

1. **创建专用私有仓库**——作为记忆仓库。必须是专用仓库（全部 Issue 均为记忆），检索与列表不做记忆/普通 Issue 区分
2. **获取 CNB 访问令牌**——在 CNB 个人设置中创建，需 `repo-issue:rw`（Issue 读写）+ `repo-code:r`（知识库检索）权限
3. **配置知识库入库流水线**——在记忆仓库的 `.cnb.yml` 挂载 `knowledge:update` 流水线（`$` 键下），语义检索依赖它；**必须先配置再写入**，错过事件的记忆不会被补录

> 令牌与配置的完整说明见 [docs/API.md · 配置](docs/API.md#配置)；流水线 YAML 示例见 [docs/API.md · 记忆仓库前置条件](docs/API.md#记忆仓库前置条件)。

## 安装

```bash
uvx cnb-agentic-memory --help                  # 免安装直接运行（推荐）
uv add "cnb-agentic-memory[mcp]"               # 或安装为依赖（含 MCP Server）
pip install "cnb-agentic-memory[mcp]"          # 或 pip
```

## 快速开始

配置环境变量（四种形态通用）：

```bash
export CNB_AGENTIC_MEMORY_TOKEN="<你的访问令牌>"
export CNB_AGENTIC_MEMORY_REPO="<组织名>/<仓库名>"
```

**Agent Skill 是本工具的首要使用方式**——安装 skill 后，智能体在对话中自动调用，无需手动执行任何命令：

```bash
# 安装 skill（任选一源）
npx skills add https://cnb.cool/xqitw/cnb-agentic-memory.git
npx skills add https://github.com/xqitw/cnb-agentic-memory.git
```

之后对智能体说一句话即可：

```text
用户：记住这个：我们的 CI 用 cnb 流水线，配置文件是 .cnb.yml
智能体：（自动调用 skill 写入记忆，自动提炼 title 与标签）
用户：上次我们的 CI 是怎么配的？
智能体：（自动检索记忆，找回上面那条）
```

其他形态：希望手动操作可用 [CLI](docs/CLI.md)，集成到自建智能体可用 [MCP Server](docs/MCP.md) 或 [Python SDK](docs/API.md)，见下方使用形态。

## 使用形态

| 形态 | 入口 | 文档 |
| --- | --- | --- |
| Python SDK | `from cnb_agentic_memory import CNBApiClient, Memory` | [docs/API.md](docs/API.md)（配置、错误处理、语义层设计约定） |
| CLI | `cnb-agentic-memory --help` | [docs/CLI.md](docs/CLI.md)（命令清单、错误处理） |
| MCP Server | `cnb-agentic-memory-mcp`（需 `[mcp]` extra） | [docs/MCP.md](docs/MCP.md)（客户端接入配置、智能体使用指导） |
| Agent Skill | `npx skills add https://cnb.cool/xqitw/cnb-agentic-memory.git` 或 GitHub 源 `https://github.com/xqitw/cnb-agentic-memory.git` | [skills/cnb-agentic-memory/SKILL.md](skills/cnb-agentic-memory/SKILL.md) |

## 贡献

欢迎参与贡献！开发环境搭建、分支与提交流程请参考 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## License

[MIT](./LICENSE)
