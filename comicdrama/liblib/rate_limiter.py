"""上游节流：官方 /apis 页面条款为 QPS-1、并发-5。

全局令牌间隔 + 并发信号量，两个维度都按客户端整体计，而不是按单个任务。
退避 jitter 只用于 poll/fetch 的可重试读，submit 永不重试。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


def bounded_backoff(attempt: int, *, base: float = 0.2, cap: float = 2.0) -> float:
    """有界指数退避 + 抖动，避免多客户端同步重试。"""
    return min(cap, base * (2**attempt)) * (0.8 + 0.4 * random.random())


class RateLimiter:
    def __init__(self, *, qps: float = 1.0, concurrency: int = 5):
        if qps <= 0 or type(concurrency) is not int or concurrency < 1:
            raise ValueError("qps must be positive and concurrency a positive integer")
        self.qps = qps
        self.concurrency = concurrency
        self._interval = 1.0 / qps
        self._semaphore = asyncio.Semaphore(concurrency)
        self._gate = asyncio.Lock()
        self._next_at = 0.0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._semaphore:
            async with self._gate:
                now = time.monotonic()
                delay = max(0.0, self._next_at - now)
                # 先预约时隙再 sleep，保证并发下仍严格满足 QPS。
                self._next_at = max(now, self._next_at) + self._interval
            if delay > 0:
                await asyncio.sleep(delay)
            yield
