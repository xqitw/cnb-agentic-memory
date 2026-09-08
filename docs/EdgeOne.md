# EdgeOne Pages 部署指南

以腾讯云 EdgeOne Pages（EdgeOne Makers）Python 运行时承载本项目的 MCP 服务器，streamable-http 形态，核心代码零改动、仅薄适配层。

## 形态决策

- **streamable-http + `json_response` + `stateless_http`**：EO Cloud Functions 执行时长上限 120s（`edgeone.json` 已配 `cloudFunctions.maxDuration`），SSE 长连接与有状态 session 会被平台切断，不适配
- 适配层：`cloud-functions/mcp/[[default]].py`——EO 约定 `cloud-functions/` 目录文件即路由，`[[default]].py` 为 catch-all；EO 运行时剥掉函数目录前缀后把请求交给应用内挂载的 MCP ASGI 应用
- Python 运行时为 3.10（平台硬编码），本项目 `requires-python >= 3.10` 与之兼容

## 部署步骤

1. EO 项目指向本仓库根（构建时自动扫描 `cloud-functions/`）
2. EO 控制台配置环境变量（值均须 ≤500 字节）：

   | 环境变量 | 必填 | 说明 |
   | --- | --- | --- |
   | `CNB_AGENTIC_MEMORY_REQUIRE_HEADERS` | 共享部署必填 | 置 `1` 强制凭据头：请求须带 `X-CNB-Token`/`X-CNB-Repo`（凭据由调用方传递），匿名请求在工具入口即拒绝。**服务端禁止配置 `CNB_AGENTIC_MEMORY_TOKEN`/`CNB_AGENTIC_MEMORY_REPO`**——服务端持凭据 + 漏配本开关时门禁形同虚设 |

   注：EO 会把转发请求的 Host 头改写为平台内部源站域名（动态不可预知），应用层 Host/Origin 白名单不可行；恶意 Host 由 EO 边缘按路由键直接拒绝（未绑定域名 418），匿名与跨源滥用由凭据头门禁阻断，服务端无需配置域名类变量。

3. 部署，二选一：
   - **web 触发**（推荐）：CNB web 页面「一键部署」按钮（声明见 `.cnb/web_trigger.yml`），支持选择生产/预览环境与项目名
   - 本地：`npx edgeone pages deploy`
4. MCP 客户端连接 `https://<对外域名>/mcp`（streamable-http 形态），请求须携带 `X-CNB-Token` 与 `X-CNB-Repo` 请求头（多用户各自传递自己的凭据）

## 依赖说明

`cloud-functions/requirements.txt` 显式声明依赖（用户声明优先级最高，压过 import 自动检测），钉 PyPI 已发布版本。

**发版时序**：升级依赖钉版前须先完成 PyPI 发版（合并 → 打 tag → CI 发布），否则 EO 构建解析失败。

## 已知限制

- 执行时长上限 120s，工具调用须在该窗口内完成；实例回收后冷启动有 import 与连接重建延迟
- 默认域名 `*.edgeone.app` 实测函数路由 404，请绑定自定义域名访问
