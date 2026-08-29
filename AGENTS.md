# AGENTS.md — AI 编码助手指南

本文件面向 AI 编码助手，定义其职责与操作规范。

## 项目

cnb-agentic-memory — 基于 CNB 平台的通用智能体记忆工具。以 CNB Issue 为存储、CNB 知识库为语义检索，以 MCP Server 形式对外提供服务。Python 3.11+，PyPI 包名 `cnb-agentic-memory`，CLI 命令 `cnb-agentic-memory`，MCP Server 入口 `cnb-agentic-memory-mcp`。

## 目标

- 保持现有代码风格与工程结构
- 修改尽量小且可解释
- 文档与功能同步更新

## 基本规范

- 语言与注释：代码注释使用中文，面向开发者的文档使用中文
- 命名风格：遵循现有文件命名与函数风格，不做风格重构
- 兼容性：避免破坏现有 API 的默认行为
- 可读性优先：明确胜过精巧
- 复用抽离：可复用的逻辑必须抽离为独立函数，禁止在多处重复实现；新增前先检索是否已有等价实现

## 架构约束（不可违背）

- **零 git 依赖**：所有存储操作走 CNB Open API（Issue CRUD），禁止引入 git 二进制 / git 协议封装 / 本地 clone
- **专用记忆仓库**：`CNB_AGENTIC_MEMORY_REPO` 指向的仓库须为专用仓库（全部 Issue 均为记忆），检索与列表不做记忆/非记忆区分
- **一记忆 = 一 Issue**：Issue `number` 是记忆唯一标识，禁止自造 id 体系
- **两步写入**：创建 Issue（POST /issues）后必须单独补打标签（POST /issues/{n}/labels）——创建接口对新标签会静默丢弃
- **写路径显式校验**：所有写操作完成后 GET 回读确认，CNB API 存在静默失败形态
- **title 强制生成**：keyword 搜索只匹配标题，`memory_write` 的 title 必须由工具生成关键词摘要

## 修改前检查

- 先阅读文档与相关模块，不做"猜测式"改动
- 新增参数必须有默认值或兼容旧行为
- 添加新功能时必须考虑错误兜底和边界输入
- API 行为相关的改动需核对实测结论，与实测冲突时先验证再改

## 常用命令

```bash
uv sync --extra dev               # 安装依赖
uv run ruff check src/ tests/     # 代码风格检查
uv run ruff format --check src/ tests/  # 格式检查
uv run mypy src tests             # 类型检查
uv run pytest -v                  # 单元测试（respx mock，不依赖网络）
npx markdownlint-cli2 '**/*.md'   # Markdown 文档校验
```

## 提交规范

- 提交信息使用中文，遵循 Conventional Commits 风格（feat: / fix: / refactor: / docs: / ci: / test: / chore:）
- 提交前必须通过：ruff check、ruff format --check、mypy、pytest、markdownlint
- 开发在短生命周期分支，main 通过 PR 合并（保护分支，不直接推送）

## 测试注意事项

- 单元测试使用 respx mock CNB API 请求，不依赖真实网络
- 集成测试（真实 CNB API）不进入 CI，本地手动验证
- 集成测试需要 CNB_AGENTIC_MEMORY_REPO / CNB_AGENTIC_MEMORY_TOKEN 环境变量
