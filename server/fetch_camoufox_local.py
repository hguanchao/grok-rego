"""Camoufox 内核离线安装：从本地 zip 安装，绕过 GitHub 直连慢的问题。

用法（在 server/ 目录下，zip 必须与 fetch 输出的资产 URL 完全一致）：
    uv run python fetch_camoufox_local.py /path/to/camoufox-<ver>-lin.x86_64.zip

原理：把 pkgman.webdl（下载）替换为读本地文件，版本信息解析、sha256 校验、
版本化安装目录、version.json 全部走 camoufox 官方逻辑，与在线安装结果一致。
仍需访问 GitHub API 获取 release 元数据（请求量极小）；被限流时先 export GITHUB_TOKEN。
"""

import io
import sys
from pathlib import Path

import camoufox.pkgman as pkgman


def local_webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
    """替换 pkgman.webdl：忽略 url，直接读命令行传入的本地 zip。"""
    local = Path(sys.argv[1]).resolve()
    if not local.is_file():
        sys.exit(f"[FAIL] 本地文件不存在: {local}")
    data = local.read_bytes()
    if buffer is None:
        buffer = io.BytesIO()
    buffer.write(data)
    buffer.seek(0)
    print(f"[本地安装] 跳过网络下载，使用 {local} ({len(data) / 1e6:.1f} MB)")
    return buffer


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    pkgman.webdl = local_webdl

    from camoufox.pkgman import CamoufoxFetcher, installed_verstr

    fetcher = CamoufoxFetcher()  # 仅请求 release 元数据（小 JSON）
    print(f"[本地安装] 目标版本: {fetcher.verstr}")
    print(f"[本地安装] 资产 URL: {fetcher.url}")
    fetcher.install()
    print(f"[OK] Camoufox 已安装: {installed_verstr()} -> {pkgman.INSTALL_DIR}")


if __name__ == "__main__":
    main()
