# 贡献指南

感谢你对 cnb-agentic-memory (cam) 的关注！欢迎提交 Issue 和 Pull Request。

## 开发环境

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) 包管理（推荐）

## 开发流程

### 1. 安装依赖

```bash
uv sync --extra dev
```

### 2. 提交代码前

运行全部质量检查：

```bash
# 代码风格
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# 类型检查
uv run mypy src tests

# 单元测试
uv run pytest tests/ -v

# Markdown 文档校验（需 node 环境）
npx markdownlint-cli2 '**/*.md'
```

目标：新增代码的测试覆盖率不低于 90%，所有检查必须通过。

### 3. 提交规范

- 提交信息使用中文，遵循 [Conventional Commits](https://www.conventionalcommits.org/) 风格：
  - `feat:` 新功能
  - `fix:` 缺陷修复
  - `refactor:` 重构
  - `docs:` 文档
  - `ci:` 流水线 / CI 配置
  - `test:` 测试
  - `chore:` 杂项
- 示例：`feat: 支持 memory_append 追加更新记录`

## 代码规范

### 约定

- 代码注释与文档使用中文
- 遵循现有文件命名与函数风格，不做风格重构
- 新增参数必须有默认值或兼容旧行为
- 可复用的逻辑必须抽离为独立函数，禁止在多处重复实现

### 架构约束

- **零 git 依赖**：存储操作全部走 CNB Open API，禁止引入 git 协议封装
- **写路径显式校验**：写操作完成后 GET 回读确认，防范 CNB API 静默失败
- 完整架构约束见 [AGENTS.md](./AGENTS.md)

### 目录约定

- `src/cam/` — 核心（API 客户端 / 工具语义层 / MCP Server / CLI）

## 分支与发布

- 开发在短生命周期分支进行，main 分支通过 PR 合并（保护分支）
- 打 tag（`v*`）触发 CI：测试 → 发布 PyPI → code-wiki → git release

## 测试注意事项

- 单元测试使用 `respx` mock CNB API 请求，不依赖真实网络
- 集成测试（真实 CNB API）不进入 CI，本地手动验证
- 集成测试需要 `MEMORY_REPO` / `CNB_TOKEN` 环境变量
