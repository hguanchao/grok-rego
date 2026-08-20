import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BadgeCheck,
  ChevronDown,
  CircleX,
  Eraser,
  Eye,
  EyeOff,
  FoldVertical,
  Play,
  Save,
  SlidersHorizontal,
  Square,
  Terminal,
  UnfoldVertical,
  UserRound,
  UsersRound,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui";
import { Empty } from "@/components/ui";
import {
  DomainListEditor,
  Field,
  LogLineView,
  Stepper,
  ThreadPanelHeader,
} from "@/components/register/RegisterPageParts";
import {
  ApiError,
  clearRegisterLogs,
  fetchConfig,
  fetchRegisterStatus,
  saveConfig,
  startRegister,
  stopRegister,
  type AppConfig,
  type DomainMode,
  type LogEntry,
  type MailProvider,
  type RegisterJobState,
} from "@/lib/api";
import { cn, groupRegisterLogs } from "@/lib/utils";

const IDLE_JOB: RegisterJobState = {
  id: null,
  status: "idle",
  count: 0,
  threads: 0,
  headless: true,
  mail_provider: "",
  success: 0,
  failed: 0,
  denied: 0,
  done: 0,
  running: 0,
  started_at: null,
  finished_at: null,
  error: null,
  logs: [],
  last_log_id: 0,
  progress: 0,
};

function isActiveStatus(status: string): boolean {
  return status === "pending" || status === "running" || status === "stopping";
}

function formatElapsed(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt) return "00:00:00";
  const start = Date.parse(startedAt);
  if (Number.isNaN(start)) return "00:00:00";
  const end = finishedAt ? Date.parse(finishedAt) : Date.now();
  const sec = Math.max(0, Math.floor(((Number.isNaN(end) ? Date.now() : end) - start) / 1000));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

function statusLabel(status: string): string {
  switch (status) {
    case "pending":
      return "排队中";
    case "running":
      return "运行中";
    case "stopping":
      return "停止中";
    case "completed":
      return "已完成";
    case "cancelled":
      return "已取消";
    case "failed":
      return "失败";
    default:
      return "空闲";
  }
}

function mailProviderLabel(provider: string): string {
  return provider === "yyds" ? "YYDS" : provider === "cf" ? "Cloudflare" : "邮箱";
}

function errMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function RegisterPage() {
  const [loadingConfig, setLoadingConfig] = useState(true);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);

  // 表单（本地编辑，保存/启动时提交）
  const [mailProvider, setMailProvider] = useState<MailProvider>("cf");
  const [mode, setMode] = useState<"batch" | "single">("single");
  const [headless, setHeadless] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(true);
  const [count, setCount] = useState(1);
  const [threads, setThreads] = useState(1);
  const [proxy, setProxy] = useState("");

  // 邮箱设置草稿
  const [mailDialogOpen, setMailDialogOpen] = useState(false);
  const [cfApiBase, setCfApiBase] = useState("");
  const [cfApiKey, setCfApiKey] = useState("");
  const [cfDomains, setCfDomains] = useState<string[]>([]);
  const [cfDomainMode, setCfDomainMode] = useState<DomainMode>("random");
  const [yydsApiBase, setYydsApiBase] = useState("");
  const [yydsApiKey, setYydsApiKey] = useState("");

  // 推送目标设置
  const [pushDialogOpen, setPushDialogOpen] = useState(false);
  const [g2aBaseUrl, setG2aBaseUrl] = useState("");
  const [g2aUsername, setG2aUsername] = useState("");
  const [g2aPassword, setG2aPassword] = useState("");
  const [cpaBaseUrl, setCpaBaseUrl] = useState("");
  const [cpaManagementKey, setCpaManagementKey] = useState("");

  // 任务
  const [job, setJob] = useState<RegisterJobState>(IDLE_JOB);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const lastLogIdRef = useRef(0);
  const hasConfigRef = useRef(false);
  const logEndRef = useRef<HTMLDivElement | null>(null);
  const [, setTick] = useState(0);

  /** 展开中的线程组键（main / T001 …） */
  const [openThreads, setOpenThreads] = useState<string[]>([]);
  const seenWorkersRef = useRef<Set<string>>(new Set());
  const userTouchedCollapse = useRef(false);

  const busy = isActiveStatus(job.status);
  const formDisabled = busy || loadingConfig || starting;

  /** 主控 / 认证池 / 线程 三组 */
  const logGroups = useMemo(() => groupRegisterLogs(logs), [logs]);
  const threadGroups = useMemo(() => {
    return [logGroups.main, logGroups.auth, ...logGroups.workers].filter(
      (g): g is NonNullable<typeof g> => g != null,
    );
  }, [logGroups]);

  // 新线程默认展开；用户手动折叠后只展开新出现的线程
  useEffect(() => {
    if (!threadGroups.length) {
      seenWorkersRef.current = new Set();
      return;
    }
    const fresh: string[] = [];
    for (const group of threadGroups) {
      if (!seenWorkersRef.current.has(group.worker)) {
        seenWorkersRef.current.add(group.worker);
        fresh.push(group.worker);
      }
    }
    if (!fresh.length) return;
    setOpenThreads((prev) => {
      if (!userTouchedCollapse.current && prev.length === 0) {
        return threadGroups.map((g) => g.worker);
      }
      const set = new Set(prev);
      fresh.forEach((worker) => set.add(worker));
      return Array.from(set);
    });
  }, [threadGroups]);

  /** 全部展开 / 折叠线程日志 */
  const expandAllLogs = () => {
    userTouchedCollapse.current = true;
    setOpenThreads(threadGroups.map((g) => g.worker));
  };

  const collapseAllLogs = () => {
    userTouchedCollapse.current = true;
    setOpenThreads([]);
  };

  const toggleThread = (worker: string, open: boolean) => {
    userTouchedCollapse.current = true;
    setOpenThreads((prev) => {
      if (open) {
        return prev.includes(worker) ? prev : [...prev, worker];
      }
      return prev.filter((w) => w !== worker);
    });
  };

  const applyConfig = useCallback((data: AppConfig) => {
    hasConfigRef.current = true;
    setMailProvider(data.mail_provider === "yyds" ? "yyds" : "cf");
    setProxy(data.proxy || "");
    setAuthEnabled(Boolean(data.auth_enabled));
    setCfApiBase(data.cf_api_base || "");
    setCfApiKey(data.cf_api_key || "");
    setCfDomains(Array.isArray(data.cf_domains) ? data.cf_domains : []);
    setCfDomainMode(data.cf_domain_mode === "poll" ? "poll" : "random");
    setYydsApiBase(data.yyds_api_base || "");
    setYydsApiKey(data.yyds_api_key || "");
    setG2aBaseUrl(data.g2a_base_url || "");
    setG2aUsername(data.g2a_username || "");
    setG2aPassword(data.g2a_password || "");
    setCpaBaseUrl(data.cpa_base_url || "");
    setCpaManagementKey(data.cpa_management_key || "");
  }, []);

  const buildConfigPatch = useCallback((): Partial<AppConfig> => {
    return {
      mail_provider: mailProvider,
      proxy: proxy.trim(),
      auth_enabled: authEnabled,
      cf_api_base: cfApiBase.trim(),
      cf_api_key: cfApiKey,
      cf_domains: cfDomains,
      cf_domain_mode: cfDomainMode,
      yyds_api_base: yydsApiBase.trim(),
      yyds_api_key: yydsApiKey,
      g2a_base_url: g2aBaseUrl.trim(),
      g2a_username: g2aUsername.trim(),
      g2a_password: g2aPassword,
      cpa_base_url: cpaBaseUrl.trim(),
      cpa_management_key: cpaManagementKey,
    };
  }, [
    mailProvider,
    proxy,
    authEnabled,
    cfApiBase,
    cfApiKey,
    cfDomains,
    cfDomainMode,
    yydsApiBase,
    yydsApiKey,
    g2aBaseUrl,
    g2aUsername,
    g2aPassword,
    cpaBaseUrl,
    cpaManagementKey,
  ]);

  // 初始加载配置 + 任务状态
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cfg, status] = await Promise.all([
          fetchConfig(),
          fetchRegisterStatus(0),
        ]);
        if (cancelled) return;
        applyConfig(cfg);
        setApiOnline(true);
        setJob(status);
        if (status.logs?.length) {
          setLogs(status.logs);
          lastLogIdRef.current = status.last_log_id;
        }
      } catch (error) {
        if (!cancelled) {
          setApiOnline(false);
          toast.error("无法连接管理 API", { description: errMessage(error) });
        }
      } finally {
        if (!cancelled) setLoadingConfig(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyConfig]);

  // 任务轮询
  useEffect(() => {
    let timer: number | undefined;
    let alive = true;

    const poll = async () => {
      try {
        const status = await fetchRegisterStatus(lastLogIdRef.current);
        if (!alive) return;
        setApiOnline(true);
        setJob((prev) => ({
          ...status,
          // 增量日志由 logs 状态单独合并
          logs: prev.logs,
        }));
        if (status.logs.length) {
          setLogs((prev) => {
            const seen = new Set(prev.map((item) => item.id));
            const merged = [...prev];
            for (const entry of status.logs) {
              if (!seen.has(entry.id)) merged.push(entry);
            }
            return merged;
          });
          lastLogIdRef.current = Math.max(
            lastLogIdRef.current,
            status.last_log_id,
          );
        }
        if (!hasConfigRef.current) {
          try {
            applyConfig(await fetchConfig());
          } catch {
            /* 下次轮询再试 */
          }
        }
      } catch {
        if (alive) setApiOnline(false);
      } finally {
        if (alive) {
          const active = isActiveStatus(job.status);
          timer = window.setTimeout(poll, active ? 1000 : 3000);
        }
      }
    };

    timer = window.setTimeout(poll, 800);
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [applyConfig, job.status]);

  // 运行中刷新耗时
  useEffect(() => {
    if (!isActiveStatus(job.status)) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [job.status]);

  // 日志自动滚底
  useEffect(() => {
    if (!threadGroups.length) return;
    logEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [logs, threadGroups.length]);

  const successRate = useMemo(() => {
    if (!job.done) return "0.00%";
    return `${((job.success / job.done) * 100).toFixed(2)}%`;
  }, [job.done, job.success]);

  const handleSaveConfig = async () => {
    setSaving(true);
    try {
      const next = await saveConfig(buildConfigPatch());
      applyConfig(next);
      toast.success("配置已保存");
    } catch (error) {
      toast.error("保存失败", { description: errMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  const handleStart = async () => {
    const finalCount = mode === "single" ? 1 : Math.max(1, Math.min(100, Math.trunc(count)));
    const finalThreads = mode === "single" ? 1 : Math.max(1, Math.min(20, Math.trunc(threads)));
    if (mailProvider === "cf" && !cfApiBase.trim()) {
      toast.error("请先配置 Cloudflare 邮箱 API 地址");
      setMailDialogOpen(true);
      return;
    }
    if (mailProvider === "cf" && !cfDomains.length) {
      toast.error("请先配置 Cloudflare 邮箱域名");
      setMailDialogOpen(true);
      return;
    }
    if (mailProvider === "yyds" && !yydsApiKey.trim()) {
      toast.error("请先配置 YYDS API 密钥");
      setMailDialogOpen(true);
      return;
    }
    setStarting(true);
    try {
      // 新任务清空本地日志游标与线程折叠态，避免沿用上轮展开集合
      setLogs([]);
      lastLogIdRef.current = 0;
      seenWorkersRef.current = new Set();
      userTouchedCollapse.current = false;
      setOpenThreads([]);
      const status = await startRegister({
        count: finalCount,
        threads: finalThreads,
        headless,
        config: buildConfigPatch(),
      });
      setJob(status);
      if (status.logs?.length) {
        setLogs(status.logs);
        lastLogIdRef.current = status.last_log_id;
      }
      toast.success("注册任务已启动");
    } catch (error) {
      toast.error("启动失败", { description: errMessage(error) });
    } finally {
      setStarting(false);
    }
  };

  const handleStop = async () => {
    setStopping(true);
    try {
      const status = await stopRegister();
      setJob((prev) => ({ ...status, logs: prev.logs }));
      toast.message("已请求停止", { description: "当前步骤结束后退出" });
    } catch (error) {
      toast.error("停止失败", { description: errMessage(error) });
    } finally {
      setStopping(false);
    }
  };

  const handleClearLogs = async () => {
    try {
      const status = await clearRegisterLogs();
      setLogs(status.logs ?? []);
      lastLogIdRef.current = status.last_log_id;
    } catch (error) {
      toast.error("清空日志失败", { description: errMessage(error) });
    }
  };

  const handleMailDialogDone = () => {
    setMailDialogOpen(false);
  };

  const handlePushDialogDone = () => {
    setPushDialogOpen(false);
  };

  const elapsed = formatElapsed(job.started_at, job.finished_at);
  const progressWidth = `${Math.min(100, Math.max(0, job.progress))}%`;

  return (
    <div className="page-stack register-page">
      <div className="deck-register">
        {/* CONTROL RAIL */}
        <aside className="panel control-rail">
          <div className="rail-head">
            <div className="rail-title">
              <em>//</em>Control
            </div>
          </div>

          <div className="rail-section">
            <div className="section-label">邮箱</div>
            <div className="field-block">
              <div className="field-label">
                <b>服务商</b>
              </div>
              <div className="email-service-control">
                <div className="email-service-select">
                  <Select
                    value={mailProvider}
                    onValueChange={(value) =>
                      setMailProvider(value === "yyds" ? "yyds" : "cf")
                    }
                    disabled={formDisabled}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="yyds">YYDS Mail</SelectItem>
                      <SelectItem value="cf">Cloudflare</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  aria-label="打开邮箱设置"
                  title="邮箱设置"
                  disabled={formDisabled}
                  onClick={() => setMailDialogOpen(true)}
                >
                  <SlidersHorizontal className="size-3.5" strokeWidth={1.6} />
                </Button>
              </div>
            </div>
          </div>

          <div className="rail-section">
            <div className="section-label">任务</div>
            <div className="field-block">
              <div className="field-label">
                <b>注册模式</b>
              </div>
              <ToggleGroup
                type="single"
                className="mode-toggle"
                value={mode}
                onValueChange={(value) => {
                  if (value === "batch") {
                    setMode("batch");
                    setCount(10);
                    setThreads(2);
                  }
                  if (value === "single") setMode(value);
                }}
                disabled={formDisabled}
                aria-label="注册模式"
              >
                <ToggleGroupItem className="mode-toggle-item" value="batch" aria-label="批量注册">
                  <UsersRound className="size-3.5" strokeWidth={1.6} />
                  批量
                </ToggleGroupItem>
                <ToggleGroupItem className="mode-toggle-item" value="single" aria-label="单个注册">
                  <UserRound className="size-3.5" strokeWidth={1.6} />
                  单个
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
            <div className="field-pair">
              <div className="field-block">
                <div className="field-label">
                  <b>数量</b>
                </div>
                <Stepper
                  value={mode === "single" ? 1 : count}
                  min={1}
                  max={100}
                  disabled={formDisabled || mode === "single"}
                  onChange={setCount}
                />
              </div>
              <div className="field-block">
                <div className="field-label">
                  <b>并发</b>
                </div>
                <Stepper
                  value={mode === "single" ? 1 : threads}
                  min={1}
                  max={20}
                  disabled={formDisabled || mode === "single"}
                  onChange={setThreads}
                />
              </div>
            </div>
            <div className="field-block">
              <div className="field-label">
                <b>浏览器模式</b>
              </div>
              <ToggleGroup
                type="single"
                className="mode-toggle"
                value={headless ? "headless" : "visible"}
                onValueChange={(value) => {
                  if (value === "headless") setHeadless(true);
                  if (value === "visible") setHeadless(false);
                }}
                disabled={formDisabled}
                aria-label="浏览器模式"
              >
                <ToggleGroupItem className="mode-toggle-item" value="visible" aria-label="可视浏览器">
                  <Eye className="size-3.5" strokeWidth={1.6} />
                  可视
                </ToggleGroupItem>
                <ToggleGroupItem className="mode-toggle-item" value="headless" aria-label="无头浏览器">
                  <EyeOff className="size-3.5" strokeWidth={1.6} />
                  无头
                </ToggleGroupItem>
              </ToggleGroup>
            </div>

            <Collapsible className="field-block">
              <CollapsibleTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="rail-more-trigger"
                >
                  <span>更多选项</span>
                  <ChevronDown className="rail-more-chevron size-3.5" strokeWidth={1.6} />
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="rail-more-body">
                  <div className="field-block">
                    <div className="field-label">
                      <b>本地代理</b>
                    </div>
                    <Input
                      type="text"
                      placeholder="http://127.0.0.1:7890"
                      value={proxy}
                      disabled={formDisabled}
                      onChange={(event) => setProxy(event.target.value)}
                    />
                  </div>
                  <div className="field-block">
                    <div className="field-label">
                      <b>SSO 授权</b>
                    </div>
                    <ToggleGroup
                      type="single"
                      className="mode-toggle"
                      value={authEnabled ? "auth" : "noauth"}
                      onValueChange={(value) => {
                        if (value === "auth") setAuthEnabled(true);
                        if (value === "noauth") setAuthEnabled(false);
                      }}
                      disabled={formDisabled}
                      aria-label="注册后自动进行 SSO 授权"
                    >
                      <ToggleGroupItem className="mode-toggle-item" value="auth" aria-label="自动 SSO 授权">
                        <BadgeCheck className="size-3.5" strokeWidth={1.6} />
                        开启
                      </ToggleGroupItem>
                      <ToggleGroupItem className="mode-toggle-item" value="noauth" aria-label="不自动 SSO 授权">
                        <CircleX className="size-3.5" strokeWidth={1.6} />
                        关闭
                      </ToggleGroupItem>
                    </ToggleGroup>
                  </div>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="w-full"
                    title="G2A / CPA 推送目标"
                    disabled={formDisabled}
                    onClick={() => setPushDialogOpen(true)}
                  >
                    <SlidersHorizontal className="size-3.5" strokeWidth={1.6} />
                    推送目标设置
                  </Button>
                </div>
              </CollapsibleContent>
            </Collapsible>
          </div>

          <div className="rail-actions rail-actions-primary">
            {busy ? (
              <Button
                type="button"
                variant="destructive"
                className="rail-stop-btn"
                disabled={stopping}
                onClick={() => void handleStop()}
              >
                <Square strokeWidth={1.6} />
                {stopping || job.status === "stopping" ? "停止中…" : "停止注册"}
              </Button>
            ) : (
              <Button
                type="button"
                className="rail-start-btn"
                disabled={formDisabled || starting}
                onClick={() => void handleStart()}
              >
                <Play strokeWidth={1.6} />
                {starting ? "启动中…" : "开始注册"}
              </Button>
            )}
            {!busy ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="w-full"
                disabled={formDisabled || saving}
                onClick={() => void handleSaveConfig()}
              >
                <Save strokeWidth={1.6} />
                {saving ? "保存中…" : "保存配置"}
              </Button>
            ) : null}
          </div>
        </aside>

        {/* TELEMETRY */}
        <div className="panel telemetry">
          <div className="mission-strip">
            <div className="metric">
              <div className="metric-k">任务</div>
              <div className={cn("metric-v", busy && "is-live")}>
                {statusLabel(job.status)}
              </div>
              <div className="metric-sub">
                {statusLabel(job.status)} · {job.threads || threads} 线程 ·{" "}
                {mailProviderLabel(job.mail_provider || mailProvider)}
              </div>
              <div className="mission-progress">
                <i style={{ width: progressWidth }} />
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">成功</div>
              <div className="metric-v is-ok">{job.success}</div>
              <div className="metric-sub">
                / {job.count || 0} · {successRate}
              </div>
            </div>
            <div className="metric">
              <div className="metric-k">拒绝</div>
              <div className={cn("metric-v", job.denied > 0 && "is-warn")}>{job.denied}</div>
              <div className="metric-sub">风控拒绝</div>
            </div>
            <div className="metric">
              <div className="metric-k">失败</div>
              <div className={cn("metric-v", job.failed > 0 && "is-bad")}>{job.failed}</div>
              <div className="metric-sub">运行异常</div>
            </div>
            <div className="metric">
              <div className="metric-k">进行中</div>
              <div className={cn("metric-v", busy && "is-live is-pulse")}>
                {job.running}
              </div>
              <div className="metric-sub">{elapsed}</div>
            </div>
          </div>

          {job.error ? (
            <div className="job-error" role="alert">
              {job.error}
            </div>
          ) : null}

          <div className="terminal">
            <div className="term-bar">
              <div className="term-bar-title">运行日志</div>
              <div className="term-tools">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={!threadGroups.length}
                  aria-label="全部展开"
                  onClick={expandAllLogs}
                >
                  <UnfoldVertical className="size-3.5" />
                  展开
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={!threadGroups.length}
                  aria-label="全部折叠"
                  onClick={collapseAllLogs}
                >
                  <FoldVertical className="size-3.5" />
                  折叠
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={!logs.length}
                  onClick={() => void handleClearLogs()}
                >
                  <Eraser className="size-3.5" />
                  清空
                </Button>
              </div>
            </div>

            <div className="log-console">
              {threadGroups.length === 0 ? (
                <div className="flex h-full min-h-[120px] items-center justify-center">
                  <Empty
                    className="opacity-80"
                    icon={Terminal}
                    title={
                      loadingConfig
                        ? "连接管理 API…"
                        : apiOnline === false
                          ? "管理 API 离线"
                          : "配置左侧参数后启动"
                    }
                    description={
                      loadingConfig
                        ? "正在读取配置与任务状态。"
                        : apiOnline === false
                          ? "无法连接服务端，保存/启动会失败。恢复后会自动重试。"
                          : "选择邮箱服务与注册数量，点击「开始注册」。主控 / 认证池 / 线程日志会实时出现在此。"
                    }
                  />
                </div>
              ) : (
                <div className="log-thread-list">
                  {threadGroups.map((group) => {
                    const open = openThreads.includes(group.worker);
                    return (
                      <Collapsible
                        key={group.worker}
                        open={open}
                        onOpenChange={(next) =>
                          toggleThread(group.worker, next)
                        }
                        className="log-thread-item"
                        data-state={open ? "open" : "closed"}
                      >
                        <CollapsibleTrigger asChild>
                          <button type="button" className="log-thread-trigger">
                            <ThreadPanelHeader group={group} />
                            <ChevronDown className="log-thread-chevron size-4" />
                          </button>
                        </CollapsibleTrigger>
                        <CollapsibleContent>
                          <div className="log-thread-body">
                            {group.entries.map((entry) => (
                              <LogLineView
                                key={`${group.worker}-${entry.id}`}
                                entry={entry}
                              />
                            ))}
                          </div>
                        </CollapsibleContent>
                      </Collapsible>
                    );
                  })}
                  {/* 单一滚底锚点：避免多线程各挂 ref 时只跟最后一组 */}
                  <div ref={logEndRef} aria-hidden />
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* 邮箱设置 */}
      <Dialog open={mailDialogOpen} onOpenChange={setMailDialogOpen}>
        <DialogContent className="advanced-dialog sm:max-w-2xl" showClose>
          <DialogHeader>
            <DialogTitle>邮箱设置</DialogTitle>
            <DialogDescription>
              按当前邮箱服务（{mailProviderLabel(mailProvider)}）配置接口与域名。
            </DialogDescription>
          </DialogHeader>
          <div className="advanced-content">
            {mailProvider === "cf" ? (
              <div className="provider-block">
                <Field label="API 地址" hint="临时邮箱服务根地址，不含末尾斜杠">
                  <Input
                    value={cfApiBase}
                    disabled={formDisabled}
                    onChange={(event) => setCfApiBase(event.target.value)}
                    placeholder="https://mail.example.com"
                    autoComplete="off"
                  />
                </Field>
                <Field label="API 密钥" hint="管理员模式可选；留空则匿名创建">
                  <Input
                    type="password"
                    autoComplete="off"
                    value={cfApiKey}
                    disabled={formDisabled}
                    onChange={(event) => setCfApiKey(event.target.value)}
                  />
                </Field>
                <Field
                  label="域名模式"
                  hint="多个根域名时：顺序轮询，或随机选取"
                >
                  <Select
                    value={cfDomainMode}
                    onValueChange={(value) =>
                      setCfDomainMode(value === "poll" ? "poll" : "random")
                    }
                    disabled={formDisabled}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="poll">轮询</SelectItem>
                      <SelectItem value="random">随机</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                <Field
                  label="邮箱域名"
                  hint="可添加多个根域名；创建地址时会再套随机子域"
                >
                  <DomainListEditor
                    value={cfDomains}
                    disabled={formDisabled}
                    onChange={setCfDomains}
                    placeholder="mail.example.com"
                  />
                </Field>
              </div>
            ) : (
              <div className="provider-block">
                <Field label="API 地址">
                  <Input
                    value={yydsApiBase}
                    disabled={formDisabled}
                    onChange={(event) => setYydsApiBase(event.target.value)}
                    placeholder="https://maliapi.215.im/v1"
                    autoComplete="off"
                  />
                </Field>
                <Field label="API 密钥" hint="YYDS X-API-Key（AC- 前缀）">
                  <Input
                    type="password"
                    autoComplete="off"
                    value={yydsApiKey}
                    disabled={formDisabled}
                    onChange={(event) => setYydsApiKey(event.target.value)}
                  />
                </Field>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleMailDialogDone}>
              完成
            </Button>
            <Button
              type="button"
              disabled={formDisabled || saving}
              onClick={() => {
                void handleSaveConfig().then(() => setMailDialogOpen(false));
              }}
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={pushDialogOpen} onOpenChange={setPushDialogOpen}>
        <DialogContent className="advanced-dialog sm:max-w-2xl" showClose>
          <DialogHeader>
            <DialogTitle>推送目标设置</DialogTitle>
            <DialogDescription>
              配置 G2A / CPA 推送目标，保存后可在「号池管理 → 推送」中使用。
            </DialogDescription>
          </DialogHeader>
          <div className="advanced-content">
            <div className="provider-block">
              <div className="provider-title">G2A</div>
              <Field label="地址" hint="管理端地址，如 http://127.0.0.1:8765">
                <Input
                  type="text"
                  autoComplete="off"
                  placeholder="http://127.0.0.1:8765"
                  value={g2aBaseUrl}
                  disabled={formDisabled}
                  onChange={(event) => setG2aBaseUrl(event.target.value)}
                />
              </Field>
              <Field label="用户名">
                <Input
                  type="text"
                  autoComplete="off"
                  placeholder="G2A 管理端用户名"
                  value={g2aUsername}
                  disabled={formDisabled}
                  onChange={(event) => setG2aUsername(event.target.value)}
                />
              </Field>
              <Field label="密码">
                <Input
                  type="password"
                  autoComplete="new-password"
                  placeholder="G2A 管理端密码"
                  value={g2aPassword}
                  disabled={formDisabled}
                  onChange={(event) => setG2aPassword(event.target.value)}
                />
              </Field>
            </div>

            <div className="provider-block">
              <div className="provider-title">CPA</div>
              <Field
                label="管理地址"
                hint="如 http://127.0.0.1:8317（Management API 根地址）"
              >
                <Input
                  type="text"
                  autoComplete="off"
                  placeholder="http://127.0.0.1:8317"
                  value={cpaBaseUrl}
                  disabled={formDisabled}
                  onChange={(event) => setCpaBaseUrl(event.target.value)}
                />
              </Field>
              <Field
                label="管理密钥"
                hint="对应 CPA 的 MANAGEMENT_PASSWORD / 管理密钥"
              >
                <Input
                  type="password"
                  autoComplete="new-password"
                  placeholder="CPA 管理密钥"
                  value={cpaManagementKey}
                  disabled={formDisabled}
                  onChange={(event) => setCpaManagementKey(event.target.value)}
                />
              </Field>
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={handlePushDialogDone}>
              完成
            </Button>
            <Button
              type="button"
              disabled={formDisabled || saving}
              onClick={() => {
                void handleSaveConfig().then(() => setPushDialogOpen(false));
              }}
            >
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
