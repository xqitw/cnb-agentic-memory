# SDK API 参考

`cnb-agentic-memory` 的 SDK 层是所有形态（CLI / MCP / Skill）的基座：架构红线（两步写入、写后回读校验、title 不变量、软删除）全部沉淀在此层，上层只是薄封装。

```python
from cnb_agentic_memory import CNBApiClient, Memory

async with CNBApiClient(token="...", repo="group/memory") as client:
    memory = Memory(client)
    result = await memory.write(
        "PostgreSQL 分区表使用 pg_partman 解决慢查询",
        title="PostgreSQL 分区表 pg_partman",
    )
    print(result.number, result.title)
```

## 配置

配置优先级：**显式参数 > `CNB_AGENTIC_MEMORY_` 前缀环境变量 > 默认值**。环境变量在 `CNBApiClient` 构造时由 `api.env()` 读取（非法 `TIMEOUT` 回落默认值），SDK / CLI / MCP 各形态行为一致。

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `CNB_AGENTIC_MEMORY_TOKEN` | CNB API Token，需 `repo-issue:rw`（Issue 读写）+ `repo-code:r`（知识库检索） | 无（必填） |
| `CNB_AGENTIC_MEMORY_REPO` | 记忆仓库 slug，如 `group/memory` | 无（必填） |
| `CNB_AGENTIC_MEMORY_BASE_URL` | CNB Open API 地址 | `https://api.cnb.cool` |
| `CNB_AGENTIC_MEMORY_TIMEOUT` | 请求超时秒数 | `30` |
| `CNB_AGENTIC_MEMORY_DEBUG` | 设为 1 时未预期异常抛出完整堆栈（默认友好一行，退出码 70） | 未设置 |

```python
# 三种等价写法
CNBApiClient(token="t", repo="g/r")  # 显式参数
CNBApiClient()  # 全走 CNB_AGENTIC_MEMORY_ 环境变量
CNBApiClient(token="t", timeout=10)  # 混合：参数覆盖对应环境变量
```

## 错误处理

SDK 不做重试/限流——调用方（智能体）收到错误后自行决策。错误路径：`CNB 响应 → ApiError(原文) → MCP/CLI 透传`，SDK 全程不吞错、不包装语义。

| 异常 | 含义 | 常见场景 |
| --- | --- | --- |
| `cnb_agentic_memory.ApiError` | CNB API 非 2xx，`status_code` + `message`（响应体原文） | 404 记忆不存在、401 token 无效 |
| `cnb_agentic_memory.MemoryError` | 记忆业务规则失败（在 ApiError 之上） | 内容为空、写后回读校验不一致、知识库检索失败 |

```python
from cnb_agentic_memory import ApiError, MemoryError

try:
    await memory.write(content)
except MemoryError as err:
    ...  # 业务规则失败，含回读校验失败
except ApiError as err:
    ...  # err.status_code / err.message 为 CNB 响应原文
```

## cnb_agentic_memory.api — CNB API 薄封装

`CNBApiClient` 封装 8 个端点，纯 CRUD 语义，不加记忆业务规则。全部方法为 `async`。

| 方法 | 端点 | 说明 |
| --- | --- | --- |
| `create_issue(form)` | `POST /{-}/issues` | 创建 Issue；标签必须走 `add_labels` 两步写入 |
| `add_labels(number, labels)` | `POST /{-}/issues/{n}/labels` | 补打标签，可自动创建不存在的标签 |
| `get_issue(number)` | `GET /{-}/issues/{n}` | 读取 Issue |
| `update_issue(number, form)` | `PATCH /{-}/issues/{n}` | 更新 Issue，`None` 字段不发送 |
| `list_issues(...)` | `GET /{-}/issues` | 列表；`keyword` 只匹配标题，分页上限 100/页 |
| `create_comment(number, form)` | `POST /{-}/issues/{n}/comments` | 追加评论 |
| `list_comments(number)` | `GET /{-}/issues/{n}/comments` | 评论列表 |
| `query_knowledge_base(query, top_k)` | `GET /{-}/knowledge/base/query` | 语义检索（主检索通道） |

> 实测约束：所有请求自动携带 `Accept: application/json`，缺失时 CNB 返回 406。

## cnb_agentic_memory.memory — 记忆语义层

`Memory(client)` 在 API 层之上实现记忆业务规则。一记忆 = 一 Issue，`number` 即记忆唯一标识。

| 方法 | 说明 |
| --- | --- |
| `write(content, *, title, tags, category, verify)` | 写入记忆：创建 Issue → 补打标签（两步写入）→ 回读校验。返回 `WriteResult(number, title, parts)` |
| `update(number, *, content, title, tags, category, verify)` | 更新正文/标题/标签，回读校验。`content` 为全量替换；`title` 传纯空白视为未提供而忽略 |
| `append(number, note, *, verify)` | 追加更新记录（评论），进知识库可被语义检索 |
| `delete(number, *, verify)` | 软删除（`state=closed` + `not_planned`），可恢复 |
| `restore(number)` | 恢复软删除的记忆（reopen） |
| `get(number)` | 精确读取记忆原文 |
| `list(*, category, tags, state, limit)` | 按分类/标签过滤列表（结构化过滤，与语义检索解耦） |
| `list_recent(limit=5)` | 最近更新的记忆 |
| `keyword_search(query, *, limit, include_closed)` | 关键词标题检索：仅匹配标题（CNB keyword 检索特性），无需知识库；`include_closed=True` 时 open/closed 各查一次合并去重，按 `updated_at` 降序 |
| `search(query, *, top_k, include_closed)` | 语义检索：知识库召回 → 解析 `number` → 回读补齐元信息。知识库不可用时抛 `MemoryError`，错误信息提示可改用 `keyword_search` |

设计约定：

- **title 撰写权在调用方**：keyword 检索只匹配标题，title 由智能体撰写（提炼高区分度关键词短语）；未提供时兜底为正文首行截取。工具保证不变量：无控制字符（C0/DEL，实测服务端不拦但会污染检索与知识库）+ 非空 + ≤60 字符。MCP 与 Skill 层需在工具描述中给智能体明确的 title 撰写指导
- **标签字符白名单（实测沉淀，cam-test #78-#81）**：CNB 标签只允许汉字、字母、数字、下划线(_)、小数点(.)、冒号(:)、中划线(-)、正斜杠(/)、反斜杠(\\)、全角字符（U+FF00-FFEF 等宽字符）与中间空格（首尾不能为空格）；长度按 **UTF-8 字节**计数，上限 50 字节（1 汉字/全角字符占 3 字节）。注意："全角符号"不含省略号 …(U+2026) 与破折号 —(U+2014)，实测被服务端拒绝。写路径预检（fail-fast）在发起任何写请求前校验，不合法直接报错，避免孤儿分片
- **两步写入**：创建时不传 labels（服务端对新标签静默丢弃），创建后单独补打
- **写后回读校验**：写操作 GET 回读确认，短重试（3 次 × 0.5s）后仍不一致才报错；`verify=False` 可跳过（仅测试）
- **超长拆分**：正文超过 30KB 自动按段落拆为多条，title 带 `(i/n)` 序号关联；`WriteResult.parts` 携带全部分片供循迹
- **软删除**：无硬删除接口（CNB DELETE 返回 404），`delete` 后可 `restore`
- **标签约定**：`category` 自动补 `category:` 前缀（对齐 CNB 平台分类约定，选择器中单选），`tags` 为普通标签原样保留；记忆仓库须为专用仓库（全部 Issue 均为记忆）
- **检索分层**：语义检索走知识库向量召回（相关度受语料规模、查询内容与切分策略影响，PoC 实测样例中可达 0.98+）；`keyword_search` 是与它并列的第二检索方法（标题检索，无需知识库），供 title 含确切关键词时精准直达
- **分页参数钳制**：limit / top_k 统一钳制到 1~100（CNB 服务端分页上限），SDK 与 CLI / MCP 入口层口径一致

## cnb_agentic_memory.models — 数据模型

字段以「是否参与记忆的生命周期或检索语义」为准入，CNB 展示层字段（优先级/颜色/作者/计数等）不收。`extra="ignore"` 宽容上游新增字段。

| 模型 | 字段 |
| --- | --- |
| `Issue` | `number`（唯一标识）/ `title` / `body` / `state` / `labels` / `created_at` / `updated_at` |
| `Comment` | `id` / `body` / `created_at` |
| `WriteResult` | `number` / `title`；`parts`（全部分片，单分片为空元组） |
| `SearchResult` | `score` / `chunk` / `number` / `title` / `state` |
| `KbChunk` | `score` / `chunk` / `metadata`；`number` 属性从 `metadata.path` 解析 |

各字段语义见模型内 `Field(description=...)`——它是 P2 CLI 帮助文本与 P3 MCP 参数 Schema 的单一来源。

## 记忆仓库前置条件

**记忆仓库须为专用仓库**：仓库中全部 Issue 均为记忆，不与普通 Issue 混用（检索与列表不做记忆/非记忆区分）。

知识库检索依赖仓库已配置 Issue 事件同步流水线。`.cnb.yml` 事件必须挂在 `$` 键下（顶层写法静默无效），且**先配置流水线再写入记忆**——错过事件的 Issue 不会被补录：

```yaml
$:
  issue.open:
    - stages:
        - name: sync kb on issue open
          type: knowledge:update
          options:
            issueSyncEnabled: true
  issue.update:
    - stages:
        - name: sync kb on issue update
          type: knowledge:update
          options:
            issueSyncEnabled: true
  issue.comment:
    - stages:
        - name: sync kb on issue comment
          type: knowledge:update
          options:
            issueSyncEnabled: true
  issue.reopen:
    - stages:
        - name: sync kb on issue reopen
          type: knowledge:update
          options:
            issueSyncEnabled: true
  issue.close:
    - stages:
        - name: sync kb on issue close
          type: knowledge:update
          options:
            issueSyncEnabled: true
```

写入到可检索有 1~2 分钟同步时延，属平台预期行为。
