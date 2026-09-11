import {
  Ban,
  CloudOff,
  Eye,
  EyeOff,
  Info,
  LogIn,
  SearchX,
  ShieldCheck,
  Trash2,
  Undo2,
} from "lucide-react";
import {
  Badge,
  Button,
  Checkbox,
  Empty,
  Table,
  TableBody,
  TableCell,
  TableEmptyRow,
  TableHead,
  TableHeader,
  TableRow,
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui";
import type { PoolAccount } from "@/lib/api";
import { STATUS_LABELS } from "@/lib/api";
import { cn, formatAccountTime } from "@/lib/utils";
import {
  ACCOUNT_STATUS_VARIANT,
  INSPECT_VARIANT,
  formatPoolExpiry,
  inspectLabel,
  inspectTitle,
  poolExpiryTone,
} from "./PoolPageParts";

export type PoolRowAction = "auth" | "reauth" | "disable" | "enable" | "delete";

interface PoolAccountsTableProps {
  accounts: PoolAccount[];
  selected: Set<number>;
  visiblePasswords: Set<number>;
  loading: boolean;
  loadError: string | null;
  inspecting: boolean;
  onToggleSelectAll: () => void;
  onToggleSelect: (id: number) => void;
  onTogglePassword: (id: number) => void;
  onShowDetails: (account: PoolAccount) => void;
  onRowAction: (id: number, action: PoolRowAction) => void;
}

const SKELETON_ROWS = 6;

function AccountActions({
  account,
  inspecting,
  onShowDetails,
  onAction,
}: {
  account: PoolAccount;
  inspecting: boolean;
  onShowDetails: () => void;
  onAction: (action: PoolRowAction) => void;
}) {
  return (
    <div className="row-actions">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="row-action-btn"
        title="详情"
        aria-label={`详情 ${account.email}`}
        onClick={onShowDetails}
      >
        <Info className="size-3.5" strokeWidth={1.8} aria-hidden />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="row-action-btn"
        title="认证"
        aria-label={`认证 ${account.email}`}
        disabled={inspecting || account.has_token !== 0}
        onClick={() => onAction("auth")}
      >
        <ShieldCheck className="size-3.5" strokeWidth={1.8} aria-hidden />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="row-action-btn"
        title={
          account.has_token !== 0
            ? "重登（需重登/拒权/异常状态）"
            : "未认证：请先点认证"
        }
        aria-label={`重登 ${account.email}`}
        disabled={inspecting || account.has_token === 0 || ![2, 4, 5].includes(account.status)}
        onClick={() => onAction("reauth")}
      >
        <LogIn className="size-3.5" strokeWidth={1.8} aria-hidden />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className={cn(
          "row-action-btn",
          account.status === 6 ? "row-action-ok" : "row-action-warn",
        )}
        title={account.status === 6 ? "解禁" : "禁用"}
        aria-label={`${account.status === 6 ? "解禁" : "禁用"} ${account.email}`}
        disabled={inspecting}
        onClick={() => onAction(account.status === 6 ? "enable" : "disable")}
      >
        {account.status === 6 ? (
          <Undo2 className="size-3.5" strokeWidth={1.8} aria-hidden />
        ) : (
          <Ban className="size-3.5" strokeWidth={1.8} aria-hidden />
        )}
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="row-action-btn row-action-danger"
        title="删除"
        aria-label={`删除 ${account.email}`}
        onClick={() => onAction("delete")}
      >
        <Trash2 className="size-3.5" strokeWidth={1.8} aria-hidden />
      </Button>
    </div>
  );
}

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: SKELETON_ROWS }, (_, i) => (
        <TableRow key={`sk-${i}`} className="pool-skeleton-row" aria-hidden>
          <TableCell className="pool-col-check">
            <span className="pool-skel pool-skel-box" />
          </TableCell>
          <TableCell className="pool-col-id">
            <span className="pool-skel pool-skel-sm" />
          </TableCell>
          <TableCell className="pool-col-email">
            <span className="pool-skel pool-skel-lg" />
          </TableCell>
          <TableCell className="pool-col-pwd">
            <span className="pool-skel pool-skel-md" />
          </TableCell>
          <TableCell className="pool-col-time">
            <span className="pool-skel pool-skel-md" />
          </TableCell>
          <TableCell className="pool-col-time">
            <span className="pool-skel pool-skel-md" />
          </TableCell>
          <TableCell className="pool-col-auth">
            <span className="pool-skel pool-skel-badge" />
          </TableCell>
          <TableCell className="pool-col-status">
            <span className="pool-skel pool-skel-badge" />
          </TableCell>
          <TableCell className="pool-col-inspect">
            <span className="pool-skel pool-skel-badge" />
          </TableCell>
          <TableCell className="pool-col-reason">
            <span className="pool-skel pool-skel-md" />
          </TableCell>
          <TableCell className="pool-col-actions">
            <span className="pool-skel pool-skel-actions" />
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}

export function PoolAccountsTable({
  accounts,
  selected,
  visiblePasswords,
  loading,
  loadError,
  inspecting,
  onToggleSelectAll,
  onToggleSelect,
  onTogglePassword,
  onShowDetails,
  onRowAction,
}: PoolAccountsTableProps) {
  const selectedOnPage = accounts.reduce(
    (n, a) => n + (selected.has(a.id) ? 1 : 0),
    0,
  );
  const allPageSelected =
    accounts.length > 0 && selectedOnPage === accounts.length;
  const somePageSelected = selectedOnPage > 0 && !allPageSelected;
  const showSkeleton = loading && accounts.length === 0 && !loadError;

  return (
    <div className="pool-table-wrap">
      <Table>
        <TableHeader>
          <TableRow className="border-0 hover:bg-transparent">
            <TableHead className="pool-col-check">
              <Checkbox
                checked={
                  allPageSelected
                    ? true
                    : somePageSelected
                      ? "indeterminate"
                      : false
                }
                onCheckedChange={onToggleSelectAll}
                aria-label="全选当前页"
                disabled={showSkeleton}
              />
            </TableHead>
            <TableHead className="pool-col-id">ID</TableHead>
            <TableHead className="pool-col-email">邮箱</TableHead>
            <TableHead className="pool-col-pwd">密码</TableHead>
            <TableHead className="pool-col-time">到期时间</TableHead>
            <TableHead className="pool-col-time">注册时间</TableHead>
            <TableHead className="pool-col-auth">认证状态</TableHead>
            <TableHead className="pool-col-status">账号状态</TableHead>
            <TableHead className="pool-col-inspect">巡检</TableHead>
            <TableHead className="pool-col-reason">原因</TableHead>
            <TableHead className="pool-col-actions">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {showSkeleton ? (
            <SkeletonRows />
          ) : accounts.length === 0 ? (
            <TableEmptyRow colSpan={11}>
              <div
                className="pool-table-empty"
                role={loadError ? "alert" : "status"}
              >
                <Empty
                  icon={loadError ? CloudOff : SearchX}
                  title={loadError ? "加载失败" : "暂无账号"}
                  description={
                    loadError
                      ? `接口异常：${loadError}`
                      : "调整筛选条件，或先到注册页创建账号。"
                  }
                />
              </div>
            </TableEmptyRow>
          ) : (
            accounts.map((account) => {
              const passwordVisible = visiblePasswords.has(account.id);
              const inspect = inspectLabel(account.dumbed);
              const expiryTone = poolExpiryTone(account.expires_at);
              return (
                <TableRow
                  key={account.id}
                  data-state={
                    selected.has(account.id) ? "selected" : undefined
                  }
                >
                  <TableCell className="pool-col-check">
                    <Checkbox
                      checked={selected.has(account.id)}
                      onCheckedChange={() => onToggleSelect(account.id)}
                      aria-label={`选择 ${account.email}`}
                    />
                  </TableCell>
                  <TableCell className="pool-col-id">
                    <span
                      className="pool-cell-clip"
                      title={`账号 ID ${account.id}`}
                    >
                      {account.id}
                    </span>
                  </TableCell>
                  <TableCell className="pool-col-email">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="pool-cell-clip cursor-pointer">
                          {account.email}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top">{account.email}</TooltipContent>
                    </Tooltip>
                  </TableCell>
                  <TableCell className="pool-col-pwd">
                    {account.password ? (
                      <div className="pool-password-cell">
                        <span className="pool-cell-clip">
                          {passwordVisible ? account.password : "••••••••"}
                        </span>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-6"
                          title={passwordVisible ? "隐藏密码" : "显示密码"}
                          aria-label={
                            passwordVisible ? "隐藏密码" : "显示密码"
                          }
                          onClick={() => onTogglePassword(account.id)}
                        >
                          {passwordVisible ? (
                            <EyeOff
                              className="size-3.5"
                              strokeWidth={1.8}
                              aria-hidden
                            />
                          ) : (
                            <Eye
                              className="size-3.5"
                              strokeWidth={1.8}
                              aria-hidden
                            />
                          )}
                        </Button>
                      </div>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="pool-col-time">
                    <span
                      className={cn(
                        "pool-expiry",
                        expiryTone === "expired" && "is-expired",
                      )}
                      title={
                        expiryTone === "expired" ? "已过期" : undefined
                      }
                    >
                      {formatPoolExpiry(
                        account.expires_at,
                        account.expires_in,
                      )}
                    </span>
                  </TableCell>
                  <TableCell className="pool-col-time">
                    <span className="pool-cell-clip text-muted-foreground">
                      {formatAccountTime(account.created_at)}
                    </span>
                  </TableCell>
                  <TableCell className="pool-col-auth">
                    <Badge variant={account.has_token ? "success" : "outline"}>
                      {account.has_token ? "已认证" : "未认证"}
                    </Badge>
                  </TableCell>
                  <TableCell className="pool-col-status">
                    <Badge
                      variant={
                        ACCOUNT_STATUS_VARIANT[account.status] || "secondary"
                      }
                    >
                      {STATUS_LABELS[account.status] || "未知"}
                    </Badge>
                  </TableCell>
                  <TableCell className="pool-col-inspect">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span>
                          <Badge
                            variant={INSPECT_VARIANT[inspect] || "secondary"}
                          >
                            {inspect}
                          </Badge>
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top">
                        {inspectTitle(
                          account.dumbed,
                          account.inspect_tps,
                          account.inspect_thinking,
                          account.inspected_at
                            ? formatAccountTime(account.inspected_at)
                            : null,
                        )}
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                  <TableCell className="pool-col-reason">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="pool-cell-clip cursor-pointer">
                          {account.reason || "—"}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent
                        side="top"
                        className="max-w-xs text-left leading-relaxed"
                      >
                        {account.reason || "无"}
                      </TooltipContent>
                    </Tooltip>
                  </TableCell>
                  <TableCell className="pool-col-actions">
                    <AccountActions
                      account={account}
                      inspecting={inspecting}
                      onShowDetails={() => onShowDetails(account)}
                      onAction={(action) =>
                        onRowAction(account.id, action)
                      }
                    />
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>
    </div>
  );
}
