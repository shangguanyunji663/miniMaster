"""miniMaster CLI：完整实现（console_script 入口点也指向这里）。

用法:
    minimaster "目标"
    minimaster "目标" --workdir workspaces/demo --max-rounds 3 --parallel 2
    minimaster --resume runs/<ts>/state.json   # 断点续跑
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK：强制 UTF-8 输出，避免 rich/中文乱码。
# 必须在导入 rich/minimaster 之前执行，故这两处导入顺序为例外。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and getattr(_stream, "encoding", "").lower() not in ("utf-8", "utf8"):
        with contextlib.suppress(AttributeError, OSError):
            _stream.reconfigure(encoding="utf-8")

from minimaster.config import Settings  # noqa: E402
from minimaster.core.harness import Harness  # noqa: E402
from minimaster.utils.reporter import Reporter  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="miniMaster",
        description="Planner-Executor-Validator 三层嵌套循环 Harness 执行框架",
    )
    parser.add_argument(
        "goal", nargs="?", default=None,
        help="要完成的自然语言目标（--resume 时可省略）",
    )
    parser.add_argument(
        "--workdir", default=None, help="工作目录沙箱（默认 workspaces/run-<时间戳>）"
    )
    parser.add_argument("--max-rounds", type=int, default=None, help="外层最大轮数")
    parser.add_argument("--max-steps", type=int, default=None, help="单任务最大步数")
    parser.add_argument("--max-retries", type=int, default=None, help="单任务最大重试次数")
    parser.add_argument("--parallel", type=int, default=None, help="独立任务并行度（默认 1 顺序）")
    parser.add_argument(
        "--resume", default=None, metavar="STATE_JSON",
        help="从快照 state.json 断点续跑（goal 可省略，workdir 取快照记录）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出最终报告")
    parser.add_argument("--no-trace", action="store_true", help="不落盘运行轨迹与快照")
    return parser.parse_args()


def snapshot_workdir(path: str) -> str | None:
    """从快照读取原始工作目录，用于 resume 时默认复用。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("workdir") or None
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    args = parse_args()
    settings = Settings.load()
    overrides = {}
    if args.max_rounds is not None:
        overrides["max_rounds"] = args.max_rounds
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    if args.max_retries is not None:
        overrides["max_retries"] = args.max_retries
    if args.parallel is not None:
        overrides["parallel"] = max(1, args.parallel)
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    if args.resume:
        if not args.workdir:
            saved = snapshot_workdir(args.resume)
            if saved:
                args.workdir = saved
        if not args.workdir:
            args.workdir = str(Path("workspaces") / f"run-{time.strftime('%Y%m%d-%H%M%S')}")
    elif not args.goal:
        print("错误：必须提供目标，或使用 --resume 指定快照续跑。", file=sys.stderr)
        return 2

    workdir = Path(args.workdir)
    run_dir = None if args.no_trace else Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    reporter = Reporter(run_dir=run_dir, quiet=args.quiet)

    reporter.banner(f"miniMaster · {settings.model}" + (" · resume" if args.resume else ""))
    harness = Harness(settings, workdir, reporter, snapshot_dir=run_dir)
    try:
        if args.resume:
            harness.load_state(args.resume)
        report = harness.run(args.goal)
    except KeyboardInterrupt:
        reporter.final("被用户中断（可用 --resume 从最近快照续跑）", success=False)
        return 130
    except Exception as exc:  # 顶层兜底：报告失败原因
        reporter.final(f"Harness 异常终止: {type(exc).__name__}: {exc}", success=False)
        return 1

    reporter.final(report.render(), success=report.success)
    if run_dir is not None:
        print(f"运行轨迹: {run_dir / 'trace.jsonl'}")
        print(f"断点快照: {run_dir / 'state.json'}（--resume 可续跑）")
    return 0 if report.success else 1


if __name__ == "__main__":
    sys.exit(main())
