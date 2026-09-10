import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import type {
  UsageByDay,
  UsageByHour,
  UsageByModel,
  UsageGroupDim,
  UsageGroupedRow,
  UsageRow,
} from "@/lib/api";
import {
  Badge,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui";
import { cn, formatAccountTime } from "@/lib/utils";

export function fmtCompact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) {
    const v = abs / 1_000_000;
    return `${sign}${abs % 1_000_000 === 0 ? String(v) : v.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (abs >= 1_000) {
    const v = abs / 1_000;
    return `${sign}${abs % 1_000 === 0 ? String(v) : v.toFixed(1).replace(/\.0$/, "")}K`;
  }
  return `${sign}${Math.round(abs)}`;
}

export function fmtInt(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

/** 可见输出吞吐：token/s；0 / 缺失显示 — */
export function fmtTps(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return "—";
  return n >= 100 ? `${Math.round(n)}/s` : `${n.toFixed(1)}/s`;
}

export function shortClientUa(ua: string | null | undefined): string {
  const t = (ua || "").trim();
  if (!t) return "—";
  if (/grok-shell|grok-pager/i.test(t)) return "grok CLI";
  if (/claude/i.test(t)) return "Claude Code";
  if (/opencode/i.test(t)) return "OpenCode";
  if (/Edg\//.test(t)) return "Edge";
  if (/Chrome\/[\d.]+/.test(t) && !/Edg\//.test(t)) return "Chrome";
  if (/Firefox\//.test(t)) return "Firefox";
  if (/Safari\//.test(t) && /Version\//.test(t)) return "Safari";
  if (/OpenAI\/Python/i.test(t)) return "OpenAI SDK";
  if (/curl\//i.test(t)) return "curl";
  if (/okhttp/i.test(t)) return "OkHttp";
  return t.length > 18 ? `${t.slice(0, 16)}…` : t;
}

export function shortEndpoint(path: string | undefined): string {
  const p = (path || "").trim();
  if (!p) return "—";
  if (p.includes("/chat/completions")) return "/chat/completions";
  if (p.includes("/messages")) return "/v1/messages";
  if (p.includes("/responses")) return "/v1/responses";
  return p.length > 18 ? `${p.slice(0, 16)}…` : p;
}

export interface TrendPoint {
  label: string;
  requests: number;
  tokens: number;
  errors: number;
}

export function UsageTrendChart({
  points,
  metric,
}: {
  points: TrendPoint[];
  metric: "requests" | "tokens";
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [width, setWidth] = useState(640);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && Number.isFinite(w)) setWidth(Math.max(280, Math.floor(w)));
    });
    ro.observe(el);
    setWidth(Math.max(280, Math.floor(el.clientWidth || 640)));
    return () => ro.disconnect();
  }, []);

  const height = 220;
  const pad = { top: 16, right: 12, bottom: 28, left: 44 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const values = points.map((p) => (metric === "requests" ? p.requests : p.tokens));
  const maxV = Math.max(1, ...values);

  const pts = points.map((p, i) => {
    const x =
      points.length <= 1 ? pad.left + innerW / 2 : pad.left + (i / (points.length - 1)) * innerW;
    const raw = metric === "requests" ? p.requests : p.tokens;
    const y = pad.top + innerH - (raw / maxV) * innerH;
    return { x, y, p, raw };
  });

  const linePath = pts
    .map((pt, i) => `${i === 0 ? "M" : "L"}${pt.x.toFixed(1)},${pt.y.toFixed(1)}`)
    .join(" ");
  const singlePoint = pts.length === 1 ? pts[0] : null;
  const areaPath =
    pts.length > 0
      ? [
          `M${pts[0]!.x.toFixed(1)},${(pad.top + innerH).toFixed(1)}`,
          ...pts.map((pt) => `L${pt.x.toFixed(1)},${pt.y.toFixed(1)}`),
          `L${pts[pts.length - 1]!.x.toFixed(1)},${(pad.top + innerH).toFixed(1)}`,
          "Z",
        ].join(" ")
      : "";

  const yTicks = [0, 0.5, 1].map((t) => {
    const v = maxV * t;
    const label =
      maxV < 5 ? String(Math.round(v * 10) / 10) : fmtCompact(Math.round(v));
    return { y: pad.top + innerH * (1 - t), label };
  });
  // 今日（小时粒度）：label 形如 "HH:00"，X 轴标注整点（每 4 小时）；
  // 多日（天粒度）：等距标注至多 8 个
  const isHourGranularity = points.some((p) => /^\d{2}:00$/.test(p.label));
  let labelIdx: Set<number>;
  if (isHourGranularity) {
    labelIdx = new Set(
      points
        .map((p, i) => ({ i, l: p.label }))
        .filter(({ l }) => {
          const m = /^(\d{2}):00$/.exec(l);
          return !!m && Number(m[1]) % 4 === 0;
        })
        .map(({ i }) => i),
    );
    if (!labelIdx.size) labelIdx = new Set([points.length - 1]);
  } else {
    const labelCount = Math.min(points.length, 8);
    labelIdx = new Set(
      labelCount <= 1
        ? [points.length - 1]
        : Array.from({ length: labelCount }, (_, i) =>
            Math.round((i * (points.length - 1)) / (labelCount - 1)),
          ),
    );
  }

  const onMove = (e: ReactMouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    if (!pts.length) return;
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const d = Math.abs(pts[i]!.x - x);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    }
    setHover(best);
  };

  const hp = hover != null ? pts[hover] : null;

  return (
    <div ref={wrapRef} className="usage-chart">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="usage-chart-svg"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={metric === "requests" ? "请求趋势" : "Token 趋势"}
      >
        <defs>
          <linearGradient id="usage-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--primary)" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {yTicks.map((tick) => (
          <g key={`${tick.label}-${tick.y}`}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={tick.y}
              y2={tick.y}
              className="usage-chart-grid"
            />
            <text x={pad.left - 8} y={tick.y + 3} textAnchor="end" className="usage-chart-axis">
              {tick.label}
            </text>
          </g>
        ))}
        {points.map((p, i) =>
          labelIdx.has(i) ? (
            <text
              key={`${p.label}-${i}`}
              x={pts[i]!.x}
              y={height - 8}
              textAnchor="middle"
              className="usage-chart-axis"
            >
              {p.label}
            </text>
          ) : null,
        )}
        {areaPath ? <path d={areaPath} fill="url(#usage-area)" /> : null}
        {linePath ? (
          <path d={linePath} fill="none" className="usage-chart-line" strokeWidth={1.75} />
        ) : null}
        {singlePoint ? (
          <circle cx={singlePoint.x} cy={singlePoint.y} r={4} className="usage-chart-dot" />
        ) : null}
        {hp ? (
          <>
            <line x1={hp.x} x2={hp.x} y1={pad.top} y2={pad.top + innerH} className="usage-chart-cross" />
            <circle cx={hp.x} cy={hp.y} r={4} className="usage-chart-dot" />
          </>
        ) : null}
      </svg>
      {hp ? (
        <div className="usage-chart-tip" style={{ left: Math.min(Math.max(hp.x, 72), width - 72) }}>
          <div className="usage-chart-tip-t">{hp.p.label}</div>
          <div className="usage-chart-tip-row">
            <span>请求</span>
            <b>{fmtInt(hp.p.requests)}</b>
          </div>
          <div className="usage-chart-tip-row">
            <span>Token</span>
            <b>{fmtCompact(hp.p.tokens)}</b>
          </div>
          <div className="usage-chart-tip-row">
            <span>错误</span>
            <b className={hp.p.errors > 0 ? "is-bad" : undefined}>{fmtInt(hp.p.errors)}</b>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function ModelBars({ models }: { models: UsageByModel[] }) {
  const maxReq = Math.max(1, ...models.map((m) => m.requests));
  return (
    <ul className="usage-model-list">
      {models.map((m) => {
        const pct = Math.round((m.requests / maxReq) * 1000) / 10;
        const rate = m.requests > 0 ? (m.success / m.requests) * 100 : 100;
        return (
          <li key={m.model} className="usage-model-row">
            <div className="usage-model-head">
              <span className="usage-model-name">{m.model || "—"}</span>
              <span className="usage-model-meta">
                <b>{fmtCompact(m.requests)}</b>
                <em>{fmtPct(pct, 0)}</em>
              </span>
            </div>
            <div className="usage-model-track">
              <i style={{ width: `${pct}%` }} />
            </div>
            <div className="usage-model-foot">
              <span>Token {fmtCompact(m.total_tokens)}</span>
              <span>推理 Token {fmtCompact(m.reasoning_tokens)}</span>
              <span className="is-ok">{fmtPct(rate, 0)} ok</span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function CacheCell({
  cacheTokens,
  promptTokens,
}: {
  cacheTokens: number;
  promptTokens: number;
}) {
  const has = (cacheTokens || 0) > 0;
  const rate = promptTokens > 0 ? (Math.min(cacheTokens, promptTokens) / promptTokens) * 100 : 0;
  const tone = !has ? "is-miss" : rate >= 70 ? "is-high" : rate >= 30 ? "is-mid" : "is-low";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          className={cn("usage-cache-cell", tone)}
          tabIndex={0}
          aria-label={
            has
              ? `缓存命中 ${fmtPct(rate, 1)}，${fmtInt(cacheTokens)} / ${fmtInt(promptTokens)}`
              : "无缓存命中"
          }
        >
          <div className="usage-cache-track" aria-hidden>
            <i style={{ width: `${has ? rate : 0}%` }} />
          </div>
        </div>
      </TooltipTrigger>
      <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
        {has ? (
          <>
            命中 {fmtPct(rate, 1)}
            <span className="mt-1 block">
              缓存 {fmtInt(cacheTokens)} / 输入 {fmtInt(promptTokens)}
            </span>
          </>
        ) : (
          "无缓存命中"
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** 推理档位徽标配色：max/primary > high·xhigh/signal > medium > low/muted */
function effortTone(effort: string | null): string {
  const level = (effort || "").toLowerCase();
  if (level === "max") return "is-max";
  if (level === "high" || level === "xhigh") return "is-high";
  if (level === "medium") return "is-medium";
  if (level === "low") return "is-low";
  return "";
}

export function RequestDetailTable({ rows }: { rows: UsageRow[] }) {
  if (!rows.length) return <div className="usage-empty">暂无请求明细</div>;
  return (
    <div className="usage-table-wrap">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>时间</TableHead>
            <TableHead>客户端</TableHead>
            <TableHead>出口 IP</TableHead>
            <TableHead>模型</TableHead>
            <TableHead>端点</TableHead>
            <TableHead>流式</TableHead>
            <TableHead>推理等级</TableHead>
            <TableHead className="text-right">Token</TableHead>
            <TableHead className="text-right">吞吐</TableHead>
            <TableHead>结果</TableHead>
            <TableHead className="usage-col-reason">原因</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.id}>
              <TableCell className="font-mono text-[11px] text-muted-foreground tabular-nums">
                {formatAccountTime(row.created_at)}
              </TableCell>
              <TableCell>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="usage-client-ua font-mono text-[11px]">
                      {shortClientUa(row.client_ua)}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-sm break-all font-mono text-[11px]">
                    {row.client_ua || "—"}
                  </TooltipContent>
                </Tooltip>
              </TableCell>
              <TableCell>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="usage-ip font-mono text-[11px]">
                      {row.ip || "—"}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="font-mono text-[11px]">
                    {row.ip || "—"}
                  </TooltipContent>
                </Tooltip>
              </TableCell>
              <TableCell className="font-mono text-[12px]">{row.model || "—"}</TableCell>
              <TableCell>
                <span className="usage-endpoint font-mono text-[11px]">
                  {shortEndpoint(row.endpoint)}
                </span>
              </TableCell>
              <TableCell>
                <span
                  className={cn(
                    "usage-stream font-mono text-[11px]",
                    row.stream ? "is-on" : "is-off",
                  )}
                >
                  {row.stream ? "stream" : "buffer"}
                </span>
              </TableCell>
              <TableCell>
                {row.effort || row.reasoning_tokens > 0 ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span
                        className={cn(
                          "usage-effort font-mono text-[11px]",
                          effortTone(row.effort),
                        )}
                      >
                        {row.effort || "—"}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                      {row.reasoning_tokens > 0
                        ? `推理 Token ${fmtInt(row.reasoning_tokens)}`
                        : "无推理 Token"}
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <span className="font-mono text-[11px] text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-right font-mono text-[11px] tabular-nums whitespace-nowrap">
                <div className="usage-token-block">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="usage-token-pair">
                        {fmtCompact(row.prompt_tokens)}
                        <em>/</em>
                        {fmtCompact(row.completion_tokens)}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                      输入 {fmtInt(row.prompt_tokens)}
                      <span className="mt-1 block">输出 {fmtInt(row.completion_tokens)}</span>
                      <span className="mt-1 block">
                        总计 {fmtInt((row.prompt_tokens || 0) + (row.completion_tokens || 0))}
                      </span>
                    </TooltipContent>
                  </Tooltip>
                  <CacheCell
                    cacheTokens={row.cache_tokens}
                    promptTokens={row.prompt_tokens}
                  />
                </div>
              </TableCell>
              <TableCell className="text-right font-mono text-[11px] tabular-nums">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span>{fmtTps(row.output_tps)}</span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="font-mono text-[11px] tabular-nums">
                    可见输出 {fmtTps(row.output_tps)}
                    <span className="mt-1 block">
                      流式按首字节后窗口，非流式按全程
                    </span>
                  </TooltipContent>
                </Tooltip>
              </TableCell>
              <TableCell>
                <Badge
                  variant={row.status === 1 ? "success" : "danger"}
                  className="font-mono text-[11px]"
                >
                  {row.status === 1 ? "成功" : "失败"}
                </Badge>
              </TableCell>
              <TableCell className="usage-col-reason">
                {row.reason ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="usage-reason text-[12px] text-muted-foreground">
                        {row.reason}
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="max-w-xs">
                      {row.reason}
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function GroupedDetailTable({
  rows,
  dimension,
}: {
  rows: UsageGroupedRow[];
  dimension: UsageGroupDim;
}) {
  const isAccount = dimension === "account";
  if (!rows.length) {
    return <div className="usage-empty">暂无{isAccount ? "账号" : "模型"}聚合数据</div>;
  }
  return (
    <div className="usage-table-wrap">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{isAccount ? "账号" : "模型"}</TableHead>
            <TableHead className="text-right">请求</TableHead>
            <TableHead className="text-right">成功</TableHead>
            <TableHead className="text-right">失败</TableHead>
            <TableHead className="text-right">流式</TableHead>
            <TableHead className="text-right">Token</TableHead>
            <TableHead className="text-right">推理 Token</TableHead>
            <TableHead className="text-right">缓存</TableHead>
            <TableHead>最近</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.key}>
              <TableCell>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="usage-grouped-name font-mono text-[12px]">
                      {row.key}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-xs break-all font-mono text-[11px]">
                    {row.key}
                  </TooltipContent>
                </Tooltip>
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums">
                {fmtInt(row.requests)}
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums is-ok">
                {fmtInt(row.success)}
              </TableCell>
              <TableCell
                className={cn(
                  "text-right font-mono text-[12px] tabular-nums",
                  row.failed > 0 ? "is-bad" : "text-muted-foreground",
                )}
              >
                {fmtInt(row.failed)}
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums">
                {fmtInt(row.stream_count)}
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums whitespace-nowrap">
                <span className="usage-token-pair">
                  {fmtCompact(row.prompt_tokens)}
                  <em>/</em>
                  {fmtCompact(row.completion_tokens)}
                </span>
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums">
                {row.reasoning_tokens ? fmtCompact(row.reasoning_tokens) : "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-[12px] tabular-nums">
                {row.cache_tokens ? fmtCompact(row.cache_tokens) : "—"}
              </TableCell>
              <TableCell className="font-mono text-[11px] text-muted-foreground tabular-nums">
                {formatAccountTime(row.last_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function LoadingSkeleton() {
  return (
    <div className="usage-page page-stack">
      <div className="panel usage-workspace">
        <div className="usage-toolbar">
          <div className="usage-skel usage-skel-title" />
          <div className="usage-skel usage-skel-actions" />
        </div>
        <div className="mission-strip mission-strip-usage" aria-hidden>
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="metric">
              <div className="usage-skel usage-skel-k" />
              <div className="usage-skel usage-skel-v" />
            </div>
          ))}
        </div>
        <div className="usage-grid">
          <div className="usage-panel usage-panel-chart">
            <div className="usage-skel usage-skel-chart" />
          </div>
          <div className="usage-panel">
            <div className="usage-skel usage-skel-chart" />
          </div>
        </div>
      </div>
    </div>
  );
}

function toHourLabel(hour: string): string {
  const m = /T(\d{2})/.exec(hour);
  return m ? `${m[1]}:00` : hour;
}

export function toTrendPoints(points: Array<UsageByDay | UsageByHour>): TrendPoint[] {
  return points.map((p) => {
    const hour = (p as UsageByHour).hour ?? "";
    const day = (p as UsageByDay).day ?? "";
    return {
      label: hour ? toHourLabel(hour) : day.slice(5) || day,
      requests: p.requests,
      tokens: p.total_tokens,
      errors: Math.max(0, p.requests - p.success),
    };
  });
}
