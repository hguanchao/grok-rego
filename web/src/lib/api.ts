/**
 * 管理 API 客户端（开发态经 Vite 代理到 :8787）。
 */

export type MailProvider = "cf" | "yyds";
export type DomainMode = "poll" | "random";
/** 全局仿真人强度：light 快 / normal 平衡 / heavy 最像人 */
export type HumanLevel = "light" | "normal" | "heavy";

export interface AppConfig {
  cf_api_base: string;
  cf_domains: string[];
  cf_api_key: string;
  cf_domain_mode: DomainMode;
  mail_provider: MailProvider;
  yyds_api_base: string;
  yyds_api_key: string;
  proxy: string;
  proxies?: string[];
  auth_enabled: boolean;
  g2a_base_url: string;
  g2a_username: string;
  g2a_password: string;
  cpa_base_url: string;
  cpa_management_key: string;
  gateway_api_key: string;
  grok_version: string;
  human_sim: boolean;
  human_level: HumanLevel;
}

export interface LogEntry {
  id: number;
  ts: string;
  level: string;
  message: string;
  thread?: string | null;
}

export type JobStatus =
  | "idle"
  | "pending"
  | "running"
  | "stopping"
  | "completed"
  | "cancelled"
  | "failed";

export interface RegisterJobState {
  id: string | null;
  status: JobStatus;
  count: number;
  threads: number;
  headless: boolean;
  mail_provider: string;
  success: number;
  failed: number;
  done: number;
  running: number;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  logs: LogEntry[];
  last_log_id: number;
  progress: number;
}

interface ApiOk<T> {
  ok: true;
  data: T;
}

interface ApiErr {
  ok: false;
  error: string;
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  let payload: ApiOk<T> | ApiErr | null = null;
  try {
    payload = (await response.json()) as ApiOk<T> | ApiErr;
  } catch {
    throw new ApiError(response.status, `无效响应 (${response.status})`);
  }
  if (!response.ok || !payload || payload.ok === false) {
    const message =
      payload && "error" in payload && payload.error
        ? payload.error
        : `请求失败 (${response.status})`;
    throw new ApiError(response.status, message);
  }
  return payload.data;
}

export async function fetchConfig(): Promise<AppConfig> {
  return request<AppConfig>("/api/config");
}

export async function saveConfig(
  patch: Partial<AppConfig>,
): Promise<AppConfig> {
  return request<AppConfig>("/api/config", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export async function fetchRegisterStatus(
  afterLogId = 0,
): Promise<RegisterJobState> {
  const q = afterLogId > 0 ? `?after=${afterLogId}` : "";
  return request<RegisterJobState>(`/api/register/status${q}`);
}

export async function startRegister(body: {
  count: number;
  threads: number;
  headless: boolean;
  config?: Partial<AppConfig>;
}): Promise<RegisterJobState> {
  return request<RegisterJobState>("/api/register/start", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function stopRegister(): Promise<RegisterJobState> {
  return request<RegisterJobState>("/api/register/stop", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function clearRegisterLogs(): Promise<RegisterJobState> {
  return request<RegisterJobState>("/api/register/logs/clear", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export { ApiError };

// ─── 号池管理 ───────────────────────────────────────────

export const ACCOUNT_STATUS = {
  active: 1,
  reauth: 2,
  limited: 3,
  permission_denied: 4,
  abnormal: 5,
  disabled: 6,
} as const;

export const STATUS_LABELS: Record<number, string> = {
  1: "正常",
  2: "需重登",
  3: "限额",
  4: "权限被拒",
  5: "异常",
  6: "禁用",
};

export interface PoolAccount {
  id: number;
  email: string;
  password: string | null;
  first_name: string | null;
  last_name: string | null;
  has_token: number;
  has_refresh: number;
  expires_in: number | null;
  expires_at: number | null;
  status: number;
  reason: string | null;
  inspected_at: string | null;
  is_deleted: number;
  created_at: string;
  updated_at: string | null;
}

export interface PoolQueryResult {
  items: PoolAccount[];
  total: number;
  page: number;
  page_size: number;
}

export interface PoolStats {
  total: number;
  active: number;
  pending_action: number;
  abnormal: number;
  /** 各任务全量模式的候选账号数（服务端全库统计） */
  task_counts: {
    push: number;
    auth: number;
    reauth: number;
    inspect: number;
  };
}

export interface PoolQuery {
  page?: number;
  page_size?: number;
  status?: string;
  keyword?: string;
  authed?: string;
  expiry?: string;
}

export async function fetchPoolStats(): Promise<PoolStats> {
  return request<PoolStats>("/api/pool/stats");
}

export async function fetchPoolAccounts(q: PoolQuery = {}): Promise<PoolQueryResult> {
  const params = new URLSearchParams();
  if (q.page) params.set("page", String(q.page));
  if (q.page_size) params.set("page_size", String(q.page_size));
  if (q.status && q.status !== "all") params.set("status", q.status);
  if (q.keyword) params.set("keyword", q.keyword);
  if (q.authed && q.authed !== "all") params.set("authed", q.authed);
  if (q.expiry && q.expiry !== "all") params.set("expiry", q.expiry);
  const qs = params.toString();
  return request<PoolQueryResult>(`/api/pool/accounts${qs ? `?${qs}` : ""}`);
}

export async function deletePoolAccounts(ids: number[]): Promise<{ deleted: number }> {
  return request<{ deleted: number }>("/api/pool/accounts", {
    method: "DELETE",
    body: JSON.stringify({ ids }),
  });
}

export type PoolOpKind = "inspect" | "reauth" | "";

export interface PoolOpTask {
  id: string | null;
  kind: PoolOpKind;
  status: PoolPushStatus;
  count: number;
  concurrency: number;
  pushed: number;
  failed: number;
  pending: number;
  done: number;
  skipped: number;
  skipped_list: Array<{ id: number; reason: string }>;
  /** 重登降级：已入认证池、等待后台 SSO 自动认证的账号（对齐后端 PoolJob.pending_list） */
  pending_list: Array<{
    id: number;
    email: string;
    reason: string;
  }>;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  logs: PoolPushLog[];
  last_log_id: number;
  progress: number;
}

export async function inspectPoolAccounts(
  ids?: number[],
  concurrency?: number,
): Promise<PoolOpTask> {
  return request<PoolOpTask>("/api/pool/inspect", {
    method: "POST",
    body: JSON.stringify({ ids, concurrency }),
  });
}

export async function fetchPoolOpTaskStatus(
  taskId: string,
  afterLogId: number,
): Promise<PoolOpTask> {
  return request<PoolOpTask>(
    `/api/pool/inspect/status?task_id=${encodeURIComponent(taskId)}&after=${afterLogId}`,
  );
}

export async function cancelPoolOpTask(): Promise<PoolOpTask> {
  return request<PoolOpTask>("/api/pool/inspect/cancel", { method: "POST" });
}

export async function reauthPoolAccounts(
  ids?: number[],
  concurrency?: number,
): Promise<PoolOpTask> {
  return request<PoolOpTask>("/api/pool/reauth", {
    method: "POST",
    body: JSON.stringify({ ids, concurrency }),
  });
}

export interface AuthPoolStatus {
  running: boolean;
  queue_size: number;
  logs: PoolPushLog[];
  last_log_id: number;
}

export async function fetchAuthPoolStatus(afterLogId = 0): Promise<AuthPoolStatus> {
  return request<AuthPoolStatus>(`/api/pool/auth/status?after=${Math.max(0, afterLogId)}`);
}

/** 全局任务互斥状态：任一为 true 表示对应重任务正在执行（用于禁用其它任务按钮）。 */
export interface TaskActiveStatus {
  register: boolean;
  push: boolean;
  pool: boolean; // 巡检 / 重登 共享槽
  auth: boolean;
}

export async function fetchTaskActive(): Promise<TaskActiveStatus> {
  return request<TaskActiveStatus>("/api/tasks/active");
}

export interface PoolAuthResultItem {
  id: number;
  email?: string;
  status: "pending" | "skipped" | "failed";
  reason?: string;
}

export async function authPoolAccounts(
  ids?: number[],
): Promise<{ results: PoolAuthResultItem[] }> {
  return request<{ results: PoolAuthResultItem[] }>("/api/pool/auth", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

export async function updatePoolAccountStatus(
  ids: number[],
  status: number,
  reason?: string,
): Promise<{ updated: number }> {
  return request<{ updated: number }>("/api/pool/status", {
    method: "POST",
    body: JSON.stringify({ ids, status, reason }),
  });
}

export interface PoolPushSkipped {
  id: number;
  reason: string;
}

export interface PoolPushLog {
  id: number;
  level: "INFO" | "SUCCESS" | "WARNING" | "ERROR";
  message: string;
}

export type PoolPushStatus =
  | "idle"
  | "pending"
  | "running"
  | "done"
  | "cancelled";

export interface PoolPushTask {
  id: string | null;
  status: PoolPushStatus;
  targets: Array<"g2a" | "cpa">;
  count: number;
  concurrency: number;
  pushed: number;
  failed: number;
  skipped: number;
  done: number;
  skipped_list: PoolPushSkipped[];
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  logs: PoolPushLog[];
  last_log_id: number;
  progress: number;
}

export async function pushPoolAccounts(
  targets: Array<"g2a" | "cpa">,
  ids?: number[],
  concurrency?: number,
): Promise<PoolPushTask> {
  return request<PoolPushTask>("/api/pool/push", {
    method: "POST",
    body: JSON.stringify({ targets, ids, concurrency }),
  });
}

export async function fetchPushTaskStatus(
  taskId: string,
  afterLogId: number,
): Promise<PoolPushTask> {
  return request<PoolPushTask>(
    `/api/pool/push/status?task_id=${encodeURIComponent(taskId)}&after=${afterLogId}`,
  );
}

export async function cancelPushTask(): Promise<PoolPushTask> {
  return request<PoolPushTask>("/api/pool/push/cancel", { method: "POST" });
}

// ─── 网关运维（聚合快照）──────────────────────────────────────

export interface GatewayChannel {
  id: string;
  title: string;
  base_path: string;
  client_base: string;
  upstream: string;
  key?: string;
  select?: string;
}

export interface GatewayChannelStats {
  requests: number;
  failed: number;
  tokens: number;
}

export interface GatewayAccountRow {
  id: number;
  email: string;
  authed: boolean;
  status: number;
  disabled: boolean;
  cooling: boolean;
  sticky: boolean;
  dumbed: boolean;
  requests_24h: number;
}

export interface GatewayOpsData {
  uptime_sec: number;
  generated_at: string;
  channels: GatewayChannel[];
  stats_24h: Record<string, GatewayChannelStats>;
  account_pool: {
    total: number;
    active: number;
    in_use: number;
    sticky: number;
    cooling: number;
    requests_24h: number;
    accounts: GatewayAccountRow[];
  };
  config: {
    proxy: string;
    proxy_pool?: {
      total: number;
      cooling: number;
      disabled?: number;
      items: {
        display: string;
        /** 本机出口（回环地址）：常驻可用，不参与冷却与降智排除 */
        local?: boolean;
        cooling: boolean;
        cool_left_sec: number;
        strikes?: number;
        disabled?: boolean;
      }[];
    };
    free_models: string[];
    api_key: string;
    auth_enabled: boolean;
  };
}

export async function fetchGatewayOps(): Promise<GatewayOpsData> {
  return request<GatewayOpsData>("/api/gateway/ops");
}

export interface GatewayProbe {
  ok: boolean;
  status: number;
  ms: number;
  models: number;
  error: string;
}

export async function probeGateway(): Promise<GatewayProbe> {
  return request<GatewayProbe>("/api/gateway/probe", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// ─── 用量统计 ─────────────────────────────────────────────

export interface UsageRow {
  id: number;
  ip: string | null;
  client_ua: string | null;
  endpoint: string;
  model: string;
  effort: string | null;
  stream: number;
  account_id: number | null;
  account_email: string | null;
  status: number;
  reason: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  reasoning_tokens: number;
  created_at: string;
}

export interface UsageSummary {
  requests: number;
  success: number;
  failed: number;
  stream_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  success_rate: number;
  cache_hit_rate: number;
  reasoning_share: number;
}

export interface UsageByModel {
  model: string;
  requests: number;
  success: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
}

export interface UsageByDay {
  day: string;
  requests: number;
  success: number;
  total_tokens: number;
}

export interface UsageByHour {
  hour: string;
  requests: number;
  success: number;
  total_tokens: number;
}

export interface UsageSummaryData {
  days: number;
  from: string;
  to: string;
  summary: UsageSummary;
  by_model: UsageByModel[];
  by_day: UsageByDay[];
  by_hour: UsageByHour[];
}

export interface UsageRecentData {
  items: UsageRow[];
  total: number;
  offset: number;
  limit: number;
}

export type UsageGroupDim = "account" | "model";

export interface UsageGroupedRow {
  key: string;
  requests: number;
  success: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  stream_count: number;
  last_at: string;
}

export interface UsageGroupedData {
  dimension: UsageGroupDim;
  items: UsageGroupedRow[];
  /** 分组总数（不分页时等于 items.length） */
  total: number;
  offset: number;
  limit: number;
}

export async function fetchUsageSummary(days = 1): Promise<UsageSummaryData> {
  return request<UsageSummaryData>(`/api/usage?days=${Math.max(1, days)}`);
}

export async function fetchUsageRecent(
  offset = 0,
  limit = 20,
): Promise<UsageRecentData> {
  const params = new URLSearchParams({
    offset: String(Math.max(0, offset)),
    limit: String(Math.max(1, Math.min(limit, 100))),
  });
  return request<UsageRecentData>(`/api/usage/recent?${params.toString()}`);
}

export async function fetchUsageGrouped(
  dimension: UsageGroupDim,
  offset = 0,
  limit = 0,
): Promise<UsageGroupedData> {
  // limit = 0 → 后端不分页，返回全量
  const params = new URLSearchParams({
    dim: dimension,
    offset: String(Math.max(0, offset)),
    limit: String(Math.max(0, limit)),
  });
  return request<UsageGroupedData>(`/api/usage/grouped?${params.toString()}`);
}
