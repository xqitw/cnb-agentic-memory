---
name: cnb-agentic-memory
description: 基于 CNB 平台的智能体记忆工具：跨会话写入/检索/管理记忆。写入时必须自己撰写 title（提炼 3~8 个高区分度关键词短语，keyword 检索只匹配 title）。当用户提到记忆、记住、之前、上次、经验、教训时使用。用法：cnb-agentic-memory CLI（write/get/append/update/delete/restore/list/recent/search/keyword）。
slug: cnb-agentic-memory
displayName: CNB 智能体记忆
version: 2.0.2
summary: 基于 CNB 平台的智能体记忆工具：以 Issue 为存储、知识库为语义检索，CLI/MCP/SDK 三入口跨会话记忆。
license: MIT
homepage: https://cnb.cool/xqitw/cnb-agentic-memory
tags: [记忆, 智能体, MCP, 知识库, CLI]
---

# cnb-agentic-memory — 智能体记忆系统

基于 CNB 平台的跨会话记忆：一条记忆 = 一个 Issue，`number` 是记忆唯一标识。写入即持久化，读取走语义检索（知识库向量，高相关度）。记忆仓库中全部 Issue 均为记忆，检索与列表结果不区分记忆与普通 Issue。

把重要信息写入记忆仓库，之后用语义检索或分类浏览找回。适合回答"记住这个"、"帮我记一下"、"之前说过什么"、"上次是怎么解决的"、"查一下我们的记忆库"这类跨会话请求。

**写入时必须自己撰写 title**（提炼 3~8 个高区分度关键词短语，keyword 标题检索只匹配 title）——title 质量决定记忆能否被找回。检索有两个并列通道：语义检索（`search`，按内容模糊查找，需知识库）与关键词标题检索（`keyword`，仅匹配标题，无需知识库）；知识库不可用时用 `keyword` 或按第 3 节分类/标签浏览。三种入口（SDK/MCP/CLI）能力一致。

## 核心决策指引（重要）

1. **写入时必须认真撰写 title**：title 是标题检索的唯一入口（keyword 搜索只匹配标题）。
   好的 title = 高区分度关键词的短语（如 `PostgreSQL 分区表 pg_partman`），
   坏的 title = 长句或概括性描述（如 `关于数据库优化的记录`）。工具会保证
   非空与长度上限（≤60 字符），并自动剔除控制字符，但关键词质量由你决定。
2. **找回记忆按需选路**：按内容模糊查找（"记得写过类似的东西"）用 `search`
   （语义召回）；title 含确切关键词（技术名词/编号/命令）用 `keyword`
   （标题检索，无需知识库，更精准）；按已知分类/标签浏览用 `list`（适合
   "看看 db 分类下都有什么"）。
3. **追加 vs 更新**：在原记忆上补充新信息用 `append`（追加记录，进知识库
   可被检索）；信息本身错了需要改用 `update`（content 是全量替换，注意先
   `get` 拿到旧正文再合并）。
4. **修正记忆用 update，不用删除重建**：恢复阶梯为 update 补齐/修正 >
   append 续写 > delete 废弃（最后手段）。delete 是软删除——仅从默认检索
   隐藏，内容仍留在知识库向量中（`--include-closed` 可召回），不是内容
   清除；误删重建还会让向量库多一份重复内容。
5. **写入后约 1~2 分钟才能被语义检索到**（知识库同步时延），这是平台预期，
   不是故障。写入成功返回的 number 就是永久凭据，可先记录。

## 安装与调用

CLI 已发布到 PyPI（包名 `cnb-agentic-memory`）。三种方式按需选择：

### 方式一：uvx 免安装直接调用（推荐）

无需任何安装步骤，`uvx` 自动下载并运行（命令名与包名一致，可直接运行）：

```bash
uvx cnb-agentic-memory --help
uvx cnb-agentic-memory search "分区表"
```

> 需要 uv 工具：`curl -LsSf https://astral.sh/uv/install.sh | sh` 或 `pip install uv`。

### 方式二：pip / uv 安装（长期使用）

```bash
pip install cnb-agentic-memory
# 或
uv tool install cnb-agentic-memory
cnb-agentic-memory --help
```

### 环境变量（必需）

```bash
export CNB_AGENTIC_MEMORY_TOKEN="<CNB API 令牌，需 repo-issue:rw + repo-code:r>"
export CNB_AGENTIC_MEMORY_REPO="<记忆仓库 slug，如 group/memory>"
# 可选：CNB_AGENTIC_MEMORY_BASE_URL（默认 https://api.cnb.cool）、CNB_AGENTIC_MEMORY_TIMEOUT（默认 30 秒）
```

## 命令

### 1. 写入记忆

```bash
cnb-agentic-memory write "PostgreSQL 分区表使用 pg_partman 按月分区，慢查询从 8s 降到 200ms" \
  --title "PostgreSQL 分区表 pg_partman" \
  --tag postgresql --tag 运维经验 \
  --category db
# → {"number": 12, "title": "PostgreSQL 分区表 pg_partman",
#    "parts": []}
```

- `--title`：提炼关键词短语，不传则兜底截取正文首行（质量会差，建议总是传）
- `--tag`：可多次传入，普通标签；单值内逗号会拆分为多标签（`--tag 'a,b'` 等价
  `--tag a --tag b`）。标签只允许汉字、字母、数字、_.: - / \\ 、全角符号及中间
  空格（首尾不能为空格，1~50 字符），不合法会在发起写入前直接报错
- `--category`：分类，自动补 `category:` 前缀
- 超长内容（>30KB）自动拆成多条，title 带 `(i/n)` 序号，输出 `parts` 含全部分片

### 2. 语义检索（找回记忆的主通道）

```bash
cnb-agentic-memory search "分区表 慢查询" --top-k 3
# → [{"score": 0.98, "number": 12, "title": "PostgreSQL 分区表 pg_partman",
#     "state": "open", "chunk": "…命中片段…"}]
```

- 返回按相关度排序，`state: closed` 的记忆默认被过滤（除非 `--include-closed`）
- 用 `number` 可进一步 `cnb-agentic-memory get <n>` 看全文
- 知识库未配置/不可用时命令会报错，错误信息提示改用 `cnb-agentic-memory keyword`

### 3. 关键词标题检索 / 按分类/标签浏览

**关键词标题检索**：仅匹配 title，无法检索正文，无需知识库——
title 含确切关键词（技术名词/编号/命令）时比语义检索更精准：

```bash
cnb-agentic-memory keyword "pg_partman" --limit 10
# → [{"number": 12, "title": "PostgreSQL 分区表 pg_partman", "body": null, ...}]
```

keyword 与 list 的结果**不回显正文**（body 为 null，是接口特性而非写入失败），
需要全文用 `get <编号>` 按需获取。

**按分类/标签浏览**（结构化过滤，与检索互补）：

```bash
cnb-agentic-memory list --category db --limit 10
cnb-agentic-memory list --tag postgresql
cnb-agentic-memory recent --limit 5
```

### 4. 读取 / 追加 / 更新

```bash
cnb-agentic-memory get 12                          # 精确读取全文
cnb-agentic-memory append 12 "补充：pg_partman 2.x 配置格式有变化"   # 追加更新记录
cnb-agentic-memory update 12 --content "$(cat 新正文.md)" --title "新标题"  # 全量替换正文
cnb-agentic-memory update 12 --tag 已验证          # 只追加标签
```

### 5. 软删除 / 恢复

```bash
cnb-agentic-memory delete 12      # → {"number": 12, "state": "closed"}
cnb-agentic-memory restore 12     # → {"number": 12, "state": "open"}
```

## 输出与错误

- 所有命令成功输出 JSON（`ensure_ascii=False`），可直接 `json.loads` 解析
- 错误输出到 stderr，退出码非 0：
  - 退出码 2：配置错误（缺 `CNB_AGENTIC_MEMORY_TOKEN` / `CNB_AGENTIC_MEMORY_REPO`，提示缺什么）
  - 退出码 1：API 错误（含状态码与 CNB 响应原文）/ 业务错误（如写入回读校验失败，含已落盘分片编号）
  - 退出码 70：内部错误（设置 `CNB_AGENTIC_MEMORY_DEBUG=1` 重试可看完整堆栈）
- **不要自行重试写操作**：写入失败时错误信息已携带每片体检结果与行动建议
  （如"#5 正文完整、标签缺失 → update 5 修正/补齐"），按建议执行即可。
  恢复优先级：update > append > delete（最后手段）；重复 write 会重复创建
  并永久污染知识库向量

## 常见任务示例

```text
用户：记住这个：我们的 CI 用的是 cnb 流水线，配置在 .cnb.yml
Agent：cnb-agentic-memory write "CI 使用 cnb 流水线，配置文件 .cnb.yml，PR 触发测试、tag 触发发布" \
        --title "CI cnb 流水线配置" --tag ci --category 基础设施

用户：上次数据库慢查询是怎么解决的？
Agent：cnb-agentic-memory search "数据库 慢查询 解决"

用户：把这条经验也补充到刚才那条记忆里
Agent：cnb-agentic-memory append <编号> "补充内容……"    # 先 search/get 找到编号

用户：看看 db 分类下都有哪些经验
Agent：cnb-agentic-memory list --category db --limit 20

用户：那条记忆过时了，删掉吧
Agent：cnb-agentic-memory delete <编号>                  # 软删除，可恢复
```
