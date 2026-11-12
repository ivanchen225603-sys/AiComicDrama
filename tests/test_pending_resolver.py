"""待确认凭据的解析端口与自动结算：确定性、幂等、不把已知抹成未知。

场景用「直接在 state 里落一条 pending_review 凭据」构造（不走真实 Provider），
但**绝不**用 `UPSTREAM_COST_T2I` / `UPSTREAM_COST_VIDEO` 之类的预算常量回填
`upstreamPoints`——那是「估」，测试里用了就等于把未知伪装成已知。

资金全部手算：种子 980，预占 cap 后 balance=980-cap / frozen=cap；
succeeded 后 balance=980-cap+(cap-price) / frozen=0 / cost=price；
failed 后 balance=980 / frozen=0 / cost=0。
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama.pending_resolver import (
    OfflinePendingChargeResolver,
    PendingOutcome,
    RemotePendingChargeResolver,
)
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

OWNER = "local"
SEED_BALANCE = 980
PRICE = 100
CAP = 120
UPSTREAM_ID = "up-task-250"


@pytest.fixture
def service(tmp_path: Path) -> DramaService:
    svc = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({OWNER}),
        autopump=False,
    )
    svc.store.install()
    return svc


def today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def write_state(svc: DramaService, state: dict[str, Any], owner: str = OWNER) -> None:
    svc.store.run(lambda db: svc.store.replace_in(db, owner, state))


def seed_pending(
    svc: DramaService,
    *,
    charge_id: str = "CH-pending-1",
    price: int = PRICE,
    cap: int = CAP,
    upstream_task_id: str | None = None,
    upstream_points: int | None = None,
) -> dict[str, Any]:
    """落一条「上游结果未知」的待复核凭据：冻结 cap，余额同步减少 cap。"""
    state = svc.store.state(OWNER)
    assert state["ledger"]["balance"] == SEED_BALANCE
    assert state["ledger"]["frozen"] == 0
    state["ledger"]["balance"] -= cap
    state["ledger"]["frozen"] += cap
    state["charges"].append(
        {
            "id": charge_id,
            "projectId": None,
            "action": "image-to-video",
            "requestId": "req-pending-1",
            "price": price,
            "cap": cap,
            "cost": 0,
            "state": "pending_review",
            "result": None,
            "note": "上游结果未知：预占保留",
            "createdAt": time.time(),
            "day": today(),
            "target": "",
            "upstreamTaskId": upstream_task_id,
            "sourceProvider": "",
            "upstreamPoints": upstream_points,
            "upstreamBalance": None,
        }
    )
    write_state(svc, state)
    return state


def charge_of(state: dict[str, Any], charge_id: str = "CH-pending-1") -> dict[str, Any]:
    return next(c for c in state["charges"] if c["id"] == charge_id)


def transactions(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """只看本次结算新写的流水：种子流水（seed=True）是开户自带的历史账。"""
    return [tx for tx in state["ledger"]["tx"] if tx["kind"] == kind and not tx.get("seed")]


# ---------------------------------------------------------------- 解析端口
def test_offline_resolver_defaults_to_unknown_and_replays_recorded_results():
    resolver = OfflinePendingChargeResolver()
    assert resolver.resolve("up-unknown") == PendingOutcome("unknown")
    assert resolver.resolve("up-unknown").upstream_points is None

    outcome = PendingOutcome("succeeded", upstream_points=250, account_balance=700)
    resolver.record("up-1", outcome)
    assert resolver.resolve("up-1") == outcome
    assert resolver.resolve("up-1").upstream_points == 250
    assert resolver.resolve("up-2") == PendingOutcome("unknown")


def test_remote_resolver_is_an_explicit_placeholder():
    with pytest.raises(NotImplementedError) as caught:
        RemotePendingChargeResolver().resolve("up-1")
    assert "NOT_IMPLEMENTED" in str(caught.value)


# ---------------------------------------------------------------- 自动结算
def test_pending_charge_without_an_upstream_task_id_is_never_settled(
    service: DramaService,
):
    seed_pending(service, upstream_task_id=None)
    service.set_pending_resolver(
        OfflinePendingChargeResolver({"": PendingOutcome("succeeded", 250, 700)})
    )
    before = service.store.state(OWNER)

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 0

    assert state == before, "没有编号就没有查询对象，不能凭猜测结算"
    charge = charge_of(state)
    assert charge["state"] == "pending_review"
    assert charge["cost"] == 0
    assert state["ledger"]["frozen"] == CAP
    assert state["ledger"]["balance"] == SEED_BALANCE - CAP


def test_auto_settle_succeeded_charges_the_price_and_keeps_the_upstream_truth(
    service: DramaService,
):
    seed_pending(service, upstream_task_id=UPSTREAM_ID)
    service.set_pending_resolver(
        OfflinePendingChargeResolver(
            {UPSTREAM_ID: PendingOutcome("succeeded", upstream_points=250, account_balance=700)}
        )
    )

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 1
    write_state(service, state)

    after = service.store.state(OWNER)
    charge = charge_of(after)
    assert charge["state"] == "settled"
    assert charge["cost"] == PRICE
    assert after["ledger"]["frozen"] == 0
    assert after["ledger"]["balance"] == SEED_BALANCE - CAP + (CAP - PRICE), "只退差额"
    assert charge["upstreamPoints"] == 250
    assert charge["upstreamBalance"] == 700
    assert int(after["creditLimits"]["used"]) == PRICE, "_count_spend 必须生效"
    assert service.store.daily_spend(OWNER, today()) == PRICE
    DramaService._assert_conservation(after)
    assert len(transactions(after, "settle")) == 1
    assert len(transactions(after, "refund")) == 1


def test_auto_settle_failed_releases_everything_and_never_counts_as_spend(
    service: DramaService,
):
    seed_pending(service, upstream_task_id=UPSTREAM_ID)
    service.set_pending_resolver(
        OfflinePendingChargeResolver({UPSTREAM_ID: PendingOutcome("failed")})
    )

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 1
    write_state(service, state)

    after = service.store.state(OWNER)
    charge = charge_of(after)
    assert charge["state"] == "released"
    assert charge["cost"] == 0
    assert after["ledger"]["frozen"] == 0
    assert after["ledger"]["balance"] == SEED_BALANCE, "预占必须一分不少地回满"
    assert int(after["creditLimits"].get("used") or 0) == 0, "退款不能计入已用"
    assert service.store.daily_spend(OWNER, today()) == 0
    released = transactions(after, "release")
    assert len(released) == 1
    assert int(released[0]["amount"]) == CAP
    DramaService._assert_conservation(after)


@pytest.mark.parametrize("outcome", ["still_running", "unknown"])
def test_auto_settle_leaves_the_charge_alone_when_the_outcome_is_unclear(
    service: DramaService, outcome: str
):
    seed_pending(service, upstream_task_id=UPSTREAM_ID)
    service.set_pending_resolver(
        OfflinePendingChargeResolver({UPSTREAM_ID: PendingOutcome(outcome)})
    )
    before = service.store.state(OWNER)

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 0

    assert state == before, "没结论就不能动状态、冻结与流水"
    assert charge_of(state)["state"] == "pending_review"
    assert state["ledger"]["frozen"] == CAP
    assert len(state["ledger"]["tx"]) == len(before["ledger"]["tx"])


def test_auto_settle_is_idempotent(service: DramaService):
    seed_pending(service, upstream_task_id=UPSTREAM_ID)
    service.set_pending_resolver(
        OfflinePendingChargeResolver({UPSTREAM_ID: PendingOutcome("succeeded", 250, 700)})
    )

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 1
    write_state(service, state)

    again = service.store.state(OWNER)
    ledger_before = copy.deepcopy(again["ledger"])
    assert service._auto_settle_pending_charges(again) == 0
    assert again["ledger"] == ledger_before, "第二次扫描必须零副作用"
    assert again["ledger"]["balance"] == SEED_BALANCE - CAP + (CAP - PRICE)
    assert len(transactions(again, "settle")) == 1
    assert len(transactions(again, "refund")) == 1


def test_settlement_never_downgrades_a_known_upstream_cost_to_unknown(
    service: DramaService,
):
    seed_pending(service, upstream_task_id=UPSTREAM_ID, upstream_points=250)
    service.set_pending_resolver(
        # 本次查询没读到 points：outcome 为 succeeded 但 upstream_points is None。
        OfflinePendingChargeResolver({UPSTREAM_ID: PendingOutcome("succeeded")})
    )

    state = service.store.state(OWNER)
    assert service._auto_settle_pending_charges(state) == 1
    write_state(service, state)

    after = service.store.state(OWNER)
    charge = charge_of(after)
    assert charge["state"] == "settled"
    assert charge["upstreamPoints"] == 250, "已知的 250 不能被一次「没读到」抹成 None"
    assert charge["upstreamBalance"] is None


async def test_sweep_only_scans_owners_with_a_task_id(service: DramaService, tmp_path: Path):
    seed_pending(service, upstream_task_id=None)
    before = service.store.state(OWNER)
    db_path = tmp_path / "drama.sqlite"
    wildcard = [db_path, Path(f"{db_path}-wal")]
    snapshot = {path: path.read_bytes() for path in wildcard if path.exists()}

    assert await service.sweep_pending_charges() == 0

    assert service.store.state(OWNER) == before
    after_bytes = {path: path.read_bytes() for path in wildcard if path.exists()}
    assert after_bytes == snapshot, "没有编号的 owner 连字节都不该被扫动"


def test_settle_pending_charge_is_a_no_op_outside_pending_review(
    service: DramaService,
):
    seed_pending(service, upstream_task_id=UPSTREAM_ID)
    state = service.store.state(OWNER)
    charge = charge_of(state)
    charge["state"] = "settled"
    charge["cost"] = PRICE
    before = copy.deepcopy(state)

    assert service._settle_pending_charge(state, charge, "succeeded") is False
    assert state == before, "非 pending_review 的凭据不能被动第二次"

    assert service._settle_pending_charge(state, charge, "failed") is False
    assert state == before
