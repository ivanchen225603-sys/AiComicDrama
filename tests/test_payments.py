"""离线充值订单：创建 / 提交 / 取消 / 回调 / 验签 / 崩溃恢复 / 跨 owner 隔离。

资金口径（全部手算，不用「前后差值」自证）：
- 新账户种子余额恒为 980；
- 到账只经 `DramaService.credit_order`：balance += amount 并写**一条**
  `kind="credit"`、`seed=False` 的流水；
- 守恒：980 + Σcredit(非 seed) − Σsettled cost − retired == balance + frozen。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from comicdrama.app import create_app
from comicdrama.config import Settings
from comicdrama.errors import AppError
from comicdrama.payments import OfflineSimulatedChannel, PaymentService
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

ACCOUNT = "/api/v1/drama/account"
PAY = "/api/v1/drama/payment"
SEED_BALANCE = 980
PASSWORD = "offline-pass-1"
SECRET_ENV = "COMIC_DRAMA_PAYMENT_SECRET"
TEST_SECRET = "offline-test-secret-do-not-use-in-production"


@pytest.fixture
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    # 进程内固定一个已知密钥，测试才能用同一个 HMAC 亲手签一份「负载不一致」的回调。
    monkeypatch.setenv(SECRET_ENV, TEST_SECRET)
    settings = Settings(
        data_dir=tmp_path / "api",
        drama_autopump=False,
        drama_daily_cap=100000,
    )
    app = create_app(settings)
    clients: list[httpx.AsyncClient] = []

    def new_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://127.0.0.1:8001",
        )
        clients.append(client)
        return client

    async with app.router.lifespan_context(app):
        try:
            yield SimpleNamespace(
                app=app,
                new_client=new_client,
                db=tmp_path / "api" / "drama.sqlite",
            )
        finally:
            for client in clients:
                await client.aclose()


def rows(db: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(db: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def error_code(response: httpx.Response) -> str:
    body = response.json()
    return str(body.get("error") or body.get("code") or "")


async def account(api: SimpleNamespace, username: str) -> tuple[httpx.AsyncClient, str]:
    client = api.new_client()
    registered = await client.post(
        f"{ACCOUNT}/register", json={"username": username, "password": PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    owner = str(registered.json()["owner"])
    logged_in = await client.post(
        f"{ACCOUNT}/login", json={"username": username, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text
    return client, owner


async def state(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/api/v1/drama/state")
    assert response.status_code == 200, response.text
    return response.json()


def credits(state_doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [tx for tx in state_doc["ledger"]["tx"] if tx["kind"] == "credit" and not tx["seed"]]


def settled_cost(state_doc: dict[str, Any]) -> int:
    charges = sum(int(c["cost"]) for c in state_doc["charges"] if c["state"] == "settled")
    tasks = sum(int(t["cost"]) for t in state_doc["tasks"] if t["settled"])
    retired = int(state_doc["ledger"].get("retiredCost") or 0)
    return charges + tasks + retired


def assert_conservation(state_doc: dict[str, Any], credited: int) -> None:
    """手算守恒：种子 980 + 非种子充值 − 已结算成本 == 余额 + 冻结。"""
    ledger = state_doc["ledger"]
    expected = SEED_BALANCE + credited - settled_cost(state_doc)
    actual = int(ledger["balance"]) + int(ledger["frozen"])
    assert sum(int(tx["amount"]) for tx in credits(state_doc)) == credited
    assert expected == actual, f"资金守恒被破坏：期望 {expected}，实际 {actual}"


async def new_order(
    client: httpx.AsyncClient,
    amount: int,
    request_id: str,
    expected: int = 201,
) -> httpx.Response:
    response = await client.post(
        f"{PAY}/orders", json={"amount_points": amount, "request_id": request_id}
    )
    assert response.status_code == expected, response.text
    return response


async def submit(client: httpx.AsyncClient, order_id: str, expected: int = 200) -> httpx.Response:
    response = await client.post(f"{PAY}/orders/submit", json={"order_id": order_id})
    assert response.status_code == expected, response.text
    return response


async def cancel(client: httpx.AsyncClient, order_id: str, expected: int = 200) -> httpx.Response:
    response = await client.post(f"{PAY}/orders/cancel", json={"order_id": order_id})
    assert response.status_code == expected, response.text
    return response


async def callback(
    client: httpx.AsyncClient,
    order_id: str,
    supplied: dict[str, Any] | None = None,
    expected: int = 200,
) -> httpx.Response:
    body: dict[str, Any] = {"order_id": order_id}
    if supplied is not None:
        body.update(supplied)
    response = await client.post(f"{PAY}/orders/callback", json=body)
    assert response.status_code == expected, response.text
    return response


async def credited_order(client: httpx.AsyncClient, amount: int, request_id: str) -> dict[str, Any]:
    created = (await new_order(client, amount, request_id)).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)
    return (await callback(client, order_id)).json()


# ---------------------------------------------------------------- 创建 / 幂等
async def test_create_order_returns_an_offline_created_order(api: SimpleNamespace):
    client, _ = await account(api, "pay-create")
    response = await new_order(client, 300, "req-create-1")
    body = response.json()

    assert body["status"] == "created"
    assert body["replayed"] is False
    assert body["amount_points"] == 300
    assert body["request_id"] == "req-create-1"
    assert body["order_id"].startswith("ord-")
    assert body["payment"]["channel"] == "offline-simulated"
    assert body["payment"]["order_id"] == body["order_id"]


async def test_same_request_id_replays_the_same_order_without_a_second_row(
    api: SimpleNamespace,
):
    client, owner = await account(api, "pay-replay")
    first = (await new_order(client, 300, "req-replay-1")).json()

    second = (await new_order(client, 300, "req-replay-1")).json()
    assert second["replayed"] is True
    assert second["order_id"] == first["order_id"]
    stored = rows(api.db, "SELECT * FROM drama_orders WHERE owner=?", (owner,))
    assert len(stored) == 1
    assert stored[0]["amount_points"] == 300


async def test_same_request_id_with_a_different_amount_is_a_conflict(api: SimpleNamespace):
    client, owner = await account(api, "pay-conflict")
    await new_order(client, 300, "req-conflict-1")

    response = await new_order(client, 500, "req-conflict-1", expected=409)
    assert error_code(response) == "payment_request_conflict"
    stored = rows(api.db, "SELECT * FROM drama_orders WHERE owner=?", (owner,))
    assert len(stored) == 1
    assert stored[0]["amount_points"] == 300, "冲突不能改掉原订单的金额"


@pytest.mark.parametrize("amount", [0, -1, True, 30.5])
def test_service_rejects_invalid_amounts(tmp_path: Path, amount: Any):
    drama = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    drama.store.install()
    payments = PaymentService(drama.store, drama)

    with pytest.raises(AppError) as caught:
        payments.create_order("local", amount, "req-invalid-1")
    assert caught.value.code == "payment_amount_invalid"
    assert caught.value.status == 400
    assert rows(tmp_path / "drama.sqlite", "SELECT * FROM drama_orders") == []


@pytest.mark.parametrize("amount", [0, -1])
async def test_http_rejects_out_of_range_amount_with_a_human_message(
    api: SimpleNamespace, amount: Any
):
    """0 / 负数走**服务层**的 400，而不是 pydantic 的 422。

    为什么这一条重要：pydantic 的 `ge`/`le` 只会回 422 `validation_error`，
    前端的 `ERROR_TEXT` 拿不到中文文案，用户会直接看到原始错误码。
    区间判断刻意放在 PaymentService 里，就是让它回 400 `payment_amount_invalid`
    并带上人话；这条用例锁住「文案可达」，不是锁住状态码本身。
    """
    client, _ = await account(api, "pay-schema")
    response = await client.post(
        f"{PAY}/orders", json={"amount_points": amount, "request_id": "req-schema-1"}
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "payment_amount_invalid"
    assert "1 到 100 万" in body["message"]
    assert rows(api.db, "SELECT * FROM drama_orders") == []


@pytest.mark.parametrize("amount", [True, 30.5, "100"])
async def test_http_schema_rejects_non_integer_amount_before_the_service(
    api: SimpleNamespace, amount: Any
):
    """类型错（bool / 浮点 / 字符串）不是「人会用错」，在 schema 层就 422。

    这几个值根本不该走到资金路径，也不需要中文文案——它们是调用方的 bug。
    """
    client, _ = await account(api, "pay-schema-type")
    response = await client.post(
        f"{PAY}/orders", json={"amount_points": amount, "request_id": "req-schema-2"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "validation_error"
    assert rows(api.db, "SELECT * FROM drama_orders") == []


async def test_http_rejects_empty_request_id_with_a_human_message(api: SimpleNamespace):
    """空 request_id 同样走服务层的 400，而不是 422 string_too_short。"""
    client, _ = await account(api, "pay-schema-rid")
    response = await client.post(f"{PAY}/orders", json={"amount_points": 100, "request_id": ""})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "payment_invalid_request"
    assert rows(api.db, "SELECT * FROM drama_orders") == []


# ---------------------------------------------------------------- 状态机
async def test_submit_moves_created_to_pending_and_replays_pending(api: SimpleNamespace):
    client, _ = await account(api, "pay-submit")
    order_id = str((await new_order(client, 300, "req-submit-1")).json()["order_id"])

    first = (await submit(client, order_id)).json()
    assert first["status"] == "pending"

    replay = (await submit(client, order_id)).json()
    assert replay["status"] == "pending"
    assert replay["order_id"] == order_id

    await callback(client, order_id)
    response = await submit(client, order_id, expected=409)
    assert error_code(response) == "payment_order_state"


async def test_cancel_only_applies_to_pending_and_only_once(api: SimpleNamespace):
    client, _ = await account(api, "pay-cancel")
    order_id = str((await new_order(client, 300, "req-cancel-1")).json()["order_id"])

    created = await cancel(client, order_id, expected=409)
    assert error_code(created) == "payment_order_state", "created 不能直接取消"

    await submit(client, order_id)
    cancelled = (await cancel(client, order_id)).json()
    assert cancelled["status"] == "cancelled"

    again = await cancel(client, order_id, expected=409)
    assert error_code(again) == "payment_order_state"


# ---------------------------------------------------------------- 到账 / 幂等 / 验签
async def test_callback_credits_exactly_once_and_conserves_funds(api: SimpleNamespace):
    client, _ = await account(api, "pay-credit")
    before = await state(client)
    assert before["ledger"]["balance"] == SEED_BALANCE
    assert credits(before) == []

    created = (await new_order(client, 300, "req-credit-1")).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)

    body = (await callback(client, order_id)).json()
    assert body["status"] == "credited"
    assert body["amount_points"] == 300

    after = await state(client)
    assert after["ledger"]["balance"] == SEED_BALANCE + 300
    assert after["ledger"]["frozen"] == 0
    new_credits = credits(after)
    assert len(new_credits) == 1, "一次到账只能写一条 credit 流水"
    assert int(new_credits[0]["amount"]) == 300
    assert_conservation(after, 300)

    stored = rows(
        api.db, "SELECT status,paid_at,credited_at FROM drama_orders WHERE id=?", (order_id,)
    )[0]
    assert stored["status"] == "credited"
    assert stored["paid_at"] is not None
    assert stored["credited_at"] is not None


async def test_repeated_callback_is_idempotent(api: SimpleNamespace):
    client, _ = await account(api, "pay-idem")
    created = (await new_order(client, 300, "req-idem-1")).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)
    await callback(client, order_id)

    before = await state(client)
    balance_before = int(before["ledger"]["balance"])
    count_before = len(credits(before))

    again = (await callback(client, order_id)).json()
    assert again["status"] == "credited"
    assert again["order_id"] == order_id

    after = await state(client)
    assert int(after["ledger"]["balance"]) == balance_before
    assert len(credits(after)) == count_before
    assert_conservation(after, 300)


async def test_bad_signature_keeps_the_order_pending_and_the_books_untouched(
    api: SimpleNamespace,
):
    client, owner = await account(api, "pay-sign")
    created = (await new_order(client, 300, "req-sign-1")).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)
    before = await state(client)

    payload = {
        "order_id": order_id,
        "owner": owner,
        "amount_points": 300,
        "request_id": "req-sign-1",
        "event": "payment.paid",
    }
    response = await callback(
        client,
        order_id,
        supplied={"payload": payload, "signature": "deadbeef"},
        expected=401,
    )
    assert error_code(response) == "payment_signature_invalid"

    after = await state(client)
    assert after["ledger"] == before["ledger"]
    assert (
        rows(api.db, "SELECT status FROM drama_orders WHERE id=?", (order_id,))[0]["status"]
        == "pending"
    )


async def test_signed_but_mismatched_payload_is_rejected(api: SimpleNamespace):
    client, owner = await account(api, "pay-mismatch")
    created = (await new_order(client, 300, "req-mismatch-1")).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)
    before = await state(client)

    # 用同一把密钥签一份「金额被改过」的负载：验签通过，但必须被一致性校验挡住。
    tampered = {
        "order_id": order_id,
        "owner": owner,
        "amount_points": 900,
        "request_id": "req-mismatch-1",
        "event": "payment.paid",
    }
    signature = OfflineSimulatedChannel().sign(tampered)
    response = await callback(
        client,
        order_id,
        supplied={"payload": tampered, "signature": signature},
        expected=403,
    )
    assert error_code(response) == "payment_payload_mismatch"

    after = await state(client)
    assert after["ledger"] == before["ledger"]
    assert (
        rows(api.db, "SELECT status FROM drama_orders WHERE id=?", (order_id,))[0]["status"]
        == "pending"
    )


async def test_crash_after_credit_does_not_double_credit_and_finishes_the_order(
    api: SimpleNamespace,
):
    """账本已入账、订单却停在 pending：再次回调必须只推进订单，不能再加钱。"""
    client, _ = await account(api, "pay-crash")
    created = (await new_order(client, 300, "req-crash-1")).json()
    order_id = str(created["order_id"])
    await submit(client, order_id)
    await callback(client, order_id)

    before = await state(client)
    balance_before = int(before["ledger"]["balance"])
    credits_before = len(credits(before))

    execute(
        api.db,
        "UPDATE drama_orders SET status='pending', paid_at=NULL, credited_at=NULL WHERE id=?",
        (order_id,),
    )

    again = (await callback(client, order_id)).json()
    assert again["status"] == "credited"

    after = await state(client)
    assert int(after["ledger"]["balance"]) == balance_before, "不能重复入账"
    assert len(credits(after)) == credits_before, "不能多写一条 credit 流水"
    assert_conservation(after, 300)
    stored = rows(
        api.db, "SELECT status,paid_at,credited_at FROM drama_orders WHERE id=?", (order_id,)
    )[0]
    assert stored["status"] == "credited"
    assert stored["paid_at"] is not None
    assert stored["credited_at"] is not None


# ---------------------------------------------------------------- 隔离 / 上限 / 渠道
async def test_orders_are_isolated_per_owner(api: SimpleNamespace):
    alice, _ = await account(api, "pay-alice")
    bob, _ = await account(api, "pay-bob")
    created = (await new_order(alice, 300, "req-iso-1")).json()
    order_id = str(created["order_id"])
    await submit(alice, order_id)

    for attempt in (
        await bob.post(f"{PAY}/orders/submit", json={"order_id": order_id}),
        await bob.post(f"{PAY}/orders/cancel", json={"order_id": order_id}),
        await bob.post(f"{PAY}/orders/callback", json={"order_id": order_id}),
    ):
        assert attempt.status_code == 404, attempt.text
        assert error_code(attempt) == "payment_order_missing"

    listed = (await bob.get(f"{PAY}/orders")).json()["orders"]
    assert order_id not in [item["order_id"] for item in listed]

    mine = (await alice.get(f"{PAY}/orders")).json()["orders"]
    assert order_id in [item["order_id"] for item in mine]
    assert (
        rows(api.db, "SELECT status FROM drama_orders WHERE id=?", (order_id,))[0]["status"]
        == "pending"
    ), "别人的失败尝试不能改动订单"


async def test_recharge_is_not_capped_by_the_credit_limits(api: SimpleNamespace):
    client, _ = await account(api, "pay-limit")
    limited = await client.post(
        "/api/v1/drama/action",
        json={"type": "saveCreditLimits", "args": [{"total": 1}]},
    )
    assert limited.status_code == 200, limited.text
    assert limited.json()["result"]["total"] == 1

    body = await credited_order(client, 500, "req-limit-1")
    assert body["status"] == "credited"

    after = await state(client)
    assert after["creditLimits"]["total"] == 1, "充值不能顺手改用户设的上限"
    assert after["creditLimits"]["used"] == 0, "充值不是消费，不能计入已用"
    assert after["ledger"]["balance"] == SEED_BALANCE + 500
    assert_conservation(after, 500)


async def test_orders_record_the_offline_channel(api: SimpleNamespace):
    client, owner = await account(api, "pay-channel")
    created = (await new_order(client, 200, "req-channel-1")).json()
    stored = rows(
        api.db, "SELECT channel,status FROM drama_orders WHERE id=?", (created["order_id"],)
    )[0]
    assert stored["channel"] == "offline-simulated"
    assert created["channel"] == "offline-simulated"
    assert (
        rows(api.db, "SELECT owner FROM drama_orders WHERE id=?", (created["order_id"],))[0][
            "owner"
        ]
        == owner
    )
