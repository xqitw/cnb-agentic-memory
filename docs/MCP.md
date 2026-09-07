# MCP Server 参考

`cnb-agentic-memory-mcp` 把记忆语义层注册为 MCP 工具，供任何 MCP 客户端（Claude Desktop、CNB AI 助手等）调用。业务逻辑（两步写入、回读校验、title 不变量、超长拆分、软删除）全部在 SDK 层，MCP 是纯适配层。

## 安装与配置

```bash
pip install "cnb-agentic-memory[mcp]"
```

配置走 `CNB_AGENTIC_MEMORY_` 前缀环境变量（与 SDK/CLI 一致）：

| 环境变量 | 说明 |
| --- | --- |
| `CNB_AGENTIC_MEMORY_TOKEN` | CNB API Token（需 `repo-issue:rw` + `repo-code:r`） |
| `CNB_AGENTIC_MEMORY_REPO` | 记忆仓库 slug，如 `group/memory` |
| `CNB_AGENTIC_MEMORY_BASE_URL` | API 地址，默认 `https://api.cnb.cool` |
| `CNB_AGENTIC_MEMORY_TIMEOUT` | 请求超时秒数，默认 30 |

### 请求头覆盖（多用户共享部署）

HTTP transport（`streamable-http`/`sse`）模式下，各工具在**每次调用时**读取以下请求头，可逐请求覆盖 token/repo 等配置，实现多用户共用一个 MCP 服务实例、各用各的凭据与仓库、互不影响：

| 请求头 | 覆盖的环境变量 | 说明 |
| --- | --- | --- |
| `X-CNB-Token` | `CNB_AGENTIC_MEMORY_TOKEN` | 调用方自己的 CNB API Token |
| `X-CNB-Repo` | `CNB_AGENTIC_MEMORY_REPO` | 调用方自己的记忆仓库 slug |
| `X-CNB-Base-URL` | `CNB_AGENTIC_MEMORY_BASE_URL` | API 地址（私有化部署场景） |

- 头名大小写不敏感；空值/空白视为未提供；重复同名头取首值
- **安全约定（全有或全无）**：`X-CNB-Token` 与 `X-CNB-Repo` 必须同时出现才启用头覆盖，否则全部头忽略、整体回落环境变量——防止调用方只改 `X-CNB-Base-URL` 时，服务端环境变量的凭据被发送到调用方指定的任意主机
- 未携带头或凭据不齐时回落环境变量（与 stdio 行为一致）；stdio 下无请求头，永远走环境变量
- MCP 框架的 stdio 客户端（Claude Desktop 等）不支持自定义请求头，此类客户端沿用环境变量配置

> 安全提示：凭据经由请求头传输，请务必在 HTTPS/反向代理之后暴露服务，避免明文网络截获；头中的 Token 是调用方自己的凭据，服务端仅透传给 CNB API 用于访问对应仓库，不做存储。

## 传输协议（transport）

支持三种 MCP 传输协议，通过 CLI 参数或环境变量选择（CLI 参数优先）：

| transport | 启动方式 | 端点 | 适用场景 |
| --- | --- | --- | --- |
| `stdio`（默认） | 无参数，客户端以子进程拉起 | 标准输入/输出 | 本地客户端（Claude Desktop、CNB AI 助手等） |
| `streamable-http` | `--transport streamable-http` | `http://<host>:<port>/mcp` | 远程/共享接入（推荐） |
| `sse` | `--transport sse` | `http://<host>:<port>/sse`（消息回传 `/messages/`） | 仅支持旧版 SSE 的远程客户端 |

```bash
# stdio（默认，历史行为不变）
cnb-agentic-memory-mcp

# streamable-http：监听 0.0.0.0:8000，端点 /mcp
cnb-agentic-memory-mcp --transport streamable-http --host 0.0.0.0 --port 8000

# sse：监听 0.0.0.0:8000，端点 /sse
cnb-agentic-memory-mcp --transport sse --host 0.0.0.0 --port 8000
```

| 参数 | 环境变量兜底 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--transport` | `CNB_AGENTIC_MEMORY_MCP_TRANSPORT` | `stdio` | 传输协议：`stdio` / `sse` / `streamable-http` |
| `--host` | `CNB_AGENTIC_MEMORY_MCP_HOST` | `127.0.0.1` | HTTP 监听地址，仅 sse/streamable-http 有效；接受域名 / IPv4 / IPv6 / `[IPv6]`；空值/空白回落默认；CLI 畸形地址直接报错，env 畸形值告警回落默认；不接受 `host:port` 与 `[IPv6]:port` 合并形态（端口由 `--port` 指定）；对外暴露时用 `0.0.0.0`（须置于反代之后） |
| `--port` | `CNB_AGENTIC_MEMORY_MCP_PORT` | `8000` | HTTP 监听端口，仅 sse/streamable-http 有效；CLI 传非法/越界值直接报错退出（环境变量异常值静默回落默认） |
| `--allowed-host` | `CNB_AGENTIC_MEMORY_MCP_ALLOWED_HOSTS` | 无 | DNS rebinding 防护额外放行的 Host 白名单（可多次传入或逗号分隔）。反代按最佳实践保留真实 Host（`proxy_set_header Host $host`）部署时，须把对外域名加入白名单，否则会被 421 拒绝；输入支持纯域名 / `host:port` / `host:*` / `[IPv6]` 形态（可多次传入或单值内逗号分隔；`ipaddress` 判别归一化，IPv6 自动裹方括号；畸形条目如端口段非数字会 stderr 告警并跳过）；每个域名自动同时生成 `:*` 端口通配（非标准端口兜底）与无端口精确（默认 443/80 下浏览器不序列化端口）两种 Host 条目，并放行 http/https 双 scheme Origin |

> 安全提示：HTTP transport 无内置鉴权，务必配合反向代理/网关做访问控制与
> Token 校验后再对外暴露，避免 `CNB_AGENTIC_MEMORY_TOKEN` 凭据被任意调用方
> 间接使用。

## 客户端接入

**stdio（本地子进程）——推荐：uvx 方式运行**（无需预装，uv 自动拉取包并执行）。`--from` 用于声明 `[mcp]` extra（MCP 依赖在 extra 中，无法随默认安装带上）：

```json
{
  "mcpServers": {
    "cnb-agentic-memory": {
      "command": "uvx",
      "args": [
        "--from",
        "cnb-agentic-memory[mcp]",
        "cnb-agentic-memory-mcp"
      ],
      "env": {
        "CNB_AGENTIC_MEMORY_TOKEN": "<token>",
        "CNB_AGENTIC_MEMORY_REPO": "group/memory"
      }
    }
  }
}
```

已安装包的环境也可直接用入口命令：

```json
{
  "mcpServers": {
    "cnb-agentic-memory": {
      "command": "cnb-agentic-memory-mcp",
      "env": {
        "CNB_AGENTIC_MEMORY_TOKEN": "<token>",
        "CNB_AGENTIC_MEMORY_REPO": "group/memory"
      }
    }
  }
}
```

**streamable-http（远程/多用户共享接入，推荐）**：服务端先以 HTTP transport 启动，客户端按 URL 接入；凭据与仓库可经请求头逐请求携带（见上文「请求头覆盖」），无需在服务端配置：

```bash
# 注意：0.0.0.0 为通配监听，必须置于反向代理/网关之后（访问控制 + HTTPS）再对外暴露
# 启动时若监听通配地址，服务会向 stderr 打印提醒
cnb-agentic-memory-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

客户端配置示例（`headers` 字段为 Cursor/VS Code/Claude Code 等主流 MCP 客户端通用写法，随每次工具调用发送）：

```json
{
  "mcpServers": {
    "cnb-agentic-memory": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "X-CNB-Token": "<调用方自己的token>",
        "X-CNB-Repo": "group/memory"
      }
    }
  }
}
```

同一服务实例上不同用户各配各的 `headers`（token 与 repo 都可以不同），互不影响；不配置 `headers` 的客户端则使用服务端环境变量。

用 Python MCP 客户端接入时，通过自定义 `httpx2.AsyncClient` 携带请求头（示例已实测）：

```python
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

headers = {"X-CNB-Token": "<token>", "X-CNB-Repo": "group/memory"}
async with httpx2.AsyncClient(headers=headers) as http_client:
    async with streamable_http_client("http://127.0.0.1:8000/mcp", http_client=http_client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("memory_get", {"number": 1})
```

原始 HTTP 调用（curl 等）同理：`X-CNB-*` 就是普通 HTTP 请求头，按 MCP 协议流程
（initialize 获取 `Mcp-Session-Id` 后携带调用）发送即可。

**sse（旧版远程客户端）**：服务端以 `--transport sse` 启动，客户端 URL 为 `http://<host>:<port>/sse`，headers 配置方式与 streamable-http 相同。

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

- **写入时务必写好 title**：keyword 标题检索只匹配 title，它是无需知识库的独立检索通道（`memory_keyword_search`）。好的 title 是「高区分度关键词的短语」，不是句子
- **检索按需选路**：按内容模糊查找用 `memory_search`（PoC 实测样例中相关度可达 0.98+）；
  title 含确切关键词（技术名词/编号/命令）用 `memory_keyword_search` 更精准
- **`memory_update` 的 content 是全量替换**：只想追加信息时用 `memory_append`
- **`memory_delete` 是软删除**：可随时 `memory_restore` 恢复；仅从默认检索与
  列表中隐藏，内容仍留在知识库向量中（`include_closed` 可召回），不是内容
  清除。修正/补充记忆请用 `memory_update`，删除仅用于真正废弃
- **错误处理**：工具返回的错误文本携带 CNB 原始信息（状态码/原因），请据此自行决策重试、换参数或放弃；仓库未配置知识库流水线时 `memory_search` 会失败，错误文本提示可改用 `memory_keyword_search`（标题检索，无需知识库）

## 记忆仓库前置条件

`memory_search` 依赖仓库配置 Issue 事件同步流水线（`.cnb.yml` 的 `$` 键，见 [SDK 参考](API.md#记忆仓库前置条件)），且**先配置流水线再写入**——错过事件的记忆不会被补录。写入到可检索的时延取决于流水线配置：事件触发（实时）通常秒级到分钟级，定时入库（如每小时/每日）需等下次运行，还受网络、记忆数量影响；需立即确认时用 `memory_get` 按 number 回查。
