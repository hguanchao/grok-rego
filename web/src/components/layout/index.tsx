import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  Bot,
  Boxes,
  LayoutGrid,
  Monitor,
  Moon,
  Radio,
  Sun,
} from "lucide-react";
import {
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  Tabs,
  TabsList,
  TabsTrigger,
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui";
import {
  fetchRegisterStatus,
  type RegisterJobState,
} from "@/lib/api";
import { useTheme, type ThemeMode } from "@/lib/theme";
import { cn } from "@/lib/utils";

const nav = [
  { to: "/register", label: "账号注册", icon: LayoutGrid },
  { to: "/pool", label: "号池管理", icon: Boxes },
  { to: "/usage", label: "用量统计", icon: Activity },
  { to: "/gateway", label: "网关运维", icon: Radio },
] as const;

const themeMeta: Record<
  ThemeMode,
  { icon: typeof Sun; label: string }
> = {
  light: { icon: Sun, label: "浅色" },
  dark: { icon: Moon, label: "深色" },
  system: { icon: Monitor, label: "跟随系统" },
};

/** 主题切换：平铺三图标（浅色 / 深色 / 跟随系统），点击直接选中 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();

  return (
    <ToggleGroup
      type="single"
      value={theme}
      onValueChange={(value) => {
        if (value) setTheme(value as ThemeMode);
      }}
      size="sm"
      className={cn("theme-toggle", className)}
      aria-label="主题切换"
    >
      {(Object.keys(themeMeta) as ThemeMode[]).map((mode) => {
        const meta = themeMeta[mode];
        const Icon = meta.icon;
        return (
          // asChild 方向必须是 ToggleGroupItem 包 TooltipTrigger：
          // Radix 两者都把 data-state 写在 props 展开之前，谁在外层谁的 data-state 被覆盖。
          // 反过来写（TooltipTrigger asChild 包 Item）会让 tooltip 的 data-state="closed"
          // 冲掉 Toggle 的 "on"，选中态样式全部失效。
          <Tooltip key={mode}>
            <ToggleGroupItem
              asChild
              value={mode}
              className="theme-toggle-item"
              aria-label={`主题：${meta.label}`}
            >
              <TooltipTrigger>
                <Icon className="size-3.5" strokeWidth={1.75} />
              </TooltipTrigger>
            </ToggleGroupItem>
            <TooltipContent side="bottom">{meta.label}</TooltipContent>
          </Tooltip>
        );
      })}
    </ToggleGroup>
  );
}

/** 顶栏注册呼吸灯：跨页轮询任务状态，运行中计数呼吸，点击回注册页 */
function RegisterBreathStatus() {
  const [job, setJob] = useState<RegisterJobState | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const state = await fetchRegisterStatus();
        if (alive) setJob(state);
      } catch {
        // 管理 API 离线：保留上一次状态
      } finally {
        if (alive) timer = window.setTimeout(poll, 3000);
      }
    };
    timer = window.setTimeout(poll, 600);
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  const status = job?.status ?? "idle";
  const live =
    status === "pending" || status === "running" || status === "stopping";
  const finished = (job?.success ?? 0) + (job?.failed ?? 0);
  const total = job?.count ?? 0;

  let value = "空闲";
  if (status === "pending") value = "排队";
  else if (status === "running")
    value = total > 0 ? `${finished}/${total}` : "运行中";
  else if (status === "stopping") value = "停止中";
  else if (status === "completed") value = "完成";
  else if (status === "failed") value = "失败";
  else if (status === "cancelled") value = "已停";

  const tone = live
    ? "is-live"
    : status === "completed"
      ? "is-ok"
      : status === "failed"
        ? "is-bad"
        : "is-idle";

  const tip = live
    ? `注册进行中 · ${value}`
    : status !== "idle"
      ? `最近注册 · ${value}`
      : "暂无注册任务";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <NavLink
          to="/register"
          className={cn("register-breath", tone)}
          aria-label={`注册状态 ${value}`}
        >
          <span className="register-breath-dot" aria-hidden />
          <span className="register-breath-k">注册</span>
          <span className="register-breath-v">{value}</span>
        </NavLink>
      </TooltipTrigger>
      <TooltipContent side="bottom">{tip}</TooltipContent>
    </Tooltip>
  );
}

function resolveTab(pathname: string): string {
  if (pathname.startsWith("/pool")) return "/pool";
  if (pathname.startsWith("/gateway")) return "/gateway";
  if (pathname.startsWith("/usage")) return "/usage";
  return "/register";
}

/** Aperture Console 壳层：顶栏 + 全幅内容 */
export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();

  return (
    <div className="app-shell">
      <header className="top-rail">
        <div>
          <NavLink
            to="/register"
            className="brand-lockup"
            aria-label="Grok Ops"
          >
            <span className="brand-mark" aria-hidden>
              <Bot strokeWidth={1.6} />
            </span>
            <span className="brand-name">
              Grok <span className="brand-sub">Ops</span>
            </span>
          </NavLink>
        </div>

        <Tabs
          value={resolveTab(location.pathname)}
          onValueChange={(value) => navigate(value)}
          className="header-nav"
        >
          <TabsList className="nav-tabs">
            {nav.map((item) => (
              <TabsTrigger key={item.to} value={item.to} className="nav-tab">
                <item.icon
                  className="nav-tab-icon"
                  aria-hidden
                  strokeWidth={1.6}
                />
                <span>{item.label}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <div className="header-actions">
          <RegisterBreathStatus />
          <ThemeToggle />
        </div>
      </header>

      <div className="app-frame">
        <PageEnter>
          <Outlet />
        </PageEnter>
      </div>
    </div>
  );
}

/**
 * 路由切换：轻量淡入（仅 opacity，Edge 友好）。
 */
export function PageEnter({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const { pathname } = useLocation();
  return (
    <div key={pathname} className={cn("page-enter", className)}>
      {children}
    </div>
  );
}
