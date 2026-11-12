"""充值订单 HTTP 边界。身份一律取服务端 principal（Cookie 会话 / local / bearer），
请求体里不允许传 owner；所有订单查询与变更在 PaymentService 内按 owner 隔离。

刻意不写 `from __future__ import annotations`：与 drama/http.py 同一原因，
FastAPI 需要在这里直接拿到 Annotated[Principal, Depends(...)] 的真实类型。
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import Field

from .models import Principal, StrictModel
from .payments import PaymentService


class CreateOrderInput(StrictModel):
    # 数值/长度区间**刻意不在这一层收**：pydantic 的 ge/le 只会返回 422
    # `validation_error`，前端的 ERROR_TEXT 拿不到中文文案，用户会看到原始错误码。
    # 区间判断交给 PaymentService.create_order —— 它抛 400 `payment_amount_invalid` /
    # `payment_invalid_request`，文案是人话且已在 ERROR_TEXT 里配好。
    # 类型仍然严格（bool / 浮点 / 字符串一律 422），那不是人会用错的东西。
    amount_points: int
    request_id: str = Field(max_length=64)


class OrderIdInput(StrictModel):
    order_id: str = Field(min_length=1, max_length=64)


class CallbackInput(StrictModel):
    order_id: str = Field(min_length=1, max_length=64)
    # 不传 = 服务端构造一次「模拟网关回调」并当场验签；传了 = 按提供的值验签
    # （错误签名一律 401，用于验证「验签失败不改状态、不入账」）。
    payload: dict[str, Any] | None = None
    signature: str | None = None


def mount_payment_routes(app: Any, payments: PaymentService, principal: Any) -> None:
    router = APIRouter(prefix="/api/v1/drama/payment", tags=["drama-payment"])
    identity_dep = Annotated[Principal, Depends(principal)]

    @router.post("/orders", status_code=201)
    async def create_order(body: CreateOrderInput, identity: identity_dep):
        return await asyncio.to_thread(
            payments.create_order, identity.owner, body.amount_points, body.request_id
        )

    @router.get("/orders")
    async def list_orders(identity: identity_dep):
        return await asyncio.to_thread(payments.list_orders, identity.owner)

    @router.post("/orders/submit")
    async def submit_order(body: OrderIdInput, identity: identity_dep):
        return await asyncio.to_thread(payments.submit_order, identity.owner, body.order_id)

    @router.post("/orders/cancel")
    async def cancel_order(body: OrderIdInput, identity: identity_dep):
        return await asyncio.to_thread(payments.cancel_order, identity.owner, body.order_id)

    @router.post("/orders/callback")
    async def callback(body: CallbackInput, identity: identity_dep):
        supplied: dict[str, Any] | None = None
        if body.payload is not None or body.signature is not None:
            supplied = {"payload": body.payload, "signature": body.signature or ""}
        return await payments.handle_callback(identity.owner, body.order_id, supplied)

    app.include_router(router)
