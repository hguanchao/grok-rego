"""SQLite：连接、账号、认证池、初始化。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator

from core.config import DB_PATH


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """创建数据库连接（上下文管理器）：开启 WAL 模式 + 外键 + 忙等待，自动关闭。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()

"""
accounts 表数据访问模块。

管理账号表的 CRUD：注册账号、保存 token、查询账号、风控结果更新等。
"""

import time
from datetime import date, timedelta
from typing import Any

from core.logger import logger
from core.util import decode_jwt_exp, now_dt, now_iso_tz

# 兼容已有数据库：表已存在但缺少新字段时补加
_ACCOUNT_MIGRATIONS = [
    ("access_token", "TEXT"),
    ("refresh_token", "TEXT"),
    ("expires_in", "INTEGER"),
    ("bfs", "INTEGER"),
    ("risk", "TEXT"),
    ("checked_at", "TEXT"),
    ("status", "INTEGER DEFAULT 1"),
    ("reason", "TEXT"),
    ("is_deleted", "INTEGER DEFAULT 0"),
    ("updated_at", "TEXT"),
    ("used", "INTEGER DEFAULT 500000"),
    ("dumbed", "INTEGER"),
    ("inspect_tps", "REAL"),
    ("inspect_thinking", "INTEGER"),
    ("inspected_at", "TEXT"),
]

# 账号状态码（整数，对齐前端 PoolPage 筛选）
# 1  active             — 正常可用（含待 Token 交换）
# 2  reauth             — 需重登（token 过期，需刷新或重新登录）
# 3  limited            — 限额（上游返回 spending-limit）
# 4  permission_denied  — 权限被拒（上游 403/拒绝）
# 5  abnormal           — 异常（探活失败、网络异常等）
# 6  disabled           — 账号禁用
STATUS_ACTIVE = 1
STATUS_REAUTH = 2
STATUS_LIMITED = 3
STATUS_PERMISSION_DENIED = 4
STATUS_ABNORMAL = 5
STATUS_DISABLED = 6


def init_accounts_table() -> None:
    """初始化 accounts 表并兼容已有表结构。"""
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                sso_cookie TEXT,
                access_token TEXT,
                refresh_token TEXT,
                expires_in INTEGER,
                status INTEGER DEFAULT 1,
                reason TEXT,
                is_deleted INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()}
        for column, column_type in _ACCOUNT_MIGRATIONS:
            if column not in existing_cols:
                cursor.execute(f"ALTER TABLE accounts ADD COLUMN {column} {column_type}")
        # 兼容旧库：ALTER 补列的历史数据会留下 NULL，回填为默认状态
        cursor.execute("UPDATE accounts SET status = 1 WHERE status IS NULL")
        cursor.execute("UPDATE accounts SET is_deleted = 0 WHERE is_deleted IS NULL")
        # 兼容旧库：updated_at 补列后回填为创建时间（历史数据无更新时间）
        cursor.execute("UPDATE accounts SET updated_at = created_at WHERE updated_at IS NULL")
        cursor.execute("UPDATE accounts SET used = 500000 WHERE used IS NULL")
        # 兼容旧库：如果存在旧的 quota_* 列，把 quota_limit - quota_used 迁移到 used
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()}
        if "quota_limit" in existing and "quota_used" in existing:
            cursor.execute(
                "UPDATE accounts SET used = MAX(0, COALESCE(quota_limit, 500000) - COALESCE(quota_used, 0)) "
                "WHERE used = 500000 OR used IS NULL"
            )
        # 兼容旧库：过往 created_at/updated_at 曾以裸北京时间串（无时区标记）写入，
        # 统一迁移为 ISO 带时区格式（YYYY-MM-DDTHH:MM:SS+08:00），保证含时区信息。
        # 分列独立判断，避免 created_at 已迁移而 updated_at 仍是裸串时漏迁；
        # 迁移后不再含空格分隔，天然幂等。
        cursor.execute(
            """
            UPDATE accounts
               SET created_at = replace(created_at, ' ', 'T') || '+08:00'
             WHERE created_at LIKE '% %'
               AND created_at NOT LIKE '%T%'
            """
        )
        cursor.execute(
            """
            UPDATE accounts
               SET updated_at = replace(updated_at, ' ', 'T') || '+08:00'
             WHERE updated_at LIKE '% %'
               AND updated_at NOT LIKE '%T%'
            """
        )
        conn.commit()
    logger.info("[数据库] accounts 表初始化完成")


def _row_to_account(row: sqlite3.Row) -> dict[str, Any]:
    """将查询行转为 dict。sso_cookie 为字符串（sso 会话凭证原值）。"""
    return dict(row)


def save_account(
    email: str,
    password: str,
    first_name: str | None,
    last_name: str | None,
    sso_cookie: str | None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    status: int | None = None,
    reason: str | None = None,
) -> int:
    """保存或更新账号信息，返回账号 id。如果 email 已存在则更新。"""
    if status is None:
        status = STATUS_ACTIVE
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO accounts (email, password, first_name, last_name, sso_cookie,
                                  access_token, refresh_token, expires_in, status, reason,
                                  is_deleted, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                password=excluded.password,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                sso_cookie=excluded.sso_cookie,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_in=excluded.expires_in,
                status=COALESCE(excluded.status, accounts.status),
                reason=COALESCE(excluded.reason, accounts.reason),
                is_deleted=0,
                updated_at=excluded.updated_at
            """,
            (
                email, password, first_name, last_name,
                sso_cookie,
                access_token, refresh_token, expires_in,
                status, reason, now_iso_tz(), now_iso_tz(),
            ),
        )
        conn.commit()
        account_id = cursor.execute(
            "SELECT id FROM accounts WHERE email=?", (email,)
        ).fetchone()[0]
    logger.success(f"[数据库] 账号已保存 (ID: {account_id}, email: {email})")
    return account_id


def get_account_by_email(email: str) -> dict[str, Any] | None:
    """根据邮箱获取账号信息，返回 dict 或 None。"""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM accounts WHERE email=? AND COALESCE(is_deleted, 0) = 0", (email,)
        ).fetchone()
    if row:
        return _row_to_account(row)
    logger.warning(f"[数据库] 未找到账号: {email}")
    return None


def list_gateway_candidates() -> list[dict[str, Any]]:
    """网关自动选号候选：ACTIVE、已认证、未降智的未删除账号（按 id 升序）。

    dumbed=1 由推理巡检写入，转发会拿到无思考链的降智号，这里直接排除。
    dumbed 为空（尚未巡检）仍可入选。只返回 id / email / access_token。
    """
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, email, access_token FROM accounts "
            "WHERE COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(status, 1) = ? "
            "AND COALESCE(access_token, '') != '' "
            "AND COALESCE(dumbed, 0) = 0 "
            "ORDER BY id",
            (STATUS_ACTIVE,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_due_refresh_candidates(
    due_within_sec: int, exclude_statuses: tuple[int, ...]
) -> list[dict[str, Any]]:
    """收集临期需续期账号：token exp ≤ now + due_within_sec，且状态不在排除集。

    为 AutoRefresher 服务；仅关注拥有 access_token + refresh_token 的账号，
    JWT exp 解析失败的视为临期（交由刷新链路判断）。
    """
    status_placeholders = ",".join("?" * len(exclude_statuses))
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, email, access_token, refresh_token FROM accounts "
            "WHERE COALESCE(is_deleted, 0) = 0 AND access_token IS NOT NULL AND access_token != '' "
            "AND refresh_token IS NOT NULL AND refresh_token != '' "
            f"AND status NOT IN ({status_placeholders}) "
            "ORDER BY created_at DESC",
            tuple(exclude_statuses),
        ).fetchall()
    cutoff = time.time() + due_within_sec
    due = []
    for row in rows:
        exp = decode_jwt_exp(str(row["access_token"] or ""))
        if exp is None or exp <= cutoff:
            due.append(dict(row))
    return due


def get_account_by_id(account_id: int) -> dict[str, Any] | None:
    """按主键取未删除账号；不存在返回 None。"""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND COALESCE(is_deleted, 0) = 0",
            (int(account_id),),
        ).fetchone()
    return _row_to_account(row) if row else None


def update_account_inspect(
    account_id: int,
    *,
    dumbed: int,
    inspect_tps: float,
    inspect_thinking: int,
) -> bool:
    """回写巡检降智判定（dumbed / 吞吐 / 是否有思考链）。"""
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET dumbed=?, inspect_tps=?, inspect_thinking=?, "
            "inspected_at=?, updated_at=? WHERE id=? AND COALESCE(is_deleted, 0) = 0",
            (
                int(dumbed),
                float(inspect_tps),
                int(inspect_thinking),
                now_iso_tz(),
                now_iso_tz(),
                int(account_id),
            ),
        )
        conn.commit()
        ok = cursor.rowcount > 0
    if ok:
        logger.info(
            f"[数据库] 巡检结果 id={account_id} dumbed={dumbed} "
            f"tps={inspect_tps:.1f} thinking={inspect_thinking}"
        )
    return ok


def update_risk(
    email: str, bfs: int | None = None, risk: str | None = None, checked_at: str | None = None
) -> bool:
    """更新账号注册风控体检结果（bfs / risk / checked_at）。"""
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET bfs=?, risk=?, checked_at=?, updated_at=? WHERE email=?",
            (bfs, risk, checked_at, now_iso_tz(), email),
        )
        conn.commit()
        is_updated = cursor.rowcount > 0
    if is_updated:
        logger.success(f"[数据库] 已更新风控结果 (email: {email}, bfs: {bfs})")
    else:
        logger.warning(f"[数据库] 未找到账号，风控结果未更新: {email}")
    return is_updated


def update_account_status(
    email: str, status: int, reason: str | None = None
) -> bool:
    """更新账号状态和原因（status / reason）。reason 为 None 时保留原值。"""
    with connect() as conn:
        cursor = conn.cursor()
        if reason is not None:
            cursor.execute(
                "UPDATE accounts SET status=?, reason=?, updated_at=? WHERE email=?",
                (status, reason, now_iso_tz(), email),
            )
        else:
            cursor.execute(
                "UPDATE accounts SET status=?, updated_at=? WHERE email=?",
                (status, now_iso_tz(), email),
            )
        conn.commit()
        is_updated = cursor.rowcount > 0
    if is_updated:
        logger.info(
            f"[数据库] 账号状态更新 (email: {email}, status: {status})"
        )
    else:
        logger.warning(f"[数据库] 未找到账号，状态未更新: {email}")
    return is_updated


def update_account_status_by_ids(
    account_ids: list[int], status: int | None, reason: str | None = None
) -> int:
    """按 id 批量更新账号状态并写入原因，返回受影响行数。

    status 为 None 时只更新 reason（不改状态，用于网络失败等不判死场景）；
    reason 为 None 时保留原原因。
    """
    if not account_ids:
        return 0
    placeholders = ",".join("?" * len(account_ids))
    with connect() as conn:
        cursor = conn.cursor()
        if status is not None and reason is not None:
            cursor.execute(
                f"UPDATE accounts SET status=?, reason=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                [status, reason, now_iso_tz(), *account_ids],
            )
        elif status is not None:
            cursor.execute(
                f"UPDATE accounts SET status=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                [status, now_iso_tz(), *account_ids],
            )
        elif reason is not None:
            cursor.execute(
                f"UPDATE accounts SET reason=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                [reason, now_iso_tz(), *account_ids],
            )
        else:
            return 0
        conn.commit()
        return cursor.rowcount


def update_account_tokens(
    account_id: int,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    reason: str | None = None,
) -> bool:
    """按 id 回写账号 token（巡检 / 重登刷新后），返回是否更新成功。

    refresh_token / expires_in 为 None 时保留原值（部分响应可能不含新 refresh_token）。
    """
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET access_token=?, "
            "refresh_token=COALESCE(?, refresh_token), "
            "expires_in=COALESCE(?, expires_in), "
            "reason=COALESCE(?, reason), "
            "updated_at=? "
            "WHERE id=? AND COALESCE(is_deleted, 0) = 0",
            (access_token, refresh_token, expires_in, reason, now_iso_tz(), account_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_account_sso_cookie(
    account_id: int,
    sso_cookie: str,
    reason: str | None = None,
) -> bool:
    """按 id 回写 sso_cookie（补 cookie / 重登会话），不改动 token。"""
    value = str(sso_cookie or "").strip()
    if not value:
        return False
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET sso_cookie=?, "
            "reason=COALESCE(?, reason), updated_at=? "
            "WHERE id=? AND COALESCE(is_deleted, 0) = 0",
            (value, reason, now_iso_tz(), account_id),
        )
        conn.commit()
        ok = cursor.rowcount > 0
    if ok:
        logger.success(f"[数据库] 已更新 SSO cookie (ID: {account_id})")
    else:
        logger.warning(f"[数据库] 未更新 SSO cookie (ID: {account_id})")
    return ok


def get_all_accounts() -> list[dict[str, Any]]:
    """获取所有账号列表。"""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM accounts WHERE COALESCE(is_deleted, 0) = 0 ORDER BY id"
        ).fetchall()
    return [_row_to_account(row) for row in rows]


# ─── 号池管理查询 ───────────────────────────────────────────

# 对外暴露的字段（sso_cookie 等额外凭据不返回前端）
_POOL_COLUMNS = (
    "id, email, password, first_name, last_name, "
    "access_token IS NOT NULL AND access_token != '' AS has_token, "
    "refresh_token IS NOT NULL AND refresh_token != '' AS has_refresh, "
    "expires_in, status, reason, dumbed, inspect_tps, inspect_thinking, inspected_at, "
    "is_deleted, created_at, updated_at, used"
)


def query_accounts(
    *,
    page: int = 1,
    page_size: int = 20,
    statuses: list[int] | None = None,
    keyword: str | None = None,
    authed: str | None = None,
    expiry: str | None = None,
) -> dict[str, Any]:
    """分页查询账号列表（排除已软删），返回 {items, total, page, page_size}。

    - statuses: 状态值列表（逗号拆分传入，如 [4,5,6] 表示全部异常态）
    - expiry: 基于 JWT exp 与当前时间比较（到期 1h 内 / 已到期 / 未到期）；
      注意不能用上限返回的 expires_in 列——那是注册时的静态有效期秒数，与剩余时间无关。
    """
    where = ["COALESCE(is_deleted, 0) = 0"]
    params: list[Any] = []

    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where.append(f"COALESCE(status, 1) IN ({placeholders})")
        params.extend(statuses)
    if keyword:
        where.append("(email LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    if authed == "authed":
        where.append("access_token IS NOT NULL AND access_token != ''")
    elif authed == "unauthed":
        where.append("(access_token IS NULL OR access_token = '')")

    where_clause = " AND ".join(where)
    offset = max(0, (page - 1) * page_size)

    # 到期筛选需要逐行解码 JWT 的 exp（绝对时间戳）后才能判断，无法下推到 SQL
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        if expiry:
            now = int(time.time())
            candidates = conn.execute(
                f"SELECT id, access_token FROM accounts WHERE {where_clause}",
                params,
            ).fetchall()
            matched: list[int] = []
            for row in candidates:
                exp = decode_jwt_exp(row["access_token"])
                if exp is None:
                    continue
                if expiry == "expired" and exp <= now or expiry == "soon" and now < exp <= now + 3600 or expiry == "valid" and exp > now + 3600:
                    matched.append(row["id"])
            total = len(matched)
            page_ids = matched[offset : offset + page_size]
            rows: list[sqlite3.Row] = []
            exp_rows: list[sqlite3.Row] = []
            if page_ids:
                ph = ",".join("?" * len(page_ids))
                rows = conn.execute(
                    f"SELECT {_POOL_COLUMNS} FROM accounts WHERE id IN ({ph}) "
                    f"ORDER BY id DESC",
                    page_ids,
                ).fetchall()
                exp_rows = conn.execute(
                    f"SELECT id, access_token FROM accounts WHERE id IN ({ph}) ",
                    page_ids,
                ).fetchall()
        else:
            total = conn.execute(
                f"SELECT COUNT(*) FROM accounts WHERE {where_clause}", params
            ).fetchone()[0]
            exp_rows = conn.execute(
                f"SELECT id, access_token FROM accounts WHERE {where_clause} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
            rows = conn.execute(
                f"SELECT {_POOL_COLUMNS} FROM accounts WHERE {where_clause} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()

    exp_map = {row["id"]: decode_jwt_exp(row["access_token"]) for row in exp_rows}
    items = [dict(row) for row in rows]
    for item in items:
        item["expires_at"] = exp_map.get(item["id"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_pool_stats() -> dict[str, int]:
    """获取号池统计（排除已软删），含各任务全量模式的候选账号数。"""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(status, 1) = 1 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN COALESCE(status, 1) IN (2, 3) THEN 1 ELSE 0 END) AS pending_action,
                SUM(CASE WHEN COALESCE(status, 1) IN (4, 5) THEN 1 ELSE 0 END) AS abnormal,
                SUM(
                    CASE WHEN COALESCE(access_token, '') != ''
                              AND COALESCE(status, 1) = 1 THEN 1 ELSE 0 END
                ) AS push_count,
                SUM(
                    CASE WHEN COALESCE(access_token, '') = ''
                              AND COALESCE(status, 1) != 6 THEN 1 ELSE 0 END
                ) AS auth_count,
                SUM(
                    CASE WHEN COALESCE(access_token, '') != ''
                              AND COALESCE(status, 1) = 2 THEN 1 ELSE 0 END
                ) AS reauth_count,
                SUM(
                    CASE WHEN COALESCE(access_token, '') != ''
                              AND COALESCE(status, 1) NOT IN (2, 6) THEN 1 ELSE 0 END
                ) AS inspect_count
            FROM accounts WHERE COALESCE(is_deleted, 0) = 0
            """
        ).fetchone()
    return {
        "total": row["total"] or 0,
        "active": row["active"] or 0,
        "pending_action": row["pending_action"] or 0,
        "abnormal": row["abnormal"] or 0,
        "task_counts": {
            "push": row["push_count"] or 0,
            "auth": row["auth_count"] or 0,
            "reauth": row["reauth_count"] or 0,
            "inspect": row["inspect_count"] or 0,
        },
    }


def soft_delete_accounts(account_ids: list[int]) -> int:
    """软删除指定账号（is_deleted = 1），返回受影响行数。"""
    if not account_ids:
        return 0
    placeholders = ",".join("?" * len(account_ids))
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE accounts SET is_deleted = 1, updated_at=? WHERE id IN ({placeholders})",
            [now_iso_tz(), *account_ids],
        )
        conn.commit()
        return cursor.rowcount


"""
auth_pool 认证池表数据访问模块。

管理 Allow 授权完成、待交换 Token（POST /oauth2/token）的账号队列。
"""


from core.util import now_str

# 目标表结构必需列：旧结构（无 account_id 冗余）不满足时重建
_AUTH_POOL_REQUIRED_COLS = {"account_id", "email", "device_code", "interval", "created_at"}

_AUTH_POOL_SCHEMA = """
    CREATE TABLE IF NOT EXISTS auth_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        email TEXT NOT NULL UNIQUE,
        device_code TEXT,
        interval INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    )
"""


def init_auth_pool_table() -> None:
    """初始化认证池表：id 主键自增 + account_id 冗余账号表 id。"""
    with connect() as conn:
        cursor = conn.cursor()
        # 兼容旧结构（无 account_id 等字段）：列不满足目标结构则重建
        table_info = cursor.execute(
            "SELECT * FROM sqlite_master WHERE type='table' AND name='auth_pool'"
        ).fetchall()
        if table_info:
            existing_cols = {
                row[1] for row in cursor.execute("PRAGMA table_info(auth_pool)").fetchall()
            }
            if not _AUTH_POOL_REQUIRED_COLS.issubset(existing_cols):
                cursor.execute("DROP TABLE auth_pool")
        cursor.execute(_AUTH_POOL_SCHEMA)
        conn.commit()


def add_to_auth_pool(email: str, device_code: str, interval: int, account_id: int) -> None:
    """将账号加入认证池（已存在则忽略，保留首次入库的 device_code）。"""
    init_auth_pool_table()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO auth_pool (account_id, email, device_code, interval, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account_id, email, device_code, interval, now_str()),
        )
        conn.commit()
    logger.info(f"[认证池] 已加入认证池: {email} (account_id={account_id})")


def get_auth_pool() -> list[dict[str, Any]]:
    """获取认证池全部待交换 Token 的账号。"""
    init_auth_pool_table()
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM auth_pool ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


def remove_from_auth_pool(email: str) -> None:
    """授权完成后从授权池移除账号。"""
    init_auth_pool_table()
    with connect() as conn:
        conn.execute("DELETE FROM auth_pool WHERE email=?", (email,))
        conn.commit()
    logger.info(f"[认证池] 已移除: {email}")


"""
usages 用量记录。

网关请求流水：客户端 → 请求 → 账号 → 结果 → token → 时间。
时间字段与 accounts 一致：北京时间 ISO（YYYY-MM-DDTHH:MM:SS+08:00）。
布尔/状态用 INTEGER；计数字段 NOT NULL DEFAULT 0，禁止 NULL。
"""

_USAGES_COLUMNS = (
    "id",
    "ip",
    "client_ua",
    "endpoint",
    "model",
    "effort",
    "stream",
    "account_id",
    "account_email",
    "status",
    "reason",
    "prompt_tokens",
    "completion_tokens",
    "cache_tokens",
    "reasoning_tokens",
    "output_tps",
    "created_at",
)
_USAGES_TOKEN_COLUMNS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_tokens",
    "reasoning_tokens",
)
# 声明类型（SQLite 亲和性仍按 INTEGER/TEXT；长度仅作规范约束）
_USAGES_TYPES = {
    "id": "INTEGER",
    "ip": "VARCHAR(45)",
    "client_ua": "VARCHAR(512)",
    "endpoint": "VARCHAR(64)",
    "model": "VARCHAR(64)",
    "effort": "VARCHAR(32)",
    "stream": "INTEGER",
    "account_id": "INTEGER",
    "account_email": "VARCHAR(255)",
    "status": "INTEGER",
    "reason": "VARCHAR(255)",
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "cache_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "output_tps": "REAL",
    "created_at": "TEXT",
}

_USAGES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS usages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip VARCHAR(45),
        client_ua VARCHAR(512),
        endpoint VARCHAR(64) NOT NULL DEFAULT '',
        model VARCHAR(64) NOT NULL,
        effort VARCHAR(32),
        stream INTEGER NOT NULL DEFAULT 0,
        account_id INTEGER,
        account_email VARCHAR(255),
        status INTEGER NOT NULL DEFAULT 0,
        reason VARCHAR(255),
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cache_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        output_tps REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
"""


def _usages_select_expr(name: str, old_types: dict[str, str]) -> str:
    """旧列 → 新列表达式：毫秒时间戳转北京 ISO，token NULL 填 0。"""
    declared = (old_types.get(name) or "").upper()
    if name == "created_at" and "TEXT" not in declared and "CHAR" not in declared:
        return (
            "replace(datetime(created_at / 1000, 'unixepoch', '+8 hours'), ' ', 'T')"
            " || '+08:00'"
        )
    if name in _USAGES_TOKEN_COLUMNS or name == "output_tps":
        return f"COALESCE({name}, 0)"
    return name


def _usages_schema_ok(info_rows: list[sqlite3.Row] | list[tuple[Any, ...]]) -> bool:
    """列名、顺序、声明类型均对齐才跳过重建。"""
    if [row[1] for row in info_rows] != list(_USAGES_COLUMNS):
        return False
    for row in info_rows:
        name = row[1]
        declared = str(row[2] or "").upper().replace(" ", "")
        expect = _USAGES_TYPES[name].upper().replace(" ", "")
        if declared != expect:
            return False
    return True


def init_usages_table() -> None:
    """初始化 usages：无表则建；列序/类型不对则重建并迁数据。"""
    with connect() as conn:
        cursor = conn.cursor()
        exists = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usages'"
        ).fetchone()
        if exists:
            info = cursor.execute("PRAGMA table_info(usages)").fetchall()
            if _usages_schema_ok(info):
                _ensure_usages_indexes(cursor)
                conn.commit()
                return
            old_types = {row[1]: str(row[2] or "") for row in info}
            old_cols = set(old_types)
            cursor.execute("ALTER TABLE usages RENAME TO usages_old")
            cursor.execute(_USAGES_SCHEMA)
            copy_cols = [name for name in _USAGES_COLUMNS if name in old_cols]
            col_sql = ", ".join(copy_cols)
            select_sql = ", ".join(
                _usages_select_expr(name, old_types) for name in copy_cols
            )
            cursor.execute(
                f"INSERT INTO usages ({col_sql}) SELECT {select_sql} FROM usages_old"
            )
            cursor.execute("DROP TABLE usages_old")
            logger.info("[数据库] usages 已重建（类型与列序对齐）")
        else:
            cursor.execute(_USAGES_SCHEMA)
            logger.info("[数据库] usages 表初始化完成")
        _ensure_usages_indexes(cursor)
        conn.commit()


def insert_usage(
    *,
    ip: str | None = None,
    client_ua: str | None = None,
    endpoint: str = "",
    model: str = "",
    effort: str | None = None,
    stream: bool = False,
    account_id: int | None = None,
    account_email: str | None = None,
    status: int = 0,
    reason: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_tokens: int = 0,
    reasoning_tokens: int = 0,
    output_tps: float = 0,
) -> None:
    """写入一条网关用量记录（每次请求一行，幂等可重复调用）。

    落库供前端用量统计（/api/usage）聚合展示；status=1 记成功，其余记失败。
    token 各列均为尽力而为：无法从上游解析时记 0，绝不阻塞转发。
    output_tps 为可见输出 token/s（流式按首字节后窗口，非流式按全程）。
    """
    init_usages_table()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO usages (
                ip, client_ua, endpoint, model, effort, stream,
                account_id, account_email, status, reason,
                prompt_tokens, completion_tokens, cache_tokens, reasoning_tokens,
                output_tps, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ip, client_ua, endpoint, model, effort,
                1 if stream else 0,
                account_id, account_email, status, reason,
                prompt_tokens, completion_tokens, cache_tokens, reasoning_tokens,
                float(output_tps or 0),
                now_iso_tz(),
            ),
        )
        conn.commit()


def _ensure_usages_indexes(cursor: sqlite3.Cursor) -> None:
    """用量查询索引：时间 / 模型 / 账号。"""
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usages_created ON usages (created_at)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usages_model ON usages (model)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usages_account ON usages (account_email)"
    )


def _usages_int(row: sqlite3.Row | None, key: str) -> int:
    if row is None:
        return 0
    try:
        return int(row[key] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        return 0


def _usage_rate(part: int, whole: int) -> float:
    """占比百分比，分母为 0 时返回 0。"""
    if whole <= 0:
        return 0.0
    return round(part * 100.0 / whole, 2)


def _usage_window(days: int) -> tuple[int, str, str]:
    """北京日历日闭区间 [from, to]。days=1 即今日，上限 90。"""
    days = max(1, min(int(days), 90))
    today = now_dt().date()
    start = today - timedelta(days=days - 1)
    return days, start.isoformat(), today.isoformat()


def _usage_date_where(date_from: str, date_to: str) -> tuple[str, list[str]]:
    """北京日历日闭区间。ISO 文本可直接比较，走 idx_usages_created。"""
    end = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
    return "created_at >= ? AND created_at < ?", [date_from, end]


def _fill_usage_days(
    rows: list[sqlite3.Row], date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """日桶补齐窗口内每一天，避免趋势图断档。"""
    by_day = {str(row["day"]): row for row in rows}
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    out: list[dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        src = by_day.get(key)
        out.append(
            {
                "day": key,
                "requests": _usages_int(src, "requests"),
                "success": _usages_int(src, "success"),
                "total_tokens": _usages_int(src, "total_tokens"),
            }
        )
        cursor += timedelta(days=1)
    return out


def _fill_usage_hours(rows: list[sqlite3.Row], day: str) -> list[dict[str, Any]]:
    """今日 24 小时桶补齐（00–23）。"""
    by_hour = {str(row["hour"]): row for row in rows}
    out: list[dict[str, Any]] = []
    for hour in range(24):
        key = f"{day}T{hour:02d}"
        src = by_hour.get(key)
        out.append(
            {
                "hour": f"{day}T{hour:02d}:00:00+08:00",
                "requests": _usages_int(src, "requests"),
                "success": _usages_int(src, "success"),
                "total_tokens": _usages_int(src, "total_tokens"),
            }
        )
    return out


_USAGE_BUCKET_SELECT = """
    COUNT(*) AS requests,
    SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS success,
    SUM(prompt_tokens + completion_tokens) AS total_tokens
"""


def query_usage_summary(days: int = 1) -> dict[str, Any]:
    """窗口内 KPI + 模型分布 + 日/小时趋势。今日补齐 24 小时桶，多日补齐日历日。"""
    init_usages_table()
    days, date_from, date_to = _usage_window(days)
    where, params = _usage_date_where(date_from, date_to)
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        agg = conn.execute(
            f"""
            SELECT
                COUNT(*) AS requests,
                SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status != 1 THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) AS stream_count,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(cache_tokens) AS cache_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens
            FROM usages WHERE {where}
            """,
            params,
        ).fetchone()
        by_model_rows = conn.execute(
            f"""
            SELECT model,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS success,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cache_tokens) AS cache_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens
            FROM usages WHERE {where}
            GROUP BY model
            ORDER BY requests DESC, model
            """,
            params,
        ).fetchall()
        by_day_rows = conn.execute(
            f"""
            SELECT substr(created_at, 1, 10) AS day, {_USAGE_BUCKET_SELECT}
            FROM usages WHERE {where}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ).fetchall()
        by_hour_rows: list[sqlite3.Row] = []
        if days == 1:
            by_hour_rows = conn.execute(
                f"""
                SELECT substr(created_at, 1, 13) AS hour, {_USAGE_BUCKET_SELECT}
                FROM usages WHERE {where}
                GROUP BY hour
                ORDER BY hour
                """,
                params,
            ).fetchall()

    requests = _usages_int(agg, "requests")
    success = _usages_int(agg, "success")
    failed = _usages_int(agg, "failed")
    prompt = _usages_int(agg, "prompt_tokens")
    completion = _usages_int(agg, "completion_tokens")
    cache = _usages_int(agg, "cache_tokens")
    reasoning = _usages_int(agg, "reasoning_tokens")
    total_tokens = prompt + completion
    by_model: list[dict[str, Any]] = []
    for row in by_model_rows:
        p = _usages_int(row, "prompt_tokens")
        c = _usages_int(row, "completion_tokens")
        by_model.append(
            {
                "model": row["model"] or "",
                "requests": _usages_int(row, "requests"),
                "success": _usages_int(row, "success"),
                "prompt_tokens": p,
                "completion_tokens": c,
                "cache_tokens": _usages_int(row, "cache_tokens"),
                "reasoning_tokens": _usages_int(row, "reasoning_tokens"),
                "total_tokens": p + c,
            }
        )
    return {
        "days": days,
        "from": date_from,
        "to": date_to,
        "summary": {
            "requests": requests,
            "success": success,
            "failed": failed,
            "stream_count": _usages_int(agg, "stream_count"),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_tokens": cache,
            "reasoning_tokens": reasoning,
            "total_tokens": total_tokens,
            "success_rate": _usage_rate(success, requests),
            "cache_hit_rate": _usage_rate(cache, prompt),
            "reasoning_share": _usage_rate(reasoning, total_tokens),
        },
        "by_model": by_model,
        "by_day": _fill_usage_days(by_day_rows, date_from, date_to),
        "by_hour": _fill_usage_hours(by_hour_rows, date_from) if days == 1 else [],
    }


def query_usage_recent(*, offset: int = 0, limit: int = 20) -> dict[str, Any]:
    """最近用量明细分页，与统计窗口无关。"""
    init_usages_table()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM usages").fetchone()[0]
        items = conn.execute(
            "SELECT * FROM usages ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "items": [dict(row) for row in items],
        "total": int(total or 0),
        "offset": offset,
        "limit": limit,
    }


_GROUPED_KEY = {
    "account": "COALESCE(NULLIF(account_email, ''), '未知')",
    "model": "COALESCE(NULLIF(model, ''), '未知')",
}


def query_usage_grouped(*, dimension: str) -> dict[str, Any]:
    """按维度聚合用量：account（账号）/ model（模型）。

    返回每个维度的请求数、成功/失败、token 汇总与最近一次时间。
    dimension 非法时抛 ValueError（API 映射 400）。
    """
    if dimension not in _GROUPED_KEY:
        raise ValueError(f"dimension 仅支持 {'/'.join(_GROUPED_KEY)}")
    init_usages_table()
    key_expr = _GROUPED_KEY[dimension]
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT {key_expr} AS key,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN status = 1 THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status != 1 THEN 1 ELSE 0 END) AS failed,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cache_tokens) AS cache_tokens,
                   SUM(reasoning_tokens) AS reasoning_tokens,
                   SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) AS stream_count,
                   MAX(created_at) AS last_at
            FROM usages
            GROUP BY key
            ORDER BY requests DESC, key
            """
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        p = _usages_int(row, "prompt_tokens")
        c = _usages_int(row, "completion_tokens")
        items.append(
            {
                "key": str(row["key"] or ""),
                "requests": _usages_int(row, "requests"),
                "success": _usages_int(row, "success"),
                "failed": _usages_int(row, "failed"),
                "prompt_tokens": p,
                "completion_tokens": c,
                "cache_tokens": _usages_int(row, "cache_tokens"),
                "reasoning_tokens": _usages_int(row, "reasoning_tokens"),
                "total_tokens": p + c,
                "stream_count": _usages_int(row, "stream_count"),
                "last_at": str(row["last_at"] or ""),
            }
        )
    return {"dimension": dimension, "items": items}


# ─── 网关运维聚合查询 ─────────────────────────────────────


def _usages_since(hours: int) -> tuple[str, list[str]]:
    """构造近 N 小时的时间过滤条件（与 now_iso_tz 同钟：北京时间 ISO 字符串比较）。"""
    threshold = (now_dt() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return "created_at >= ?", [threshold]


def query_channel_usage_24h() -> dict[str, dict[str, int]]:
    """近 24h 各网关通道请求统计（按 endpoint 前缀 /zen/v1、/grok/v1 归组）。

    返回形如 {"zen": {"requests", "failed", "tokens"}, "grok": {...}}；
    无记录的通道值为全零，不缺键。
    """
    init_usages_table()
    where, params = _usages_since(24)
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT CASE
                       WHEN endpoint LIKE '/zen/v1%' THEN 'zen'
                       WHEN endpoint LIKE '/grok/v1%' THEN 'grok'
                       ELSE 'other'
                   END AS channel,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN status != 1 THEN 1 ELSE 0 END) AS failed,
                   SUM(prompt_tokens + completion_tokens) AS tokens
            FROM usages WHERE {where}
            GROUP BY channel
            """,
            params,
        ).fetchall()
    out: dict[str, dict[str, int]] = {
        ch: {"requests": 0, "failed": 0, "tokens": 0} for ch in ("zen", "grok")
    }
    for row in rows:
        channel = str(row["channel"] or "other")
        if channel in out:
            out[channel] = {
                "requests": _usages_int(row, "requests"),
                "failed": _usages_int(row, "failed"),
                "tokens": _usages_int(row, "tokens"),
            }
    return out


def query_account_usage_24h() -> dict[int, int]:
    """近 24h Grok 通道各账号请求数（account_id 分组，供号池运行态展示）。"""
    init_usages_table()
    where, params = _usages_since(24)
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT account_id, COUNT(*) AS requests
            FROM usages
            WHERE {where} AND account_id IS NOT NULL AND account_id > 0
            GROUP BY account_id
            """,
            params,
        ).fetchall()
    return {int(row["account_id"]): _usages_int(row, "requests") for row in rows}


"""
数据库初始化入口。

聚合 accounts / auth_pool / usages 表的初始化。
"""


def init_db() -> None:
    """初始化全部数据库表结构（自动创建数据目录）。"""
    from core.config import DB_DIR
    import os

    os.makedirs(DB_DIR, exist_ok=True)
    init_accounts_table()
    init_auth_pool_table()
    init_usages_table()