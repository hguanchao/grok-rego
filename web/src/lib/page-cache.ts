import { useCallback, useRef, useSyncExternalStore } from "react";

/**
 * 页面数据缓存（路由切换 keep-alive 的数据层实现）
 *
 * react-router 无 keep-alive：路由切换时页面组件卸载，useState 全部丢失。
 * 本模块用模块级 Map 存数据 + useSyncExternalStore 订阅，
 * API 与 useState 完全一致（[value, setter]），替换零成本：
 *
 *   const [accounts, setAccounts] = usePageCache<PoolAccount[]>("pool.accounts", () => []);
 *
 * - 首次挂载：从缓存读旧值（若有），页面秒开，无需重新加载
 * - 路由切走再回来：数据、筛选条件、页码全部保留
 * - setter 支持函数式更新（setX(prev => ...)）
 *
 * 注意：只有"值"被缓存；卸载后的定时器/轮询/请求不会恢复，
 * 挂载方应在首屏 effect 中按需静默刷新（见 PoolPage 用法）。
 */

interface PageStore<T> {
  data: T;
  listeners: Set<() => void>;
}

/** 模块级存储：路由切换、组件卸载均不清除（页面刷新后重置） */
const pageStores = new Map<string, PageStore<unknown>>();

type Updater<T> = (prev: T) => T;

export function usePageCache<T>(key: string, initial: () => T): [T, (v: T | Updater<T>) => void] {
  const storeRef = useRef<PageStore<T> | null>(null);

  // 惰性初始化：优先复用既有缓存，无缓存才建新 store（initial 只执行一次）
  if (!storeRef.current) {
    let store = pageStores.get(key) as PageStore<T> | undefined;
    if (!store) {
      store = { data: initial(), listeners: new Set() };
      pageStores.set(key, store);
    }
    storeRef.current = store;
  }
  const store = storeRef.current;

  const getSnapshot = useCallback(() => store.data, [store]);
  const subscribe = useCallback(
    (cb: () => void) => {
      store.listeners.add(cb);
      return () => {
        store.listeners.delete(cb);
      };
    },
    [store],
  );

  // 与 useState 的 setter 语义一致：支持值或函数式更新；
  // 相同引用时 bail out，避免无意义重渲染
  const setValue = useCallback(
    (v: T | Updater<T>) => {
      const next = typeof v === "function" ? (v as Updater<T>)(store.data) : v;
      if (Object.is(next, store.data)) return;
      store.data = next;
      store.listeners.forEach((l) => l());
    },
    [store],
  );

  const value = useSyncExternalStore(subscribe, getSnapshot);
  return [value, setValue];
}

/** 清空全部页面缓存（如用户登出/重置场景） */
export function clearPageCache(): void {
  pageStores.clear();
}
