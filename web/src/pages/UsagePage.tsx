/**
 * 用量统计：窗口 KPI / 趋势 / 模型分布 + 最近明细分页。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, Brain, Cpu, Database, Gauge, RefreshCw, Zap } from "lucide-react";
import { toast } from "sonner";
import {
  ApiError,
  fetchUsageGrouped,
  fetchUsageRecent,
  fetchUsageSummary,
  type UsageGroupDim,
  type UsageGroupedRow,
  type UsageRow,
  type UsageSummaryData,
} from "@/lib/api";
import { Button, Pagination, ToggleGroup, ToggleGroupItem } from "@/components/ui";
import { cn } from "@/lib/utils";
import { usePageCache } from "@/lib/page-cache";
import {
  GroupedDetailTable,
  LoadingSkeleton,
  ModelBars,
  RequestDetailTable,
  UsageTrendChart,
  fmtCompact,
  fmtInt,
  fmtPct,
  toTrendPoints,
} from "@/components/usage";

const RANGE_OPTIONS = [
  { value: "1", label: "今日" },
  { value: "7", label: "7 天" },
  { value: "14", label: "14 天" },
  { value: "30", label: "30 天" },
] as const;

const DETAIL_PAGE_SIZE_OPTIONS = [10, 20, 50] as const;
const DETAIL_DEFAULT_PAGE_SIZE = 20;
const AUTO_REFRESH_MS = 30_000;
const MIN_SPIN_MS = 450;

type DetailDim = "requests" | "account" | "model";

export function UsagePage() {
  const [days, setDays] = usePageCache("usage.days", () => "1");
  const [data, setData] = useState<UsageSummaryData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [chartMetric, setChartMetric] = usePageCache<"requests" | "tokens">(
    "usage.chartMetric",
    () => "requests",
  );
  const [detailPage, setDetailPage] = usePageCache("usage.detailPage", () => 1);
  const [detailPageSize, setDetailPageSize] = usePageCache(
    "usage.detailPageSize",
    () => DETAIL_DEFAULT_PAGE_SIZE,
  );
  const [recent, setRecent] = useState<UsageRow[]>([]);
  const [recentTotal, setRecentTotal] = useState(0);
  const [grouped, setGrouped] = useState<Record<UsageGroupDim, UsageGroupedRow[]>>({
    account: [],
    model: [],
  });
  const [groupTotal, setGroupTotal] = useState(0);
  const [groupPage, setGroupPage] = usePageCache("usage.groupPage", () => 1);
  const [groupPageSize, setGroupPageSize] = usePageCache(
    "usage.groupPageSize",
    () => DETAIL_DEFAULT_PAGE_SIZE,
  );
  const [detailDim, setDetailDim] = usePageCache<DetailDim>("usage.detailDim", () => "requests");
  const dimRef = useRef<DetailDim>(detailDim);
  dimRef.current = detailDim;

  const reqSeq = useRef(0);
  const detailSeq = useRef(0);
  const groupSeq = useRef(0);
  const pageRef = useRef(detailPage);
  const sizeRef = useRef(detailPageSize);
  pageRef.current = detailPage;
  sizeRef.current = detailPageSize;
  const groupPageRef = useRef(groupPage);
  const groupSizeRef = useRef(groupPageSize);
  groupPageRef.current = groupPage;
  groupSizeRef.current = groupPageSize;

  // 加载账号/模型分组聚合（分页）：只拉当前维度那一页，避免全量下发
  const loadGrouped = useCallback(
    async (page: number, size: number, silent = false) => {
      const seq = ++groupSeq.current;
      const dim = dimRef.current;
      if (dim === "requests") return;
      try {
        const next = await fetchUsageGrouped(dim, (page - 1) * size, size);
        if (seq !== groupSeq.current) return;
        setGrouped((prev) => ({ ...prev, [dim]: next.items }));
        setGroupTotal(next.total);
        const totalPages = Math.max(1, Math.ceil(next.total / Math.max(1, size)));
        if (page > totalPages) {
          setGroupPage(totalPages);
          return;
        }
      } catch (err) {
        if (seq !== groupSeq.current) return;
        if (!silent) {
          toast.error(err instanceof ApiError ? err.message : "加载用量分组失败");
        }
      }
    },
    [setGroupPage],
  );

  const load = useCallback(
    async (soft = false) => {
      if (soft) setRefreshing(true);
      else setLoading(true);
      const seq = ++reqSeq.current;
      const spunAt = Date.now();
      try {
        const next = await fetchUsageSummary(Number(days));
        if (seq !== reqSeq.current) return;
        setData(next);
      } catch (err) {
        if (seq !== reqSeq.current) return;
        if (!soft) {
          toast.error(err instanceof ApiError ? err.message : "加载用量统计失败");
        }
      } finally {
        if (seq !== reqSeq.current) return;
        if (soft) {
          const left = MIN_SPIN_MS - (Date.now() - spunAt);
          if (left > 0) await new Promise((r) => window.setTimeout(r, left));
          if (seq !== reqSeq.current) return;
        }
        setLoading(false);
        setRefreshing(false);
      }
    },
    [days],
  );

  const loadDetail = useCallback(async (page: number, size: number, silent = false) => {
    const seq = ++detailSeq.current;
    try {
      const next = await fetchUsageRecent((page - 1) * size, size);
      if (seq !== detailSeq.current) return;
      const totalPages = Math.max(1, Math.ceil(next.total / Math.max(1, size)));
      if (page > totalPages) {
        setDetailPage(totalPages);
        return;
      }
      setRecent(next.items);
      setRecentTotal(next.total);
    } catch (err) {
      if (seq !== detailSeq.current) return;
      if (!silent) {
        toast.error(err instanceof ApiError ? err.message : "加载用量明细失败");
      }
    }
  }, [setDetailPage]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void load(true);
      if (dimRef.current === "requests") {
        void loadDetail(pageRef.current, sizeRef.current, true);
      } else {
        void loadGrouped(groupPageRef.current, groupSizeRef.current, true);
      }
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load, loadDetail, loadGrouped]);

  // 明细维度切换：请求→加载逐条分页；账号/模型→加载分组分页
  useEffect(() => {
    if (detailDim === "requests") {
      void loadDetail(detailPage, detailPageSize);
    } else {
      void loadGrouped(groupPage, groupPageSize);
    }
  }, [detailDim, detailPage, detailPageSize, groupPage, groupPageSize, loadDetail, loadGrouped]);

  const summary = data?.summary;

  const trendPoints = useMemo(
    () => toTrendPoints(days === "1" ? (data?.by_hour ?? []) : (data?.by_day ?? [])),
    [data, days],
  );

  const peak = useMemo(() => {
    if (!trendPoints.length) return null;
    let best = trendPoints[0]!;
    for (const p of trendPoints) {
      if (p.requests > best.requests) best = p;
    }
    return best.requests > 0 ? best : null;
  }, [trendPoints]);

  const detailTotalPages = Math.max(1, Math.ceil(recentTotal / Math.max(1, detailPageSize)));
  const detailPageSafe = Math.min(Math.max(1, detailPage), detailTotalPages);

  useEffect(() => {
    if (detailPage !== detailPageSafe) setDetailPage(detailPageSafe);
  }, [detailPage, detailPageSafe, setDetailPage]);

  const groupTotalPages = Math.max(1, Math.ceil(groupTotal / Math.max(1, groupPageSize)));
  const groupPageSafe = Math.min(Math.max(1, groupPage), groupTotalPages);

  useEffect(() => {
    if (groupPage !== groupPageSafe) setGroupPage(groupPageSafe);
  }, [groupPage, groupPageSafe, setGroupPage]);

  if (loading && !data) return <LoadingSkeleton />;

  return (
    <div className="usage-page page-stack">
      <div className="panel usage-workspace">
        <div className="usage-toolbar">
          <div className="usage-toolbar-title">
            <span className="usage-eyebrow">
              <em>//</em> Telemetry
            </span>
            <h1 className="usage-title">用量统计</h1>
            <p className="usage-subtitle">网关请求 · Token · 模型 · 明细</p>
          </div>

          <div className="usage-toolbar-actions">
            <ToggleGroup
              type="single"
              value={days}
              onValueChange={(v) => {
                if (v) setDays(v);
              }}
              className="usage-range"
              aria-label="时间范围"
            >
              {RANGE_OPTIONS.map((o) => (
                <ToggleGroupItem key={o.value} value={o.value} className="usage-range-item" aria-label={o.label}>
                  {o.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={refreshing}
              onClick={() => {
                void load(true);
                if (dimRef.current === "requests") {
                  void loadDetail(detailPage, detailPageSize);
                } else {
                  void loadGrouped(groupPage, groupPageSize);
                }
              }}
              title="立即刷新（另有 30s 自动刷新）"
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

        <div className="mission-strip mission-strip-usage" role="group" aria-label="用量 KPI">
          <div className="metric">
            <div className="metric-k">
              <Activity className="metric-ico" strokeWidth={1.6} />
              请求数
            </div>
            <div className="metric-v">{fmtCompact(summary?.requests)}</div>
            <div className="metric-sub usage-metric-sub">
              流式 {fmtCompact(summary?.stream_count)} · 非流式{" "}
              {fmtCompact(Math.max(0, (summary?.requests ?? 0) - (summary?.stream_count ?? 0)))}
            </div>
          </div>
          <div className="metric">
            <div className="metric-k">
              <Zap className="metric-ico" strokeWidth={1.6} />
              总 Token
            </div>
            <div className="metric-v is-live">{fmtCompact(summary?.total_tokens)}</div>
            <div className="metric-sub usage-metric-sub">
              输入 {fmtCompact(summary?.prompt_tokens)} · 输出 {fmtCompact(summary?.completion_tokens)}
            </div>
          </div>
          <div className="metric">
            <div className="metric-k">
              <Database className="metric-ico" strokeWidth={1.6} />
              缓存 Token
            </div>
            <div className="metric-v is-ok">{fmtCompact(summary?.cache_tokens)}</div>
            <div className="metric-sub usage-metric-sub">
              <span
                className={cn(
                  "usage-cache-hit",
                  (summary?.cache_hit_rate ?? 0) >= 85
                    ? "is-ok"
                    : (summary?.cache_hit_rate ?? 0) >= 60
                      ? "is-warn"
                      : "is-bad",
                )}
              >
                命中 {fmtPct(summary?.cache_hit_rate, 1)}
              </span>
            </div>
          </div>
          <div className="metric">
            <div className="metric-k">
              <Brain className="metric-ico" strokeWidth={1.6} />
              推理 Token
            </div>
            <div
              className={cn(
                "metric-v",
                (summary?.reasoning_share ?? 0) > 60
                  ? "is-warn"
                  : (summary?.reasoning_share ?? 0) > 30
                    ? "is-live"
                    : undefined,
              )}
            >
              {fmtCompact(summary?.reasoning_tokens)}
            </div>
            <div className="metric-sub usage-metric-sub">
              占比 {fmtPct(summary?.reasoning_share, 1)}
            </div>
          </div>
          <div className="metric">
            <div className="metric-k">
              <Gauge className="metric-ico" strokeWidth={1.6} />
              成功率
            </div>
            <div
              className={cn(
                "metric-v",
                (summary?.success_rate ?? 0) >= 95
                  ? "is-ok"
                  : (summary?.success_rate ?? 0) >= 85
                    ? "is-warn"
                    : "is-bad",
              )}
            >
              {fmtPct(summary?.success_rate, 1)}
            </div>
            <div className="metric-sub usage-metric-sub">
              成功 {fmtCompact(summary?.success)} · 失败 {fmtCompact(summary?.failed)}
            </div>
          </div>
        </div>

        <div className="usage-grid">
          <section className="usage-panel usage-panel-chart">
            <div className="usage-panel-head">
              <div>
                <h2 className="usage-panel-title">
                  {chartMetric === "requests" ? "请求趋势" : "Token 趋势"}
                </h2>
              </div>
              <ToggleGroup
                type="single"
                value={chartMetric}
                onValueChange={(v) => {
                  if (v === "requests" || v === "tokens") setChartMetric(v);
                }}
                className="usage-metric-toggle"
                aria-label="趋势指标"
              >
                <ToggleGroupItem value="requests" className="usage-range-item">
                  请求
                </ToggleGroupItem>
                <ToggleGroupItem value="tokens" className="usage-range-item">
                  Token
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
            {trendPoints.length ? (
              <UsageTrendChart points={trendPoints} metric={chartMetric} />
            ) : (
              <div className="usage-empty">暂无时序数据</div>
            )}
            {peak ? (
              <div className="usage-peak">
                <Cpu className="size-3.5" strokeWidth={1.6} />
                峰值 <b>{peak.label}</b>
                <span>·</span>
                请求 <b>{fmtInt(peak.requests)}</b>
                <span>·</span>
                Token <b>{fmtCompact(peak.tokens)}</b>
              </div>
            ) : null}
          </section>

          <section className="usage-panel">
            <div className="usage-panel-head">
              <div>
                <h2 className="usage-panel-title">模型分布</h2>
              </div>
            </div>
            {data?.by_model?.length ? (
              <ModelBars models={data.by_model} />
            ) : (
              <div className="usage-empty">暂无模型数据</div>
            )}
          </section>

          <section className="usage-panel usage-panel-table">
            <div className="usage-panel-head usage-panel-head-detail">
              <div>
                <h2 className="usage-panel-title">用量明细</h2>
              </div>
              <ToggleGroup
                type="single"
                value={detailDim}
                onValueChange={(v) => {
                  if (v === "requests" || v === "account" || v === "model") {
                    // 切换维度时分组回到第一页（在加载前重置，避免按旧页码多发一次请求）
                    if (v !== "requests") setGroupPage(1);
                    setDetailDim(v);
                  }
                }}
                className="usage-metric-toggle"
                aria-label="明细维度"
              >
                <ToggleGroupItem value="requests" className="usage-range-item">
                  请求
                </ToggleGroupItem>
                <ToggleGroupItem value="account" className="usage-range-item">
                  账号
                </ToggleGroupItem>
                <ToggleGroupItem value="model" className="usage-range-item">
                  模型
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
            {detailDim === "requests" ? (
              <RequestDetailTable rows={recent} />
            ) : (
              <GroupedDetailTable rows={grouped[detailDim]} dimension={detailDim} />
            )}
            <div className="usage-detail-foot">
              {detailDim === "requests" ? (
                <Pagination
                  page={detailPageSafe}
                  pageSize={detailPageSize}
                  total={recentTotal}
                  pageSizeOptions={DETAIL_PAGE_SIZE_OPTIONS}
                  onPageChange={setDetailPage}
                  onPageSizeChange={setDetailPageSize}
                />
              ) : (
                <Pagination
                  page={groupPageSafe}
                  pageSize={groupPageSize}
                  total={groupTotal}
                  pageSizeOptions={DETAIL_PAGE_SIZE_OPTIONS}
                  onPageChange={setGroupPage}
                  onPageSizeChange={setGroupPageSize}
                />
              )}
            </div>
          </section>
        </div>

        <div className="usage-foot">
          <span className="font-mono text-[11px] text-muted-foreground">
            本机落库网关统计 · auto 30s
          </span>
        </div>
      </div>
    </div>
  );
}
