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
  RISK_VARIANT,
  STATUS_FILTER_OPTIONS,
  formatPoolExpiry,
  poolLogNow,
  riskLabel,
  riskTitle,
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
  riskPoolAccount,
  updatePoolAccountStatus,
  STATUS_LABELS,
  ApiError,
  type AppConfig,
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
  const [stats, setStats] = usePageCache<PoolStats>("pool.stats", () => ({ total: 0, active: 0, pending_action: 0, abnormal: 0 }));
  const [accounts, setAccounts] = usePageCache<PoolAccount[]>("pool.accounts", () => []);
  // accounts 的 ref：供认证轮询等 useCallback 闭包读取最新账号列表
  const accountsRef = useRef<PoolAccount[]>([]);
  useEffect(() => {
    accountsRef.current = accounts;
  }, [accounts]);
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
  const [authPending, setAuthPending] = useState<{
    email: string;
    verificationUri: string;
    userCode: string;
  } | null>(null);
  const [detailAccount, setDetailAccount] = useState<PoolAccount | null>(null);
  // 详情弹窗独立的密码可见性，避免与列表 visiblePasswords 共享导致联动
  const [detailPasswordVisible, setDetailPasswordVisible] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  // 推送异步任务：running 时轮询状态，结束后清空；null 表示无任务
  const [pushTask, setPushTask] = useState<PoolPushTask | null>(null);
  const pushPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pushAfterRef = useRef(0);
  const concurrencyRef = useRef<HTMLInputElement | null>(null);
  // 巡检 / 重登共享任务槽（服务端单任务互斥）
  const [poolTask, setPoolTask] = useState<PoolOpTask | null>(null);
  const poolPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const poolAfterRef = useRef(0);
  // 认证轮询：发起认证后短期轮询账号状态，确认 Token 是否交换成功
  const authPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 已弹过授权框的任务 id（重登降级只弹一次）
  const poolAuthPromptedRef = useRef<string | null>(null);
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

  /** 账号 id → 展示名（邮箱优先，缺省回退 ID） */
  const accountLabel = useCallback(
    (id: number): string => {
      const acc = accounts.find((a) => a.id === id);
      return acc ? acc.email : `ID ${id}`;
    },
    [accounts],
  );

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

  /** 停止巡检/重登/风控任务轮询 */
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
      pushPollRef.current = setInterval(async () => {
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
      }, 1500);
    },
    [appendLogs, load, settleTask, stopPushPolling],
  );

  /** 当前运行中的任务类型，用于按钮切换为停止态（终态保留展示中的任务不算运行中） */
  const runningTask: "inspect" | "reauth" | "risk" | "push" | "auth" | null = (() => {
    const active = (t: PoolPushTask | PoolOpTask | null) =>
      !!t && (t.status === "pending" || t.status === "running");
    if (active(pushTask)) return "push";
    if (active(poolTask)) {
      const kind = poolTask?.kind;
      if (kind === "inspect" || kind === "reauth" || kind === "risk") return kind;
    }
    // 认证轮询期间 inspecting 保持 true（见 startAuthPolling）
    if (inspecting) return "auth";
    return null;
  })();

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
    if (poolTask?.kind === "reauth") return pick(poolTask, "重登");
    if (poolTask?.kind === "risk") return pick(poolTask, "风控");
    if (poolTask?.kind === "inspect") return pick(poolTask, "巡检");
    return null;
  })();

  /**
   * 认证状态轮询：发起认证后短期轮询账号状态（has_token / status），
   * 确认后台 Token 交换结果。最多轮询 ~5 分钟（20s × 15 次）。
   * 轮询期间保持 inspecting=true，使「停止认证」按钮可用。
   */
  const startAuthPolling = useCallback(
    (ids: number[], labels: Record<number, string>) => {
      stopAuthPolling();
      if (!ids.length) {
        setInspecting(false);
        return;
      }
      setInspecting(true);
      const pending = new Set(ids);
      let ticks = 0;
      let requestInFlight = false;
      const MAX_TICKS = 15;

      const finishAuth = () => {
        stopAuthPolling();
        setInspecting(false);
      };

      authPollRef.current = setInterval(async () => {
        if (requestInFlight) return;
        requestInFlight = true;
        ticks += 1;
        try {
          await load(true);
          for (const id of [...pending]) {
            const acc = accountsRef.current.find((a) => a.id === id);
            const label = labels[id] ?? `ID ${id}`;
            if (acc && acc.has_token) {
              appendLogs([
                {
                  type: "auth",
                  level: "SUCCESS",
                  message: `认证成功: ${label} Token 已入库`,
                },
              ]);
              pending.delete(id);
              continue;
            }
            // status 被后端标记为需重登（2）或禁用（6）且无 token → 交换失败
            if (
              acc &&
              !acc.has_token &&
              (acc.status === 2 || acc.status === 6) &&
              ticks >= 2
            ) {
              appendLogs([
                {
                  type: "auth",
                  level: "ERROR",
                  message: `认证失败: ${label} — ${acc.reason || "Token 交换未成功"}`,
                },
              ]);
              pending.delete(id);
            }
          }
          if (pending.size === 0) {
            setAuthPending(null);
            finishAuth();
            return;
          }
        } catch {
          // 轮询异常忽略，继续等待
        } finally {
          requestInFlight = false;
        }
        if (ticks >= MAX_TICKS) {
          for (const id of pending) {
            const label = labels[id] ?? `ID ${id}`;
            appendLogs([
              {
                type: "auth",
                level: "WARNING",
                message: `认证等待超时: ${label} 后台 Token 交换仍在进行，稍后自动刷新`,
              },
            ]);
          }
          finishAuth();
        }
      }, 20000);
    },
    [appendLogs, load, stopAuthPolling],
  );

  /** 号池任务日志类型：inspect / reauth / risk */
  const poolLogType = (kind: PoolOpKind): PoolLogEntry["type"] => {
    if (kind === "reauth") return "reauth";
    if (kind === "risk") return "inspect"; // 风控归入巡检类日志通道
    return "inspect";
  };

  const poolKindLabel = (kind: PoolOpKind): string => {
    if (kind === "reauth") return "重登";
    if (kind === "risk") return "风控";
    return "巡检";
  };

  /** 巡检/重登/风控任务轮询：增量日志 + 待授权账号弹窗，任务结束收尾 */
  const startPoolPolling = useCallback(
    (taskId: string, kind: PoolOpKind) => {
      stopPoolPolling();
      const logType = poolLogType(kind);
      let requestInFlight = false;
      poolPollRef.current = setInterval(async () => {
        if (requestInFlight) return;
        requestInFlight = true;
        try {
          const snap = await fetchPoolOpTaskStatus(taskId, poolAfterRef.current);
          poolAfterRef.current = snap.last_log_id;
          if (snap.logs.length > 0) {
            appendLogs(
              snap.logs.map((log) => ({
                type: logType,
                level: log.level as PoolLogEntry["level"],
                message: log.message,
              })),
            );
          }
          // 重登降级：首个待授权账号弹浏览器授权框（同一任务只弹一次）
          if (
            kind === "reauth" &&
            snap.pending_list.length > 0 &&
            poolAuthPromptedRef.current !== taskId
          ) {
            poolAuthPromptedRef.current = taskId;
            const first = snap.pending_list[0];
            setAuthPending({
              email: first.email || accountLabel(first.id),
              verificationUri: first.verification_uri,
              userCode: first.user_code ?? "",
            });
          }
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
              type: logType,
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
      }, 1500);
    },
    [accountLabel, appendLogs, load, settleTask, stopPoolPolling],
  );

  /** 取消巡检/重登/风控任务 */
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

  // 10s 自动刷新（静默，不闪骨架屏）
  useEffect(() => {
    const timer = setInterval(() => load(true), 10000);
    return () => clearInterval(timer);
  }, [load]);

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
        const res = await authPoolAccounts([id]);
        const r = res.results[0];
        if (r && r.status === "pending" && r.verification_uri) {
          setAuthPending({
            email: r.email ?? label,
            verificationUri: r.verification_uri,
            userCode: r.user_code ?? "",
          });
          appendLogs([
            {
              type: "auth",
              level: "INFO",
              message: `[认证] ${label}  待授权，请在浏览器完成`,
            },
          ]);
          startAuthPolling([id], { [id]: label });
          authPollingStarted = true;
        } else if (r && r.status === "pending") {
          appendLogs([
            {
              type: "auth",
              level: "INFO",
              message: `[认证] ${label}  正在交换 Token`,
            },
          ]);
          startAuthPolling([id], { [id]: label });
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
        poolAuthPromptedRef.current = null;
        poolAfterRef.current = task.last_log_id;
        if (task.id) {
          startPoolPolling(task.id, "reauth");
        }
      } else if (action === "risk") {
        openLogDrawer();
        const task = await riskPoolAccount(id);
        setPoolTask(task);
        poolAuthPromptedRef.current = null;
        poolAfterRef.current = task.last_log_id;
        if (task.id) {
          startPoolPolling(task.id, "risk");
        }
      } else if (action === "disable") {
        await updatePoolAccountStatus([id], 6, "已禁用");
        toast.success("已禁用");
      }
      await load(true);
    } catch (e) {
      const reason =
        e instanceof ApiError ? e.message : `${(e as Error)?.name ?? "Error"}: ${(e as Error)?.message ?? "未知"}`;
      const type = action === "auth" ? "auth" : action === "reauth" ? "reauth" : "inspect";
      const tag = action === "auth" ? "认证" : action === "reauth" ? "重登" : action === "risk" ? "风控" : "任务";
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
    const authedIds =
      selected.size > 0
        ? accounts.filter((a) => a.has_token && selected.has(a.id)).map((a) => a.id)
        : [];

    // 勾选了账号但全部未认证 → 不执行
    if (selected.size > 0 && authedIds.length === 0) {
      openLogDrawer();
      appendLogs([
        {
          type: "inspect",
          level: "WARNING",
          message: "[巡检] 选中账号均未认证，已跳过",
        },
      ]);
      setInspecting(false);
      return;
    }

    // 必须先 openLogDrawer（清空旧日志）再 append，否则启动日志会被清掉
    openLogDrawer();
    appendLogs([
      {
        type: "inspect",
        level: "INFO",
        message: `[任务] 巡检开始  ${authedIds.length > 0 ? `选中 ${authedIds.length}` : "全部已认证"} / 并发 ${Number(concurrencyRef.current?.value) || 20}`,
      },
    ]);
    try {
      const task = await inspectPoolAccounts(
        authedIds.length > 0 ? authedIds : undefined,
        Number(concurrencyRef.current?.value) || undefined,
      );
      setPoolTask(task);
      poolAuthPromptedRef.current = null;
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
    // 与单行重登一致：状态 2/4/5 即可，不强制 has_token（无 token 走设备授权降级）
    const ids = accounts
      .filter(
        (a) =>
          (selected.size === 0 || selected.has(a.id)) &&
          [2, 4, 5].includes(a.status),
      )
      .map((a) => a.id);
    if (ids.length === 0) {
      openLogDrawer();
      appendLogs([
        {
          type: "reauth",
          level: "WARNING",
          message: "[重登] 无待重登账号，已跳过",
        },
      ]);
      setInspecting(false);
      return;
    }
    openLogDrawer();
    try {
      const task = await reauthPoolAccounts(
        ids,
        Number(concurrencyRef.current?.value) || undefined,
      );
      setPoolTask(task);
      poolAuthPromptedRef.current = null;
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
    if (runningTask || selected.size === 0) return;
    setInspecting(true);
    const ids = accounts
      .filter((a) => !a.has_token && selected.has(a.id))
      .map((a) => a.id);
    if (ids.length === 0) {
      openLogDrawer();
      appendLogs([
        {
          type: "auth",
          level: "WARNING",
          message: "[认证] 选中账号均已认证，已跳过",
        },
      ]);
      setInspecting(false);
      return;
    }
    openLogDrawer();
    let authPollingStarted = false;
    try {
      const res = await authPoolAccounts(ids);
      const results = res.results ?? [];
      const pending = results.filter((r) => r.status === "pending");
      const rows: PoolLogInput[] = results.map((r): PoolLogInput => {
        const label = accountLabel(r.id);
        if (r.status === "pending" && r.verification_uri) {
          return {
            type: "auth",
            level: "INFO",
            message: `[认证] ${label}  待授权，请在浏览器完成`,
          };
        }
        if (r.status === "pending") {
          return {
            type: "auth",
            level: "INFO",
            message: `[认证] ${label}  正在交换 Token`,
          };
        }
        const ok = r.status === "skipped" && r.reason === "已认证，无需认证";
        return {
          type: "auth",
          level: ok ? "SUCCESS" : r.status === "skipped" ? "WARNING" : "ERROR",
          message: ok
            ? `[认证] ${label}  ${r.reason}`
            : `[认证] ${label}  ${r.reason || "未发起"}`,
        };
      });
      appendLogs(rows);
      const first = pending.find((r) => r.verification_uri);
      if (first?.verification_uri) {
        setAuthPending({
          email: first.email ?? accountLabel(first.id),
          verificationUri: first.verification_uri,
          userCode: first.user_code ?? "",
        });
      }
      if (pending.length > 0) {
        const labels: Record<number, string> = {};
        for (const r of pending) labels[r.id] = accountLabel(r.id);
        startAuthPolling(
          pending.map((r) => r.id),
          labels,
        );
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

  /** 停止当前运行中的任务（推送/认证/重登/巡检/风控统一入口） */
  const handleStopRunning = useCallback(() => {
    switch (runningTask) {
      case "push":
        void handleCancelPush();
        break;
      case "inspect":
      case "reauth":
      case "risk":
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
        selected.size > 0 ? [...selected] : undefined,
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

  /** 勾选中是否存在可推送账号（已认证且状态正常）；未勾选视为可推送 */
  const hasPushableSelected =
    selected.size === 0 ||
    accounts.some((a) => a.has_token && a.status === 1 && selected.has(a.id));

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
            <div className="metric-sub">需重新登录 + 限额</div>
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
                className={cn("ops-log-trigger", runningTask && "is-live")}
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
                {runningTask ? (
                  <span className="ops-log-live-dot" aria-hidden />
                ) : null}
              </Button>
              <Button
                size="sm"
                variant={runningTask === "push" ? "destructive" : "outline"}
                disabled={
                  !runningTask && (!anyPushTarget || !hasPushableSelected)
                }
                title={
                  runningTask === "push"
                    ? "停止推送任务"
                    : runningTask
                      ? "任务执行中，请先停止"
                      : !anyPushTarget
                        ? "请先在「注册页 → 推送目标设置」中配置目标"
                        : !hasPushableSelected
                          ? "选中账号均未认证或状态非正常，禁止推送"
                          : selected.size > 0
                            ? "推送选中账号（仅已认证且状态正常，其余跳过）"
                            : "推送全部已认证且状态正常的账号"
                }
                onClick={
                  runningTask === "push"
                    ? handleCancelPush
                    : () => setPushDialogOpen(true)
                }
              >
                {runningTask === "push" ? (
                  <Square className="size-3.5" strokeWidth={1.6} />
                ) : (
                  <Share className="size-3.5" strokeWidth={1.6} />
                )}
                {runningTask === "push" ? "停止推送" : "推送"}
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
                variant={runningTask === "auth" ? "destructive" : "secondary"}
                disabled={
                  runningTask === "auth"
                    ? false
                    : runningTask !== null ||
                      selected.size === 0 ||
                      !accounts.some(
                        (a) => !a.has_token && selected.has(a.id),
                      )
                }
                title={
                  runningTask === "auth"
                    ? "停止认证等待"
                    : runningTask
                      ? "任务执行中，请先停止"
                      : selected.size === 0
                        ? "请先勾选账号"
                        : !accounts.some(
                              (a) => !a.has_token && selected.has(a.id),
                            )
                          ? "选中账号均已认证"
                          : "对选中账号执行认证"
                }
                onClick={() => void handleBatchAuth()}
              >
                <ShieldCheck className="size-3.5" />
                {runningTask === "auth" ? "停止认证" : "认证"}
              </Button>
              <Button
                size="sm"
                variant={
                  runningTask === "reauth" ? "destructive" : "secondary"
                }
                disabled={
                  runningTask !== null ||
                  !accounts.some(
                    (a) =>
                      [2, 4, 5].includes(a.status) &&
                      (selected.size === 0 || selected.has(a.id)),
                  )
                }
                title={
                  runningTask
                    ? "任务执行中，请先停止"
                    : !accounts.some(
                          (a) =>
                            [2, 4, 5].includes(a.status) &&
                            (selected.size === 0 || selected.has(a.id)),
                        )
                      ? "没有待重登的账号"
                      : "对需重登 / 限额 / 异常账号刷新登录"
                }
                onClick={
                  runningTask === "reauth"
                    ? handleCancelPoolTask
                    : handleBatchReauth
                }
              >
                <LogIn className="size-3.5" />
                {runningTask === "reauth" ? "停止重登" : "重登"}
              </Button>
              <Button
                size="sm"
                variant={runningTask ? "destructive" : "default"}
                disabled={
                  !runningTask &&
                  selected.size > 0 &&
                  !accounts.some((a) => a.has_token && selected.has(a.id))
                }
                title={
                  runningTask
                    ? `停止${
                        runningTask === "push"
                          ? "推送"
                          : runningTask === "auth"
                            ? "认证"
                            : runningTask === "reauth"
                              ? "重登"
                              : runningTask === "risk"
                                ? "风控"
                                : "巡检"
                      }任务`
                    : selected.size > 0 &&
                        !accounts.some(
                          (a) => a.has_token && selected.has(a.id),
                        )
                      ? "选中账号均未认证，无法巡检"
                      : selected.size > 0
                        ? "巡检探活选中已认证账号"
                        : "巡检探活全部已认证账号"
                }
                onClick={
                  runningTask
                    ? handleStopRunning
                    : () => void handleInspect()
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
            <DialogDescription>ID {detailAccount?.id} 的完整账号信息。</DialogDescription>
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
                <dt>风控强度</dt>
                <dd title={riskTitle(detailAccount.risk, detailAccount.bfs, detailAccount.checked_at)}>
                  <Badge variant={RISK_VARIANT[riskLabel(detailAccount.risk, detailAccount.bfs).label] || "secondary"}>
                    {riskLabel(detailAccount.risk, detailAccount.bfs).label}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt>风控检测时间</dt>
                <dd>{formatAccountTime(detailAccount.checked_at)}</dd>
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

      <Dialog
        open={authPending !== null}
        onOpenChange={(open) => {
          if (!open) setAuthPending(null);
        }}
      >
        <DialogContent className="advanced-dialog sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>账号认证授权</DialogTitle>
            <DialogDescription>
              请在浏览器中打开以下链接，登录并完成授权。授权完成后后台会自动交换 Token，
              账号将变为「已认证」。
            </DialogDescription>
          </DialogHeader>
          {authPending ? (
            <div className="auth-pending-card">
              <div className="auth-pending-email">{authPending.email}</div>
              {authPending.userCode ? (
                <div className="auth-pending-code">
                  <span>授权码</span>
                  <code>{authPending.userCode}</code>
                </div>
              ) : null}
              <a
                href={authPending.verificationUri}
                target="_blank"
                rel="noreferrer"
                className="auth-pending-link"
              >
                {authPending.verificationUri}
              </a>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                navigator.clipboard
                  ?.writeText(authPending?.verificationUri ?? "")
                  .catch(() => {});
              }}
            >
              复制链接
            </Button>
            <Button type="button" onClick={() => setAuthPending(null)}>
              我知道了
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pushDialogOpen} onOpenChange={setPushDialogOpen}>
        <DialogContent className="advanced-dialog sm:max-w-md">
          <DialogHeader>
            <DialogTitle>推送账号</DialogTitle>
            <DialogDescription>
              将已认证账号同步到所选目标。后台并发执行，进度与巡检探活一致。目标连接信息在「注册页 → 推送目标设置」中配置。
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
              disabled={!anyPushTarget || !hasPushableSelected}
              onClick={handleStartPush}
              title={
                !anyPushTarget
                  ? "请先在「注册页 → 推送目标设置」中配置目标"
                  : !hasPushableSelected
                    ? "选中账号均未认证或状态非正常，禁止推送"
                    : "开始推送（未认证或状态非正常的账号将被跳过）"
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
    </div>
  );
}
