"""运行上报：rich 终端实时输出 + runs/<ts>/trace.jsonl 轨迹落盘（可复盘）。

- trace.jsonl 写全量 payload（完整观察/参数），终端只显示 240 字符预览；
- emit / stream_text 均带锁，支持并行任务下的多线程安全；
- stream_text 打印模型流式增量，下一次事件前自动补换行。
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path

from rich.console import Console

_ROLE_STYLE = {
    "PLANNER": "magenta",
    "EXECUTOR": "cyan",
    "VALIDATOR": "yellow",
    "HARNESS": "bold white",
    "GUARD": "red",
    "POLICY": "red",
    "MEMORY": "blue",
}

_PAYLOAD_PREVIEW = 240


class Reporter:
    def __init__(self, run_dir: Path | None = None, quiet: bool = False) -> None:
        self.console = Console(highlight=False, soft_wrap=True)
        self.quiet = quiet
        self._trace_path: Path | None = None
        self._lock = threading.Lock()
        self._stream_open = False
        self.event_counts: Counter[str] = Counter()  # 各事件触发次数（eval 指标用）
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            self._trace_path = run_dir / "trace.jsonl"

    def emit(self, event: str, payload: dict | None = None) -> None:
        payload = payload or {}
        with self._lock:
            self.event_counts[event] += 1
        self._write_trace(event, payload)
        if self.quiet:
            return
        role = str(payload.get("role", "HARNESS")).upper()
        style = _ROLE_STYLE.get(role, "white")
        ts = time.strftime("%H:%M:%S")
        preview = self._preview(payload)
        with self._lock:
            self._flush_stream()
            self.console.print(f"[dim]{ts}[/dim] [{style}]{role:<9}[/{style}] {event}  {preview}")

    def stream_text(self, text: str) -> None:
        """流式增量输出（dim 弱化），在下一个事件前由 _flush_stream 补换行。"""
        if self.quiet or not text:
            return
        with self._lock:
            self._stream_open = True
            self.console.print(text, end="", style="dim")

    def banner(self, text: str) -> None:
        if not self.quiet:
            with self._lock:
                self._flush_stream()
                self.console.rule(f"[bold]{text}")

    def final(self, text: str, success: bool) -> None:
        color = "bold green" if success else "bold red"
        if not self.quiet:
            with self._lock:
                self._flush_stream()
                self.console.print(f"\n[{color}]{text}[/{color}]")

    def _flush_stream(self) -> None:
        if self._stream_open:
            self.console.print()
            self._stream_open = False

    def _write_trace(self, event: str, payload: dict) -> None:
        if self._trace_path is None:
            return
        record = {"ts": time.time(), "event": event, **payload}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock, self._trace_path.open("a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _preview(payload: dict) -> str:
        parts = []
        for key, value in payload.items():
            if key == "role":
                continue
            text = str(value).replace("\n", " ⏎ ")
            if len(text) > _PAYLOAD_PREVIEW:
                text = text[:_PAYLOAD_PREVIEW] + f"...(共{len(text)}字符)"
            parts.append(f"{key}={text}")
        return " ".join(parts)
