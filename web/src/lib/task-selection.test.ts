import assert from "node:assert/strict";
import test from "node:test";

import { taskAccountIds, taskRequestBody, type PoolTaskKind } from "./task-selection.ts";

const KINDS: PoolTaskKind[] = ["inspect", "reauth", "auth", "push"];

test("确认时的勾选 id 在弹窗状态清空后仍然是请求体里的 id", () => {
  const checked = [11, 4, 11];
  let dialog: { kind: PoolTaskKind; ids: number[] } | null = {
    kind: "inspect",
    ids: [...checked],
  };
  const sent = Object.fromEntries(
    KINDS.map((kind) => [kind, taskRequestBody(kind, dialog!.ids, {
      concurrency: 4,
      targets: ["g2a"],
    })]),
  );
  dialog = null;
  assert.equal(dialog, null);
  assert.deepEqual(taskAccountIds(checked), [11, 4, 11]);
  assert.deepEqual(sent.inspect, { ids: [11, 4, 11], concurrency: 4 });
  assert.deepEqual(sent.reauth, { ids: [11, 4, 11], concurrency: 4 });
  assert.deepEqual(sent.auth, { ids: [11, 4, 11] });
  assert.deepEqual(sent.push, {
    ids: [11, 4, 11],
    targets: ["g2a"],
    concurrency: 4,
  });
});

test("没有勾选时四个任务都不带 ids，走服务端全库筛选", () => {
  for (const kind of KINDS) {
    const body = taskRequestBody(kind, [], { concurrency: 20, targets: ["cpa"] });
    assert.equal(Object.hasOwn(body, "ids"), false, kind);
    assert.equal(taskAccountIds([]), undefined);
  }
  assert.deepEqual(taskRequestBody("auth", []), {});
  assert.deepEqual(taskRequestBody("inspect", []), { concurrency: undefined });
});
