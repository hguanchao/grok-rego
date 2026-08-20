export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

export type BadgeVariant =
  | "success"
  | "warning"
  | "destructive"
  | "secondary";

export const ACCOUNT_STATUS_VARIANT: Record<number, BadgeVariant> = {
  1: "success",
  2: "warning",
  3: "warning",
  4: "destructive",
  5: "destructive",
  6: "destructive",
};

export const RISK_VARIANT: Record<string, BadgeVariant> = {
  低: "success",
  中: "warning",
  高: "destructive",
};

export const STATUS_FILTER_OPTIONS: Array<{
  value: string;
  label: string;
}> = [
  { value: "all", label: "全部状态" },
  { value: "1", label: "正常" },
  { value: "2", label: "需重登" },
  { value: "3", label: "限额" },
  { value: "4", label: "权限被拒" },
  { value: "5", label: "异常" },
  { value: "6", label: "禁用" },
];

export const AUTH_FILTER_OPTIONS: Array<{
  value: string;
  label: string;
}> = [
  { value: "all", label: "全部认证" },
  { value: "authed", label: "已认证" },
  { value: "unauthed", label: "未认证" },
];

export const EXPIRY_FILTER_OPTIONS: Array<{
  value: string;
  label: string;
}> = [
  { value: "all", label: "全部到期" },
  { value: "soon", label: "1h 内" },
  { value: "expired", label: "已到期" },
  { value: "valid", label: "未到期" },
];

export function formatPoolExpiry(
  expiresAt: number | null,
  expiresIn: number | null,
): string {
  if (expiresAt != null) {
    const date = new Date(expiresAt * 1000);
    const pad = (value: number) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }
  if (expiresIn != null) {
    if (expiresIn > 86400) return `${Math.floor(expiresIn / 86400)}d`;
    if (expiresIn > 3600) return `${Math.floor(expiresIn / 3600)}h`;
    return `${Math.max(1, Math.floor(expiresIn / 60))}m`;
  }
  return "—";
}

export function isPoolExpired(expiresAt: number | null): boolean {
  // 仅以 JWT 绝对时间戳（expires_at，秒）判定；expires_in 是注册时静态时长，与剩余时间无关，不可作依据
  if (expiresAt == null) return false;
  return expiresAt * 1000 < Date.now();
}

/** 到期展示色：仅真正过期标警告色，未到期的正常展示（不做提前预警） */
export function poolExpiryTone(
  expiresAt: number | null,
): "expired" | "ok" | "none" {
  if (expiresAt == null) return "none"; // 无 JWT 时间戳时中性展示，不警告
  return isPoolExpired(expiresAt) ? "expired" : "ok";
}

export function riskLabel(
  risk: string | null,
  bfs: number | null,
): { label: string; score: string | null } {
  // bfs 未检测时保持「未知」；0=低 / 1=中 / ≥2=高（与后端 botFlagSource 对齐）
  let level = "未知";
  if (bfs === 0) level = "低";
  else if (bfs === 1) level = "中";
  else if (bfs != null && bfs >= 2) level = "高";
  const score = risk ? /risk=([\d.]+)/.exec(risk)?.[1] ?? null : null;
  return { label: level, score };
}

export function riskTitle(
  risk: string | null,
  bfs: number | null,
  checkedAt: string | null,
): string {
  const { label, score } = riskLabel(risk, bfs);
  const parts = [
    label === "未知" ? "未检测" : label,
    score ? `risk=${score}` : null,
    checkedAt ? `检测于 ${checkedAt}` : null,
  ].filter(Boolean) as string[];
  return parts.join(" · ") || "未检测";
}

// ─── 号池操作日志 ───────────────────────────────────────────

/** 业务日志类型：巡检 / 认证 / 重登 / 推送 */
export type PoolLogType = "inspect" | "auth" | "reauth" | "push";

/** 日志级别（对齐 loguru 语义） */
export type PoolLogLevel = "INFO" | "SUCCESS" | "WARNING" | "ERROR";

export interface PoolLogEntry {
  /** 递增序号（React key / 排序） */
  id: number;
  /** 完整时间 "YYYY-MM-DD HH:MM:SS" */
  ts: string;
  /** 业务类型 */
  type: PoolLogType;
  level: PoolLogLevel;
  message: string;
  /** 关联账号 id / 邮箱（可选，展示上下文用） */
  accountId?: number;
  email?: string;
}

/** 业务类型顺序（筛选 chips 顺序） */
export const POOL_LOG_TYPES: readonly PoolLogType[] = [
  "inspect",
  "auth",
  "reauth",
  "push",
];

export const POOL_LOG_TYPE_LABEL: Record<PoolLogType, string> = {
  inspect: "巡检",
  auth: "认证",
  reauth: "重登",
  push: "推送",
};

/** 当前时间 "YYYY-MM-DD HH:MM:SS" */
export function poolLogNow(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
