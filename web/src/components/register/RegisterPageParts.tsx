import { useState, type ReactNode } from "react";
import { CircleHelp, CircleX, Minus, Plus } from "lucide-react";
import { Badge } from "@/components/ui";
import { Button } from "@/components/ui";
import { Input } from "@/components/ui";
import { Label } from "@/components/ui";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui";
import {
  groupStatus,
  logBody,
  logLineTone,
  parseLogTag,
  type GroupStatus,
  type ThreadLogGroup,
} from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { LogEntry } from "@/lib/api";

/** 解析逗号/换行分隔的域名输入，trim 后去空去重 */
function parseDomainList(raw: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const token of String(raw ?? "").split(/[,，\s]+/)) {
    const d = token.trim().toLowerCase();
    if (!d || seen.has(d)) continue;
    seen.add(d);
    out.push(d);
  }
  return out;
}

/** 紧凑数值步进器：保持固定尺寸，避免数值变化造成布局抖动。 */
export function Stepper({
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  value: number;
  min: number;
  max: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const update = (next: number) => onChange(Math.min(max, Math.max(min, next)));
  return (
    <div className="ap-stepper">
      <button
        type="button"
        disabled={disabled || value <= min}
        onClick={() => update(value - 1)}
        aria-label="减少"
      >
        <Minus className="size-3.5" strokeWidth={1.75} />
      </button>
      <input
        type="number"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        onChange={(event) => update(Number(event.target.value) || min)}
      />
      <button
        type="button"
        disabled={disabled || value >= max}
        onClick={() => update(value + 1)}
        aria-label="增加"
      >
        <Plus className="size-3.5" strokeWidth={1.75} />
      </button>
    </div>
  );
}

function statusText(entry: LogEntry): { text: string; cls: string } {
  const tone = logLineTone(entry);
  if (tone === "ok") return { text: "成功", cls: "is-info" };
  if (tone === "fail") return { text: "失败", cls: "is-error" };
  if (tone === "warn") return { text: "警告", cls: "is-warn" };
  return { text: "进行", cls: "" };
}

function tagSlug(tag: string): string {
  switch (tag) {
    case "预检":
      return "preflight";
    case "任务":
      return "task";
    case "入池":
      return "inpool";
    case "出池":
      return "outpool";
    case "注册":
      return "signup";
    case "邮箱":
      return "mail";
    case "邮件":
      return "inbox";
    case "资料":
      return "profile";
    case "CF挑战":
      return "cf";
    case "SSO":
      return "sso";
    case "风控":
      return "risk";
    default:
      return "misc";
  }
}

/** 单行注册日志：时间 成功/失败 [标签] 正文 */
export function LogLineView({ entry }: { entry: LogEntry }) {
  const tone = logLineTone(entry);
  const status = statusText(entry);
  const tag = parseLogTag(entry.message);
  const body = logBody(entry.message);
  return (
    <div
      className={cn(
        "log-line",
        tone === "fail" && "is-fail",
        tone === "ok" && "is-ok",
        tone === "warn" && "is-warn",
      )}
    >
      <span className="log-time">
        {entry.ts?.slice(11, 19) || "--:--:--"}
      </span>
      <span className={cn("log-level", status.cls)}>{status.text}</span>
      {tag ? (
        <span className={cn("log-step", `reg-log-tag-${tagSlug(tag)}`)}>
          [{tag}]
        </span>
      ) : null}
      <span className="log-body">{body}</span>
    </div>
  );
}

/** 线程折叠面板标题 */
export function ThreadPanelHeader({ group }: { group: ThreadLogGroup }) {
  const st = groupStatus(group);
  return (
    <div className="log-thread-header">
      <span className={cn("log-status-dot", `is-${st}`)} aria-hidden />
      <span className="log-thread-title">{group.label}</span>
      <span className="log-thread-meta">
        {threadStatusBadge(st)}
        <span>{group.entries.length} 行</span>
        {group.failCount > 0 ? (
          <span className="text-destructive">失败 {group.failCount}</span>
        ) : null}
        {group.lastTime ? <span>{group.lastTime}</span> : null}
      </span>
      {group.lastBody ? (
        <span className="log-thread-preview" title={group.lastBody}>
          {group.lastBody}
        </span>
      ) : null}
    </div>
  );
}

function threadStatusBadge(status: GroupStatus) {
  switch (status) {
    case "fail":
      return <Badge variant="danger">失败</Badge>;
    case "ok":
      return <Badge variant="success">完成</Badge>;
    case "running":
      return <Badge variant="default">进行中</Badge>;
    default:
      return <Badge variant="outline">空闲</Badge>;
  }
}

/** 表单字段：中文 label；说明放在问号 Tooltip 中 */
export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex w-full min-w-0 flex-col gap-2">
      <div className="flex items-center gap-1.5">
        <Label className="text-foreground text-[13px] leading-none font-medium">
          {label}
        </Label>
        {hint ? (
          <Tooltip delayDuration={200}>
            <TooltipTrigger asChild>
              <button
                type="button"
                tabIndex={-1}
                className="text-muted-foreground hover:text-foreground inline-flex size-4 shrink-0 items-center justify-center rounded-full outline-none"
                aria-label={`${label}说明`}
                onClick={(event) => {
                  // 弹窗内阻止抢焦点/冒泡，避免打断输入与 Select
                  event.preventDefault();
                  event.stopPropagation();
                }}
              >
                <CircleHelp className="size-3.5" strokeWidth={1.75} />
              </button>
            </TooltipTrigger>
            <TooltipContent
              side="top"
              align="start"
              className="max-w-xs text-left leading-relaxed"
            >
              {hint}
            </TooltipContent>
          </Tooltip>
        ) : null}
      </div>
      <div className="w-full min-w-0">{children}</div>
    </div>
  );
}

/** 域名列表编辑：标签 + 输入添加；支持粘贴逗号/换行分隔 */
export function DomainListEditor({
  value,
  disabled,
  onChange,
  placeholder = "example.com",
}: {
  value: string[];
  disabled?: boolean;
  onChange: (next: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");

  const addDomains = () => {
    const incoming = parseDomainList(draft);
    if (!incoming.length) return;
    const seen = new Set(value);
    const next = [...value];
    for (const domain of incoming) {
      if (seen.has(domain)) continue;
      seen.add(domain);
      next.push(domain);
    }
    onChange(next);
    setDraft("");
  };

  const removeDomain = (domain: string) => {
    onChange(value.filter((item) => item !== domain));
  };

  return (
    <div className="domain-list-editor space-y-2">
      {value.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {value.map((domain) => (
            <Badge
              key={domain}
              variant="secondary"
              className="gap-1 pr-1 font-mono text-[12px]"
            >
              <span>{domain}</span>
              <button
                type="button"
                disabled={disabled}
                className="inline-flex size-4 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                aria-label={`移除 ${domain}`}
                onClick={() => removeDomain(domain)}
              >
                <CircleX className="size-3" strokeWidth={2} />
              </button>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-muted-foreground text-xs">
          未指定域名时由邮箱服务端自动分配
        </p>
      )}
      <div className="flex gap-2">
        <Input
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          className="font-mono text-[13px]"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === ",") {
              event.preventDefault();
              addDomains();
            }
          }}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || !draft.trim()}
          onClick={addDomains}
        >
          <Plus className="size-3.5" />
          添加
        </Button>
      </div>
    </div>
  );
}