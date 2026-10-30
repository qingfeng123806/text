"""时钟抽象：真实时钟与虚拟时钟。

为什么需要虚拟时钟 —— "工具超时后 Agent 会不会重复退款"这条规则，
如果靠真实 sleep 来制造超时，一个用例就要等好几秒，几十个用例跑一轮就要几分钟，
而且 sleep 的抖动会让同一份代码在不同机器上跑出不同结果。

虚拟时钟下耗时是精确累加的，既把跑批压到毫秒级，又让 trace 里的 duration_ms
完全可复现。切 `--clock real` 即可回到真实计时。
"""

from __future__ import annotations

import time


class Clock:
    """时钟接口。"""

    def now_ms(self) -> int:
        raise NotImplementedError

    def sleep_ms(self, ms: int) -> None:
        raise NotImplementedError


class RealClock(Clock):
    def now_ms(self) -> int:
        return int(time.perf_counter() * 1000)

    def sleep_ms(self, ms: int) -> None:
        if ms > 0:
            time.sleep(ms / 1000.0)


class VirtualClock(Clock):
    """确定性时钟。sleep_ms 只推进虚拟时间，不真的等待。"""

    def __init__(self, start_ms: int = 1_700_000_000_000):
        self._now = start_ms
        self._slept = 0

    def now_ms(self) -> int:
        return self._now

    def sleep_ms(self, ms: int) -> None:
        if ms > 0:
            self._now += ms
            self._slept += ms

    def advance(self, ms: int) -> None:
        self.sleep_ms(ms)

    @property
    def total_slept_ms(self) -> int:
        return self._slept


def make_clock(mode: str = "virtual") -> Clock:
    if mode == "real":
        return RealClock()
    if mode == "virtual":
        return VirtualClock()
    raise ValueError(f"未知时钟模式: {mode!r}（可选 virtual / real）")
