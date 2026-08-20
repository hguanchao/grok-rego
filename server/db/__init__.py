"""SQLite：连接、账号、认证池、初始化。"""

from __future__ import annotations

"""
数据库连接基础模块。

提供统一的 SQLite 连接上下文管理器与北京时间戳工具，供 accounts / auth_pool 模块使用。
"""

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
from typing import Any

from core.logger import logger
from core.util import decode_jwt_exp, mask_email, now_iso_tz

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
    logger.success(f"[数据库] 账号已保存 (ID: {account_id}, email: {mask_email(email)})")
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
    logger.warning(f"[数据库] 未找到账号: {mask_email(email)}")
    return None


def update_risk(
    email: str, bfs: int | None = None, risk: str | None = None, checked_at: str | None = None
) -> bool:
    """更新账号的风控体检结果（bfs / risk / checked_at）。"""
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE accounts SET bfs=?, risk=?, checked_at=?, updated_at=? WHERE email=?",
            (bfs, risk, checked_at, now_iso_tz(), email),
        )
        conn.commit()
        is_updated = cursor.rowcount > 0
    if is_updated:
        logger.success(
            f"[数据库] 已更新风控结果 (email: {mask_email(email)}, bfs: {bfs})"
        )
    else:
        logger.warning(f"[数据库] 未找到账号，风控结果未更新: {mask_email(email)}")
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
            f"[数据库] 账号状态更新 (email: {mask_email(email)}, status: {status})"
        )
    else:
        logger.warning(f"[数据库] 未找到账号，状态未更新: {mask_email(email)}")
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
    "expires_in, status, reason, bfs, risk, checked_at, is_deleted, created_at, updated_at, "
    "used"
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
    """获取号池统计（排除已软删）。"""
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(status, 1) = 1 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN COALESCE(status, 1) IN (2, 3) THEN 1 ELSE 0 END) AS pending_action,
                SUM(CASE WHEN COALESCE(status, 1) IN (4, 5) THEN 1 ELSE 0 END) AS abnormal
            FROM accounts WHERE COALESCE(is_deleted, 0) = 0
            """
        ).fetchone()
    return {
        "total": row["total"] or 0,
        "active": row["active"] or 0,
        "pending_action": row["pending_action"] or 0,
        "abnormal": row["abnormal"] or 0,
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
    logger.info(f"[认证池] 已加入认证池: {mask_email(email)} (account_id={account_id})")


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
    logger.info(f"[认证池] 已移除: {mask_email(email)}")


"""
数据库初始化入口。

聚合 accounts / auth_pool 表的初始化。
"""



def init_db() -> None:
    """初始化全部数据库表结构。"""
    init_accounts_table()
    init_auth_pool_table()
