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
   | `CNB_AGENTIC_MEMORY_REQUIRE_HEADERS` | 共享部署必填 | 置 `1` 强制凭据头：请求须带 `X-CNB-Token`/`X-CNB-Repo`（凭据由调用方传递，服务端不持有），防匿名调用 |
   | `CNB_AGENTIC_MEMORY_MCP_PUBLIC_HOST` | 生产必填 | 对外域名基名（如 `cam.xqitw.cool`），作 Origin 白名单（实测 Origin 原样透传） |
   | `CNB_AGENTIC_MEMORY_MCP_INTERNAL_HOST` | 生产必填 | EO 内部源站域名（实测 Host 头被改写为 `pages-*.qcloudteo.com` 形态），作 Host 白名单；平台变更该域名时须同步更新（表现为请求 421） |

   两 HOST 变量缺任一则防护不启用并打启动警告（仅限测试部署）。恶意 Host 在 EO 边缘即被拒（Host 是平台路由键，未绑定域名 418），函数内 Host 校验锁内部源站、Origin 校验防浏览器跨源。

3. 部署，二选一：
   - **web 触发**（推荐）：CNB web 页面「一键部署」按钮（声明见 `.cnb/web_trigger.yml`），支持选择生产/预览环境与项目名
   - 本地：`npx edgeone pages deploy`
4. MCP 客户端连接 `https://<project>.edgeone.app/mcp`（streamable-http 形态）

## 依赖说明

`cloud-functions/requirements.txt` 显式声明依赖（用户声明优先级最高，压过 import 自动检测）。当前 `cnb-agentic-memory` 以 git URL 引用——#91 新增的 `build_transport_security` 尚未发布到 PyPI，发版后应改钉版本号。

## 待实测风险点（结论回填 #91）

- [x] Python 运行时具体版本：**3.10 硬编码**（实测构建日志 + 官方文档「Python 版本 | 3.10」；uv 强制 `--python-version 3.10` 解析）——项目 requires-python 已随之降为 >=3.10
- [x] EO 转发后 Host/Origin 头实际形态与白名单匹配：**Host 被改写为内部源站域名**（`pages-*.qcloudteo.com`），Origin 原样透传对外域名，X-Forwarded-* 为空——Host/Origin 基名须分离配置（`MCP_INTERNAL_HOST`/`MCP_PUBLIC_HOST`），恶意 Origin 403、合法 Origin 200 已实测
- [x] EO 运行时执行 ASGI lifespan：**执行**——真实部署 POST ping 200（session manager 须经 lifespan 启动才可处理请求）
- [x] `requirements.txt` 的 git URL 引用在 EO 构建环境可安装性：**可行**（uv 克隆 CNB 公开仓库成功）
- [x] 自定义域名接入：EO 后台提示 CNAME → DNSPod 添加（主机记录 `cam` / 类型 `CNAME` / 记录值 `cam.xqitw.cool.pages.dnsoe6.com`），证书自动签发，分钟级生效
- [ ] `maxDuration=120` 实际生效情况与冷启动延迟（实例回收后 import + 连接池重建）
- 备注：默认域名 `*.edgeone.app` 实测全 404（自定义域名正常），疑似部署环境绑定差异，未深究
