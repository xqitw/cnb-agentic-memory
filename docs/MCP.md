# MCP Server 参考

`cam-mcp` 把记忆语义层注册为 MCP 工具，供任何 MCP 客户端（Claude Desktop、CNB AI 助手等）调用。业务逻辑（两步写入、回读校验、title 不变量、超长拆分、软删除）全部在 SDK 层，MCP 是纯适配层。

## 安装与配置

```bash
pip install "cnb-agentic-memory[mcp]"
```

配置走 `CAM_` 前缀环境变量（与 SDK/CLI 一致）：

| 环境变量 | 说明 |
| --- | --- |
| `CAM_TOKEN` | CNB API Token（需 `repo-issue:rw` + `repo-code:r`） |
| `CAM_REPO` | 记忆仓库 slug，如 `group/memory` |
| `CAM_BASE_URL` | API 地址，默认 `https://api.cnb.cool` |
| `CAM_TIMEOUT` | 请求超时秒数，默认 30 |

## 客户端接入

**推荐：uvx 方式运行**（无需预装，uv 自动拉取包并执行）：

```json
{
  "mcpServers": {
    "cam": {
      "command": "uvx",
      "args": [
        "--from",
        "cnb-agentic-memory[mcp]",
        "cam-mcp"
      ],
      "env": {
        "CAM_TOKEN": "<token>",
        "CAM_REPO": "group/memory"
      }
    }
  }
}
```

已安装包的环境也可直接用入口命令：

```json
{
  "mcpServers": {
    "cam": {
      "command": "cam-mcp",
      "env": {
        "CAM_TOKEN": "<token>",
        "CAM_REPO": "group/memory"
      }
    }
  }
}
```

## 工具清单（10 个）

| 工具 | 对应 SDK 方法 | 说明 |
| --- | --- | --- |
| `memory_write` | `Memory.write` | 写入记忆。**title 由智能体撰写：提炼 3~8 个高区分度关键词短语**（keyword 检索只匹配标题）；超长自动拆分，返回 `parts` 含全部分片 |
| `memory_get` | `Memory.get` | 按编号读取记忆原文 |
| `memory_update` | `Memory.update` | 更新记忆。`content` 为**全量替换**；追加内容用 `memory_append` |
| `memory_append` | `Memory.append` | 追加更新记录（进知识库可被语义检索） |
| `memory_delete` | `Memory.delete` | 软删除记忆（可 `memory_restore` 恢复） |
| `memory_restore` | `Memory.restore` | 恢复软删除的记忆 |
| `memory_list` | `Memory.list` | 按分类/标签过滤列表（`state` 仅支持 `open/closed`） |
| `memory_list_recent` | `Memory.list_recent` | 最近更新的记忆 |
| `memory_search` | `Memory.search` | 语义检索（知识库召回 + 回读补齐元信息，默认过滤已删除） |
| `memory_keyword_search` | `Memory.keyword_search` | 关键词标题检索（仅匹配标题；title 含确切关键词时更精准） |

## 使用指导（写给调用智能体）

- **写入时务必写好 title**：keyword 标题检索只匹配 title，它是主检索通道（知识库）故障时唯一的找回途径。好的 title 是「高区分度关键词的短语」，不是句子
- **检索按需选路**：按内容模糊查找用 `memory_search`（语义召回 0.98+）；
  title 含确切关键词（技术名词/编号/命令）用 `memory_keyword_search` 更精准
- **`memory_update` 的 content 是全量替换**：只想追加信息时用 `memory_append`
- **`memory_delete` 是软删除**：可随时 `memory_restore` 恢复，不要害怕删错
- **错误处理**：工具返回的错误文本携带 CNB 原始信息（状态码/原因），请据此自行决策重试、换参数或放弃；仓库未配置知识库流水线时 `memory_search` 会失败并提示降级

## 记忆仓库前置条件

`memory_search` 依赖仓库配置 Issue 事件同步流水线（`.cnb.yml` 的 `$` 键，见 [SDK 参考](API.md#记忆仓库前置条件)），且**先配置流水线再写入**——错过事件的记忆不会被补录。写入到可检索约 1~2 分钟。
