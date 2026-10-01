"""Bash 工具：subprocess 执行 shell 命令（danger=guarded，带危险命令拦截）。

Windows 上自动探测 Git Bash（bash.exe），找不到回落 cmd /c。
cwd 跨步持久化（bash 通过尾部标记回传最终目录；cmd 用延迟扩展 !CD!），
且始终被限制在 ToolContext.workdir 沙箱内。
超时采用进程树清理：bash 启动的孙进程（如 python server.py）一并终止，
不留后台残留。
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .base import BaseTool, ToolContext, ToolResult, ToolSpec

_BASH_CWD_MARKER = "__MINIMASTER_CWD__"
_CMD_CWD_MARKER = "__MINIMASTER_CWD__"

# 系统级破坏命令拦截（尽力而为的黑名单，命中即拒绝执行）
_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"rm\s+(-{1,2}[\w-]+\s+)*/(?:\s|$|\*)"), "递归删除根目录"),
    (re.compile(r"rm\s+(-{1,2}[\w-]+\s+)*~(?:/|\s|$)"), "递归删除用户主目录"),
    (re.compile(r"\bmkfs\b", re.I), "格式化文件系统"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "格式化磁盘"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "关机/重启系统"),
    (re.compile(r"\bdd\s+[^\n]*of=/dev/", re.I), "dd 直写设备"),
    (re.compile(r"\bdel\s+/s\b", re.I), "Windows 递归删除"),
    (re.compile(r"\brmdir\s+/s\b", re.I), "Windows 递归删除目录"),
    (
        re.compile(r"remove-item\s+[^\n]*-recurse[^\n]*[a-z]:\\", re.I),
        "PowerShell 递归删除盘符路径",
    ),
    (re.compile(r"\bchmod\s+(-\w+\s+)?777\s+/(?:\s|$)"), "chmod 777 根目录"),
    (
        re.compile(r"(curl|wget)\s+[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b", re.I),
        "下载脚本直接执行",
    ),
    (re.compile(r">\s*/dev/sd[a-z]", re.I), "直写块设备"),
]


def find_bash() -> str | None:
    exe = shutil.which("bash")
    if exe:
        return exe
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


class BashTool(BaseTool):
    spec = ToolSpec(
        name="bash",
        description=(
            "在工作目录中执行一条 shell 命令并返回 stdout/stderr/退出码。"
            "适合运行脚本、程序化检查、批处理等。工作目录跨步保持（cd 生效）。"
            "系统级破坏性命令（格式化、删根目录、关机等）会被安全拦截。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 60，上限 300",
                },
            },
            "required": ["command"],
        },
        danger="guarded",
    )

    def __init__(self) -> None:
        self._bash = find_bash()

    def is_dangerous(self, args: dict) -> str | None:
        command = str(args.get("command", ""))
        for pattern, label in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                return f"命令命中危险模式[{label}]: {command[:120]}"
        return None

    @staticmethod
    def _to_posix(path: Path) -> str:
        s = str(path).replace("\\", "/")
        if len(s) > 2 and s[1] == ":":
            return "/" + s[0].lower() + s[2:]
        return s

    @staticmethod
    def _from_posix(s: str) -> Path | None:
        s = s.strip()
        if re.match(r"^/[A-Za-z]/", s):
            return Path(s[1].upper() + ":" + s[2:])
        if re.match(r"^[A-Za-z]:[/\\]", s):
            return Path(s)
        return None

    def _current_cwd(self, ctx: ToolContext) -> str:
        raw = ctx.session.get("cwd") or str(ctx.workdir)
        try:
            return str(ctx.resolve(raw))
        except (PermissionError, OSError, ValueError):
            return str(ctx.workdir)

    def _adopt_cwd(self, ctx: ToolContext, raw: str | None) -> None:
        new_cwd = self._from_posix(raw or "")
        if new_cwd is None:
            return
        try:
            ctx.resolve(str(new_cwd))  # 逃逸沙箱的 cd 一律拒绝
            ctx.session["cwd"] = str(new_cwd)
        except (PermissionError, OSError, ValueError):
            ctx.session["cwd"] = str(ctx.workdir)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """终止整个进程树（bash 的孙进程一并清理）。"""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def _execute(self, argv: list[str], cwd: str, timeout: float):
        """Popen + communicate：超时自己控制进程树清理（run 的内置 kill 只杀直接子进程）。"""
        popen_kwargs: dict = dict(
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)
        try:
            out, err = proc.communicate(timeout=timeout)
            return out or "", err or "", proc.returncode, False
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            out, err = proc.communicate()
            return out or "", err or "", proc.returncode, True

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        command = args["command"]
        timeout = min(int(args.get("timeout", 60)), 300)
        cwd = self._current_cwd(ctx)

        if self._bash:
            posix_cwd = self._to_posix(Path(cwd))
            wrapped = (
                f"cd '{posix_cwd}' && {{ {command}\n"
                f"}}\n"
                f'__mm_exit=$?; printf "\\n{_BASH_CWD_MARKER}%s" "$(pwd)"; exit $__mm_exit'
            )
            argv = [self._bash, "-c", wrapped]
        else:
            # cmd 延迟扩展：先存退出码，再回传最终目录
            wrapped = (
                f"({command}) & set __MM_RC=!errorlevel! & echo {_CMD_CWD_MARKER}!CD!"
                " & exit /b !__MM_RC!"
            )
            argv = ["cmd", "/v:on", "/c", wrapped]

        stdout, stderr, returncode, timed_out = self._execute(argv, cwd, timeout)
        if timed_out:
            return ToolResult(
                ok=False,
                output="",
                error=f"命令超时（>{timeout}s），已终止整个进程树: {command[:200]}",
            )

        marker = _BASH_CWD_MARKER if self._bash else _CMD_CWD_MARKER
        if marker in stdout:
            stdout, _, tail = stdout.rpartition(marker)
            self._adopt_cwd(ctx, tail.strip().splitlines()[0] if tail.strip() else "")

        parts = [f"$ {command}"]
        if stdout.strip():
            parts.append(stdout.rstrip())
        if stderr:
            parts.append(f"[stderr]\n{stderr.rstrip()}")
        parts.append(f"[exit code: {returncode}]")
        return ToolResult(
            ok=returncode == 0,
            output="\n".join(parts),
            error=None if returncode == 0 else f"退出码 {returncode}",
        )
