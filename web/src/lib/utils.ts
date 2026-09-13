import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import type { LogEntry } from "@/lib/api";

/** shadcn 标准 className 合并工具 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ──────────────────────────────────────────────────────────────
// 账号时间：后端存储统一为北京时间（兼容历史裸串与 ISO 带时区两种格式），
// 前端解析后按浏览器本地时区展示。
// ──────────────────────────────────────────────────────────────

/**
 * 账号时间字符串 → 浏览器本地时区 YYYY-MM-DD HH:mm:ss。
 *
 * 兼容两种后端存储格式（均表示北京时间 UTC+8）：
 * - 裸字符串 "YYYY-MM-DD HH:MM:SS"（历史格式，无时区标记，按 +08:00 解释）
 * - ISO 带时区 "YYYY-MM-DDTHH:MM:SS+08:00"（`new Date` 直接解析）
 * 解析为 Date 后按浏览器本地时区格式化，保证非东八区环境也能正确换算。
 * 无法解析时原样返回；空值返回 "—"。
 */
export function formatAccountTime(raw: string | null | undefined): string {
  if (!raw) return "—";
  const s = raw.trim();
  if (!s) return "—";
  // 裸北京时间字符串：拼接时区偏移后交给 Date 解析
  const normalized = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$/.test(s)
    ? `${s.replace(" ", "T")}+08:00`
    : s;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return raw;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// ──────────────────────────────────────────────────────────────
// 注册日志：按标签拆成主控 / 认证池 / 线程 三组。
// ──────────────────────────────────────────────────────────────

export type ThreadLogGroup = {
  /** 分组键：main / auth / T001 … */
  worker: string;
  /** 展示名：主控 / 认证池 / 线程 N */
  label: string;
  entries: LogEntry[];
  failCount: number;
  okCount: number;
  lastTime: string;
  lastBody: string;
};

export type GroupStatus = "fail" | "ok" | "running" | "idle";

export type RegisterLogGroups = {
  main: ThreadLogGroup | null;
  auth: ThreadLogGroup | null;
  workers: ThreadLogGroup[];
};

const MAIN_TAGS = new Set(["预检", "任务"]);
const AUTH_TAGS = new Set(["入池", "出池"]);
const WORKER_TAGS = new Set([
  "注册",
  "邮箱",
  "邮件",
  "资料",
  "CF挑战",
  "SSO",
  "风控",
]);

/** 从 `[标签] 正文` 取出标签 */
export function parseLogTag(message: string): string | null {
  const match = /^\[([^\]]+)\]/.exec((message || "").trim());
  return match ? match[1] : null;
}

/** 去掉前导 `[标签]` 后的正文 */
export function logBody(message: string): string {
  return (message || "").replace(/^\[[^\]]+\]\s*/, "");
}

export function logChannel(
  entry: LogEntry,
): "main" | "auth" | "worker" | null {
  const tag = parseLogTag(entry.message);
  if (!tag) return null;
  if (MAIN_TAGS.has(tag)) return "main";
  if (AUTH_TAGS.has(tag)) return "auth";
  if (WORKER_TAGS.has(tag)) return "worker";
  return null;
}

/** 后端线程名 → 分组键 */
export function workerGroupKey(thread?: string | null): string {
  const t = (thread || "").trim();
  if (!t || t === "主控") return "main";
  const match = /注册线程[_-](\d+)/.exec(t);
  if (match) return `T${String(Number(match[1]) + 1).padStart(3, "0")}`;
  const fallback = /ThreadPoolExecutor-\d+_(\d+)/.exec(t);
  if (fallback) return `T${String(Number(fallback[1]) + 1).padStart(3, "0")}`;
  return t;
}

/** 分组键 → 展示名 */
export function workerDisplayLabel(worker: string): string {
  if (worker === "main") return "主控";
  if (worker === "auth") return "认证池";
  const match = /^T(\d+)$/.exec(worker);
  if (match) return `线程T${match[1]}`;
  return `线程T${worker}`;
}

/** 排序：主控 → 认证池 → 线程序号 */
export function workerSortKey(worker: string): string {
  if (worker === "main") return "0-main";
  if (worker === "auth") return "1-auth";
  const match = /^T(\d+)$/.exec(worker);
  if (match) return `2-T${String(Number(match[1])).padStart(4, "0")}`;
  return `3-${worker}`;
}

/** 单行视觉态：ERROR/CRITICAL=失败，SUCCESS=成功，其余按 info */
export function logLineTone(entry: LogEntry): "fail" | "ok" | "warn" | "info" {
  const upper = entry.level.toUpperCase();
  if (upper === "ERROR" || upper === "CRITICAL") return "fail";
  if (upper === "WARNING") return "warn";
  if (upper === "SUCCESS") return "ok";
  return "info";
}

/** 线程组终态。主控：有成功即成功，全部失败才失败；预检成功不算结束。 */
export function groupStatus(group: ThreadLogGroup): GroupStatus {
  const last = group.entries[group.entries.length - 1];
  if (!last) return group.entries.length ? "running" : "idle";

  if (group.worker === "main") {
    const end = [...group.entries]
      .reverse()
      .find((e) => parseLogTag(e.message) === "任务" && /结束/.test(e.message));
    if (end) return logLineTone(end) === "fail" ? "fail" : "ok";
    const lastTag = parseLogTag(last.message);
    if (lastTag === "预检" && logLineTone(last) === "ok") return "running";
    if (logLineTone(last) === "fail") return "fail";
    return "running";
  }

  if (group.worker === "auth") {
    const outOk = group.entries.some(
      (e) => parseLogTag(e.message) === "出池" && logLineTone(e) === "ok",
    );
    if (outOk) return "ok";
    const tone = logLineTone(last);
    if (tone === "fail") return "fail";
    return group.entries.length ? "running" : "idle";
  }

  // 单线程：SSO 成功才算完成，中间步骤成功不算
  const ssoOk = group.entries.some(
    (e) => parseLogTag(e.message) === "SSO" && logLineTone(e) === "ok",
  );
  if (ssoOk) return "ok";
  const tone = logLineTone(last);
  if (tone === "fail") return "fail";
  return "running";
}

function toGroup(worker: string, list: LogEntry[]): ThreadLogGroup {
  let failCount = 0;
  let okCount = 0;
  for (const entry of list) {
    const tone = logLineTone(entry);
    if (tone === "fail") failCount += 1;
    if (tone === "ok") okCount += 1;
  }
  const last = list[list.length - 1];
  return {
    worker,
    label: workerDisplayLabel(worker),
    entries: list,
    failCount,
    okCount,
    lastTime: last?.ts?.slice(11, 19) || "",
    lastBody: last ? logBody(last.message) : "",
  };
}

/** 按标签拆成主控 / 认证池 / 线程三组 */
export function groupRegisterLogs(entries: LogEntry[]): RegisterLogGroups {
  const main: LogEntry[] = [];
  const auth: LogEntry[] = [];
  const workerMap = new Map<string, LogEntry[]>();
  for (const entry of entries) {
    const channel = logChannel(entry);
    if (channel === "main") {
      main.push(entry);
      continue;
    }
    if (channel === "auth") {
      auth.push(entry);
      continue;
    }
    if (channel !== "worker") continue;
    const key = workerGroupKey(entry.thread);
    const workerKey = key === "main" ? "T001" : key;
    const list = workerMap.get(workerKey);
    if (list) list.push(entry);
    else workerMap.set(workerKey, [entry]);
  }
  const workers = Array.from(workerMap.entries())
    .map(([worker, list]) => toGroup(worker, list))
    .sort((a, b) =>
      workerSortKey(a.worker).localeCompare(workerSortKey(b.worker), "en"),
    );
  return {
    main: main.length ? toGroup("main", main) : null,
    auth: auth.length ? toGroup("auth", auth) : null,
    workers,
  };
}

/** 兼容旧调用：主控 + 认证池 + 线程拍平 */
export function groupLogsByWorker(entries: LogEntry[]): ThreadLogGroup[] {
  const { main, auth, workers } = groupRegisterLogs(entries);
  return [main, auth, ...workers].filter((g): g is ThreadLogGroup => g != null);
}

/** 配置里的代理列表 ↔ 文本框（一行一条，# 开头忽略）。 */
export function proxiesToText(list: string[] | undefined, fallback = ""): string {
  if (Array.isArray(list) && list.length > 0) {
    return list.join("\n");
  }
  return fallback;
}

export function textToProxies(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const line of text.replace(/[;,]/g, "\n").split("\n")) {
    const url = line.trim();
    if (!url || url.startsWith("#")) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    out.push(url);
  }
  return out;
}
