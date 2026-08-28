---
name: cam-skill
description: 基于-cnb-平台构建的通用智能体记忆工具（cam）。让智能体拥有跨会话的长期记忆：把重要信息写入记忆仓库，之后用语义检索或分类浏览找回。**写入时必须自己撰写 title**（提炼 3~8 个高区分度关键词短语，keyword 标题检索只匹配 title）——title 质量决定记忆能否被找回。适合回答"记住这个"、"帮我记一下"、"之前说过什么"、"上次是怎么解决的"、"查一下我们的记忆库"这类跨会话请求。当用户提到记忆、记住、想起、之前、上次、经验、教训、备忘时使用。用法：调用 `cam` CLI（write / get / append / update / delete / restore / list / recent / search 子命令）。
---

# cam — 智能体记忆系统

基于 CNB 平台的跨会话记忆：一条记忆 = 一个 Issue，`number` 是记忆唯一标识。写入即持久化，读取走语义检索（知识库向量，实测相关度 0.98+）或标题检索（降级通道）。

## 核心决策指引（重要）

1. **写入时必须认真撰写 title**：title 是标题检索的唯一入口（keyword 搜索只匹配标题）。
   好的 title = 高区分度关键词的短语（如 `cam: PostgreSQL 分区表 pg_partman`），
   坏的 title = 长句或概括性描述（如 `关于数据库优化的记录`）。工具会保证
   `cam:` 前缀与长度上限，但关键词质量由你决定。
2. **找回记忆优先用 search**（语义召回，按内容模糊查找）；按已知分类/标签
   浏览用 list。两者互补：search 适合"记得写过类似的东西"，list 适合
   "看看 db 分类下都有什么"。
3. **追加 vs 更新**：在原记忆上补充新信息用 `append`（追加记录，进知识库
   可被检索）；信息本身错了需要改用 `update`（content 是全量替换，注意先
   `get` 拿到旧正文再合并）。
4. **删除是软删除**：`delete` 后可 `restore` 恢复，不要害怕删错；但也不要
   用删除来做"内容清理"——更新内容请用 `update`。
5. **写入后约 1~2 分钟才能被语义检索到**（知识库同步时延），这是平台预期，
   不是故障。写入成功返回的 number 就是永久凭据，可先记录。

## 安装与调用

CLI 已发布到 PyPI（包名 `cnb-agentic-memory`）。三种方式按需选择：

### 方式一：uvx 免安装直接调用（推荐）

无需任何安装步骤，`uvx` 自动下载并运行：

```bash
uvx --from cnb-agentic-memory cam --help
uvx --from cnb-agentic-memory cam search "分区表"
```

> 需要 uv 工具：`curl -LsSf https://astral.sh/uv/install.sh | sh` 或 `pip install uv`。

### 方式二：pip / uv 安装（长期使用）

```bash
pip install cnb-agentic-memory
# 或
uv tool install cnb-agentic-memory
cam --help
```

### 环境变量（必需）

```bash
export CAM_TOKEN="<CNB API 令牌，需 repo-issue:rw + repo-code:r>"
export CAM_REPO="<记忆仓库 slug，如 group/memory>"
# 可选：CAM_BASE_URL（默认 https://api.cnb.cool）、CAM_TIMEOUT（默认 30 秒）
```

## 命令

### 1. 写入记忆

```bash
cam write "PostgreSQL 分区表使用 pg_partman 按月分区，慢查询从 8s 降到 200ms" \
  --title "PostgreSQL 分区表 pg_partman" \
  --tag postgresql --tag 运维经验 \
  --category db
# → {"number": 12, "title": "cam: PostgreSQL 分区表 pg_partman",
#    "url": "https://cnb.cool/group/memory/-/issues/12", "parts": []}
```

- `--title`：提炼关键词短语，不传则兜底截取正文首行（质量会差，建议总是传）
- `--tag`：可多次传入，自动补 `tag/` 前缀
- `--category`：分类，自动补 `category/` 前缀
- 超长内容（>30KB）自动拆成多条，title 带 `(i/n)` 序号，输出 `parts` 含全部分片

### 2. 语义检索（找回记忆的主通道）

```bash
cam search "分区表 慢查询" --top-k 3
# → [{"score": 0.98, "number": 12, "title": "cam: PostgreSQL 分区表 pg_partman",
#     "state": "open", "chunk": "…命中片段…", "url": "…"}]
```

- 返回按相关度排序，`state: closed` 的记忆默认被过滤（除非 `--include-closed`）
- 用 `number` 可进一步 `cam get <n>` 看全文
- 知识库未配置/不可用时命令会报错并提示降级方式

### 3. 标题检索（降级通道）

知识库不可用时，可用列表命令按已知条件浏览：

```bash
cam list --category db --limit 10
cam list --tag postgresql
cam recent --limit 5
```

### 4. 读取 / 追加 / 更新

```bash
cam get 12                          # 精确读取全文
cam append 12 "补充：pg_partman 2.x 配置格式有变化"   # 追加更新记录
cam update 12 --content "$(cat 新正文.md)" --title "新标题"  # 全量替换正文
cam update 12 --tag 已验证          # 只追加标签
```

### 5. 软删除 / 恢复

```bash
cam delete 12      # → {"number": 12, "state": "closed"}
cam restore 12     # → {"number": 12, "state": "open"}
```

## 输出与错误

- 所有命令成功输出 JSON（`ensure_ascii=False`），可直接 `json.loads` 解析
- 错误输出到 stderr，退出码非 0：
  - 退出码 2：配置错误（缺 `CAM_TOKEN` / `CAM_REPO`，提示缺什么）
  - 退出码 1：API 错误（含状态码与 CNB 响应原文）/ 业务错误（如写入回读校验失败，含已落盘分片编号）
  - 退出码 70：内部错误（设置 `CAM_DEBUG=1` 重试可看完整堆栈）
- **不要自行重试写操作**：写入失败时错误信息已携带已落盘分片编号（如"已完成 1/3：#5"），
  先处理孤儿分片再重试，否则会重复写入

## 常见任务示例

```text
用户：记住这个：我们的 CI 用的是 cnb 流水线，配置在 .cnb.yml
Agent：cam write "CI 使用 cnb 流水线，配置文件 .cnb.yml，PR 触发测试、tag 触发发布" \
        --title "CI cnb 流水线配置" --tag ci --category 基础设施

用户：上次数据库慢查询是怎么解决的？
Agent：cam search "数据库 慢查询 解决"

用户：把这条经验也补充到刚才那条记忆里
Agent：cam append <编号> "补充内容……"    # 先 search/get 找到编号

用户：看看 db 分类下都有哪些经验
Agent：cam list --category db --limit 20

用户：那条记忆过时了，删掉吧
Agent：cam delete <编号>                  # 软删除，可恢复
```
