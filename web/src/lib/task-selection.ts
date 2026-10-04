/** 号池确认弹窗交给四个任务入口的账号集合。 */

export type PoolTaskKind = "auth" | "reauth" | "inspect" | "push";

/**
 * 确认时勾选的 id。
 * 非空：请求只带这些 id。
 * 空：不带 ids，由服务端按该任务的资格做全库筛选。
 */
export function taskAccountIds(checkedIds: readonly number[]): number[] | undefined {
  return checkedIds.length > 0 ? [...checkedIds] : undefined;
}

/** 与 api.ts 里巡检 / 重登 / 认证 / 推送的 JSON 体一致：空勾选省略 ids。 */
export function taskRequestBody(
  kind: PoolTaskKind,
  checkedIds: readonly number[],
  extra: { concurrency?: number; targets?: Array<"g2a" | "cpa"> } = {},
): Record<string, unknown> {
  const ids = taskAccountIds(checkedIds);
  if (kind === "auth") {
    return ids ? { ids } : {};
  }
  if (kind === "push") {
    const body: Record<string, unknown> = {
      targets: extra.targets ?? [],
      concurrency: extra.concurrency,
    };
    if (ids) body.ids = ids;
    return body;
  }
  const body: Record<string, unknown> = { concurrency: extra.concurrency };
  if (ids) body.ids = ids;
  return body;
}
