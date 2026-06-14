"""
throttle.py
===========
传输限速：令牌桶(token bucket)算法实现。

原理：以恒定速率 rate(字节/秒) 往桶里加令牌，每发送/接收 N 字节就消耗 N 个
令牌；令牌不足时按需 sleep，从而把平均速率限制在 rate 附近。rate<=0 表示不限速。

用于"可选的传输限速"，也方便在性能测试里人为制造带宽瓶颈、观察不同
并发模型在受限带宽下的表现。
"""

from __future__ import annotations
import time


class Throttle:
    def __init__(self, rate: int):
        self.rate = rate                 # 字节/秒；<=0 表示不限速
        self._allowance = float(rate)    # 当前可用令牌（字节）
        self._last = time.monotonic()

    def consume(self, nbytes: int) -> None:
        """消耗 nbytes 个令牌；不足则阻塞等待，实现平滑限速。"""
        if self.rate <= 0:
            return
        now = time.monotonic()
        # 按经过的时间补充令牌
        self._allowance += (now - self._last) * self.rate
        self._last = now
        # 令牌上限为 1 秒的量，避免长时间空闲后突发
        if self._allowance > self.rate:
            self._allowance = float(self.rate)
        self._allowance -= nbytes
        if self._allowance < 0:
            # 令牌透支，按透支量换算成需要等待的时间
            sleep_for = -self._allowance / self.rate
            time.sleep(sleep_for)
            self._allowance = 0.0
            self._last = time.monotonic()
