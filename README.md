# grok-rego

xAI Grok Build 账号自动化工具：**批量注册 → Device Flow 授权 → Token 交换 → 号池管理 → G2A / CPA 推送** 的全链路流水线，配套 Web 管理界面。

> 原名 grok2build，项目重构后更名为 grok-rego。

## 功能特性

- **批量注册**：多线程并发注册，支持 Cloudflare / Yyds 邮箱服务与代理配置
- **Device Flow 授权**：浏览器端 OAuth Device Flow，自动完成 Token 交换
- **SSO 会话**：注册附带的会话凭证入库，供下游渠道使用
- **号池管理**：账号列表、状态、筛选与巡检，后台固定周期扫描临期账号并自动续期
- **推送**：
  - **G2A**：仅推 Web 池（`/api/admin/v1/accounts/web/import`），HTTP 2xx 即视为推送成功，不等待落库同步
  - **CPA**：auth-files 批量上传（单请求合批）
- **管理 API + Web UI**：账号注册、号池管理、推送任务与推送目标配置一体的可视化界面

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
│   ├── api/                   # 管理 API 路由（默认端口 8787）
│   ├── core/                  # 配置 / 日志 / 工具（config、logger、util）
│   ├── ops/                   # 推送（push：G2A·CPA）与池任务（pool_jobs）
│   ├── workflow/              # 注册 / OAuth / 浏览器 / 任务编排 / 邮箱
│   ├── db/                    # 数据库（init + 账号读写）
│   ├── main.py                # CLI 批量注册 + --serve 启动管理 API
│   ├── pyproject.toml         # uv 项目定义（Python ≥ 3.13）
│   └── config.example.json    # 配置模板（复制为 config.json 使用）
├── web/                       # 前端
│   ├── src/pages/             # RegisterPage（注册 / 推送目标设置）、PoolPage（号池 / 推送）
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
| `auth_enabled` | 管理 API 是否启用访问认证 |
| `g2a_base_url` / `g2a_username` / `g2a_password` | G2A 管理端配置（登录后推送 Web 池） |
| `cpa_base_url` / `cpa_management_key` | CPA 管理端配置（auth-files 批量上传） |

## 安全说明

- `server/config.json`（含密钥 / Token）不在版本库内；提交代码前请确认仅保留 `config.example.json` 模板
- 日志输出对敏感字段做脱敏处理

## License

Private / Internal use.