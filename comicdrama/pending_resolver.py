"""待确认积分的解析端口：按上游任务编号问一次「这笔生成到底成了没有」。

背景：超时 / 连不上时服务层只落一条 `pending_review` 凭据并保留冻结，钱可能已经
付给上游。以前只有管理员 `resolveCharge` 能收口；本期加一条**自动**出口：凭据带着
上游任务编号时，`tick` 会周期性地问一次解析器，`succeeded` / `failed` 复用
`resolveCharge` 的资金路径，`still_running` / `unknown` 保持待复核并在页面上如实说明。

事实分级（勿越界）：
- `OfflinePendingChargeResolver`：本期唯一实现。查本地模拟结果，确定性、无出网、
  无真实密钥；查不到就返回 `unknown`，**绝不用 pricing.py 的预算常量回填
  upstream_points / account_balance**（那是「估」，不是「实」）。
- `RemotePendingChargeResolver`：真实 Provider 的**接口占位**，NOT_IMPLEMENTED。
  待付费开通上游账号并完成公网部署后才能实现查询端点、鉴权与计费口径；本期不
  写任何猜测性的 HTTP 调用，也不读任何真实凭据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

PendingState = Literal["succeeded", "failed", "still_running", "unknown"]


@dataclass(frozen=True)
class PendingOutcome:
    """一次查询结果。

    `upstream_points` / `account_balance` 为 None 表示「不知道」，必须原样保持 None；
    `succeeded` 也不能凭售价或预算常量推一个数字出来。
    """

    outcome: PendingState
    upstream_points: int | None = None
    account_balance: int | None = None


class PendingChargeResolver(Protocol):
    """按上游任务编号解析结果的端口。实现必须无副作用、可被重复调用（幂等）。"""

    def resolve(self, upstream_task_id: str) -> PendingOutcome: ...


class OfflinePendingChargeResolver:
    """确定性本地模拟：预置什么就返回什么，没预置就是 unknown。

    `record()` 只用于离线演示与测试注入结果（例如把某个编号标成 succeeded），
    不是「编造上游真值」：真实编号对应的结果由部署方在付费实测后填入。
    """

    def __init__(self, results: dict[str, PendingOutcome] | None = None) -> None:
        self._results: dict[str, PendingOutcome] = dict(results or {})

    def record(self, upstream_task_id: str, outcome: PendingOutcome) -> None:
        self._results[str(upstream_task_id)] = outcome

    def resolve(self, upstream_task_id: str) -> PendingOutcome:
        return self._results.get(str(upstream_task_id), PendingOutcome("unknown"))


class RemotePendingChargeResolver:
    """NOT_IMPLEMENTED：真实上游结果查询，待付费 / 待公网部署。

    真实实现需要：上游任务查询端点与鉴权、按 generateUuid 查终态、读取自报的
    pointsCost / accountBalance、处理限流与重试、并把「查不到」明确区分成 unknown
    而不是失败。这些都必须以首次付费实测的响应形状为准；本期不臆造、不出网、
    不读任何真实密钥。保留这个类只是把接口位置钉住，避免以后把逻辑塞进
    `_dispatch_inner` 里。
    """

    def resolve(self, upstream_task_id: str) -> PendingOutcome:
        raise NotImplementedError(
            "NOT_IMPLEMENTED: real upstream task lookup requires a paid upstream account "
            "and a public deployment; use OfflinePendingChargeResolver in this stage"
        )
