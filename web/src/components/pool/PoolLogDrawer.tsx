import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Eraser, ScrollText, X } from "lucide-react";
import { Badge } from "@/components/ui";
import { Button } from "@/components/ui";
import { Empty } from "@/components/ui";
import { type PoolLogEntry } from "@/components/pool/PoolPageParts";
import { cn, logBody, parseLogTag } from "@/lib/utils";

/** 级别 → 行视觉态（is-ok / is-warn / is-fail） */
function rowTone(level: PoolLogEntry["level"]): string {
  switch (level) {
    case "SUCCESS":
      return "is-ok";
    case "WARNING":
      return "is-warn";
    case "ERROR":
      return "is-fail";
    default:
      return "";
  }
}

/** 级别 → 成功 / 失败 / 警告 / 进行 */
function statusText(
  level: PoolLogEntry["level"],
): { text: string; cls: string } {
  switch (level) {
    case "SUCCESS":
      return { text: "成功", cls: "is-info" };
    case "WARNING":
      return { text: "警告", cls: "is-warn" };
    case "ERROR":
      return { text: "失败", cls: "is-error" };
    default:
      return { text: "进行", cls: "" };
  }
}

interface PoolLogDrawerProps {
  open: boolean;
  /** 是否有操作正在执行（进度态：头部 LIVE 徽标 / 触发按钮脉冲点） */
  running?: boolean;
  entries: PoolLogEntry[];
  onClose: () => void;
  onClear: () => void;
}

/**
 * 号池管理日志右侧抽屉（布局/样式参照 acorn Ops Log 抽屉）。
 * 始终挂载，用 is-open 切换 transform/visibility；业务日志由前端本地记录。
 */
export function PoolLogDrawer({
  open,
  running = false,
  entries,
  onClose,
  onClear,
}: PoolLogDrawerProps) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  // 用户上翻查看历史日志时暂停自动滚底，滚回底部附近后恢复
  const stickToBottomRef = useRef(true);

  const handleBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  /** 最近一条日志（头部「最近」条） */
  const latest = entries[entries.length - 1];

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 新日志滚底（双 rAF：等列表挂载后再量 scrollHeight）；用户上翻时不打扰
  useEffect(() => {
    if (!open || !entries.length || !stickToBottomRef.current) return;
    const el = bodyRef.current;
    if (!el) return;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    });
  }, [open, entries.length]);

  const drawer = (
    <>
      <div
        className={cn("ops-drawer-backdrop", open && "is-open")}
        aria-hidden={!open}
        onClick={onClose}
      />
      <aside
        className={cn("ops-drawer", open && "is-open")}
        aria-hidden={!open}
        aria-label="号池管理日志"
        role="complementary"
      >
        <header className="ops-drawer-head">
          <div className="ops-drawer-title">
            {running ? <span className="ops-log-live-dot" aria-hidden /> : null}
            <span>Ops Log</span>
            {running ? (
              <Badge
                variant="success"
                className="h-5 normal-case tracking-normal"
              >
                LIVE
              </Badge>
            ) : null}
          </div>
          <div className="ops-drawer-tools">
            <Button
              size="sm"
              variant="ghost"
              disabled={entries.length === 0}
              title="清空当前视图"
              onClick={onClear}
            >
              <Eraser className="size-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              title="关闭"
              aria-label="关闭执行日志"
              onClick={onClose}
            >
              <X className="size-3.5" strokeWidth={1.6} />
            </Button>
          </div>
        </header>

        {latest ? (
          <div className="ops-drawer-latest" title={latest.message}>
            <span className="text-muted-foreground">最近</span>
            <span className="truncate">
              {latest.ts.slice(11, 19)} · {latest.message}
            </span>
          </div>
        ) : null}

        <div ref={bodyRef} className="ops-drawer-body" onScroll={handleBodyScroll}>
          {entries.length === 0 ? (
            <div className="flex h-full min-h-[160px] items-center justify-center px-4">
              <Empty
                className="opacity-80"
                icon={ScrollText}
                title="暂无操作记录"
                description={
                  running
                    ? "任务运行中，等待日志…"
                    : "巡检探活 / 认证 / 重新登录时日志会显示在这里。"
                }
              />
            </div>
          ) : (
            entries.map((entry) => {
              const status = statusText(entry.level);
              const tag = parseLogTag(entry.message);
              const body = logBody(entry.message);
              return (
                <p
                  key={entry.id}
                  className={cn("log-line", rowTone(entry.level))}
                >
                  <span className="log-time">{entry.ts.slice(11, 19)}</span>
                  <span className={cn("log-level", status.cls)}>
                    {status.text}
                  </span>
                  {tag ? (
                    <span
                      className={cn("log-step", `ops-log-step-${entry.type}`)}
                    >
                      [{tag}]
                    </span>
                  ) : null}
                  <span className="log-body">{body}</span>
                </p>
              );
            })
          )}
        </div>
      </aside>
    </>
  );

  if (typeof document === "undefined") return null;
  return createPortal(drawer, document.body);
}