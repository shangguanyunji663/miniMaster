"""eval 基准：标准任务套件 + 客观检查器 + 运行指标报表 + 机制消融开关。

用法（项目根目录）:
    python eval/run_eval.py --suite full            # 全部 8 个任务
    python eval/run_eval.py --suite quick           # 前 4 个简单任务
    python eval/run_eval.py --ablate guard          # 关闭 Guard 对照实验
    python eval/run_eval.py --ablate retry          # 关闭重试对照实验

每个任务的成败由**客观检查器**判定（运行产物并验证），而非 Agent 自述；
指标含成功率 / 轮数 / 重试 / Guard 拦截 / token 成本，结果存 eval/results/。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from minimaster.config import Settings
from minimaster.core.harness import Harness
from minimaster.utils.reporter import Reporter

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---- 检查器辅助 ----

def run_python(workdir: Path, script: str, *args: str, timeout: int = 60) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, script, *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def read_file(workdir: Path, name: str) -> str:
    return (workdir / name).read_text(encoding="utf-8", errors="replace")


# ---- 任务定义 ----

def check_hello(workdir: Path) -> tuple[bool, str]:
    if not (workdir / "hello.py").exists():
        return False, "hello.py 不存在"
    ok, out = run_python(workdir, "hello.py")
    return (ok and "Hello, miniMaster!" in out), f"运行输出: {out.strip()[:80]}"


def check_notes(workdir: Path) -> tuple[bool, str]:
    try:
        lines = [ln.strip() for ln in read_file(workdir, "notes.txt").splitlines() if ln.strip()]
    except OSError:
        return False, "notes.txt 不存在"
    return lines == ["alpha", "beta", "gamma"], f"实际内容 {lines}"


def check_calc(workdir: Path) -> tuple[bool, str]:
    if not (workdir / "calc.py").exists():
        return False, "calc.py 不存在"
    ok, out = run_python(workdir, "-c",
                         "from calc import add, sub, mul, div;"
                         "assert add(2,3)==5 and sub(5,2)==3;"
                         "assert mul(3,4)==12 and div(8,4)==2; print('OK')")
    return (ok and "OK" in out), out.strip()[:100]


def check_fizzbuzz(workdir: Path) -> tuple[bool, str]:
    try:
        lines = read_file(workdir, "fizzbuzz_output.txt").splitlines()
    except OSError:
        return False, "fizzbuzz_output.txt 不存在"
    if len(lines) != 100:
        return False, f"行数 {len(lines)} != 100"
    if lines[14].strip() != "FizzBuzz":
        return False, f"第 15 行是 {lines[14]!r}"
    return True, "100 行且第 15 行为 FizzBuzz"


def setup_bugfix(workdir: Path) -> None:
    """植入一个带 off-by-one bug 的脚本，供任务修复。"""
    (workdir / "sum_to.py").write_text(
        'def sum_to(n):\n'
        '    total = 0\n'
        '    for i in range(1, n):  # bug: 漏掉 n 本身\n'
        '        total += i\n'
        '    return total\n',
        encoding="utf-8",
    )


def check_bugfix(workdir: Path) -> tuple[bool, str]:
    ok, out = run_python(workdir, "-c",
                         "from sum_to import sum_to;"
                         "assert sum_to(5) == 15 and sum_to(1) == 1 and sum_to(10) == 55;"
                         "print('OK')")
    return (ok and "OK" in out), out.strip()[:100]


def setup_grep(workdir: Path) -> None:
    """植入 5 个源文件，其中 3 个含 TODO 标记。"""
    files = {
        "app/main.py": "def main():\n    pass  # TODO: 入口逻辑\n",
        "app/util.py": "def fmt(x):\n    return str(x)\n",
        "app/io_utils.py": "def load(p):\n    # TODO: 异常处理\n    return open(p).read()\n",
        "app/consts.py": "MAX = 10\n",
        "app/report.py": "def render(rows):\n    # TODO: 分页\n    return '\\n'.join(rows)\n",
    }
    for name, content in files.items():
        path = workdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def check_grep(workdir: Path) -> tuple[bool, str]:
    try:
        lines = {ln.strip() for ln in read_file(workdir, "todos.txt").splitlines() if ln.strip()}
    except OSError:
        return False, "todos.txt 不存在"
    expected = {"app/main.py", "app/io_utils.py", "app/report.py"}
    return lines == expected, f"实际 {sorted(lines)}，期望 {sorted(expected)}"


def check_dep_chain(workdir: Path) -> tuple[bool, str]:
    import json as _json
    try:
        data = _json.loads(read_file(workdir, "data.json"))
        summary = read_file(workdir, "summary.txt").strip()
    except (OSError, _json.JSONDecodeError) as exc:
        return False, f"产物缺失或损坏: {exc}"
    ok, out = run_python(workdir, "read_data.py")
    expected_total = str(sum(item["score"] for item in data))
    return (ok and expected_total in summary and expected_total in out,
            f"summary={summary[:60]!r} 运行输出含总分 {expected_total}: {expected_total in out}")


def check_parallel(workdir: Path) -> tuple[bool, str]:
    try:
        lines = [ln for ln in read_file(workdir, "combined.txt").splitlines() if ln.strip()]
    except OSError:
        return False, "combined.txt 不存在"
    fizz = lines.count("Fizz")
    buzz = lines.count("Buzz")
    return (len(lines) == 10 and fizz == 5 and buzz == 5), f"{len(lines)} 行（Fizz×{fizz}, Buzz×{buzz}）"


TASKS: list[dict] = [
    {
        "id": "hello", "difficulty": "easy",
        "goal": "在当前工作目录创建 hello.py，运行时打印 Hello, miniMaster!，然后运行验证",
        "check": check_hello,
    },
    {
        "id": "notes", "difficulty": "easy",
        "goal": "在当前工作目录创建 notes.txt，内容恰好三行：第一行 alpha，第二行 beta，第三行 gamma，然后读取验证",
        "check": check_notes,
    },
    {
        "id": "calc", "difficulty": "medium",
        "goal": "在当前工作目录创建 calc.py，实现 add/sub/mul/div 四个函数，并用命令运行断言验证：add(2,3)=5、sub(5,2)=3、mul(3,4)=12、div(8,4)=2",
        "check": check_calc,
    },
    {
        "id": "fizzbuzz", "difficulty": "medium",
        "goal": "创建 fizzbuzz.py：打印 1..100，3 的倍数输出 Fizz，5 的倍数输出 Buzz，两者倍数输出 FizzBuzz；运行它并把全部输出保存到 fizzbuzz_output.txt，验证第 15 行是 FizzBuzz",
        "check": check_fizzbuzz,
    },
    {
        "id": "parallel-files", "difficulty": "medium",
        "goal": "创建两个相互独立的脚本 fizz.py（打印 5 行 Fizz）和 buzz.py（打印 5 行 Buzz）；两者都创建完成后，依次运行它们，把两次运行的输出按顺序一次性合并写入 combined.txt（前 5 行 Fizz、后 5 行 Buzz，共 10 行）",
        "check": check_parallel,
        "overrides": {"parallel": 2},
    },
    {
        "id": "dep-chain", "difficulty": "hard",
        "goal": "分三步：1) 创建 data.json，内容为包含 3 条记录的数组，每条有 name 和 score 字段（score 自定）；2) 创建 read_data.py，读取 data.json 并打印所有 score 的总分；3) 运行 read_data.py，并把它的输出内容保存到 summary.txt",
        "check": check_dep_chain,
    },
    {
        "id": "bugfix", "difficulty": "hard",
        "setup": setup_bugfix,
        "goal": "工作目录里的 sum_to.py 有 bug：sum_to(5) 应返回 15 但实际返回 10。运行它复现问题、定位并修复 bug（保持函数签名不变），然后验证 sum_to(5)==15、sum_to(1)==1、sum_to(10)==55",
        "check": check_bugfix,
    },
    {
        "id": "codebase-grep", "difficulty": "hard",
        "setup": setup_grep,
        "goal": "app/ 目录下有多个 Python 文件，找出所有包含 TODO 注释的文件，把文件路径逐行写入 todos.txt（相对路径，如 app/main.py），然后验证清单完整",
        "check": check_grep,
    },
]

QUICK_COUNT = 4
MAX_TASK_STEPS = 12
MAX_TASK_ROUNDS = 2


def ablation_overrides(ablate: str) -> dict:
    """机制消融：把指定机制放宽到永不触发，用于对照实验。"""
    overrides: dict = {}
    for item in filter(None, ablate.split(",")):
        if item == "guard":
            overrides.update(guard_max_repeat=10**6, guard_window_size=10**6, guard_window_max=10**6)
        elif item == "retry":
            overrides.update(max_retries=0)
        elif item == "budget":
            overrides.update(max_total_tokens=0)
        else:
            raise SystemExit(f"未知消融项: {item}（可选 guard/retry/budget）")
    return overrides


def run_one(task: dict, tag: str, settings: Settings) -> dict:
    ts = time.strftime("%H%M%S")
    workdir = PROJECT_ROOT / "eval" / "workspaces" / f"{tag}-{task['id']}-{ts}"
    run_dir = PROJECT_ROOT / "eval" / "results" / tag / f"{task['id']}-{ts}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)  # setup 植入文件前先建工作区
    if task.get("setup"):
        task["setup"](workdir)

    reporter = Reporter(run_dir=run_dir, quiet=True)
    harness = Harness(settings, workdir, reporter, snapshot_dir=None)
    start = time.time()
    report = harness.run(task["goal"])
    duration = time.time() - start
    check_ok, check_detail = task["check"](workdir)

    counts = reporter.event_counts
    return {
        "task": task["id"],
        "difficulty": task["difficulty"],
        "agent_success": report.success,
        "check_passed": check_ok,
        "success": bool(report.success and check_ok),
        "check_detail": check_detail,
        "rounds": report.rounds_used,
        "attempts": sum(t.attempts for t in report.tasks),
        "requests": report.requests,
        "tokens": report.prompt_tokens + report.completion_tokens,
        "guard_blocks": counts.get("guard_block", 0),
        "retries": counts.get("task_retry_scheduled", 0),
        "replans": counts.get("replan", 0),
        "live_trims": counts.get("live_trim", 0),
        "duration_s": round(duration, 1),
        "trace": str(run_dir / "trace.jsonl"),
    }


def print_table(rows: list[dict], tag: str) -> None:
    console = Console()
    table = Table(title=f"eval 结果 · {tag}")
    for col in ("任务", "难度", "结果", "轮数", "尝试", "Guard", "重试", "tokens", "耗时s"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["task"], r["difficulty"], "✅" if r["success"] else "❌",
            str(r["rounds"]), str(r["attempts"]), str(r["guard_blocks"]),
            str(r["retries"]), str(r["tokens"]), str(r["duration_s"]),
        )
    ok = sum(1 for r in rows if r["success"])
    console.print(table)
    console.print(
        f"[bold]成功率 {ok}/{len(rows)}[/bold] · "
        f"平均轮数 {sum(r['rounds'] for r in rows) / len(rows):.1f} · "
        f"总 tokens {sum(r['tokens'] for r in rows)}"
    )
    for r in rows:
        if not r["success"]:
            console.print(f"  [red]✗ {r['task']}[/red] agent={r['agent_success']} "
                          f"check={r['check_passed']} ({r['check_detail'][:80]})")


def main() -> int:
    parser = argparse.ArgumentParser(description="miniMaster eval 基准")
    parser.add_argument("--suite", choices=["quick", "full"], default="full")
    parser.add_argument("--ablate", default="", help="消融机制: guard,retry,budget（逗号分隔）")
    parser.add_argument("--only", default=None, help="只跑指定任务 id（调试用）")
    parser.add_argument("--tag", default=None, help="结果目录标签（默认带时间戳）")
    args = parser.parse_args()

    tasks = TASKS if args.suite == "full" else TASKS[:QUICK_COUNT]
    if args.only:
        tasks = [t for t in tasks if t["id"] == args.only]
        if not tasks:
            raise SystemExit(f"未找到任务: {args.only}（可选: {', '.join(t['id'] for t in TASKS)}）")
    tag = args.tag or f"{args.suite}{('-no-' + args.ablate.replace(',', '-')) if args.ablate else ''}-{time.strftime('%m%d-%H%M')}"

    base_overrides = {
        "max_rounds": MAX_TASK_ROUNDS,
        "max_steps": MAX_TASK_STEPS,
        **ablation_overrides(args.ablate),
    }
    rows = []
    for task in tasks:
        overrides = {**base_overrides, **task.get("overrides", {})}
        settings = dataclasses.replace(Settings.load(), **overrides)
        print(f"▶ 运行 {task['id']} ({task['difficulty']}) ...", flush=True)
        try:
            rows.append(run_one(task, tag, settings))
        except Exception as exc:
            rows.append({"task": task["id"], "difficulty": task["difficulty"],
                         "success": False, "agent_success": False, "check_passed": False,
                         "check_detail": f"{type(exc).__name__}: {exc}", "rounds": 0,
                         "attempts": 0, "requests": 0, "tokens": 0, "guard_blocks": 0,
                         "retries": 0, "replans": 0, "live_trims": 0, "duration_s": 0,
                         "trace": ""})
        mark = "✅" if rows[-1]["success"] else "❌"
        print(f"  {mark} agent={rows[-1]['agent_success']} check={rows[-1]['check_passed']}", flush=True)

    result_dir = PROJECT_ROOT / "eval" / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    out_path = result_dir / f"{tag}.json"
    out_path.write_text(json.dumps({"tag": tag, "rows": rows}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print_table(rows, tag)
    print(f"结果已保存: {out_path}")
    return 0 if all(r["success"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
