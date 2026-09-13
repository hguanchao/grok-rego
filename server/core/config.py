"""
全局配置常量。

所有路径、端点、超时等配置集中管理。
"""

import json
import os
from typing import Any

# === 路径配置 ===
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(CORE_DIR)
LOG_DIR = os.path.join(SERVER_DIR, "logs")

DB_DIR = os.path.join(SERVER_DIR, "db", "data")
DB_PATH = os.path.join(DB_DIR, "main.db")

# === 临时邮箱 API（cf）===
CF_API_BASE: str = ""
CF_DOMAINS: list[str] = []
CF_API_KEY: str = ""
CF_DOMAIN_MODE: str = "random"
PROXY: str = "http://127.0.0.1:7890"

# === 临时邮箱服务商（cf / yyds）===
MAIL_PROVIDER: str = "cf"
YYDS_API_BASE: str = "https://maliapi.215.im/v1"
YYDS_API_KEY: str = ""

# === 推送目标配置（G2A / CPA）===
G2A_BASE_URL: str = ""
G2A_USERNAME: str = ""
G2A_PASSWORD: str = ""
CPA_BASE_URL: str = ""
CPA_MANAGEMENT_KEY: str = ""

# === 管理 API ===
API_HOST: str = "127.0.0.1"
API_PORT: int = 8787
# 网关模型别名：客户端请求模型名 → 上游真实模型（如 claude-sonnet-4-5 → big-pickle）
# Zen 网关鉴权密钥：非空时客户端必须携带（Authorization: Bearer <key> 或 x-api-key）
GATEWAY_API_KEY: str = ""
# 打上游的 x-grok-client-version / User-Agent，对齐 grok-build（默认取其 crate 版本）
GROK_VERSION: str = "1.0.16"
# 号池探活：GET /billing 上游
UPSTREAM_BASE: str = "https://cli-chat-proxy.grok.com/v1"

# === OAuth2 / OIDC 配置（从 grok-build-main 源码提取）===
OAUTH2_ISSUER = "https://auth.x.ai"
OAUTH2_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OAUTH2_SCOPES: list[str] = [
    "openid", "profile", "email", "offline_access",
    "grok-cli:access", "api:access",
    "conversations:read", "conversations:write",
    "workspaces:read", "workspaces:write",
]

# === 浏览器自动化配置 ===
GOTO_TIMEOUT: int = 60000
ELEMENT_TIMEOUT: int = 15000

# === 全局仿真人（防风控）===
# human_sim=false 时关闭自研拟人引擎，行为与接入前一致（仅依赖 Camoufox humanize）
HUMAN_SIM: bool = True
# 拟人强度：light（快）/ normal（默认，平衡）/ heavy（慢，最像人）
HUMAN_LEVEL: str = "normal"
HUMAN_LEVELS: tuple[str, ...] = ("light", "normal", "heavy")

# === 流程开关 ===
IS_AUTH: bool = True

# === xAI 账号页面 ===
# redirect=grok-com：注册完成后直接跳转 grok.com（不再停留账号页）
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
GROK_URL = "https://grok.com/"

# === JSON 配置加载（config.json 覆盖默认值）===
CONFIG_PATH = os.path.join(SERVER_DIR, "config.json")

# 可经 API 读写的配置字段
_PUBLIC_CONFIG_KEYS = (
    "cf_api_base",
    "cf_domains",
    "cf_api_key",
    "cf_domain_mode",
    "mail_provider",
    "yyds_api_base",
    "yyds_api_key",
    "proxy",
    "auth_enabled",
    "g2a_base_url",
    "g2a_username",
    "g2a_password",
    "cpa_base_url",
    "cpa_management_key",
    "gateway_api_key",
    "grok_version",
    "human_sim",
    "human_level",
)


def _normalize_domain(raw: str) -> str:
    """规范化单个域名：去空白、去前导 @、去协议与路径。"""
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    if value.startswith("@"):
        value = value[1:].strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].strip()
    value = value.split("?", 1)[0].strip()
    # 去掉误写的端口
    if value.count(":") == 1 and not value.endswith("]"):
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            value = host
    return value.strip(".")


def _parse_cf_domains(raw: Any) -> list[str]:
    """解析 cf_domains：支持逗号分隔字符串或 JSON 数组。"""
    parts: list[str]
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(item) for item in raw]
    elif isinstance(raw, str):
        parts = raw.replace(";", ",").replace("\n", ",").split(",")
    else:
        parts = str(raw).split(",")
    domains: list[str] = []
    seen: set[str] = set()
    for part in parts:
        domain = _normalize_domain(part)
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _read_config_file() -> dict[str, Any]:
    """读取 config.json 原始内容；不存在或损坏返回空 dict。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _apply_config_data(data: dict[str, Any]) -> None:
    """将 dict 应用到模块级运行时变量。"""
    global CF_API_BASE, CF_DOMAINS, CF_API_KEY, CF_DOMAIN_MODE, PROXY, IS_AUTH
    global MAIL_PROVIDER, YYDS_API_BASE, YYDS_API_KEY
    global G2A_BASE_URL, G2A_USERNAME, G2A_PASSWORD, CPA_BASE_URL, CPA_MANAGEMENT_KEY
    global GATEWAY_API_KEY, GROK_VERSION
    global HUMAN_SIM, HUMAN_LEVEL

    if data.get("cf_api_base") is not None:
        CF_API_BASE = str(data["cf_api_base"]).strip().rstrip("/")
    if "cf_domains" in data:
        CF_DOMAINS = _parse_cf_domains(data.get("cf_domains"))
    if data.get("cf_api_key") is not None:
        CF_API_KEY = str(data["cf_api_key"])
    if data.get("cf_domain_mode") is not None:
        domain_mode = str(data["cf_domain_mode"]).strip().lower()
        if domain_mode in ("poll", "random"):
            CF_DOMAIN_MODE = domain_mode
        else:
            print(
                f"[config] 无效 cf_domain_mode={data['cf_domain_mode']!r}，"
                f"仅支持 poll/random，已回退 random"
            )
            CF_DOMAIN_MODE = "random"
    if data.get("proxy") is not None:
        PROXY = str(data["proxy"])
    if data.get("mail_provider") is not None:
        provider = str(data["mail_provider"]).strip().lower()
        MAIL_PROVIDER = provider if provider in ("cf", "yyds") else "cf"
    if data.get("yyds_api_base") is not None:
        YYDS_API_BASE = str(data["yyds_api_base"]).strip().rstrip("/")
    if data.get("yyds_api_key") is not None:
        YYDS_API_KEY = str(data["yyds_api_key"])
    if "auth_enabled" in data:
        IS_AUTH = bool(data["auth_enabled"])
    if data.get("g2a_base_url") is not None:
        G2A_BASE_URL = str(data["g2a_base_url"]).strip().rstrip("/")
    if data.get("g2a_username") is not None:
        G2A_USERNAME = str(data["g2a_username"])
    if data.get("g2a_password") is not None:
        G2A_PASSWORD = str(data["g2a_password"])
    if data.get("cpa_base_url") is not None:
        CPA_BASE_URL = str(data["cpa_base_url"]).strip().rstrip("/")
    if data.get("cpa_management_key") is not None:
        CPA_MANAGEMENT_KEY = str(data["cpa_management_key"])
    if data.get("gateway_api_key") is not None:
        GATEWAY_API_KEY = str(data["gateway_api_key"]).strip()
    raw_ver = data.get("grok_version")
    if raw_ver is None:
        raw_ver = data.get("grok_client_version")
    if raw_ver is not None:
        ver = str(raw_ver).strip()
        if ver:
            GROK_VERSION = ver
    if "human_sim" in data:
        HUMAN_SIM = bool(data.get("human_sim"))
    if data.get("human_level") is not None:
        lvl = str(data["human_level"]).strip().lower()
        if lvl in HUMAN_LEVELS:
            HUMAN_LEVEL = lvl
        else:
            print(
                f"[config] 无效 human_level={data['human_level']!r}，"
                f"仅支持 {'/'.join(HUMAN_LEVELS)}，已回退 normal"
            )
            HUMAN_LEVEL = "normal"


def load_config() -> None:
    """从 config.json 加载用户配置覆盖默认值；文件不存在或损坏时保持默认。"""
    data = _read_config_file()
    if not data:
        if os.path.exists(CONFIG_PATH):
            print("[config] 加载 config.json 失败或为空，使用默认配置")
        return
    _apply_config_data(data)
    # grok_client_version → grok_version：读到旧键就落盘，避免运维页保存前两套并存
    if "grok_client_version" in data and "grok_version" not in data:
        ver = str(data.get("grok_client_version") or "").strip()
        if ver:
            data["grok_version"] = ver
        data.pop("grok_client_version", None)
        os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")


def get_public_config() -> dict[str, Any]:
    """返回可对前端暴露的当前配置（敏感值完整返回，仅本机管理面使用）。"""
    return {
        "cf_api_base": CF_API_BASE,
        "cf_domains": list(CF_DOMAINS),
        "cf_api_key": CF_API_KEY,
        "cf_domain_mode": CF_DOMAIN_MODE,
        "mail_provider": MAIL_PROVIDER,
        "yyds_api_base": YYDS_API_BASE,
        "yyds_api_key": YYDS_API_KEY,
        "proxy": PROXY,
        "auth_enabled": IS_AUTH,
        "g2a_base_url": G2A_BASE_URL,
        "g2a_username": G2A_USERNAME,
        "g2a_password": G2A_PASSWORD,
        "cpa_base_url": CPA_BASE_URL,
        "cpa_management_key": CPA_MANAGEMENT_KEY,
        "gateway_api_key": GATEWAY_API_KEY,
        "grok_version": GROK_VERSION,
        "human_sim": HUMAN_SIM,
        "human_level": HUMAN_LEVEL,
    }


def update_public_config(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入 config.json 并刷新运行时；返回更新后的公开配置。"""
    if not isinstance(patch, dict):
        raise ValueError("配置体必须是 JSON 对象")

    current = _read_config_file()
    for key in _PUBLIC_CONFIG_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "cf_domains":
            current[key] = ",".join(_parse_cf_domains(value))
        elif key == "cf_domain_mode":
            mode = str(value or "").strip().lower()
            if mode not in ("poll", "random"):
                raise ValueError("cf_domain_mode 仅支持 poll / random")
            current[key] = mode
        elif key == "mail_provider":
            provider = str(value or "").strip().lower()
            if provider not in ("cf", "yyds"):
                raise ValueError("mail_provider 仅支持 cf / yyds")
            current[key] = provider
        elif key == "auth_enabled":
            current[key] = bool(value)
        elif key == "grok_version":
            ver = str(value or "").strip()
            if not ver:
                raise ValueError("grok_version 不能为空")
            current[key] = ver
            current.pop("grok_client_version", None)
        elif key == "human_sim":
            current[key] = bool(value)
        elif key == "human_level":
            lvl = str(value or "").strip().lower()
            if lvl not in HUMAN_LEVELS:
                raise ValueError(f"human_level 仅支持 {'/'.join(HUMAN_LEVELS)}")
            current[key] = lvl
        elif key in ("cf_api_base", "yyds_api_base", "g2a_base_url", "cpa_base_url"):
            current[key] = str(value or "").strip().rstrip("/")
        elif value is None:
            current[key] = ""
        else:
            current[key] = str(value)

    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
        f.write("\n")

    _apply_config_data(current)
    return get_public_config()


load_config()
