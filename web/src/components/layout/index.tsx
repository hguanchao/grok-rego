import { useEffect, useState, type ReactNode } from "react";
import {
  Bot,
  Boxes,
  LayoutGrid,
  Monitor,
  Moon,
  Sun,
} from "lucide-react";
import {
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  Button,
  Tabs,
  TabsList,
  TabsTrigger,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
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
] as const;

const themeMeta: Record<
  ThemeMode,
  { icon: typeof Sun; label: string; nextHint: string }
> = {
  light: { icon: Sun, label: "浅色", nextHint: "切换深色" },
  dark: { icon: Moon, label: "深色", nextHint: "切换跟随系统" },
  system: { icon: Monitor, label: "跟随系统", nextHint: "切换浅色" },
};

/** 主题切换：单按钮 cycle，节省顶栏空间 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, cycleTheme } = useTheme();
  const meta = themeMeta[theme];
  const Icon = meta.icon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className={cn("theme-toggle-btn", className)}
          aria-label={`主题：${meta.label}，${meta.nextHint}`}
          onClick={cycleTheme}
        >
          <Icon className="size-3.5" strokeWidth={1.75} />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        {meta.label} · 点击{meta.nextHint}
      </TooltipContent>
    </Tooltip>
  );
}

const statusLabel: Record<RegisterJobState["status"], string> = {
  idle: "空闲",
  pending: "排队中",
  running: "运行中",
  stopping: "停止中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
};

const activeStatuses: RegisterJobState["status"][] = [
  "pending",
  "running",
  "stopping",
];

/** 顶栏呼吸注册状态：点击跳转注册页 */
export function RegisterStatus({ className }: { className?: string }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<RegisterJobState["status"]>("idle");

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const state = await fetchRegisterStatus();
        if (alive) setStatus(state.status);
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

  const label = statusLabel[status];
  const isLive = activeStatuses.includes(status);
  const isBad = status === "failed";

  return (
    <button
      type="button"
      className={cn(
        "reg-status",
        isLive && "is-live",
        isBad && "is-bad",
        className,
      )}
      title={`注册状态：${label}（点击前往注册页）`}
      aria-label={`注册状态：${label}，点击前往注册页`}
      onClick={() => navigate("/register")}
    >
      <i className="reg-status-dot" aria-hidden />
      <span>{label}</span>
    </button>
  );
}

function resolveTab(pathname: string): string {
  if (pathname.startsWith("/pool")) return "/pool";
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
          <RegisterStatus />
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
