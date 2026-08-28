# CLI 参考

`cam` 命令行工具是 SDK 记忆语义层的薄封装：命令与 `Memory` 方法一一对应，输出 JSON 便于智能体消费，错误透传到 stderr 并以非零码退出（智能体可自行决策重试）。

## 配置

与 SDK 一致，统一走 `CAM_` 前缀环境变量（无命令行配置参数）：

| 环境变量 | 说明 |
| --- | --- |
| `CAM_TOKEN` | CNB API Token（需 `repo-issue:rw` + `repo-code:r`） |
| `CAM_REPO` | 记忆仓库 slug，如 `group/memory` |
| `CAM_BASE_URL` | API 地址，默认 `https://api.cnb.cool` |
| `CAM_TIMEOUT` | 请求超时秒数，默认 30 |

## 命令

| 命令 | 说明 |
| --- | --- |
| `cam write CONTENT --title TITLE [--tag TAG]... [--category CATEGORY]` | 写入记忆（两步写入 + 回读校验，超长自动拆分） |
| `cam get NUMBER` | 读取记忆原文 |
| `cam update NUMBER [--content] [--title] [--tag]... [--category]` | 更新正文/标题/标签（标签为追加语义） |
| `cam append NUMBER NOTE` | 追加更新记录（进知识库可被语义检索） |
| `cam delete NUMBER` | 软删除记忆（可 restore 恢复） |
| `cam restore NUMBER` | 恢复软删除的记忆 |
| `cam list [--category] [--tag]... [--state] [--limit]` | 按分类/标签过滤列表 |
| `cam recent [--limit]` | 最近更新的记忆 |
| `cam search QUERY [--top-k] [--include-closed]` | 语义检索（知识库召回 + 元信息回读） |

## 示例

```bash
# 写入：title 建议提炼高区分度关键词短语（keyword 检索只匹配标题），
# 不传则兜底为正文首行截取
cam write "PostgreSQL 分区表使用 pg_partman 解决慢查询" \
  --title "PostgreSQL 分区表 pg_partman" \
  --tag postgresql --category db

# 输出（JSON）
# {
#   "number": 12,
#   "title": "cam: PostgreSQL 分区表 pg_partman",
#   "url": "https://cnb.cool/group/memory/-/issues/12"
# }

# 读取 / 追加 / 删除
cam get 12
cam append 12 "追加了 pg_partman 配置示例"
cam delete 12   # 软删除，cam restore 12 可恢复

# 检索（需仓库配置 knowledge:update 流水线，写入后约 1~2 分钟可检索）
cam search "分区表 慢查询" --top-k 3

# 列表
cam list --category db --limit 10
cam recent
```

## 错误处理

CLI 不做重试：API 错误（含状态码与 CNB 响应原文）与业务错误（如写后回读校验失败）均输出到 stderr，退出码 1，由调用方（智能体）自行决策。
