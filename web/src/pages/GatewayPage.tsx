/**
 * 网关运维页（UI 结构对齐 acorn 参考项目）
 * 上：工具栏 + KPI 条
 * 左：网关配置（接入地址 · 鉴权密钥 · Grok 客户端版本号）
 * 右：账号运行态
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  Check,
  CircleHelp,
  Copy,
  KeyRound,
  Link2,
  LoaderCircle,
  Lock,
  RefreshCw,
  Server,
  Shield,
  Users,
  Waypoints,
} from "lucide-react";
import { toast } from "sonner";
import { Badge, Button, Input, Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui";
import {
  ApiError,
  fetchConfig,
  fetchGatewayOps,
  saveConfig,
  type AppConfig,
  type GatewayOpsData,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** 自动刷新间隔 / 手感最短转圈时长 */
const AUTO_REFRESH_MS = 15_000;
const MIN_SPIN_MS = 450;

/** 随机生成 sk- 前缀鉴权密钥（crypto.getRandomValues，48 位十六进制） */
function generateApiKey(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `sk-${hex}`;
}

function fmtUptime(sec: number): string {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${r}s`;
  return `${r}s`;
}

/** 失败率配色档位 */
function failRateTone(rate: number): "is-ok" | "is-warn" | "is-bad" {
  if (rate <= 5) return "is-ok";
  if (rate <= 15) return "is-warn";
  return "is-bad";
}

async function copyText(text: string, okMsg: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(okMsg);
  } catch {
    toast.error("复制失败，请手动选择");
  }
}

/** 小节标签旁的说明问号 */
function LabelHelp({ tip }: { tip: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="gw-label-help"
          aria-label="说明"
          onClick={(e) => e.preventDefault()}
        >
          <CircleHelp className="size-3.5" strokeWidth={1.6} aria-hidden />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs text-[12px] leading-relaxed">
        {tip}
      </TooltipContent>
    </Tooltip>
  );
}

export function GatewayPage() {
  const [data, setData] = useState<GatewayOpsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [savingVersion, setSavingVersion] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [clientVersion, setClientVersion] = useState("");
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const seq = useRef(0);

  /** 拉取聚合运维快照；soft=true 时走静默刷新（保留旧数据 + 最短转圈） */
  const loadOps = useCallback(async (soft = false) => {
    if (soft) setRefreshing(true);
    else setLoading(true);
    const id = ++seq.current;
    const spunAt = Date.now();
    try {
      const next = await fetchGatewayOps();
      if (id !== seq.current) return;
      setData(next);
    } catch (err) {
      if (id !== seq.current) return;
      // 静默刷新失败不打扰；首载才报错
      if (!soft) {
        toast.error(err instanceof ApiError ? err.message : "加载网关运维失败");
      }
    } finally {
      if (id !== seq.current) return;
      const wait = Math.max(0, MIN_SPIN_MS - (Date.now() - spunAt));
      if (wait) await new Promise((r) => setTimeout(r, wait));
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  /** 配置回填：鉴权密钥走 config */
  const loadSettings = useCallback(async () => {
    try {
      const raw: AppConfig = await fetchConfig();
      setApiKey(raw.gateway_api_key || "");
      setClientVersion(raw.grok_version || "1.0.16");
      setSettingsLoaded(true);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "加载网关配置失败");
    }
  }, []);

  useEffect(() => {
    void loadOps(false);
    void loadSettings();
    const t = setInterval(() => void loadOps(true), AUTO_REFRESH_MS);
    return () => clearInterval(t);
  }, [loadOps, loadSettings]);

  /** 重新生成鉴权密钥：随机生成后立即保存，密钥不允许为空 */
  const handleRegenerate = async () => {
    if (regenerating) return;
    setRegenerating(true);
    const key = generateApiKey();
    try {
      const next = await saveConfig({ gateway_api_key: key });
      setApiKey(next.gateway_api_key || key);
      toast.success("新密钥已生成并保存");
      await loadOps(true);
    } catch (err) {
      // 保存失败回滚显示旧值，避免界面与实际生效密钥不一致
      toast.error(err instanceof ApiError ? err.message : "生成失败，已保留原密钥");
    } finally {
      setRegenerating(false);
    }
  };

  const handleSaveClientVersion = async () => {
    if (savingVersion) return;
    const ver = clientVersion.trim();
    if (!ver) {
      toast.error("Grok 客户端版本号不能为空");
      return;
    }
    setSavingVersion(true);
    try {
      const next = await saveConfig({ grok_version: ver });
      setClientVersion(next.grok_version || ver);
      toast.success("Grok 客户端版本号已保存，立即用于上游请求头");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "保存 Grok 客户端版本号失败");
    } finally {
      setSavingVersion(false);
    }
  };

  const handleCopy = async (id: string, text: string, ok: string) => {
    await copyText(text, ok);
    setCopied(id);
    window.setTimeout(() => setCopied(null), 1200);
  };

  const pool = data?.account_pool;
  const stats = data?.stats_24h ?? {};
  const zenStats = stats.zen;
  const grokStats = stats.grok;
  const zenFailRate =
    zenStats && zenStats.requests > 0 ? (zenStats.failed / zenStats.requests) * 100 : 0;
  const grokFailRate =
    grokStats && grokStats.requests > 0 ? (grokStats.failed / grokStats.requests) * 100 : 0;
  const channels = data?.channels ?? [];
  const zenChannel = channels.find((c) => c.id === "zen");
  const grokChannel = channels.find((c) => c.id === "grok");
  const freeModels = data?.config.free_models ?? [];
  const accounts = pool?.accounts ?? [];

  return (
    <div className="gw-page page-stack">
      <div className="panel gw-workspace">
        {/* ── 工具栏 ── */}
        <div className="usage-toolbar">
          <div className="usage-toolbar-title">
            <span className="usage-eyebrow">
              <em>//</em> Gateway
            </span>
            <h1 className="usage-title">网关运维</h1>
            <p className="usage-subtitle">
              配置 · 号池运行态
              {data ? (
                <span className="gw-uptime">运行 {fmtUptime(data.uptime_sec)}</span>
              ) : null}
            </p>
          </div>

          <div className="usage-toolbar-actions">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={refreshing || loading}
              onClick={() => void loadOps(true)}
              title="刷新运维快照（另有 15s 自动刷新）"
              aria-busy={refreshing}
              className={cn("usage-refresh-btn", refreshing && "is-refreshing")}
            >
              <RefreshCw
                className={cn("usage-refresh-ico size-3.5", refreshing && "animate-spin")}
                strokeWidth={1.6}
                aria-hidden
              />
              <span>{refreshing ? "刷新中" : "刷新"}</span>
            </Button>
          </div>
        </div>

        {loading && !data ? (
          <div className="gw-skeleton" aria-hidden />
        ) : (
          <>
            {/* ── KPI 条 ── */}
            <div className="mission-strip mission-strip-gw" role="group" aria-label="网关 KPI">
              <div className="metric">
                <div className="metric-k">
                  <Users className="metric-ico" strokeWidth={1.6} />
                  账号池
                </div>
                <div className="metric-v is-live">{pool?.active ?? 0}</div>
                <div className="metric-sub usage-metric-sub">
                  共 {pool?.total ?? 0} · 在用 {pool?.in_use ?? 0}
                </div>
              </div>
              <div className="metric">
                <div className="metric-k">
                  <Waypoints className="metric-ico" strokeWidth={1.6} />
                  免费模型
                </div>
                <div className="metric-v">{freeModels.length}</div>
                <div className="metric-sub usage-metric-sub">本地清单 · 热更新</div>
              </div>
              <div className="metric">
                <div className="metric-k">
                  <Shield className="metric-ico" strokeWidth={1.6} />
                  Zen · 24h
                </div>
                <div className="metric-v">{zenStats?.requests ?? 0}</div>
                <div className="metric-sub usage-metric-sub">
                  失败 {zenStats?.failed ?? 0} ·{" "}
                  <span className={cn("gw-fail-rate", failRateTone(zenFailRate))}>
                    失败率 {zenFailRate.toFixed(1)}%
                  </span>
                </div>
              </div>
              <div className="metric">
                <div className="metric-k">
                  <Activity className="metric-ico" strokeWidth={1.6} />
                  Grok · 24h
                </div>
                <div className="metric-v">{grokStats?.requests ?? 0}</div>
                <div className="metric-sub usage-metric-sub">
                  失败 {grokStats?.failed ?? 0} ·{" "}
                  <span className={cn("gw-fail-rate", failRateTone(grokFailRate))}>
                    失败率 {grokFailRate.toFixed(1)}%
                  </span>
                </div>
              </div>
              <div className="metric">
                <div className="metric-k">
                  <Lock className="metric-ico" strokeWidth={1.6} />
                  鉴权
                </div>
                <div
                  className={cn(
                    "metric-v",
                    data?.config.auth_enabled ? "is-ok" : "is-warn",
                  )}
                >
                  {data?.config.auth_enabled ? "ON" : "OFF"}
                </div>
                <div className="metric-sub usage-metric-sub">
                  {data?.config.auth_enabled
                    ? "密钥校验已启用"
                    : "未配置密钥，不鉴权"}
                </div>
              </div>
            </div>

            <div className="gw-grid gw-grid-split">
              {/* ── 左：网关配置 ── */}
              <section className="gw-panel gw-col">
                <div className="gw-panel-head">
                  <Server className="size-3.5" strokeWidth={1.6} aria-hidden />
                  <h2 className="gw-panel-title">网关配置</h2>
                </div>

                {/* 接入地址 — 固定只读可复制 */}
                <div className="gw-section">
                  <div className="gw-section-label">
                    <Link2 className="size-3" strokeWidth={1.6} aria-hidden />
                    <span>接入地址</span>
                    <LabelHelp tip="Claude Code 将 ANTHROPIC_BASE_URL 设为 http://127.0.0.1:8787/zen（不带 /v1）；Grok 客户端用完整 /grok/v1 地址，网关自动从号池取号，优先新注册与未降智账号。" />
                  </div>
                  {[zenChannel, grokChannel].map((channel, index) => (
                    <div
                      key={channel?.id ?? index}
                      className={cn("gw-baseurl", index > 0 && "gw-baseurl-gap")}
                      aria-label={`${channel?.title ?? ""}接入地址`}
                    >
                      <span className="gw-baseurl-tag">{channel?.title}</span>
                      <code className="gw-baseurl-value">
                        {channel?.client_base ??
                          (channel?.id === "grok"
                            ? "http://127.0.0.1:8787/grok/v1"
                            : "http://127.0.0.1:8787/zen/v1")}
                      </code>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="gw-baseurl-copy"
                        title="复制"
                        onClick={() =>
                          void handleCopy(
                            channel?.id ?? String(index),
                            channel?.client_base ?? "",
                            `已复制 ${channel?.title ?? ""}地址`,
                          )
                        }
                      >
                        {copied === channel?.id ? (
                          <Check className="size-3.5" strokeWidth={1.6} />
                        ) : (
                          <Copy className="size-3.5" strokeWidth={1.6} />
                        )}
                        <span>{copied === channel?.id ? "已复制" : "复制"}</span>
                      </Button>
                    </div>
                  ))}
                </div>

                {/* 鉴权密钥 */}
                <div className="gw-section">
                  <div className="gw-editor-head">
                    <div className="gw-editor-title">
                      <KeyRound className="size-3.5" strokeWidth={1.6} aria-hidden />
                      <span>鉴权密钥</span>
                      <LabelHelp tip="客户端必须以 Authorization: Bearer <key> 或 x-api-key 携带密钥。点击「重新生成」随机更换并立即保存生效；密钥不可为空。" />
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={regenerating || !settingsLoaded}
                      onClick={() => void handleRegenerate()}
                      title="随机生成 sk- 前缀密钥，自动保存并立即生效"
                      aria-busy={regenerating}
                    >
                      {regenerating ? (
                        <LoaderCircle className="size-3.5 animate-spin" strokeWidth={1.6} />
                      ) : (
                        <RefreshCw className="size-3.5" strokeWidth={1.6} />
                      )}
                      重新生成
                    </Button>
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      className="gw-editor-input font-mono"
                      value={apiKey}
                      readOnly
                      placeholder="sk-…"
                      aria-label="鉴权密钥"
                      spellCheck={false}
                      autoComplete="off"
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      aria-label="复制密钥"
                      title="复制密钥"
                      disabled={!apiKey.trim()}
                      onClick={() =>
                        void handleCopy("api-key", apiKey.trim(), "已复制鉴权密钥")
                      }
                    >
                      {copied === "api-key" ? (
                        <Check className="size-3.5" strokeWidth={1.6} />
                      ) : (
                        <Copy className="size-3.5" strokeWidth={1.6} />
                      )}
                    </Button>
                  </div>
                </div>

                <div className="gw-section">
                  <div className="gw-editor-head">
                    <div className="gw-editor-title">
                      <Server className="size-3.5" strokeWidth={1.6} aria-hidden />
                      <span>Grok客户端版本号</span>
                      <LabelHelp
                        tip={
                          <>
                            写入 config.json 的 grok_version，打上游时填
                            x-grok-client-version 与 User-Agent（xai-grok-workspace/版本）。
                            对齐 grok-build，Authorization 仍用号池 token。
                          </>
                        }
                      />
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={savingVersion || !settingsLoaded}
                      onClick={() => void handleSaveClientVersion()}
                      title="保存到 config.json 并立即用于上游请求头"
                      aria-busy={savingVersion}
                    >
                      {savingVersion ? (
                        <LoaderCircle className="size-3.5 animate-spin" strokeWidth={1.6} />
                      ) : null}
                      保存
                    </Button>
                  </div>
                  <Input
                    className="gw-editor-input font-mono"
                    value={clientVersion}
                    onChange={(e) => setClientVersion(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        void handleSaveClientVersion();
                      }
                    }}
                    placeholder="1.0.16"
                    aria-label="Grok客户端版本号"
                    spellCheck={false}
                    autoComplete="off"
                    disabled={!settingsLoaded}
                  />
                </div>

              </section>

              {/* ── 右：账号运行态 ── */}
              <section className="gw-panel gw-col">
                <div className="gw-panel-head">
                  <Users className="size-3.5" strokeWidth={1.6} aria-hidden />
                  <h2 className="gw-panel-title">账号运行态</h2>
                </div>

                <div className="gw-pool-summary">
                  <div className="gw-pool-chip is-idle">
                    <em>{pool?.active ?? 0}</em>
                    <span>正常</span>
                  </div>
                  <div className="gw-pool-chip is-busy">
                    <em>{pool?.in_use ?? 0}</em>
                    <span>在用</span>
                  </div>
                  <div className="gw-pool-chip is-sticky">
                    <em>{pool?.sticky ?? 0}</em>
                    <span>粘性</span>
                  </div>
                  <div className="gw-pool-chip is-cool">
                    <em>{pool?.cooling ?? 0}</em>
                    <span>冷却</span>
                  </div>
                </div>

                {accounts.length === 0 ? (
                  <div className="gw-empty">
                    号池暂无账号
                    <span className="gw-empty-sub">先到「账号注册」页批量注册并认证</span>
                  </div>
                ) : (
                  <ul className="gw-acct-list" aria-label="号池账号列表">
                    {accounts.map((a) => (
                      <li
                        key={a.id}
                        className={cn(
                          "gw-acct-row",
                          a.disabled
                            ? "is-strikes"
                            : a.cooling
                              ? "is-cool"
                              : a.dumbed
                                ? "is-dumbed"
                                : a.sticky
                                  ? "is-sticky"
                                  : a.authed
                                    ? "is-idle"
                                    : "is-cooling",
                        )}
                      >
                        <span className="gw-acct-cell gw-acct-cell-id">
                          #{a.id}
                        </span>
                        <span
                          className="gw-acct-cell gw-acct-cell-email"
                          title={a.email}
                        >
                          {a.email || "—"}
                        </span>
                        <span
                          className="gw-acct-cell gw-acct-cell-usage"
                          title="近 24h 请求数"
                        >
                          {a.requests_24h > 0 ? (
                            <Badge variant="secondary" className="gw-usage-badge">
                              24h×{a.requests_24h}
                            </Badge>
                          ) : (
                            "—"
                          )}
                        </span>
                        <span
                          className={cn(
                            "gw-acct-state",
                            a.disabled
                              ? "is-strikes"
                              : a.cooling
                                ? "is-cool"
                                : a.dumbed
                                  ? "is-dumbed"
                                  : a.sticky
                                    ? "is-sticky"
                                    : a.authed
                                      ? "is-idle"
                                      : "is-cooling",
                          )}
                        >
                          {a.disabled
                            ? "禁用"
                            : a.cooling
                              ? "冷却"
                              : a.dumbed
                                ? "降智"
                                : a.sticky
                                  ? "粘性"
                                  : a.authed
                                    ? "正常"
                                    : "未认证"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
