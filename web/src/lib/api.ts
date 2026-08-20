/**
 * 管理 API 客户端（开发态经 Vite 代理到 :8787）。
 */

export type MailProvider = "cf" | "yyds";
export type DomainMode = "poll" | "random";

export interface AppConfig {
  cf_api_base: string;
  cf_domains: string[];
  cf_api_key: string;
  cf_domain_mode: DomainMode;
  mail_provider: MailProvider;
  yyds_api_base: string;
  yyds_api_key: string;
  proxy: string;
  auth_enabled: boolean;
  g2a_base_url: string;
  g2a_username: string;
  g2a_password: string;
  cpa_base_url: string;
  cpa_management_key: string;
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
  denied: number;
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
  bfs: number | null;
  risk: string | null;
  checked_at: string | null;
  is_deleted: number;
  created_at: string;
  updated_at: string | null;
  used: number;
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

export type PoolOpKind = "inspect" | "reauth" | "risk" | "";

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
  pending_list: Array<{
    id: number;
    email: string;
    verification_uri: string;
    user_code: string;
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
  ids: number[],
  concurrency?: number,
): Promise<PoolOpTask> {
  return request<PoolOpTask>("/api/pool/reauth", {
    method: "POST",
    body: JSON.stringify({ ids, concurrency }),
  });
}

export async function riskPoolAccount(id: number): Promise<PoolOpTask> {
  return request<PoolOpTask>("/api/pool/risk", {
    method: "POST",
    body: JSON.stringify({ ids: [id] }),
  });
}

export interface AutoRefreshStatus {
  running: boolean;
  interval_min: number;
  lead_min: number;
  last_run_at: string;
  last_result: string;
  skip_reason: string;
}

export async function fetchAutoRefreshStatus(): Promise<AutoRefreshStatus> {
  return request<AutoRefreshStatus>("/api/pool/auto-refresh");
}

export interface PoolAuthResultItem {
  id: number;
  email?: string;
  status: "pending" | "skipped" | "failed";
  verification_uri?: string;
  user_code?: string;
  reason?: string;
}

export async function authPoolAccounts(ids: number[]): Promise<{ results: PoolAuthResultItem[] }> {
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
