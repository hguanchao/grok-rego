"""
grok-rego 批量注册工具。

用法:
  uv run python main.py -n 5              # 批量注册 5 个账号
  uv run python main.py -n 5 -t 3         # 3 线程并发注册
  uv run python main.py --serve           # 启动管理 API（默认 8787）
  uv run python main.py --serve 8787      # 指定端口
"""

import sys

from core.logger import logger
from db import init_db


def _parse_args(args: list[str]) -> tuple[int, int, int | None]:
    """解析命令行，返回 (count, threads, serve_port)。

    serve_port 为 None 表示未启用 --serve；0 表示使用默认端口。
    """
    count, threads = 1, 1
    serve_port: int | None = None
    index = 0
    while index < len(args):
        if args[index] in ("-n", "--count") and index + 1 < len(args):
            try:
                count = max(1, int(args[index + 1]))
            except ValueError:
                pass
            index += 2
        elif args[index] in ("-t", "--thread") and index + 1 < len(args):
            try:
                threads = max(1, int(args[index + 1]))
            except ValueError:
                pass
            index += 2
        elif args[index] in ("--serve", "-s"):
            serve_port = 0
            if index + 1 < len(args):
                try:
                    serve_port = max(1, int(args[index + 1]))
                    index += 2
                    continue
                except ValueError:
                    pass
            index += 1
        else:
            index += 1
    return count, threads, serve_port


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return

    init_db()

    count, threads, serve_port = _parse_args(args)

    if serve_port is not None:
        from api.server import serve

        serve(port=serve_port if serve_port > 0 else None)
        return

    from workflow.register import run_auth_pool, run_signups

    success_count = run_signups(count=count, threads=threads)
    if success_count > 0:
        logger.success(f"[Main] 注册完成，成功 {success_count}/{count} 个账号")
    else:
        logger.error("[Main] 注册全部失败")

    # 注册完成后串行消化认证池，一个账号换完 Token 再接下一个
    run_auth_pool()


if __name__ == "__main__":
    main()
