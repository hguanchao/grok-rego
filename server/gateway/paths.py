"""网关本地路径与上游 URL 拼接。

规则：
- 有 /v1 不处理（含多余 /v1/v1 也不折叠本地路径）
- 没 /v1 补全 /v1（/zen/messages → /zen/v1/messages）
- 上游不影响：base 已含 /v1 时剥掉资源段前导 /v1，避免打成 /v1/v1
"""

from __future__ import annotations


def _clean(path: str) -> str:
    p = (path or "/").split("?", 1)[0]
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def ensure_local_v1(path: str, mount: str) -> str:
    """本地路径：已有 /v1 原样返回；缺 /v1 则插入。"""
    p = _clean(path)
    if p == mount:
        return f"{mount}/v1"
    if not p.startswith(mount + "/"):
        return p
    rest = p[len(mount) :]
    if rest.startswith("/v1"):
        return p
    return f"{mount}/v1{rest}"


def resource_path(path: str, mount: str) -> str:
    """去掉挂载前缀和所有前导 /v1，得到资源段（/messages、/models）。"""
    p = _clean(path)
    if p == mount:
        rest = ""
    elif p.startswith(mount + "/"):
        rest = p[len(mount) :]
    else:
        rest = p
    if rest and not rest.startswith("/"):
        rest = "/" + rest
    while rest.startswith("/v1/") or rest == "/v1":
        rest = rest[3:]
        if rest and not rest.startswith("/"):
            rest = "/" + rest
    return rest or ""


def upstream_url(base: str, path: str, mount: str, query: str = "") -> str:
    """拼上游 URL：base 已以 /v1 结尾，只追加资源段。"""
    rest = resource_path(path, mount)
    url = base.rstrip("/") + rest
    if query:
        url = f"{url}?{query}"
    return url
