import { useCallback, useEffect, useRef, useState } from "react";
import { usePageCache } from "@/lib/page-cache";
import {
  Eye,
  EyeOff,
  LogIn,
  Play,
  RefreshCw,
  ScrollText,
  Search,
  Share,
  ShieldCheck,
  Square,
  Trash2,
} from "lucide-react";
import { Button, buttonVariants } from "@/components/ui";
import { CardContent } from "@/components/ui";
import { Checkbox } from "@/components/ui";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui";
import { Input } from "@/components/ui";
import { Pagination } from "@/components/ui";
import { Badge } from "@/components/ui";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui";
import {
  ACCOUNT_STATUS_VARIANT,
  AUTH_FILTER_OPTIONS,
  EXPIRY_FILTER_OPTIONS,
  PAGE_SIZE_OPTIONS,
  INSPECT_VARIANT,
  STATUS_FILTER_OPTIONS,
  formatPoolExpiry,
  inspectLabel,
  inspectTitle,
  poolLogNow,
  type PoolLogEntry,
} from "@/components/pool/PoolPageParts";
import {
  PoolAccountsTable,
  type PoolRowAction,
} from "@/components/pool/PoolAccountsTable";
import {
  PoolLogDrawer,
} from "@/components/pool/PoolLogDrawer";
import {
  fetchConfig,
  fetchPoolAccounts,
  fetchPoolStats,
  deletePoolAccounts,
  inspectPoolAccounts,
  authPoolAccounts,
  pushPoolAccounts,
  cancelPushTask,
  fetchPushTaskStatus,
  cancelPoolOpTask,
  fetchPoolOpTaskStatus,
  reauthPoolAccounts,
  updatePoolAccountStatus,
  fetchAutoRefreshStatus,
  fetchAuthPoolStatus,
  fetchTaskActive,
  STATUS_LABELS,
  ApiError,
  type AppConfig,
  type AutoRefreshStatus,
  type PoolAccount,
  type PoolOpKind,
  type PoolOpTask,
  type PoolPushTask,
  type PoolStats,
} from "@/lib/api";
import { cn, formatAccountTime } from "@/lib/utils";
import { toast } from "sonner";

/** 刷新按钮旋转动效保底时长（本地请求过快时旋转至少可见） */
const MIN_SPIN_MS = 450;

export function PoolPage() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  // ── 以下状态走页面缓存：路由切走再回来，数据/筛选/页码保留，秒开 + 静默刷新 ──
  const [stats, setStats] = usePageCache<PoolStats>("pool.stats", () => ({
    total: 0,
    active: 0,
    pending_action: 0,
    abnormal: 0,
    task_counts: { push: 0, auth: 0, reauth: 0, inspect: 0 },
  }));
  const [accounts, setAccounts] = usePageCache<PoolAccount[]>("pool.accounts", () => []);
  const [total, setTotal] = usePageCache("pool.total", () => 0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = usePageCache("pool.page", () => 1);
  const [pageSize, setPageSize] = usePageCache("pool.pageSize", () => 20);
  const [statusFilter, setStatusFilter] = usePageCache("pool.statusFilter", () => "all");
  const [authedFilter, setAuthedFilter] = usePageCache("pool.authedFilter", () => "all");
  const [expiryFilter, setExpiryFilter] = usePageCache("pool.expiryFilter", () => "all");
  // 关键词分两个状态：input 即时回显，applied 防抖后进入查询依赖（避免闭包陈旧）
  const [keywordInput, setKeywordInput] = usePageCache("pool.keywordInput", () => "");
  const [keyword, setKeyword] = usePageCache("pool.keyword", () => "");
  const [loadError, setLoadError] = useState<string | null>(null);
  // 手动刷新按钮旋转动效（瞬态，不缓存）
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = usePageCache<Set<number>>("pool.selected", () => new Set());
  const [visiblePasswords, setVisiblePasswords] = useState<Set<number>>(new Set());
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [pushDialogOpen, setPushDialogOpen] = useState(false);
  // 任务二次确认：推送 / 认证 / 巡检 / 重登执行前弹窗复核
  const [confirmTask, setConfirmTask] = useState<{
    kind: "auth" | "reauth" | "inspect" | "push";
    scope: string;
    detail: string;
  } | null>(null);
  const [detailAccount, setDetailAccount] = useState<PoolAccount | null>(null);
  // 详情弹窗独立的密码可见性，避免与列表 visiblePasswords 共享导致联动
  const [detailPasswordVisible, setDetailPasswordVisible] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  // 全局互斥：其它任务（含注册页）执行中时禁用本页任务发起按钮
  const [globalTaskBusy, setGlobalTaskBusy] = useState(false);
  // 推送异步任务：running 时轮询状态，结束后清空；null 表示无任务
  const [pushTask, setPushTask] = useState<PoolPushTask | null>(null);
  // 自动续期 daemon 状态（常驻轮询：进度条 + 增量日志；声明需在 activeTask 之前）
  const [refreshState, setRefreshState] = useState<AutoRefreshStatus | null>(null);
  const refreshAfterRef = useRef(0);
  const pushPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pushAfterRef = useRef(0);
  const concurrencyRef = useRef<HTMLInputElement | null>(null);
  // 巡检 / 重登共享任务槽（服务端单任务互斥）
  const [poolTask, setPoolTask] = useState<PoolOpTask | null>(null);
  const poolPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const poolAfterRef = useRef(0);
  // 认证轮询：发起认证后短期轮询账号状态，确认 Token 是否交换成功
  const authPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const authAfterRef = useRef(0);
  const keywordTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 列表请求序号：竞态保护（搜索/筛选/翻页快速切换时丢弃过期响应）
  const reqSeqRef = useRef(0);

  // ─── 号池操作日志（前端本地记录） ───
  const [logOpen, setLogOpen] = useState(false);
  const [logEntries, setLogEntries] = useState<PoolLogEntry[]>([]);
  const logSeqRef = useRef(0);

  type PoolLogInput = Omit<PoolLogEntry, "id" | "ts">;

  /** 追加一批业务日志（自动补 id/时间戳，保留最近 1000 条） */
  const appendLogs = useCallback((items: PoolLogInput[]) => {
    if (!items.length) return;
    const now = poolLogNow();
    setLogEntries((prev) => {
      const next = [...prev];
      for (const item of items) {
        logSeqRef.current += 1;
        next.push({ ...item, id: logSeqRef.current, ts: now });
      }
      return next.length > 1000 ? next.slice(next.length - 1000) : next;
    });
  }, []);

  /** 任务进入终态后进度条保留展示时长（毫秒），展示完成结果后消失 */
  const DONE_KEEP_MS = 10_000;
  const [taskDoneUntil, setTaskDoneUntil] = useState(0);
  const taskDoneTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 任务进入终态：进度条保留 10s 展示完成结果，随后清除任务状态 */
  const settleTask = useCallback(() => {
    if (taskDoneTimerRef.current) clearTimeout(taskDoneTimerRef.current);
    const until = Date.now() + DONE_KEEP_MS;
    setTaskDoneUntil(until);
    taskDoneTimerRef.current = setTimeout(() => {
      setPushTask(null);
      setPoolTask(null);
      setTaskDoneUntil(0);
    }, DONE_KEEP_MS);
  }, []);

  /** 仅打开日志抽屉（不清空内容，供「日志」按钮回看） */
  const showLogDrawer = useCallback(() => {
    setLogOpen(true);
  }, []);

  /** 取消推送任务（按钮由「推送」切换为「取消」） */
  const handleCancelPush = useCallback(async () => {
    try {
      await cancelPushTask();
    } catch (error) {
      appendLogs([
        {
          type: "push",
          level: "ERROR",
          message: `[任务] 取消失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    }
  }, [appendLogs]);

  /** 账号 id → 日志展示名（邮箱优先，缺省回退 ID） */
  const accountLabel = useCallback(
    (id: number): string => {
      const acc = accounts.find((a) => a.id === id);
      return acc ? acc.email : `ID ${id}`;
    },
    [accounts],
  );

  /** 记录认证日志基线；状态接口暂时不可用时不阻断认证请求。 */
  const prepareAuthLogCursor = useCallback(async () => {
    try {
      const baseline = await fetchAuthPoolStatus(0);
      authAfterRef.current = baseline.last_log_id;
    } catch {
      authAfterRef.current = 0;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    fetchConfig()
      .then((data) => {
        if (alive) setConfig(data);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const load = useCallback(async (silent = false) => {
    const seq = ++reqSeqRef.current;
    // 静默刷新（路由切回/定时轮询）：不闪骨架屏，仅后台更新数据
    if (!silent) setLoading(true);
    try {
      const [acct, st] = await Promise.all([
        fetchPoolAccounts({
          page,
          page_size: pageSize,
          status: statusFilter,
          keyword: keyword || undefined,
          authed: authedFilter,
          expiry: expiryFilter,
        }),
        fetchPoolStats(),
      ]);
      // 竞态保护：响应返回时若已有更新的请求，丢弃本次结果
      if (seq !== reqSeqRef.current) return;
      setAccounts(acct.items);
      setTotal(acct.total);
      setStats(st);
      setLoadError(null);
    } catch (e) {
      if (seq !== reqSeqRef.current) return;
      setLoadError(e instanceof Error ? e.message : "请求失败");
    } finally {
      if (seq !== reqSeqRef.current) return;
      if (!silent) setLoading(false);
    }
  }, [page, pageSize, statusFilter, keyword, authedFilter, expiryFilter]);

  /** 手动刷新：静默加载（表格不闪骨架屏），按钮旋转至少 MIN_SPIN_MS */
  const handleRefresh = useCallback(async () => {
    const spunAt = Date.now();
    setRefreshing(true);
    try {
      await load(true);
    } finally {
      const left = MIN_SPIN_MS - (Date.now() - spunAt);
      if (left > 0) await new Promise((r) => window.setTimeout(r, left));
      setRefreshing(false);
    }
  }, [load]);

  /** 停止推送任务轮询 */
  const stopPushPolling = useCallback(() => {
    if (pushPollRef.current) {
      clearInterval(pushPollRef.current);
      pushPollRef.current = null;
    }
  }, []);

  /** 停止巡检/重登任务轮询 */
  const stopPoolPolling = useCallback(() => {
    if (poolPollRef.current) {
      clearInterval(poolPollRef.current);
      poolPollRef.current = null;
    }
  }, []);

  /** 停止认证状态轮询 */
  const stopAuthPolling = useCallback(() => {
    if (authPollRef.current) {
      clearInterval(authPollRef.current);
      authPollRef.current = null;
    }
  }, []);

  /** 新任务启动：停旧轮询、清空日志与终态残影，再打开抽屉 */
  const openLogDrawer = useCallback(() => {
    stopPushPolling();
    stopPoolPolling();
    if (taskDoneTimerRef.current) {
      clearTimeout(taskDoneTimerRef.current);
      taskDoneTimerRef.current = null;
    }
    setTaskDoneUntil(0);
    setPushTask(null);
    setPoolTask(null);
    logSeqRef.current = 0;
    setLogEntries([]);
    setLogOpen(true);
  }, [stopPoolPolling, stopPushPolling]);

  /** 推送任务轮询：增量拉取日志，任务结束/取消/服务空闲时收尾 */
  const startPushPolling = useCallback(
    (taskId: string) => {
      stopPushPolling();
      let requestInFlight = false;
      const tick = async () => {
        if (requestInFlight) return;
        requestInFlight = true;
        try {
          const snap = await fetchPushTaskStatus(taskId, pushAfterRef.current);
          pushAfterRef.current = snap.last_log_id;
          if (snap.logs.length > 0) {
            appendLogs(
              snap.logs.map((log) => ({
                type: "push" as const,
                level: log.level as PoolLogEntry["level"],
                message: log.message,
              })),
            );
          }
          setPushTask(snap);
          if (snap.status === "done" || snap.status === "cancelled") {
            stopPushPolling();
            settleTask();
            load();
          } else if (snap.status === "idle") {
            stopPushPolling();
            setPushTask(null);
            load();
          }
        } catch (error) {
          stopPushPolling();
          appendLogs([
            {
              type: "push",
              level: "ERROR",
              message: `[任务] 推送轮询失败：${error instanceof Error ? error.message : String(error)}`,
            },
          ]);
          setPushTask(null);
        } finally {
          requestInFlight = false;
        }
      };
      pushPollRef.current = setInterval(() => void tick(), 1000);
      void tick();
    },
    [appendLogs, load, settleTask, stopPushPolling],
  );

  // 全局任务互斥轮询：任一重任务（注册/推送/认证/号池）执行中禁用本页发起按钮
  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (!alive) return;
      fetchTaskActive()
        .then((d) => {
          if (alive) setGlobalTaskBusy(d.register || d.push || d.pool || d.auth);
        })
        .catch(() => {});
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  /** 当前运行中的任务类型，用于按钮切换为停止态（终态保留展示中的任务不算运行中） */
  const runningTask: "inspect" | "reauth" | "push" | "auth" | null = (() => {
    const active = (t: PoolPushTask | PoolOpTask | null) =>
      !!t && (t.status === "pending" || t.status === "running");
    if (active(pushTask)) return "push";
    if (active(poolTask)) {
      const kind = poolTask?.kind;
      if (kind === "inspect" || kind === "reauth") return kind;
    }
    // 认证轮询期间 inspecting 保持 true（见 startAuthPolling）
    if (inspecting) return "auth";
    return null;
  })();

  // 页面内任务或全局其它任务执行中：发起类按钮整体禁用
  const anyBusy = runningTask !== null || globalTaskBusy;

  // 当前任务进度条数据：名称 · 状态 + done/total + 百分比；终态仅在保留期内展示
  const activeTask = (() => {
    const STATUS_TEXT: Record<string, string> = {
      pending: "排队中",
      running: "执行中",
      done: "已完成",
      cancelled: "已取消",
    };
    const pick = (
      t: PoolPushTask | PoolOpTask | null,
      name: string,
    ): { name: string; status: string; done: number; total: number; pct: number } | null => {
      if (!t) return null;
      const terminal = t.status === "done" || t.status === "cancelled";
      if (terminal && Date.now() >= taskDoneUntil) return null;
      const pct = t.count > 0 ? Math.min(100, Math.round((t.done / t.count) * 100)) : 0;
      return {
        name,
        status: STATUS_TEXT[t.status] ?? t.status,
        done: t.done,
        total: t.count,
        pct,
      };
    };
    if (pushTask) return pick(pushTask, "推送");
    if (poolTask?.kind === "reauth") return pick(poolTask, "重新登录");
    if (poolTask?.kind === "inspect") return pick(poolTask, "巡检探活");
    // 自动续期后台扫描进行中时展示进度条（非手动任务，扫描结束自动隐藏）
    if (refreshState?.running) {
      const pct =
        refreshState.total > 0
          ? Math.min(100, Math.round((refreshState.done / refreshState.total) * 100))
          : 0;
      return {
        name: "自动续期",
        status: "执行中",
        done: refreshState.done,
        total: refreshState.total,
        pct,
      };
    }
    return null;
  })();

  /** 认证任务轮询：按后端日志游标增量拉取，单账号完成后立即显示结果。 */
  const startAuthPolling = useCallback(
    () => {
      stopAuthPolling();
      setInspecting(true);
      let requestInFlight = false;

      const finishAuth = () => {
        stopAuthPolling();
        setInspecting(false);
      };

      const tick = async () => {
        if (requestInFlight) return;
        requestInFlight = true;
        try {
          const snap = await fetchAuthPoolStatus(authAfterRef.current);
          authAfterRef.current = snap.last_log_id;
          if (snap.logs.length > 0) {
            appendLogs(
              snap.logs.map((log) => ({
                type: "auth" as const,
                level: log.level as PoolLogEntry["level"],
                message: log.message,
              })),
            );
          }
          if (snap.logs.length > 0 || !snap.running) {
            await load(true);
          }
          if (!snap.running) {
            finishAuth();
          }
        } catch {
          // 轮询异常忽略，继续等待
        } finally {
          requestInFlight = false;
        }
      };
      authPollRef.current = setInterval(() => void tick(), 1000);
      void tick();
    },
    [appendLogs, load, stopAuthPolling],
  );

  /** 号池任务日志类型：inspect / reauth */
  const poolLogType = (kind: PoolOpKind): PoolLogEntry["type"] => {
    if (kind === "reauth") return "reauth";
    return "inspect";
  };

  const poolKindLabel = (kind: PoolOpKind): string => {
    if (kind === "reauth") return "重登";
    return "巡检";
  };

  /** 合并号池任务接口返回的首批/增量日志。 */
  const appendPoolTaskLogs = useCallback(
    (task: PoolOpTask, kind: PoolOpKind) => {
      if (task.logs.length === 0) return;
      const logType = poolLogType(kind);
      appendLogs(
        task.logs.map((log) => ({
          type: logType,
          level: log.level as PoolLogEntry["level"],
          message: log.message,
        })),
      );
    },
    [appendLogs],
  );

  /** 巡检/重登任务轮询：增量日志 + 待授权账号弹窗，任务结束收尾 */
  const startPoolPolling = useCallback(
    (taskId: string, kind: PoolOpKind) => {
      stopPoolPolling();
      let requestInFlight = false;
      const tick = async () => {
        if (requestInFlight) return;
        requestInFlight = true;
        try {
          const snap = await fetchPoolOpTaskStatus(taskId, poolAfterRef.current);
          poolAfterRef.current = snap.last_log_id;
          appendPoolTaskLogs(snap, kind);
          // 重登降级的 SSO 重新认证在任务内直接执行，进展由任务日志呈现
          setPoolTask(snap);
          if (snap.status === "done" || snap.status === "cancelled") {
            stopPoolPolling();
            settleTask();
            void load(true);
          } else if (snap.status === "idle") {
            stopPoolPolling();
            setPoolTask(null);
            void load(true);
          }
        } catch (error) {
          stopPoolPolling();
          appendLogs([
            {
              type: poolLogType(kind),
              level: "ERROR",
              message: `[任务] ${poolKindLabel(kind)}轮询失败：${
                error instanceof Error ? error.message : String(error)
              }`,
            },
          ]);
          setPoolTask(null);
        } finally {
          requestInFlight = false;
        }
      };
      poolPollRef.current = setInterval(() => void tick(), 1000);
      void tick();
    },
    [appendLogs, appendPoolTaskLogs, load, settleTask, stopPoolPolling],
  );

  /** 取消巡检/重登任务 */
  const handleCancelPoolTask = useCallback(async () => {
    const kind = poolTask?.kind ?? "inspect";
    try {
      await cancelPoolOpTask();
    } catch (error) {
      appendLogs([
        {
          type: poolLogType(kind),
          level: "ERROR",
          message: `[任务] 取消失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    }
  }, [appendLogs, poolTask]);

  // 首屏加载：首次挂载时若已有缓存数据（路由切回）→ 静默刷新，无缓存 → 正常加载
  const bootRef = useRef(false);
  useEffect(() => {
    if (!bootRef.current) {
      bootRef.current = true;
      load(accounts.length > 0);
      return;
    }
    load();
  }, [load]);

  // 自动续期 daemon 轮询 effect（增量日志 + 进度，常驻 1s；声明见组件顶部）

  // 组件卸载：停止任务轮询、进度条保留定时器与尚未触发的搜索防抖回调
  useEffect(() => {
    return () => {
      stopPushPolling();
      stopPoolPolling();
      stopAuthPolling();
      if (taskDoneTimerRef.current) clearTimeout(taskDoneTimerRef.current);
      if (keywordTimer.current) clearTimeout(keywordTimer.current);
    };
  }, [stopPoolPolling, stopPushPolling, stopAuthPolling]);

  /** 自动续期轮询：常驻 1s，增量拉取扫描日志；轮询失败静默（服务重启等瞬态） */
  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const snap = await fetchAutoRefreshStatus(refreshAfterRef.current);
        if (stopped) return;
        refreshAfterRef.current = snap.last_log_id;
        if (snap.logs.length > 0) {
          appendLogs(
            snap.logs.map((log) => ({
              type: "refresh" as const,
              level: log.level as PoolLogEntry["level"],
              message: log.message,
            })),
          );
        }
        setRefreshState(snap);
      } catch {
        // 静默：不打断页面，下一轮再试
      } finally {
        inFlight = false;
      }
    };
    void tick();
    const timer = setInterval(tick, 1000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [appendLogs]);

  /**
   * 页面刷新恢复执行中的后台任务：服务端快照免 taskId，日志按 last_log_id 续拉。
   * push / 巡检 / 重登完整恢复进度条与日志；自动认证池无任务快照，仅提示。
   * 挂载后恢复一次即可：轮询函数随分页变化，不能放进「每次 effect 都恢复」。
   * recovered 只能在异步恢复成功且未被卸载时置位（避免 Strict Mode 首轮被取消后永久跳过）。
   */
  const taskRecoveredRef = useRef(false);
  useEffect(() => {
    if (taskRecoveredRef.current) return;
    let cancelled = false;
    (async () => {
      // 推送任务
      try {
        const snap = await fetchPushTaskStatus("", 0);
        if (!cancelled && snap.status !== "idle") {
          const terminal = snap.status === "done" || snap.status === "cancelled";
          // 终态不重放历史日志（时间戳会误标为当前），只保留 10s 展示结果
          if (!terminal) {
            pushAfterRef.current = snap.last_log_id;
            if (snap.logs.length > 0) {
              appendLogs(
                snap.logs.map((log) => ({
                  type: "push" as const,
                  level: log.level as PoolLogEntry["level"],
                  message: log.message,
                })),
              );
            }
          }
          if (terminal) {
            setPushTask(snap);
            settleTask();
          } else if (snap.id) {
            setPushTask(snap);
            setLogOpen(true);
            startPushPolling(snap.id);
          }
        }
      } catch {
        // 静默：无可恢复任务
      }
      // 巡检 / 重登（共用同一任务槽）
      try {
        const snap = await fetchPoolOpTaskStatus("", 0);
        if (!cancelled && snap.id && snap.status !== "idle") {
          const terminal = snap.status === "done" || snap.status === "cancelled";
          if (!terminal) {
            poolAfterRef.current = snap.last_log_id;
            if (snap.logs.length > 0) {
              appendLogs(
                snap.logs.map((log) => ({
                  type: snap.kind === "reauth" ? ("reauth" as const) : ("inspect" as const),
                  level: log.level as PoolLogEntry["level"],
                  message: log.message,
                })),
              );
            }
          }
          if (terminal) {
            setPoolTask(snap);
            settleTask();
          } else {
            setPoolTask(snap);
            setLogOpen(true);
            startPoolPolling(snap.id, snap.kind);
          }
        }
      } catch {
        // 静默
      }
      // 自动认证池后台消化（无任务快照，仅提示刷新后仍在进行）
      try {
        const authSnap = await fetchAuthPoolStatus(0);
        authAfterRef.current = authSnap.last_log_id;
        if (!cancelled && authSnap.running && authSnap.logs.length > 0) {
          appendLogs(
            authSnap.logs.map((log) => ({
              type: "auth" as const,
              level: log.level as PoolLogEntry["level"],
              message: log.message,
            })),
          );
        }
        if (!cancelled && authSnap.running) {
          setLogOpen(true);
          appendLogs([
            {
              type: "auth",
              level: "INFO",
              message: `检测到后台自动认证进行中（队列 ${authSnap.queue_size}），Token 交换完成后账号列表自动更新`,
            },
          ]);
          startAuthPolling();
        }
      } catch {
        // 静默
      }
      // 必须等本次恢复跑完再打标：React Strict Mode 会立刻卸载首轮 effect，
      // 若提前把 recovered 置 true，重挂后会跳过恢复，进度条和日志都丢。
      if (!cancelled) taskRecoveredRef.current = true;
    })();
    return () => {
      cancelled = true;
    };
  }, [appendLogs, settleTask, startAuthPolling, startPoolPolling, startPushPolling]);

  const onKeywordChange = (val: string) => {
    // 输入即时回显；防抖 400ms 后将值提交为查询关键词（依赖驱动重新加载），
    // 不再在定时器里直接调 load（旧闭包含过期 keyword，会发双请求或搜索失效）
    setKeywordInput(val);
    if (keywordTimer.current) clearTimeout(keywordTimer.current);
    keywordTimer.current = setTimeout(() => {
      setKeyword(val);
      setPage(1);
    }, 400);
  };

  const onFilterChange = (setter: (v: string) => void) => (v: string) => {
    setter(v);
    setPage(1);
  };

  /**
   * KPI 快捷按钮 → 状态值映射
   * 正常=1；待处理=需重登(2)+限额(3)；异常=拒权(4)+异常(5)（禁用 6 不属异常筛选）
   */
  const METRIC_STATUS_MAP: Record<string, string> = {
    all: "all",
    active: "1",
    pending: "2,3",
    abnormal: "4,5",
  };

  /**
   * KPI 快捷筛选：点击未激活的 metric 切到对应状态；
   * 再次点击已激活的 metric 视为取消 → 恢复全部。
   * 按钮高亮由 statusFilter 派生（单一数据源，与下拉筛选始终同步）。
   */
  const activeMetric =
    statusFilter === "1"
      ? "active"
      : statusFilter === "2,3"
        ? "pending"
        : statusFilter === "4,5"
          ? "abnormal"
          : statusFilter === "all"
            ? "all"
            : null;

  const handleMetricClick = (metric: string) => {
    // 当前激活的就是点击的这个 → 取消筛选恢复全部；否则切换到对应状态
    const next = activeMetric === metric ? "all" : (METRIC_STATUS_MAP[metric] ?? "all");
    setStatusFilter(next);
    setPage(1);
  };

  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  /** 全选/取消仅作用于当前页，保留其他页已勾选 id */
  const toggleSelectAll = () => {
    const pageIds = accounts.map((a) => a.id);
    const allSelected =
      pageIds.length > 0 && pageIds.every((id) => selected.has(id));
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        pageIds.forEach((id) => next.delete(id));
      } else {
        pageIds.forEach((id) => next.add(id));
      }
      return next;
    });
  };

  const togglePassword = (id: number) => {
    setVisiblePasswords((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const runRowAction = async (
    id: number,
    action: PoolRowAction,
  ) => {
    // 删除走复用弹窗：依赖 selected 集合，需先勾选当前行；其余单行操作不改动勾选态
    if (action === "delete") {
      setSelected(new Set([id]));
      setDeleteOpen(true);
      return;
    }
    // 认证轮询由 startAuthPolling 接管 inspecting；其它动作请求期短暂置 busy
    const authAction = action === "auth";
    if (!authAction) setInspecting(true);
    const label = accountLabel(id);
    let authPollingStarted = false;
    try {
      if (action === "auth") {
        openLogDrawer();
        setInspecting(true);
        await prepareAuthLogCursor();
        const res = await authPoolAccounts([id]);
        const r = res.results[0];
        if (r && r.status === "pending") {
          appendLogs([
            {
              type: "auth",
              level: "INFO",
              message: `[认证] ${label}  正在交换 Token`,
            },
          ]);
          startAuthPolling();
          authPollingStarted = true;
        } else if (r) {
          appendLogs([
            {
              type: "auth",
              level: r.status === "skipped" ? "WARNING" : "ERROR",
              message: `[认证] ${label}  ${r.reason || "未发起"}`,
            },
          ]);
        } else {
          appendLogs([
            { type: "auth", level: "WARNING", message: `[认证] ${label}  未发起（无返回）` },
          ]);
        }
      } else if (action === "reauth") {
        openLogDrawer();
        const task = await reauthPoolAccounts([id]);
        setPoolTask(task);
        appendPoolTaskLogs(task, "reauth");
        poolAfterRef.current = task.last_log_id;
        if (task.id) {
          startPoolPolling(task.id, "reauth");
        }
      } else if (action === "disable") {
        await updatePoolAccountStatus([id], 6, "已禁用");
        toast.success("已禁用");
      } else if (action === "enable") {
        await updatePoolAccountStatus([id], 1, "已解禁");
        toast.success("已解禁");
      }
      await load(true);
    } catch (e) {
      const reason =
        e instanceof ApiError ? e.message : `${(e as Error)?.name ?? "Error"}: ${(e as Error)?.message ?? "未知"}`;
      const type =
        action === "auth" ? "auth" : action === "reauth" ? "reauth" : "inspect";
      const tag =
        action === "auth"
          ? "认证"
          : action === "reauth"
            ? "重登"
            : action === "disable"
              ? "禁用"
              : action === "enable"
                ? "解禁"
                : "任务";
      appendLogs([
        {
          type,
          level: "ERROR",
          message: `[${tag}] ${label}  失败：${reason}`,
        },
      ]);
      toast.error(`${tag}失败`, { description: reason });
    } finally {
      // 认证轮询进行中时保持 inspecting，由轮询结束回调清除
      if (!authPollingStarted) setInspecting(false);
    }
  };

  const handleDelete = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    try {
      const res = await deletePoolAccounts(ids);
      setSelected(new Set());
      setDeleteOpen(false);
      toast.success(`已删除 ${res.deleted} 个账号`);
      void load(true);
    } catch (e) {
      toast.error("删除失败", {
        description: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const handleInspect = async () => {
    if (runningTask) return;
    setInspecting(true);
    // 勾选→仅选中账号（未认证/需重登由服务端跳过）；未勾选→服务端全量筛选
    const ids = selected.size > 0 ? [...selected] : undefined;

    openLogDrawer();
    try {
      const task = await inspectPoolAccounts(
        ids,
        Number(concurrencyRef.current?.value) || undefined,
      );
      setPoolTask(task);
      appendPoolTaskLogs(task, "inspect");
      poolAfterRef.current = task.last_log_id;
      if (task.id) {
        startPoolPolling(task.id, "inspect");
      }
      load();
    } catch (error) {
      appendLogs([
        {
          type: "inspect",
          level: "ERROR",
          message: `[巡检] 请求失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    } finally {
      setInspecting(false);
    }
  };

  const handleBatchReauth = async () => {
    if (runningTask) return;
    setInspecting(true);
    // 勾选→仅选中账号；未勾选→服务端全量筛选需重登(2)账号
    const ids = selected.size > 0 ? [...selected] : undefined;
    openLogDrawer();
    try {
      const task = await reauthPoolAccounts(
        ids,
        Number(concurrencyRef.current?.value) || undefined,
      );
      setPoolTask(task);
      appendPoolTaskLogs(task, "reauth");
      poolAfterRef.current = task.last_log_id;
      if (task.id) {
        startPoolPolling(task.id, "reauth");
      }
      void load(true);
    } catch (error) {
      appendLogs([
        {
          type: "reauth",
          level: "ERROR",
          message: `[重登] 请求失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    } finally {
      setInspecting(false);
    }
  };

  const handleBatchAuth = async () => {
    if (runningTask === "auth") {
      stopAuthPolling();
      setInspecting(false);
      appendLogs([
        { type: "auth", level: "WARNING", message: "[认证] 已停止等待" },
      ]);
      return;
    }
    if (runningTask) return;
    setInspecting(true);
    // 勾选→仅选中账号（已认证由服务端跳过）；未勾选→服务端全量筛选未认证账号
    const ids = selected.size > 0 ? [...selected] : undefined;
    openLogDrawer();
    let authPollingStarted = false;
    try {
      await prepareAuthLogCursor();
      const res = await authPoolAccounts(ids);
      const results = res.results ?? [];
      const pending = results.filter((r) => r.status === "pending");
      // 已认证跳过折叠为一条摘要，避免全量模式下逐账号刷屏
      const skippedAuthed = results.filter(
        (r) => r.status === "skipped" && r.reason === "已认证，无需认证",
      );
      const rest = results.filter(
        (r) =>
          r.status !== "pending" &&
          !(r.status === "skipped" && r.reason === "已认证，无需认证"),
      );
      const rows: PoolLogInput[] = rest.map((r): PoolLogInput => {
        const label = accountLabel(r.id);
        if (r.status === "pending") {
          return {
            type: "auth",
            level: "INFO",
            message: `[认证] ${label}  正在交换 Token`,
          };
        }
        return {
          type: "auth",
          level: r.status === "skipped" ? "WARNING" : "ERROR",
          message: `[认证] ${label}  ${r.reason || "未发起"}`,
        };
      });
      if (skippedAuthed.length > 0) {
        rows.unshift({
          type: "auth",
          level: "SUCCESS",
          message: `[认证] ${skippedAuthed.length} 个账号已认证，自动跳过`,
        });
      }
      if (pending.length > 0) {
        rows.push({
          type: "auth",
          level: "INFO",
          message: `[认证] 已提交 ${pending.length} 个账号，等待 Token 交换`,
        });
      }
      appendLogs(rows);
      if (pending.length > 0) {
        startAuthPolling();
        authPollingStarted = true;
      }
      void load(true);
    } catch (error) {
      appendLogs([
        {
          type: "auth",
          level: "ERROR",
          message: `[认证] 请求失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    } finally {
      if (!authPollingStarted) setInspecting(false);
    }
  };

  /** 停止当前运行中的任务（推送/认证/重登/巡检统一入口） */
  const handleStopRunning = useCallback(() => {
    switch (runningTask) {
      case "push":
        void handleCancelPush();
        break;
      case "inspect":
      case "reauth":
        void handleCancelPoolTask();
        break;
      case "auth":
        stopAuthPolling();
        setInspecting(false);
        appendLogs([
          { type: "auth", level: "WARNING", message: "[认证] 已停止等待" },
        ]);
        break;
    }
  }, [runningTask, handleCancelPush, handleCancelPoolTask, stopAuthPolling, appendLogs]);

  /** 发起推送：异步任务 + 增量日志轮询；服务端预筛未认证/状态非正常账号 */
  const handleStartPush = async () => {
    // 勾选→仅推送选中；未勾选→服务端全量筛选已认证且状态正常账号
    const ids = selected.size > 0 ? [...selected] : undefined;
    const targets: Array<"g2a" | "cpa"> = [];
    if (g2aConfigured) targets.push("g2a");
    if (cpaConfigured) targets.push("cpa");
    setPushDialogOpen(false);
    openLogDrawer();
    try {
      const raw = concurrencyRef.current?.value;
      const concurrency = Math.min(20, Math.max(1, Number(raw) || 20));
      const task = await pushPoolAccounts(
        targets,
        ids,
        concurrency,
      );
      pushAfterRef.current = task.last_log_id;
      if (task.logs.length > 0) {
        appendLogs(
          task.logs.map((log) => ({
            type: "push" as const,
            level: log.level as PoolLogEntry["level"],
            message: log.message,
          })),
        );
      }
      setPushTask(task);
      if (task.status === "done" || task.status === "cancelled") {
        settleTask();
        load();
        return;
      }
      if (task.id) startPushPolling(task.id);
    } catch (error) {
      appendLogs([
        {
          type: "push",
          level: "ERROR",
          message: `[推送] 请求失败：${error instanceof Error ? error.message : String(error)}`,
        },
      ]);
    }
  };

  const g2aConfigured = Boolean(config?.g2a_base_url);
  const cpaConfigured = Boolean(config?.cpa_base_url);
  const anyPushTarget = g2aConfigured || cpaConfigured;

  /**
   * 打开任务二次确认弹窗：按任务类型与勾选态计算影响面文案。
   * 勾选 → 精确操作选中集合；未勾选 → 全量模式，由服务端按任务资格全库筛选
   * （数量取 /api/pool/stats 的 task_counts 全库统计，避免受当前分页影响）。
   */
  const openTaskConfirm = (kind: "auth" | "reauth" | "inspect" | "push") => {
    const picked = selected.size > 0;
    let scope: string;
    if (picked) {
      scope = `选中的 ${selected.size} 个账号`;
    } else if (stats.task_counts) {
      // 全量模式：数量取服务端全库统计（避免受当前分页影响）
      const count = stats.task_counts[kind] ?? 0;
      if (count === 0) {
        const reason: Record<typeof kind, string> = {
          auth: "当前没有未认证的账号",
          reauth: "当前没有需重登状态的账号",
          inspect: "当前没有可巡检的账号（已认证且非需重登且非禁用）",
          push: "当前没有可推送的账号（已认证且状态正常）",
        };
        toast.warning(reason[kind]);
        return;
      }
      scope = `全部符合条件的 ${count} 个账号`;
    } else {
      // 统计未加载完成：不误报，交由服务端筛选
      scope = "全部符合条件的账号";
    }
    const detail: Record<typeof kind, string> = {
      auth: "将对账号发起 SSO 认证并交换 Token，未认证账号才会被处理。",
      reauth: "将刷新账号登录态；无刷新凭据或刷新被拒时，任务内直接发起 SSO 重新认证。",
      inspect: "将逐个推理巡检并回写降智判定与到期时间，产生真实上游请求。",
      push: `将把账号同步到 ${[
        g2aConfigured ? "G2A" : null,
        cpaConfigured ? "CPA" : null,
      ]
        .filter(Boolean)
        .join(" / ")}，外部系统将创建对应记录。`,
    };
    setConfirmTask({ kind, scope, detail: detail[kind] });
  };

  /** 确认后分发到对应任务 handler（弹窗先关，避免叠层） */
  const runConfirmedTask = () => {
    const kind = confirmTask?.kind;
    setConfirmTask(null);
    if (kind === "auth") void handleBatchAuth();
    else if (kind === "reauth") void handleBatchReauth();
    else if (kind === "inspect") void handleInspect();
    else if (kind === "push") void handleStartPush();
  };

  return (
    <div className="page-stack pool-page deck-pool">
      <div className="panel pool-workspace">
        <div
          className="mission-strip mission-strip-pool"
          role="toolbar"
          aria-label="号池统计筛选"
        >
          <button
            type="button"
            className={`metric metric-btn ${activeMetric === "all" ? "is-on" : ""}`}
            onClick={() => handleMetricClick("all")}
          >
            <div className="metric-k">全部</div>
            <div className="metric-v">{stats.total}</div>
            <div className="metric-sub">账号总数</div>
          </button>
          <button
            type="button"
            className={`metric metric-btn ${activeMetric === "active" ? "is-on" : ""}`}
            onClick={() => handleMetricClick("active")}
          >
            <div className="metric-k">可用</div>
            <div className="metric-v is-ok">{stats.active}</div>
            <div className="metric-sub">探活正常</div>
          </button>
          <button
            type="button"
            className={`metric metric-btn ${activeMetric === "pending" ? "is-on" : ""}`}
            onClick={() => handleMetricClick("pending")}
          >
            <div className="metric-k">待处理</div>
            <div className="metric-v is-warn">{stats.pending_action}</div>
            <div className="metric-sub">需重登 + 限额</div>
          </button>
          <button
            type="button"
            className={`metric metric-btn ${activeMetric === "abnormal" ? "is-on" : ""}`}
            onClick={() => handleMetricClick("abnormal")}
          >
            <div className="metric-k">异常</div>
            <div className="metric-v is-bad">{stats.abnormal}</div>
            <div className="metric-sub">拒权 + 探活失败</div>
          </button>
        </div>

        <div
          className="command-bar command-bar-inset"
          role="toolbar"
          aria-label="号池命令"
        >
          <div className="command-bar-search">
            <Search strokeWidth={1.6} />
            <Input
              placeholder="邮箱 / 关键词"
              value={keywordInput}
              onChange={(e) => onKeywordChange(e.target.value)}
            />
          </div>
          <div className="command-bar-filters">
            {/*
              状态下拉：KPI 组合筛选（待处理=2,3 / 异常=4,5）不在下拉选项中，
              此时外观强制显示「全部」，实际筛选值保留在 statusFilter。
            */}
            <Select
              value={
                STATUS_FILTER_OPTIONS.some((o) => o.value === statusFilter)
                  ? statusFilter
                  : "all"
              }
              onValueChange={onFilterChange(setStatusFilter)}
            >
              <SelectTrigger size="sm" className="w-[7.5rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_FILTER_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={authedFilter}
              onValueChange={onFilterChange(setAuthedFilter)}
            >
              <SelectTrigger size="sm" className="w-[7.5rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AUTH_FILTER_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={expiryFilter}
              onValueChange={onFilterChange(setExpiryFilter)}
            >
              <SelectTrigger size="sm" className="w-[7.5rem]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {EXPIRY_FILTER_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="command-bar-spacer" />
          <div className="command-bar-actions">
            <div className="command-bar-group" role="group" aria-label="数据操作">
              <Button
                type="button"
                size="sm"
                variant="outline"
                title="立即刷新（另有 10s 自动刷新）"
                className="pool-refresh-btn"
                onClick={() => void handleRefresh()}
                disabled={refreshing}
              >
                <RefreshCw
                  className={refreshing ? "size-3.5 animate-spin" : "size-3.5"}
                  strokeWidth={1.6}
                  aria-hidden
                />
                <span>刷新</span>
              </Button>
              <Button
                size="sm"
                variant={logOpen ? "default" : "outline"}
                className={cn("ops-log-trigger", (runningTask || refreshState?.running) && "is-live")}
                onClick={() => {
                  if (logOpen) setLogOpen(false);
                  else showLogDrawer();
                }}
                title={
                  logEntries.length > 0
                    ? `操作日志 · 最近：${logEntries[logEntries.length - 1].message}`
                    : "查看巡检 / 认证 / 重登 / 推送的业务日志"
                }
              >
                <ScrollText className="size-3.5" strokeWidth={1.6} aria-hidden />
                日志
                {runningTask || refreshState?.running ? (
                  <span className="ops-log-live-dot" aria-hidden />
                ) : null}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={anyBusy}
                title={
                  runningTask
                    ? "任务执行中，请先停止"
                    : selected.size > 0
                      ? "推送选中账号（仅已认证且状态正常，其余跳过）"
                      : "未勾选：推送全部已认证且状态正常的账号"
                }
                onClick={() => setPushDialogOpen(true)}
              >
                <Share className="size-3.5" strokeWidth={1.6} />
                推送
              </Button>
              <Button
                size="sm"
                variant="destructive"
                disabled={selected.size === 0}
                onClick={() => setDeleteOpen(true)}
              >
                <Trash2 className="size-3.5" />
                删除
              </Button>
            </div>
            <span className="command-bar-sep" aria-hidden />
            <div
              className="command-bar-group op-concurrency"
              role="group"
              aria-label="并发数与号池任务"
            >
              <Input
                ref={concurrencyRef}
                type="number"
                min={1}
                max={20}
                inputMode="numeric"
                className="concurrency-input"
                defaultValue="20"
                title="并发数(1-20；认证/重登/巡检/推送默认并发 20)"
                aria-label="并发数"
              />
              <Button
                size="sm"
                variant="secondary"
                disabled={anyBusy}
                title={
                  runningTask
                    ? "任务执行中，请先停止"
                    : selected.size > 0
                      ? "对选中账号执行认证（已认证自动跳过）"
                      : "未勾选：认证全部未认证账号"
                }
                onClick={() => openTaskConfirm("auth")}
              >
                <ShieldCheck className="size-3.5" />
                认证
              </Button>
              <Button
                size="sm"
                variant="secondary"
                disabled={anyBusy}
                title={
                  runningTask
                    ? "任务执行中，请先停止"
                    : selected.size > 0
                      ? "对选中账号刷新登录"
                      : "未勾选：重登全部需重登状态的账号"
                }
                onClick={() => openTaskConfirm("reauth")}
              >
                <LogIn className="size-3.5" />
                重登
              </Button>
              <Button
                size="sm"
                variant={runningTask ? "destructive" : "default"}
                disabled={!runningTask && globalTaskBusy}
                title={
                  runningTask
                    ? `停止${
                        runningTask === "push"
                          ? "推送"
                          : runningTask === "auth"
                            ? "认证"
                            : runningTask === "reauth"
                              ? "重登"
                              : "巡检"
                      }任务`
                    : selected.size > 0
                      ? "巡检探活选中账号（未认证自动跳过）"
                      : "未勾选：巡检全部可探活账号（排除需重登）"
                }
                onClick={
                  runningTask
                    ? handleStopRunning
                    : () => openTaskConfirm("inspect")
                }
              >
                {runningTask ? (
                  <Square className="size-3.5" strokeWidth={1.6} />
                ) : (
                  <Play className="size-3.5" strokeWidth={1.6} />
                )}
                {runningTask ? "停止" : "巡检"}
              </Button>
            </div>
          </div>
        </div>

        <div className="pool-body">
          <CardContent className="pool-table-body space-y-0">
            {/* 任务执行进度条（acorn 风格）：名称 · 状态 + done/total + 百分比 + 细进度条；终态保留展示 10s */}
            {activeTask && (
              <div
                className={cn(
                  "pool-progress",
                  (activeTask.status === "已完成" ||
                    activeTask.status === "已取消") &&
                    "is-done",
                )}
                role="status"
                aria-live="polite"
              >
                <div className="pool-progress-head font-mono">
                  <span className="pool-progress-label">
                    {activeTask.name} · {activeTask.status}
                  </span>
                  <span className="pool-progress-nums tabular-nums">
                    {activeTask.done}/{activeTask.total}
                    <i>{activeTask.pct}%</i>
                  </span>
                </div>
                <div className="pool-progress-track" aria-hidden>
                  <i style={{ width: `${activeTask.pct}%` }} />
                </div>
              </div>
            )}
            <PoolAccountsTable
              accounts={accounts}
              selected={selected}
              visiblePasswords={visiblePasswords}
              loading={loading}
              loadError={loadError}
              inspecting={inspecting}
              onToggleSelectAll={toggleSelectAll}
              onToggleSelect={toggleSelect}
              onTogglePassword={togglePassword}
              onShowDetails={setDetailAccount}
              onRowAction={(id, action: PoolRowAction) => void runRowAction(id, action)}
            />

            <div className="deck-foot">
              <Pagination
                page={page}
                pageSize={pageSize}
                total={total}
                pageSizeOptions={PAGE_SIZE_OPTIONS}
                onPageChange={setPage}
                onPageSizeChange={(ps) => {
                  setPageSize(ps);
                  setPage(1);
                }}
              />
            </div>
          </CardContent>
        </div>
      </div>

      <Dialog
        open={detailAccount !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDetailAccount(null);
            setDetailPasswordVisible(false);
          }
        }}
      >
        <DialogContent className="account-detail-dialog sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>账号详情</DialogTitle>
          </DialogHeader>
          {detailAccount ? (
            <dl className="account-detail-grid">
              <div>
                <dt>ID</dt>
                <dd>{detailAccount.id}</dd>
              </div>
              <div>
                <dt>邮箱</dt>
                <dd>{detailAccount.email}</dd>
              </div>
              <div>
                <dt>密码</dt>
                <dd className="detail-password">
                  <span>
                    {detailPasswordVisible
                      ? detailAccount.password || "—"
                      : detailAccount.password
                        ? "••••••••"
                        : "—"}
                  </span>
                  {detailAccount.password ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      title={detailPasswordVisible ? "隐藏密码" : "显示密码"}
                      onClick={() => setDetailPasswordVisible((v) => !v)}
                    >
                      {detailPasswordVisible ? (
                        <EyeOff className="size-3.5" strokeWidth={1.8} aria-hidden />
                      ) : (
                        <Eye className="size-3.5" strokeWidth={1.8} aria-hidden />
                      )}
                    </Button>
                  ) : null}
                </dd>
              </div>
              <div>
                <dt>认证状态</dt>
                <dd>
                  <Badge variant={detailAccount.has_token ? "success" : "outline"}>
                    {detailAccount.has_token ? "已认证" : "未认证"}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt>到期时间</dt>
                <dd>{formatPoolExpiry(detailAccount.expires_at, detailAccount.expires_in)}</dd>
              </div>
              <div>
                <dt>注册时间</dt>
                <dd>{formatAccountTime(detailAccount.created_at)}</dd>
              </div>
              <div>
                <dt>账号状态</dt>
                <dd>
                  <Badge variant={ACCOUNT_STATUS_VARIANT[detailAccount.status] || "secondary"}>
                    {STATUS_LABELS[detailAccount.status] || "未知"}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt>巡检</dt>
                <dd
                  title={inspectTitle(
                    detailAccount.dumbed,
                    detailAccount.inspect_tps,
                    detailAccount.inspect_thinking,
                    detailAccount.inspected_at
                      ? formatAccountTime(detailAccount.inspected_at)
                      : null,
                  )}
                >
                  <Badge
                    variant={
                      INSPECT_VARIANT[inspectLabel(detailAccount.dumbed)] || "secondary"
                    }
                  >
                    {inspectLabel(detailAccount.dumbed)}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt>巡检时间</dt>
                <dd>{formatAccountTime(detailAccount.inspected_at)}</dd>
              </div>
              <div className="detail-reason">
                <dt>原因</dt>
                <dd>{detailAccount.reason || "—"}</dd>
              </div>
            </dl>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                setDetailAccount(null);
                setDetailPasswordVisible(false);
              }}
            >
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pushDialogOpen} onOpenChange={setPushDialogOpen}>
        <DialogContent className="advanced-dialog sm:max-w-md">
          <DialogHeader>
            <DialogTitle>推送账号</DialogTitle>
            <DialogDescription>
              将已认证账号同步到所选目标；未勾选账号时推送全部已认证且状态正常的账号。后台并发执行，进度与巡检探活一致。目标连接信息在「注册页 → 推送目标设置」中配置。
            </DialogDescription>
          </DialogHeader>
          <div className="push-target-list">
            <label className="push-target-item">
              <Checkbox checked={g2aConfigured} onCheckedChange={() => {}} />
              <span className="push-target-name">G2A</span>
              <span className="push-target-desc">
                {g2aConfigured ? config!.g2a_base_url : "未配置，请先在注册页设置"}
              </span>
            </label>
            <label className="push-target-item">
              <Checkbox checked={cpaConfigured} onCheckedChange={() => {}} />
              <span className="push-target-name">CPA</span>
              <span className="push-target-desc">
                {cpaConfigured ? config!.cpa_base_url : "未配置，请先在注册页设置"}
              </span>
            </label>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPushDialogOpen(false)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={!anyPushTarget}
              onClick={() => {
                // 推送设置弹窗 → 二次确认弹窗（两段式确认）
                setPushDialogOpen(false);
                openTaskConfirm("push");
              }}
              title={
                !anyPushTarget
                  ? "请先在「注册页 → 推送目标设置」中配置目标"
                  : selected.size > 0
                    ? "开始推送选中账号（未认证或状态非正常的将被跳过）"
                    : "开始推送全部已认证且状态正常的账号"
              }
            >
              <Share className="size-3.5" />
              开始推送
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <PoolLogDrawer
        open={logOpen}
        running={runningTask !== null}
        entries={logEntries}
        onClose={() => setLogOpen(false)}
        onClear={() => {
          logSeqRef.current = 0;
          setLogEntries([]);
        }}
      />

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除账号</AlertDialogTitle>
            <AlertDialogDescription>
              确定删除选中的 {selected.size} 个账号？此操作为逻辑删除，可恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className={buttonVariants({ variant: "destructive" })}
              onClick={handleDelete}
            >
              确定删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={confirmTask !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmTask(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              确认发起{confirmTask?.kind === "push"
                ? "推送"
                : confirmTask?.kind === "auth"
                  ? "认证"
                  : confirmTask?.kind === "reauth"
                    ? "重登"
                    : "巡检"}
              ？
            </AlertDialogTitle>
            <AlertDialogDescription>
              即将对 {confirmTask?.scope ?? "—"} 执行任务。{confirmTask?.detail ?? ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={runConfirmedTask}>
              确认执行
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
