# EdgeOne Pages 部署指南

以腾讯云 EdgeOne Pages（EdgeOne Makers）Python 运行时承载本项目的 MCP 服务器，streamable-http 形态，核心代码零改动、仅薄适配层。追踪 Issue：#91。

## 形态决策

- **streamable-http + `json_response`**：EO Cloud Functions 执行时长上限 120s（`edgeone.json` 已配 `cloudFunctions.maxDuration`），SSE 长连接会被平台切断，不适配
- **`stateless_http`**：Serverless 实例随时回收，有状态 session（session id）跨实例失效即断连，无状态模式才匹配短执行模型
- 适配层：`cloud-functions/mcp/[[default]].py`——EO 约定 `cloud-functions/` 目录文件即路由，`[[default]].py` 为 catch-all；EO 运行时剥掉函数目录前缀后把请求交给应用内挂载的 MCP ASGI 应用

## 部署步骤

1. EO 项目指向本仓库根（构建时自动扫描 `cloud-functions/`）
2. EO 控制台配置环境变量（值均须 ≤500 字节）：

   | 环境变量 | 必填 | 说明 |
   | --- | --- | --- |
   | `CNB_AGENTIC_MEMORY_TOKEN` | 是 | 服务端兜底凭据（CNB token） |
   | `CNB_AGENTIC_MEMORY_REPO` | 是 | 兜底记忆仓库（`组织/仓库`） |
   | `CNB_AGENTIC_MEMORY_REQUIRE_HEADERS` | 共享部署必填 | 置 `1` 强制凭据头：请求须带 `X-CNB-Token`/`X-CNB-Repo`，防匿名调用间接使用服务端凭据 |
   | `CNB_AGENTIC_MEMORY_MCP_PUBLIC_HOST` | 生产必填 | 对外域名基名（如 `my-project.edgeone.app`），作 DNS rebinding 防护白名单；未设置时防护不启用并打启动警告，仅限测试部署 |

3. 部署，二选一：
   - **web 触发**（推荐）：CNB web 页面「一键部署」按钮（声明见 `.cnb/web_trigger.yml`），支持选择生产/预览环境与项目名
   - 本地：`npx edgeone pages deploy`
4. MCP 客户端连接 `https://<project>.edgeone.app/mcp`（streamable-http 形态）

## 依赖说明

`cloud-functions/requirements.txt` 显式声明依赖（用户声明优先级最高，压过 import 自动检测）。当前 `cnb-agentic-memory` 以 git URL 引用——#91 新增的 `build_transport_security` 尚未发布到 PyPI，发版后应改钉版本号。

## 待实测风险点（结论回填 #91）

- [ ] Python 运行时具体版本（本项目 `requires-python >= 3.11`，EO 官方文档仅标 3.9+）
- [ ] EO 转发后 Host/Origin 头实际形态与白名单匹配（适配层按「无端口对外域名 + https Origin」生成）
- [ ] EO 运行时是否执行 ASGI lifespan：session manager 启动挂在外层 FastAPI lifespan 上（适配层已按 SDK 官方挂载模式手动进入子应用 `lifespan_context`），若平台不跑 lifespan 则启动即 500
- [ ] `requirements.txt` 的 git URL 引用在 EO 构建环境可安装性（需 git 且可匿名克隆 CNB 公开仓库）
- [ ] `maxDuration=120` 实际生效情况与冷启动延迟（实例回收后 import + 连接池重建）
