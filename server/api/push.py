"""账号推送：G2A / CPA。"""

from __future__ import annotations

"""
G2A / CPA 推送客户端。

借鉴 acorn 号池推送的架构分解：凭证构造（Builder）/ 传输 / 结果判定三层分离，
按 grok-rego 技术栈重写并定制：
- HTTP 直连 curl_cffi（Chrome TLS 指纹），multipart 用 CurlMime
- token 元数据解码复用 common.util 的 JWT 工具，不引入第三方库
- 客户端指纹头对齐 grok-cli 协议
- sso_cookie 为字符串（sso 会话凭证原值；sso 与 sso-rw value 相同），推送时拼为 Cookie 串

协议：
- G2A: POST /api/admin/v1/auth/login → multipart /api/admin/v1/accounts/import（Build 池，必推）
  + /api/admin/v1/accounts/web/import（Web 池，有 SSO cookie 时一并推）
  成功判定以 HTTP 2xx 为准；G2A 落库/同步属上游内部行为，syncFailed 与本项目无关，不解析不计失败
- CPA: multipart POST /v0/management/auth-files（Bearer / X-Management-Key）
"""

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from curl_cffi import CurlMime, requests

from core.config import OAUTH2_CLIENT_ID, OAUTH2_ISSUER, OAUTH2_SCOPES
from core.logger import logger
from core.util import decode_jwt_exp

# CPA auth-file 固定值（与 grok-build 官方导出格式一致）
_CPA_REDIRECT_URI = "http://127.0.0.1:2468"
_CPA_CLIENT_VERSION = "1.0.0"
# CPA 下游固定出口
_CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
_CPA_TOKEN_ENDPOINT = f"{OAUTH2_ISSUER}/oauth2/token"
_CPA_SCOPE = " ".join(OAUTH2_SCOPES)
_CPA_EXPIRES_IN = 21600

# 文件名非法字符（Windows 路径保留字 + 空白）
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|#%&\s]+')

_LOGIN_TIMEOUT = 30.0
_IMPORT_TIMEOUT = 180.0


# ─── 目标规范化 ─────────────────────────────────────────


def normalize_targets(targets: list[str] | None) -> list[str]:
    """去重保序，仅保留 g2a / cpa 白名单目标。"""
    wanted: list[str] = []
    for item in targets or []:
        name = str(item or "").strip().lower()
        if name in ("g2a", "cpa") and name not in wanted:
            wanted.append(name)
    return wanted


# ─── 通用工具 ─────────────────────────────────────────


def _safe_filename(email: str, account_id: int) -> str:
    """邮箱 → 文件名安全形式（清洗非法字符、限长，缺省回退账号 id）。"""
    raw = str(email or "").strip() or f"account_{account_id}"
    safe = _SAFE_NAME_RE.sub("_", raw)[:80].strip("._") or f"account_{account_id}"
    return f"xai-{safe}.json"


def _extract_error_text(resp: requests.Response, limit: int = 240) -> str:
    """从失败响应提取可读错误：JSON 多层 error → message/detail → 正文截断。"""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("message", "error", "detail"):
                if err.get(key):
                    return str(err[key])[:limit]
        for key in ("message", "detail", "error"):
            if body.get(key):
                return str(body[key])[:limit]
    text = (resp.text or "").strip()
    return text[:limit] if text else f"HTTP {resp.status_code}"


def _client_headers(version: str = _CPA_CLIENT_VERSION) -> dict[str, str]:
    """grok-shell 客户端指纹头。"""
    ver = (version or _CPA_CLIENT_VERSION).strip()
    return {
        "x-grok-client-version": ver,
        "x-xai-token-auth": "xai-grok-cli",
        "x-authenticateresponse": "authenticate-response",
        "User-Agent": f"grok-shell/{ver} (windows; x86_64)",
    }


# ─── token 元数据 ─────────────────────────────────────────


def _jwt_user_id(token: str) -> str:
    """解码 JWT payload 取 sub（user_id）；解析失败返回空串。"""
    if not token:
        return ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return str(payload.get("sub") or "")
    except (IndexError, ValueError, json.JSONDecodeError):
        return ""


def _expires_iso(token: str) -> str:
    """JWT exp 时间戳 → ISO8601；无有效期返回空串。"""
    exp = decode_jwt_exp(token)
    if exp is None:
        return ""
    return datetime.fromtimestamp(exp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── 凭证构造（账号行 → 目标平台导入文档）──────────────────


def build_g2a_build_entry(row: dict) -> dict[str, Any] | None:
    """账号行 → G2A Build 池导入条目；无 OAuth 凭据返回 None。

    provider 固定 grok_build，字段对齐 grok-build CLI 客户端凭据；
    Build 池是 G2A 主池，凡已认证账号必推。
    """
    email = str(row.get("email") or "").strip()
    token = str(row.get("access_token") or "").strip()
    if not token:
        return None
    user_id = _jwt_user_id(token)
    return {
        "provider": "grok_build",
        "name": email or user_id or f"account_{row.get('id')}",
        "client_id": OAUTH2_CLIENT_ID,
        "access_token": token,
        "refresh_token": str(row.get("refresh_token") or ""),
        "token_type": "Bearer",
        "scope": _CPA_SCOPE,
        "expires_at": _expires_iso(token),
        "email": email,
        "sub": user_id,
        "user_id": user_id,
        "principal_id": user_id,
        "team_id": "",
    }


def build_g2a_web_entry(row: dict) -> dict[str, Any] | None:
    """账号行 → G2A Web 池导入条目；无 SSO cookie 返回 None。

    sso_cookie 为字符串（sso 会话凭证原值；sso 与 sso-rw value 相同），
    拼为 sso=...; sso-rw=... 串；Web 池可转换 Build/Console，有 SSO 时一并推送。
    """
    email = str(row.get("email") or "").strip()
    user_id = _jwt_user_id(str(row.get("access_token") or ""))
    sso_value = str(row.get("sso_cookie") or "").strip()
    if not sso_value:
        return None
    cookie_pair = f"sso={sso_value}; sso-rw={sso_value}"
    return {
        "name": email or user_id or f"account_{row.get('id')}",
        "email": email,
        "user_id": user_id,
        "sso_token": cookie_pair,
        "tier": "auto",
    }


def build_cpa_auth_file(row: dict) -> dict[str, Any]:
    """账号行 → CPA xai oauth auth-file JSON（对齐 grok-build 官方导出格式）。

    本库无 updated_at 字段，沿用注册时间 created_at 作为 last_refresh。
    """
    email = str(row.get("email") or "").strip()
    token = str(row.get("access_token") or "")
    user_id = _jwt_user_id(token)
    expires = _expires_iso(token)
    return {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": token,
        "refresh_token": str(row.get("refresh_token") or ""),
        "token_type": "Bearer",
        "expires_in": _CPA_EXPIRES_IN,
        "expired": expires,
        "expires_at": expires,
        "last_refresh": str(row.get("created_at") or ""),
        "email": email,
        "sub": user_id,
        "user_id": user_id,
        "base_url": _CPA_BASE_URL,
        "token_endpoint": _CPA_TOKEN_ENDPOINT,
        "redirect_uri": _CPA_REDIRECT_URI,
        "client_id": OAUTH2_CLIENT_ID,
        "issuer": OAUTH2_ISSUER,
        "scope": _CPA_SCOPE,
        "referrer": "grok-build",
        "disabled": False,
        "headers": _client_headers(),
        "proxy_base_url": _CPA_BASE_URL,
    }


# ─── G2A 客户端 ─────────────────────────────────────────


def g2a_login(*, base_url: str, username: str, password: str) -> tuple[str, str]:
    """G2A 管理端登录一次，返回 (access_token, error)；成功时 error 为空。

    token 供后续全部账号导入请求复用（避免每账号重复登录）。
    """
    base = str(base_url or "").strip().rstrip("/")
    user = str(username or "").strip()
    pwd = str(password or "").strip()
    if not base:
        return "", "未配置 G2A 地址"
    if not user or not pwd:
        return "", "未配置 G2A 用户名/密码"

    url = urljoin(base + "/", "api/admin/v1/auth/login")
    logger.info(f"[推送] G2A 预登录开始: {url}")
    try:
        resp = requests.post(
            url,
            json={"username": user, "password": pwd},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=_LOGIN_TIMEOUT,
            impersonate="chrome",
        )
    except requests.RequestsError as exc:
        logger.error(f"[推送] G2A 登录请求异常: {type(exc).__name__}: {exc}")
        return "", f"G2A 登录请求失败：{exc}"
    if resp.status_code != 200:
        err = _extract_error_text(resp)
        logger.error(f"[推送] G2A 登录失败 · HTTP {resp.status_code} {err}")
        return "", f"G2A 登录失败：{err}"
    try:
        body = resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        tokens = data.get("tokens") if isinstance(data, dict) else None
    except ValueError:
        logger.error("[推送] G2A 登录响应非 JSON")
        return "", "G2A 登录响应非 JSON"
    access = ""
    if isinstance(tokens, dict):
        access = str(
            tokens.get("accessToken") or tokens.get("access_token") or ""
        ).strip()
    if not access:
        logger.error("[推送] G2A 登录成功但未返回 accessToken")
        return "", "G2A 登录成功但未返回 accessToken"
    logger.success("[推送] G2A 预登录成功")
    return access, ""


def _import_credentials(
    sess: requests.Session,
    url: str,
    headers: dict[str, str],
    document: dict[str, Any],
    label: str,
) -> tuple[bool, str, int]:
    """向 G2A 导入接口（Build / Web 共用）推送一个账号文档，返回 (是否成功, 错误, HTTP 状态码)。

    以 HTTP 2xx 判定成功；G2A 落库与同步属上游内部行为，其 syncFailed
    与本项目无关，不解析 SSE complete 事件，也不计入失败。
    """
    raw = json.dumps(
        {"accounts": [document]}, ensure_ascii=False, indent=2
    ).encode("utf-8")
    name = _safe_filename(
        str(document.get("email") or ""), int(document.get("id") or 0)
    )
    mp = CurlMime()
    try:
        mp.addpart(
            name="file",
            filename=name,
            data=raw,
            content_type="application/json",
        )
        resp = sess.post(
            url,
            multipart=mp,
            headers=headers,
            timeout=_IMPORT_TIMEOUT,
        )
    except requests.RequestsError as exc:
        return False, f"{label} 池导入请求失败：{exc}", 0
    finally:
        mp.close()

    if resp.status_code < 200 or resp.status_code >= 300:
        return False, f"{label} 池导入失败：{_extract_error_text(resp)}", resp.status_code
    return True, "", resp.status_code


def push_one_g2a(row: dict, *, base_url: str, access_token: str) -> dict[str, Any]:
    """单账号推送到 G2A：Build 池必推，Web 池视 SSO cookie 而定。

    判定口径：各池 HTTP 2xx 即推送成功，不解析 G2A 落库/同步结果；
    无 SSO cookie 仅跳过 Web 池（不报错，Build 池已推送）。
    """
    base = str(base_url or "").strip().rstrip("/")
    token = str(access_token or "").strip()
    email = str(row.get("email") or "").strip()
    log_email = email
    if not base:
        return {"ok": False, "target": "g2a", "action": "", "message": "G2A 未配置地址", "http_status": 0}
    if not token:
        return {"ok": False, "target": "g2a", "action": "", "message": "G2A 未登录", "http_status": 0}
    if not str(row.get("access_token") or "").strip():
        return {"ok": False, "target": "g2a", "action": "", "message": "G2A 无 OAuth 凭据", "http_status": 0}

    build_entry = build_g2a_build_entry(row)
    assert build_entry is not None  # 上方已校验 access_token，必非空
    web_entry = build_g2a_web_entry(row)  # 无 SSO cookie 时为 None，跳过 Web 池

    logger.info(f"[推送] G2A {log_email} 开始推送 Build 池")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream, application/json",
    }
    with requests.Session(impersonate="chrome") as sess:
        build_url = urljoin(base + "/", "api/admin/v1/accounts/import")
        ok, err, http_status = _import_credentials(
            sess, build_url, headers, build_entry, "Build"
        )
        if not ok:
            logger.error(f"[推送] G2A {log_email} Build 池推送失败: {err}")
            return {
                "ok": False,
                "target": "g2a",
                "action": "",
                "message": err,
                "http_status": http_status,
            }
        # Web 池：仅当存在 SSO cookie 时推送，缺失不视为失败
        web_ok = True
        web_http = http_status
        if web_entry is not None:
            logger.info(f"[推送] G2A {log_email} 开始推送到 Web 池")
            web_url = urljoin(base + "/", "api/admin/v1/accounts/web/import")
            web_ok, web_err, web_http = _import_credentials(
                sess, web_url, headers, web_entry, "Web"
            )
            if not web_ok:
                logger.error(f"[推送] G2A {log_email} Web 池推送失败: {web_err}")
                return {
                    "ok": False,
                    "target": "g2a",
                    "action": "",
                    "message": web_err,
                    "http_status": web_http,
                }
        else:
            logger.debug(f"[推送] G2A {log_email} 无 SSO cookie，跳过 Web 池")

    pools = "Build" if web_entry is None else "Build、Web"
    logger.success(f"[推送] G2A {log_email} {pools} 池推送成功")
    return {
        "ok": True,
        "target": "g2a",
        "action": "新增",
        "message": f"G2A {pools} 池推送成功",
        "http_status": max(http_status, web_http),
    }


# ─── CPA 客户端 ─────────────────────────────────────────


def push_batch_cpa(
    rows: list[dict], *, base_url: str, management_key: str
) -> dict[str, Any]:
    """CPA auth-files 批量上传（单请求全量合批，优于逐账号多次请求）。"""
    base = str(base_url or "").strip().rstrip("/")
    key = str(management_key or "").strip()
    if not base:
        return {"ok": False, "target": "cpa", "message": "未配置 CPA 地址", "uploaded": 0, "failed": len(rows), "http_status": 0}
    if not key:
        return {"ok": False, "target": "cpa", "message": "未配置 CPA 管理密钥", "uploaded": 0, "failed": len(rows), "http_status": 0}
    if not rows:
        return {"ok": False, "target": "cpa", "message": "无可推送账号", "uploaded": 0, "failed": 0, "http_status": 0}

    url = urljoin(base + "/", "v0/management/auth-files")
    headers = {"Authorization": f"Bearer {key}", "X-Management-Key": key}
    logger.info(f"[推送] CPA 批量上传开始: {len(rows)} 个账号 · {url}")
    mp = CurlMime()
    try:
        for row in rows:
            name = _safe_filename(str(row.get("email") or ""), int(row.get("id") or 0))
            mp.addpart(
                name="file",
                filename=name,
                data=json.dumps(
                    build_cpa_auth_file(row), ensure_ascii=False, indent=2
                ).encode("utf-8"),
                content_type="application/json",
            )
        resp = requests.post(
            url,
            multipart=mp,
            headers=headers,
            timeout=_IMPORT_TIMEOUT,
            impersonate="chrome",
        )
    except requests.RequestsError as exc:
        logger.error(f"[推送] CPA 请求异常: {type(exc).__name__}: {exc}")
        return {"ok": False, "target": "cpa", "message": f"CPA 请求失败：{exc}", "uploaded": 0, "failed": len(rows), "http_status": 0}
    finally:
        mp.close()

    # 200/201 全成功；207 Multi-Status 常见于批量上传，需结合 failed 判断
    if resp.status_code not in (200, 201, 207):
        err = _extract_error_text(resp)
        logger.error(f"[推送] CPA 批量上传失败 · HTTP {resp.status_code} {err}")
        return {
            "ok": False,
            "target": "cpa",
            "message": f"CPA 推送失败：{_extract_error_text(resp)}",
            "uploaded": 0,
            "failed": len(rows),
            "http_status": resp.status_code,
        }

    uploaded, failed = len(rows), 0
    try:
        body = resp.json()
        if isinstance(body, dict):
            if "uploaded" in body:
                uploaded = int(body.get("uploaded") or 0)
            # failed 字段两种形态都兼容：数字（失败计数）或数组（失败明细）
            failed_raw = body.get("failed")
            if isinstance(failed_raw, list):
                failed = len(failed_raw)
            elif isinstance(failed_raw, (int, float)):
                failed = max(0, int(failed_raw))
    except ValueError:
        pass

    ok_flag = failed == 0
    message = (
        f"CPA 推送成功：{uploaded} 个"
        if ok_flag
        else f"CPA 推送部分失败：成功 {uploaded}，失败 {failed}"
    )
    if ok_flag:
        logger.success(f"[推送] CPA 批量上传成功: {uploaded} 个 · HTTP {resp.status_code}")
    else:
        logger.warning(f"[推送] CPA 批量上传部分失败: 成功 {uploaded} 失败 {failed} · HTTP {resp.status_code}")
    return {
        "ok": ok_flag,
        "target": "cpa",
        "message": message,
        "uploaded": uploaded,
        "failed": failed,
        "http_status": resp.status_code,
    }

"""
推送任务管理（进程内单例，对齐 api/jobs.py 的 JobManager 惯例）。

- 内存任务快照 + daemon 线程；snapshot(after_log_id) 增量日志轮询
- 单任务互斥：已有任务进行中拒绝新任务
- 资格预筛：未认证 / 状态非 ACTIVE 跳过（沿用号池既有口径），预筛明细启动即可见
- G2A 预登录一次，逐账号并发导入（随机间隔防风控）；CPA 全量合批单请求
- 协作式取消：cancel 事件贯穿预登录 / 合批 / 逐账号循环
"""

import threading
import time
import uuid

from core import config
from core.mutex import acquire as mutex_acquire, release as mutex_release
from core.util import (
    ACCOUNT_WORKER_GAP_SEC,
    ACCOUNT_WORKERS,
    elapsed_label,
    now_str,
    run_account_workers,
)
from db import STATUS_ACTIVE, get_all_accounts

# 并发上限（与前端并发输入框 1-20 对齐；实际 20 线程、每线程间隔 1s）
MAX_CONCURRENCY = ACCOUNT_WORKERS
# 任务日志内存环形保留条数
_LOG_LIMIT = 500


def _who(acc: dict[str, Any]) -> str:
    """账号日志标识：完整邮箱（排查用），缺邮箱则回退 #id。"""
    email = str(acc.get("email") or "").strip()
    aid = int(acc.get("id") or 0)
    return email if email else f"#{aid}"


def _end_log(job: PushJob) -> None:
    skipped = len(job.skipped_list)
    cancelled = job.cancel_event.is_set()
    if cancelled:
        level, action = "WARNING", "已取消"
    elif job.pushed > 0:
        level, action = "SUCCESS", "结束"
    else:
        level, action = "ERROR", "结束"
    job.append_log(
        level,
        f"[任务] 推送{action} 成功 {job.pushed} 失败 {job.failed} 跳过 {skipped}",
    )


def _clamp_concurrency(value: Any) -> int:
    """并发钳制到 1-20。"""
    try:
        return max(1, min(int(value), MAX_CONCURRENCY))
    except (TypeError, ValueError):
        return MAX_CONCURRENCY


class PushJob:
    """单次推送任务（内存态；镜像 RegisterJob 的 snapshot 风格）。"""

    def __init__(
        self,
        task_id: str,
        targets: list[str],
        account_ids: list[int],
        concurrency: int,
    ) -> None:
        self.id = task_id
        self.targets = targets
        self.account_ids = account_ids
        self.concurrency = concurrency
        self.status = "pending"  # pending / running / done / cancelled
        self.count = 0  # 符合资格候选数
        self.done = 0  # 已处理操作数（成功 + 失败）
        self.pushed = 0
        self.failed = 0
        self.skipped_list: list[dict[str, Any]] = []
        self.started_at = now_str()
        self.finished_at = ""
        self.error = ""
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._logs: list[dict[str, Any]] = []
        self._last_log_id = 0

    def append_log(self, level: str, message: str) -> None:
        """追加任务日志（环形保留 _LOG_LIMIT 条，id 单调递增供增量轮询）。"""
        with self._lock:
            self._last_log_id += 1
            self._logs.append({"id": self._last_log_id, "level": level, "message": message})
            if len(self._logs) > _LOG_LIMIT:
                self._logs = self._logs[-_LOG_LIMIT:]

    def snapshot(self, after_log_id: int = 0) -> dict[str, Any]:
        """任务快照；增量日志按 after_log_id 截取。"""
        with self._lock:
            logs = [log for log in self._logs if log["id"] > after_log_id]
            return {
                "id": self.id,
                "status": self.status,
                "targets": list(self.targets),
                "count": self.count,
                "concurrency": self.concurrency,
                "pushed": self.pushed,
                "failed": self.failed,
                "skipped": len(self.skipped_list),
                "done": self.done,
                "skipped_list": list(self.skipped_list),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "logs": logs,
                "last_log_id": self._last_log_id,
                "progress": round(self.done / self.count * 100, 2) if self.count else 0,
            }


class PushManager:
    """全局推送任务管理器（进程内单例），使用方式对齐 JobManager。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: PushJob | None = None
        self._worker: threading.Thread | None = None

    # ─── 对外接口 ─────────────────────────────────────────

    def status(self, after_log_id: int = 0) -> dict[str, Any]:
        """当前任务状态；无任务时返回 idle 占位。"""
        job = self._job
        if job is None:
            return {
                "id": None,
                "status": "idle",
                "targets": [],
                "count": 0,
                "concurrency": 0,
                "pushed": 0,
                "failed": 0,
                "skipped": 0,
                "done": 0,
                "skipped_list": [],
                "started_at": None,
                "finished_at": None,
                "error": None,
                "logs": [],
                "last_log_id": 0,
                "progress": 0,
            }
        return job.snapshot(after_log_id)

    def start(
        self,
        targets: list[str],
        account_ids: list[int] | None,
        concurrency: int = MAX_CONCURRENCY,
    ) -> dict[str, Any]:
        """启动推送任务；已有任务进行中或目标配置全缺时抛 RuntimeError。"""
        wanted = normalize_targets(targets)
        if not wanted:
            raise RuntimeError("至少选择一个推送目标（g2a / cpa）")
        # 目标配置全缺：直接拒绝，避免启动即失败的空任务
        missing = self._missing_targets(wanted)
        if len(missing) == len(wanted):
            raise RuntimeError(
                "推送目标配置缺失："
                + "；".join(self._target_label(t) for t in wanted)
                + "，请先在「注册页 → 推送目标设置」中配置"
            )
        concurrency = _clamp_concurrency(concurrency)
        ids = [int(i) for i in account_ids] if account_ids else []
        # 全局互斥：其它重任务（号池任务/认证/注册）进行中则拒绝
        mutex_acquire("推送")

        with self._lock:
            job = self._job
            if job is not None and job.status in ("pending", "running"):
                raise RuntimeError("已有推送任务进行中，请等待完成或取消")
            job = PushJob(
                task_id=uuid.uuid4().hex[:12],
                targets=wanted,
                account_ids=ids,
                concurrency=concurrency,
            )
            self._job = job
            self._worker = threading.Thread(
                target=self._run_job, args=(job,), name="推送任务", daemon=True
            )
            self._worker.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        """请求取消当前任务（协作式，由 worker 在检查点退出）。"""
        with self._lock:
            job = self._job
            if job is None or job.status not in ("pending", "running"):
                raise RuntimeError("当前没有可取消的推送任务")
            job.cancel_event.set()
        return self.status()

    # ─── 内部实现 ─────────────────────────────────────────

    @staticmethod
    def _target_label(target: str) -> str:
        return {"g2a": "G2A", "cpa": "CPA"}.get(target, target.upper())

    @staticmethod
    def _missing_targets(targets: list[str]) -> list[str]:
        """返回配置缺失的目标列表（g2a 需地址+账号+密码，cpa 需地址+密钥）。

        运行时动态读取 config 模块属性：配置可能在进程启动后经
        「注册页 → 推送目标设置」更新，模块级导入是值拷贝会拿到旧值。
        """
        missing: list[str] = []
        if "g2a" in targets and not (
            config.G2A_BASE_URL and config.G2A_USERNAME and config.G2A_PASSWORD
        ):
            missing.append("g2a")
        if "cpa" in targets and not (config.CPA_BASE_URL and config.CPA_MANAGEMENT_KEY):
            missing.append("cpa")
        return missing

    def _run_job(self, job: PushJob) -> None:
        """任务主循环：预筛 → 目标预检 → G2A 预登录 → CPA 合批 → G2A 并发。"""
        try:
            self._execute(job)
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.append_log("ERROR", f"[任务] 推送异常 {job.error}")
            logger.error(f"[推送] 任务异常: {job.error}")
        finally:
            job.status = "cancelled" if job.cancel_event.is_set() else "done"
            job.finished_at = now_str()
            _end_log(job)
            mutex_release("推送")
            logger.info(
                f"[推送] 任务 {job.id} 结束: status={job.status} "
                f"成功={job.pushed} 失败={job.failed} 跳过={len(job.skipped_list)}"
            )

    def _execute(self, job: PushJob) -> None:
        job.status = "running"
        label = "+".join(self._target_label(t) for t in job.targets)
        candidates = self._screen(job)
        job.append_log(
            "INFO",
            f"[任务] 推送开始 目标 {label} / {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s",
        )
        logger.info(
            f"[推送] 任务启动 目标 {label} / 候选 {job.count} 个 / {ACCOUNT_WORKERS} 线程 "
            f"每线程间隔 {ACCOUNT_WORKER_GAP_SEC:.0f}s / 任务 {job.id}"
        )
        if not candidates:
            return

        # 2. 目标可用性预检（单目标缺失不阻塞另一目标）
        usable = self._usable_targets(job)
        if not usable:
            return

        # 3. G2A 预登录一次（token 供全部账号复用）
        g2a_token = ""
        if "g2a" in usable:
            usable, g2a_token = self._g2a_prepare(job, usable)
            if not usable:
                return

        # 4. CPA 全量合批（单请求，先于 G2A 逐账号，便于尽早反馈结果）
        if "cpa" in usable:
            self._push_cpa_batch(job, candidates)
            if job.cancel_event.is_set():
                return

        # 5. G2A 逐账号并发导入
        if "g2a" in usable:
            self._push_g2a_concurrent(job, candidates, g2a_token)

    def _screen(self, job: PushJob) -> list[dict[str, Any]]:
        """资格预筛：返回候选账号行，跳过账号写入 skipped_list。

        跳过明细只记入 skipped_list 与文件日志，任务日志聚合为一条摘要，
        避免全量模式下逐账号刷屏、日志一次性倾泻。
        """
        by_id = {a["id"]: a for a in get_all_accounts()}
        accounts = (
            [by_id[i] for i in job.account_ids if i in by_id]
            if job.account_ids
            else list(by_id.values())
        )
        candidates: list[dict[str, Any]] = []
        skip_summary: dict[str, int] = {}
        for acc in accounts:
            if job.cancel_event.is_set():
                break
            aid = int(acc.get("id") or 0)
            skip_reason = ""
            # 未认证账号仅可走认证（/api/pool/auth），推送一律排除
            if not str(acc.get("access_token") or "").strip():
                skip_reason = "未认证，仅可认证"
            elif int(acc.get("status") or 1) != STATUS_ACTIVE:
                skip_reason = f"状态非正常(status={acc.get('status')})"
            if skip_reason:
                job.skipped_list.append({"id": aid, "reason": skip_reason})
                # 状态非正常按具体状态值归并，避免摘要碎片化
                summary_key = (
                    "状态非正常"
                    if skip_reason.startswith("状态非正常")
                    else skip_reason
                )
                skip_summary[summary_key] = skip_summary.get(summary_key, 0) + 1
                logger.debug(f"[推送] {_who(acc)} 已跳过：{skip_reason}")
                continue
            candidates.append(acc)
        if skip_summary:
            detail = "、".join(
                f"{reason} {n} 个"
                for reason, n in sorted(skip_summary.items(), key=lambda kv: -kv[1])
            )
            job.append_log(
                "INFO",
                f"[推送] 预筛完成：候选 {len(candidates)} 个，跳过 {detail}",
            )
        # 按注册时间倒序执行（新注册的账号优先处理）
        candidates.sort(
            key=lambda a: str(a.get("created_at") or ""), reverse=True
        )
        job.count = len(candidates)
        return candidates

    def _usable_targets(self, job: PushJob) -> dict[str, str]:
        """目标可用性：配置完整的目标进入用于列表，缺失的记日志并剔除。"""
        usable: dict[str, str] = {}
        for target in job.targets:
            if target in self._missing_targets([target]):
                job.append_log(
                    "ERROR",
                    f"[推送] {self._target_label(target)} 不可用：配置缺失",
                )
                logger.error(f"[推送] {self._target_label(target)} 不可用：配置缺失")
            else:
                usable[target] = self._target_label(target)
        if not usable:
            job.append_log("ERROR", "[推送] 全部目标不可用，任务中止")
            logger.error("[推送] 全部目标不可用，任务中止")
            job.error = "推送目标配置缺失"
        return usable

    def _g2a_prepare(self, job: PushJob, usable: dict[str, str]) -> tuple[dict[str, str], str]:
        """G2A 预登录；失败时剔除该目标（不阻塞 CPA）。"""
        logger.info("[推送] G2A 预登录中")
        token, err = g2a_login(
            base_url=config.G2A_BASE_URL,
            username=config.G2A_USERNAME,
            password=config.G2A_PASSWORD,
        )
        if err:
            job.append_log("ERROR", f"[推送] G2A 预登录失败：{err}")
            logger.error(f"[推送] G2A 预登录失败：{err}")
            del usable["g2a"]
            if not usable:
                job.append_log("ERROR", "[推送] 无可用目标，任务中止")
                logger.error("[推送] 无可用目标，任务中止")
                job.error = err
            return usable, ""
        logger.success("[推送] G2A 预登录成功，token 已获取")
        return usable, token

    def _push_cpa_batch(self, job: PushJob, candidates: list[dict[str, Any]]) -> None:
        """CPA 全量合批上传；结果按账号摊分进度与成败。"""
        total = len(candidates)
        t0 = time.monotonic()
        logger.info(f"[推送] CPA 合批上传开始: {total} 个账号")
        result = push_batch_cpa(
            candidates, base_url=config.CPA_BASE_URL, management_key=config.CPA_MANAGEMENT_KEY
        )
        uploaded = int(result.get("uploaded") or 0)
        failed = int(result.get("failed") or 0)
        if result.get("ok"):
            job.pushed += total
        else:
            # 批量无账号明细：按 uploaded/failed 计数摊分
            job.pushed += max(0, min(uploaded, total))
            job.failed += max(0, min(failed, total))
            if job.failed == 0:
                job.failed = total
        job.done += total
        msg = str(result.get("message") or "CPA 推送失败")
        job.append_log(
            "SUCCESS" if result.get("ok") else "ERROR",
            f"[推送] CPA {msg} · {elapsed_label(t0)}",
        )

    def _push_g2a_concurrent(
        self, job: PushJob, candidates: list[dict[str, Any]], g2a_token: str
    ) -> None:
        """G2A：20 个 worker 并发导入，每个 worker 做完一个号再隔 1 秒接下一个。"""

        def work(acc: dict[str, Any]) -> dict[str, Any] | None:
            """单账号导入；取消信号下不发送请求。"""
            if job.cancel_event.is_set():
                return None
            return push_one_g2a(acc, base_url=config.G2A_BASE_URL, access_token=g2a_token)

        def on_complete(_index: int, acc: dict[str, Any], result: Any) -> None:
            level = "ERROR"
            message = ""
            with job._lock:
                if isinstance(result, Exception):
                    job.failed += 1
                    job.done += 1
                    message = f"[推送] {_who(acc)} 失败：{type(result).__name__}: {result}"
                elif result is None:
                    return
                else:
                    job.done += 1
                    label = _who(acc)
                    body = str(result.get("message") or "")
                    if result.get("ok"):
                        job.pushed += 1
                        level = "SUCCESS"
                        message = f"{label}·推送成功"
                    else:
                        job.failed += 1
                        message = f"{label}·{body}"
            job.append_log(level, message)
            if isinstance(result, Exception):
                logger.error(
                    f"[推送] {_who(acc)} G2A 导入异常: {type(result).__name__}: {result}"
                )

        run_account_workers(
            candidates,
            work,
            workers=ACCOUNT_WORKERS,
            thread_name_prefix="推送",
            should_stop=job.cancel_event.is_set,
            on_complete=on_complete,
        )


# 进程内单例（模块级，对齐 api/jobs.manager 的用法）
push_manager = PushManager()
