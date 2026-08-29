# CLI 参考

`cnb-agentic-memory` 命令行工具是 SDK 记忆语义层的薄封装：命令与 `Memory` 方法一一对应，输出 JSON 便于智能体消费，错误透传到 stderr 并以非零码退出（智能体可自行决策重试）。

## 配置

与 SDK 一致，统一走 `CNB_AGENTIC_MEMORY_` 前缀环境变量（无命令行配置参数）：

| 环境变量 | 说明 |
| --- | --- |
| `CNB_AGENTIC_MEMORY_TOKEN` | CNB API Token（需 `repo-issue:rw` + `repo-code:r`） |
| `CNB_AGENTIC_MEMORY_REPO` | 记忆仓库 slug，如 `group/memory` |
| `CNB_AGENTIC_MEMORY_BASE_URL` | API 地址，默认 `https://api.cnb.cool` |
| `CNB_AGENTIC_MEMORY_TIMEOUT` | 请求超时秒数，默认 30 |
| `CNB_AGENTIC_MEMORY_DEBUG` | 设为 1 时未预期异常抛出完整堆栈（默认仅输出友好一行，退出码 70） |

## 命令

| 命令 | 说明 |
| --- | --- |
| `cnb-agentic-memory write CONTENT --title TITLE [--tag TAG]... [--category CATEGORY]` | 写入记忆（两步写入 + 回读校验，超长自动拆分；--tag 单值内逗号拆分为多标签，标签限汉字/字母/数字/_.: - / \ /全角/中间空格，1~50 字符，写前预检） |
| `cnb-agentic-memory get NUMBER` | 读取记忆原文 |
| `cnb-agentic-memory update NUMBER [--content] [--title] [--tag]... [--category]` | 更新正文/标题/标签（标签为追加语义） |
| `cnb-agentic-memory append NUMBER NOTE` | 追加更新记录（进知识库可被语义检索） |
| `cnb-agentic-memory delete NUMBER` | 软删除记忆（可 restore 恢复；仅默认检索隐藏，内容仍留知识库向量，真正废弃才用） |
| `cnb-agentic-memory restore NUMBER` | 恢复软删除的记忆 |
| `cnb-agentic-memory list [--category] [--tag]... [--state] [--limit]` | 按分类/标签过滤列表（不回显正文，需全文用 get） |
| `cnb-agentic-memory recent [--limit]` | 最近更新的记忆（不回显正文，需全文用 get） |
| `cnb-agentic-memory search QUERY [--top-k] [--include-closed]` | 语义检索（知识库召回 + 元信息回读，按内容模糊查找；与 `cnb-agentic-memory keyword` 并列） |
| `cnb-agentic-memory keyword QUERY [--limit] [--include-closed]` | 关键词标题检索（仅匹配标题，不回显正文需全文用 get；无需知识库；title 含确切关键词时更精准） |
| `cnb-agentic-memory --version` | 显示版本号并退出 |
| `cnb-agentic-memory mcp` | 启动 MCP Server（stdio；需安装 mcp extra） |

## 示例

```bash
# 写入：title 建议提炼高区分度关键词短语（keyword 检索只匹配标题），
# 不传则兜底为正文首行截取
cnb-agentic-memory write "PostgreSQL 分区表使用 pg_partman 解决慢查询" \
  --title "PostgreSQL 分区表 pg_partman" \
  --tag postgresql --category db

# 输出（JSON）
# {
#   "number": 12,
#   "title": "PostgreSQL 分区表 pg_partman",
#   "parts": []  # 超长拆分时携带全部分片（number/title）供循迹
# }

# 读取 / 追加 / 删除
cnb-agentic-memory get 12
cnb-agentic-memory append 12 "追加了 pg_partman 配置示例"
cnb-agentic-memory delete 12   # 软删除，cnb-agentic-memory restore 12 可恢复

# 语义检索（需仓库配置 knowledge:update 流水线，写入后约 1~2 分钟可检索）
cnb-agentic-memory search "分区表 慢查询" --top-k 3

# 关键词标题检索（与 search 并列，无需知识库；title 含确切关键词时更精准）
cnb-agentic-memory keyword "pg_partman" --limit 10

# 列表
cnb-agentic-memory list --category db --limit 10
cnb-agentic-memory recent
```

## 错误处理

CLI 不做重试：API 错误（含状态码与 CNB 响应原文）与业务错误（如写后回读校验失败）均输出到 stderr，退出码 1，由调用方（智能体）自行决策。
