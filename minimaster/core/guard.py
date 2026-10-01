"""重复动作 Guard：连续重复 + 振荡（窗口计数 / 周期循环）三重检测。

防死循环闭环的三条规则：
1. 连续重复：同一指纹连续出现超过 max_repeat 次；
2. 窗口计数：最近 window_size 步内同一指纹累计 ≥ window_max 次（绕开交替的刷步）；
3. 周期检测：末尾出现步长 2~3 的 A→B→A→B 循环（振荡）。

命中任一规则即拦截，并把原因回灌给模型逼迫策略切换。
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass


@dataclass
class GuardVerdict:
    allowed: bool
    repeat_count: int
    message: str | None = None
    rule: str = ""  # consecutive | window | cycle


class RepeatedActionGuard:
    def __init__(
        self,
        max_repeat: int = 2,
        window_size: int = 12,
        window_max: int = 4,
        cycle_periods: tuple[int, ...] = (2, 3),
    ) -> None:
        self.max_repeat = max_repeat
        self.window_size = window_size
        self.window_max = window_max
        self.cycle_periods = cycle_periods
        self._last_fingerprint: str | None = None
        self._consecutive = 0
        self._history: deque[str] = deque(maxlen=window_size)
        self._action_names: deque[str] = deque(maxlen=window_size)

    @staticmethod
    def fingerprint(action: str, args: dict) -> str:
        canonical = json.dumps(
            {"action": action, "args": args}, ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def check(self, action: str, args: dict) -> GuardVerdict:
        fp = self.fingerprint(action, args)
        if fp == self._last_fingerprint:
            self._consecutive += 1
        else:
            self._consecutive = 1
        self._last_fingerprint = fp
        self._history.append(fp)
        self._action_names.append(action)

        # 规则一：连续重复
        if self._consecutive > self.max_repeat:
            return GuardVerdict(
                allowed=False,
                repeat_count=self._consecutive,
                rule="consecutive",
                message=(
                    f"动作 '{action}' 与上一次完全相同，已连续重复 {self._consecutive} 次，"
                    f"超过上限 {self.max_repeat}，本次调用被 Guard 拦截。"
                    "请更换策略：调整参数、换用其他工具，或用 submit_result 汇报结论。"
                ),
            )

        count = self._history.count(fp)

        # 规则二：窗口内高频复现
        if count >= self.window_max:
            return GuardVerdict(
                allowed=False,
                repeat_count=count,
                rule="window",
                message=(
                    f"动作 '{action}'（含相同参数）在最近 {self.window_size} 步内已出现 "
                    f"{count} 次，达到窗口上限 {self.window_max}，疑似低效刷步，已拦截。"
                    "请总结已知信息并改用不同方法，或用 submit_result 收口。"
                ),
            )

        # 规则三：周期性振荡（A→B→A→B）
        period = self._detect_cycle()
        if period is not None:
            return GuardVerdict(
                allowed=False,
                repeat_count=period * 2,
                rule="cycle",
                message=(
                    f"检测到长度 {period} 的动作循环（同样几个调用反复交替），本次调用被 "
                    f"Guard 拦截。请跳出循环：换策略、升级汇报或 submit_result 收口。"
                ),
            )

        return GuardVerdict(allowed=True, repeat_count=self._consecutive)

    def _detect_cycle(self) -> int | None:
        hist = list(self._history)
        for p in self.cycle_periods:
            if len(hist) >= 2 * p and all(
                hist[-1 - i] == hist[-1 - p - i] for i in range(p)
            ):
                return p
        return None

    def reset(self) -> None:
        """任务切换时重置全部指纹状态（跨任务的同参数调用不算死循环）。"""
        self._last_fingerprint = None
        self._consecutive = 0
        self._history.clear()
        self._action_names.clear()
