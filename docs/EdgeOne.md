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
   | `CNB_AGENTIC_MEMORY_REQUIRE_HEADERS` | 共享部署必填 | 置 `1` 强制凭据头：请求须带 `X-CNB-Token`/`X-CNB-Repo`（凭据由调用方传递），匿名请求在工具入口即拒绝。**服务端禁止配置 `CNB_AGENTIC_MEMORY_TOKEN`/`CNB_AGENTIC_MEMORY_REPO`**——服务端持凭据 + 漏配本开关时门禁形同虚设 |

   **Host/Origin 传输层校验在 EO 形态下不启用**（防护职责分层）：恶意 Host 在 EO 边缘即被拒（Host 是平台路由键，未绑定域名 418）；函数收到的 Host 被改写为平台内部源站域名（动态不可预知），SDK 白名单无法稳定配置且 SDK 无「只校验 Origin」粒度；匿名/跨源滥用的实质阻断由 `REQUIRE_HEADERS` 门禁承担（服务端零凭据，无凭据头请求无资产可碰）。

3. 部署，二选一：
   - **web 触发**（推荐）：CNB web 页面「一键部署」按钮（声明见 `.cnb/web_trigger.yml`），支持选择生产/预览环境与项目名
   - 本地：`npx edgeone pages deploy`
4. MCP 客户端连接 `https://<project>.edgeone.app/mcp`（streamable-http 形态）

## 依赖说明

`cloud-functions/requirements.txt` 显式声明依赖（用户声明优先级最高，压过 import 自动检测），钉 `cnb-agentic-memory[mcp]==2.0.4`。**时序约束：合并后先打 tag `v2.0.4` 触发 CI 发 PyPI，发版成功后才可触发 EO 部署**（PyPI 无 2.0.4 时构建解析失败）；升版时须同步钉号。

## 待实测风险点（结论回填 #91）

- [x] Python 运行时具体版本：**3.10 硬编码**（实测构建日志 + 官方文档「Python 版本 | 3.10」；uv 强制 `--python-version 3.10` 解析）——项目 requires-python 已随之降为 >=3.10
- [x] EO 转发后 Host/Origin 头实际形态与白名单匹配：**Host 被改写为平台内部源站域名**（`pages-*.qcloudteo.com` 形态，动态不可预知），Origin 原样透传，X-Forwarded-* 为空——SDK Host/Origin 校验在该形态下无稳定白名单可配且无防御增量（边缘路由 + REQUIRE_HEADERS 分层防护），适配层不启用
- [x] EO 运行时执行 ASGI lifespan：**执行**——真实部署 POST ping 200（session manager 须经 lifespan 启动才可处理请求）
- [x] `requirements.txt` 的 git URL 引用在 EO 构建环境可安装性：**可行**（uv 克隆 CNB 公开仓库成功）
- [x] 自定义域名接入：EO 后台提示 CNAME → DNSPod 添加（主机记录 `cam` / 类型 `CNAME` / 记录值 `cam.xqitw.cool.pages.dnsoe6.com`），证书自动签发，分钟级生效
- [ ] `maxDuration=120` 实际生效情况与冷启动延迟（实例回收后 import + 连接池重建）
- 备注：默认域名 `*.edgeone.app` 实测全 404（自定义域名正常），疑似部署环境绑定差异，未深究
