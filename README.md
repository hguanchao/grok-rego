# grok-rego

xAI Grok Build 账号自动化工具：**批量注册 → Device Flow 授权 → Token 交换 → 号池管理 → G2A / CPA 推送** 的全链路流水线，配套 Web 管理界面。

> 原名 grok2build，项目重构后更名为 grok-rego。

## 功能特性

- **批量注册**：多线程并发注册，支持 Cloudflare / Yyds 邮箱服务与代理配置
- **全局仿真人**：注册链路全量拟人化（贝塞尔鼠标轨迹、拟人点击与键入、空闲微动），降低被风控识别概率
- **Device Flow 授权**：浏览器端 OAuth Device Flow，自动完成 Token 交换
- **SSO 会话**：注册附带的会话凭证入库，供下游渠道使用
- **号池管理**：账号列表、状态、筛选与巡检，后台固定周期扫描临期账号并自动续期
- **推送**：
  - **G2A**：仅推 Web 池（`/api/admin/v1/accounts/web/import`），HTTP 2xx 即视为推送成功，不等待落库同步
  - **CPA**：auth-files 批量上传（单请求合批）
- **管理 API + Web UI**：账号注册、号池管理、推送、网关运维与用量统计一体的可视化界面
- **OpenCode Zen 网关**：本机 `/v1` 透明反代 `https://opencode.ai/zen/v1`，固定请求头与 `public` 密钥，兼容 Chat Completions / Responses / Messages 与 SSE 流式

## 技术栈

| 端 | 技术 |
| --- | --- |
| 后端 | Python ≥ 3.13 · `http.server`(ThreadingHTTPServer) · curl_cffi（Chrome TLS 指纹）· Playwright/Camoufox · loguru |
| 前端 | React · Vite · Tailwind CSS · Radix UI · Lucide 图标 |
| 依赖 | uv（后端）· npm（前端） |

## 目录结构

```
grok-rego/
├── server/                    # 后端
│   ├── api/                   # 管理 API 路由、号池任务（pool_jobs）、推送（push）
│   ├── core/                  # 配置 / 日志 / 工具（config、logger、util）
│   ├── gateway/               # 上游反代（opencode：OpenCode Zen · grok：号池 · ops：运维聚合）
│   ├── workflow/              # 注册 / OAuth / 浏览器 / 仿真人 / 任务编排 / 邮箱
│   ├── db/                    # 数据库（init + 账号读写）
│   ├── main.py                # CLI 批量注册 + --serve 启动管理 API
│   ├── pyproject.toml         # uv 项目定义（Python ≥ 3.13）
│   └── config.example.json    # 配置模板（复制为 config.json 使用）
├── web/                       # 前端
│   ├── src/pages/             # RegisterPage、PoolPage、GatewayPage、UsagePage
│   ├── src/components/        # 号池表格等 UI 组件
│   └── src/styles/            # tokens / base / pool / register 样式体系
├── start.bat / start.ps1 / start.sh
└── README.md
```

## 快速开始

### 环境要求

- Python ≥ 3.13（建议通过 [uv](https://docs.astral.sh/uv/) 管理）
- Node.js ≥ 18 + npm

### 一键启动

```
start.bat     # Windows CMD
start.ps1     # Windows PowerShell
./start.sh    # Linux / macOS
```

脚本会依次完成：生成 `server/config.json`（不存在时从模板复制）→ 后端 `uv sync` → 前端 `npm install` → 拉起前后端：

- 后端管理 API：`http://127.0.0.1:8787`
- 前端 Web UI：`http://127.0.0.1:5274`

### 手动启动

```bash
# 后端（管理 API）
cd server && uv sync && uv run python main.py --serve 8787

# 前端
cd web && npm install && npm run dev
```

### CLI 批量注册（不开 Web UI）

```bash
cd server
uv run python main.py -n 5          # 批量注册 5 个账号
uv run python main.py -n 5 -t 3     # 3 线程并发注册
uv run python main.py --serve       # 启动管理 API（默认 8787）
```

## 配置说明

复制 `server/config.example.json` 为 `server/config.json` 并按需填写（**config.json 已被 .gitignore 排除，不会入库**）：

| 字段 | 说明 |
| --- | --- |
| `cf_api_base` / `cf_domains` / `cf_api_key` | Cloudflare 邮箱域名服务配置 |
| `cf_domain_mode` | 域名分配模式（`random` 等） |
| `mail_provider` | 邮箱服务商：`cf`（Cloudflare）/ `yyds` |
| `yyds_api_base` / `yyds_api_key` | Yyds 邮箱服务配置 |
| `proxy` | 代理地址（默认 `http://127.0.0.1:7890`） |
| `auth_enabled` | 注册完成后是否自动执行 SSO 授权与 grok.com 风控体检 |
| `g2a_base_url` / `g2a_username` / `g2a_password` | G2A 管理端配置（登录后推送 Web 池） |
| `cpa_base_url` / `cpa_management_key` | CPA 管理端配置（auth-files 批量上传） |
| `human_sim` | 全局仿真人开关（`false` 时退回 Camoufox 自带 humanize） |
| `human_level` | 拟人强度：`light` 快 / `normal` 平衡（默认）/ `heavy` 最像人 |

## 全局仿真人（防风控）

注册链路的鼠标与键盘动作统一由 `server/workflow/human.py` 接管，替代机械式 click / fill：

| 维度 | 做法 |
| --- | --- |
| 鼠标轨迹 | 三次贝塞尔曲线（缓入缓出）+ 垂直方向弧度 + 逐点抖动 + 偶发过冲回拉，绝不是直线 |
| 点击 | 移动 → 点击前驻留 → 随机按下时长 → 点击后停顿；落点在元素内随机（不总点正中心） |
| 键入 | 逐字高斯延迟、思考停顿、偶发错字（邻键）回删重打、大写走 `Shift` |
| 校验 | 键入后回读输入框实际值，不一致则清掉重打一次，仍不一致仅告警不阻塞 |
| 滚动 | 分段滚动 + 段间停顿 |
| 空闲 | 等待轮询期间随机微动 / 轻微滚动，打散光标长时间静止的自动化特征 |

三档强度（`human_level`）在速度与拟真度之间取舍，Web「注册」页可实时切换：

| 档位 | 轨迹上限 | 键入间隔 | 错字率 | 适用 |
| --- | --- | --- | --- | --- |
| `light` | 0.35s | 45~115ms | 0% | 追求吞吐 |
| `normal` | 0.75s | 70~190ms | 2% | 默认平衡 |
| `heavy` | 1.25s | 95~265ms | 3% | 被风控盯上时降速跑 |

**不会卡死**：单次轨迹有总时长硬上限；逐步实测 `mouse.move` 真实耗时并动态收敛步数；
任何异常都吞掉并退化为「一次直达移动 + 原生点击」。引擎开启时会关掉 Camoufox 自带
`humanize`（二者同时开会双重曲线化，既拖慢又可能等待轨迹结束而卡住），
`human_sim=false` 时恢复原来的 `humanize=0.8` 行为。

## OpenCode Zen 网关

管理 API 同时暴露 OpenAI 兼容入口：

| 项 | 值 |
| --- | --- |
| 客户端 Base URL | `http://127.0.0.1:8787/zen/v1` |
| 鉴权 | 默认不鉴权；配置 `gateway_api_key` 后客户端必须携带 `Authorization: Bearer <key>` 或 `x-api-key`（Web「网关运维」页可随机生成） |
| API Key | `public`（固定，客户端可带可不带，网关会覆盖） |
| 上游 | `https://opencode.ai/zen/v1` |
| 固定请求头 | `HTTP-Referer=https://opencode.ai` · `User-Agent=opencode/1.18.16` · `X-Title=opencode` |

`GET /zen/v1/models` 固定返回 `server/gateway/zen-models.json` 中固化的免费模型清单（id 以 `-free` 结尾，以及 stealth 免费模型 `big-pickle`），并注入 Claude Code 可识别的别名模型；该端点不请求上游，清单文件热更新后自动生效。

```bash
curl http://127.0.0.1:8787/zen/v1/models

curl http://127.0.0.1:8787/zen/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"big-pickle","messages":[{"role":"user","content":"hi"}]}'
```

Web UI「网关运维」页：接入点复制、鉴权密钥（随机生成 sk- 前缀）、号池账号运行态与 24h 分通道统计（单接口 `/api/gateway/ops` 聚合拉取，15s 自动刷新）。请求明细在「用量」页。出口代理走 `config.json` 的 `proxy`。


## Grok 号池网关

用号池已认证账号的 `access_token` 转发到 `https://cli-chat-proxy.grok.com/v1`。每次请求自动从号池取号（ACTIVE + 已认证 + token 未过期，轮询分摊）；上游判定为坏号的账号（401/403/404/429 等）临时冷却，到期自动解冻，避免反复命中。

| 项 | 值 |
| --- | --- |
| 客户端 Base URL | `http://127.0.0.1:8787/grok/v1` |
| 选号 | 自动轮询 + 坏号临时冷却（无需配置） |
| 上游 | `https://cli-chat-proxy.grok.com/v1` |
| 鉴权 | 号池账号 Bearer token（网关注入） |

```bash
curl http://127.0.0.1:8787/grok/v1/models

curl http://127.0.0.1:8787/grok/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4","messages":[{"role":"user","content":"hi"}]}'
```

## 安全说明

- `server/config.json`（含密钥 / Token）不在版本库内；提交代码前请确认仅保留 `config.example.json` 模板
- 日志尽量保留完整账号信息（邮箱等）便于排查，但**绝不打印 access_token / refresh_token / SSO cookie 等凭据明文**
- 管理 API **无访问认证**，默认仅监听 `127.0.0.1`；请勿暴露到公网或反代到外网

## License

Private / Internal use.