"""充值订单与支付回调：本期只有**离线模拟渠道**，全程零出网、零真实支付。

边界：
- `drama_orders` 与账户表一样落在同一个 drama.sqlite，不另起 DB；不进
  export_doc / import_doc（回调凭证跨库导出会泄露身份），不进 store.reset() 的
  DELETE 清单（它是账务凭证，不随演示数据重置）。SELECT/DELETE 一律按 owner 隔离。
- 状态机 created -> pending -> paid -> credited；paid -> credited 幂等；
  仅 pending 可取消 -> cancelled。验签失败不改状态、不入账。
- 到账**必须**走 DramaService.credit_order（唯一资金入口）：在该事务里
  balance += amount 且写一条 kind="credit"/seed=False 的流水，然后 _assert_conservation。
  绝不直接 UPDATE drama_ledger 绕过守恒。充值不受用户自设积分上限约束
  （上限只管花钱），不调 _count_spend，也不进 daily_spend 的两个 SUM。
- 密钥只从**环境变量名**读取（COMIC_DRAMA_PAYMENT_SECRET）。未配置时用进程内
  生成的临时密钥；代码里只出现环境变量名，绝不写死密钥，也不写进 .env.example。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from .errors import AppError

if TYPE_CHECKING:
    from .service import DramaService
    from .store import DramaStore

ORDER_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS drama_orders("
    "id TEXT PRIMARY KEY,owner TEXT NOT NULL,amount_points INTEGER NOT NULL,"
    "channel TEXT NOT NULL,status TEXT NOT NULL,request_id TEXT NOT NULL,"
    "created_at REAL NOT NULL,paid_at REAL,credited_at REAL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS drama_orders_request ON drama_orders(owner,request_id)",
    "CREATE INDEX IF NOT EXISTS drama_orders_owner ON drama_orders(owner,created_at)",
)

# 只保存环境变量「名」；真实密钥由进程环境提供，绝不进代码 / .env.example。
PAYMENT_SECRET_ENV = "COMIC_DRAMA_PAYMENT_SECRET"
CHANNEL_NAME = "offline-simulated"
CALLBACK_EVENT = "payment.paid"
MAX_ORDER_POINTS = 1_000_000
ORDER_ID_PREFIX = "ord-"


class PaymentChannel(Protocol):
    """支付渠道端口。本期唯一实现是 OfflineSimulatedChannel。"""

    name: str

    def create_order(
        self, order_id: str, owner: str, amount_points: int, request_id: str
    ) -> dict[str, Any]:
        """返回离线支付参数（不产生真实付款、不发起任何网络请求）。"""
        ...

    def verify_callback(self, payload: dict[str, Any], signature: str) -> bool:
        """HMAC-SHA256 验签；失败时调用方必须原样保留订单状态、不入账。"""
        ...

    def build_callback(self, order: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        """在没有外部回调时，由渠道自己构造一份可验签的回调负载。

        离线模拟渠道才有这个能力（本进程内自签自验）；真实渠道必须返回 `None`，
        因为真实回调只能由支付网关发起——服务端绝不自造一份「已付款」，
        那等于把「没收到钱」写成「收到钱」。
        """
        ...


class OfflineSimulatedChannel:
    """离线模拟渠道：HMAC-SHA256 签名在**服务端**生成并核验。

    浏览器拿不到密钥，因此无法伪造一次成功回调；`/orders/callback` 不传签名时
    由服务端自己构造一份「模拟网关回调」并当场验签，完整走一遍验签链路。
    显式传入 payload/signature 时则按客户端提供的值验签（错误签名 -> 401）。
    """

    name = CHANNEL_NAME

    def __init__(self, env_name: str = PAYMENT_SECRET_ENV) -> None:
        self.env_name = env_name
        configured = os.environ.get(env_name, "").strip()
        # 未配置时生成进程内临时密钥：进程重启后旧签名自然失效，不会留下可用的
        # 长期密钥，也不会误以为「已经接了真实支付」。
        self.ephemeral = not bool(configured)
        self._secret: bytes = configured.encode("utf-8") if configured else secrets.token_bytes(32)

    @staticmethod
    def canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(self._secret, self.canonical(payload), hashlib.sha256).hexdigest()

    def create_order(
        self, order_id: str, owner: str, amount_points: int, request_id: str
    ) -> dict[str, Any]:
        return {
            "channel": self.name,
            "order_id": order_id,
            "amount_points": amount_points,
            "request_id": request_id,
            "status": "created",
            "hint": "本地体验渠道：确认支付后积分自动到账，不产生真实付款",
        }

    def build_callback(self, order: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """构造一次「模拟网关回调」的负载与签名（只在本进程内使用）。"""
        payload = {
            "order_id": str(order["id"]),
            "owner": str(order["owner"]),
            "amount_points": int(order["amount_points"]),
            "request_id": str(order["request_id"]),
            "event": CALLBACK_EVENT,
        }
        return payload, self.sign(payload)

    def verify_callback(self, payload: dict[str, Any], signature: str) -> bool:
        if not isinstance(payload, dict) or not isinstance(signature, str) or not signature:
            return False
        return hmac.compare_digest(self.sign(payload), signature)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class PaymentService:
    """订单状态机 + 到账编排。所有 DB 访问复用 DramaStore 的同一连接/锁。"""

    def __init__(
        self,
        store: DramaStore,
        drama: DramaService,
        channel: PaymentChannel | None = None,
    ) -> None:
        self.store = store
        self.drama = drama
        self.channel: PaymentChannel = channel or OfflineSimulatedChannel()

    @staticmethod
    def order_view(row: dict[str, Any]) -> dict[str, Any]:
        """只返回页面需要的字段：不把 owner 之类的内部字段带进页面。"""
        return {
            "order_id": str(row["id"]),
            "amount_points": int(row["amount_points"]),
            "channel": str(row["channel"]),
            "status": str(row["status"]),
            "request_id": str(row["request_id"]),
            "created_at": float(row["created_at"]),
            "paid_at": row["paid_at"],
            "credited_at": row["credited_at"],
        }

    @staticmethod
    def credited_view(row: dict[str, Any]) -> dict[str, Any]:
        """回调首次结果与重放结果**同形**：都由订单行派生，不重复入账。"""
        return {
            "order_id": str(row["id"]),
            "status": "credited",
            "amount_points": int(row["amount_points"]),
            "channel": str(row["channel"]),
            "paid_at": row["paid_at"],
            "credited_at": row["credited_at"],
            "message": "充值已到账，积分已增加",
        }

    def _read_order(self, owner: str, order_id: str) -> dict[str, Any] | None:
        row = self.store.read(
            lambda db: db.execute(
                "SELECT * FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
        )
        return _row_dict(row) if row is not None else None

    def create_order(self, owner: str, amount_points: int, request_id: str) -> dict[str, Any]:
        if type(amount_points) is not int or not 1 <= amount_points <= MAX_ORDER_POINTS:
            raise AppError("payment_amount_invalid", "充值积分必须是 1 到 100 万之间的整数", 400)
        rid = str(request_id or "").strip()
        if not rid or len(rid) > 64:
            raise AppError("payment_invalid_request", "充值请求编号不能为空且不超过 64 位", 400)
        order_id = ORDER_ID_PREFIX + uuid.uuid4().hex[:16]
        now = time.time()
        try:
            self.store.run(
                lambda db: db.execute(
                    "INSERT INTO drama_orders(id,owner,amount_points,channel,status,"
                    "request_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (order_id, owner, amount_points, self.channel.name, "created", rid, now),
                )
            )
        except sqlite3.IntegrityError as exc:
            # 唯一索引 (owner, request_id)。重复下单**不报错**：同一个 requestId 就是
            # 同一笔意图书，重放原订单，双击 / 网络重试不会凭空多出一笔待支付订单。
            existing = self.store.read(
                lambda db: db.execute(
                    "SELECT * FROM drama_orders WHERE owner=? AND request_id=?", (owner, rid)
                ).fetchone()
            )
            if existing is None:
                # 撞了唯一索引却查不到同一 requestId：不是可复放的重复，如实报冲突。
                raise AppError(
                    "payment_duplicate_request", "该充值请求已提交，请勿重复下单", 409
                ) from exc
            row = _row_dict(existing)
            if int(row["amount_points"]) != amount_points:
                # 同一 requestId 换个金额：不猜用户想要哪笔，直接拒绝。
                raise AppError(
                    "payment_request_conflict",
                    "该充值请求编号已用于另一笔金额，请重新发起",
                    409,
                ) from exc
            params = self.channel.create_order(
                str(row["id"]), owner, int(row["amount_points"]), rid
            )
            return {**self.order_view(row), "payment": params, "replayed": True}
        order = self._read_order(owner, order_id)
        assert order is not None  # 刚插入且同一连接，必然可读
        params = self.channel.create_order(order_id, owner, amount_points, rid)
        return {**self.order_view(order), "payment": params, "replayed": False}

    def list_orders(self, owner: str) -> dict[str, Any]:
        rows = self.store.read(
            lambda db: db.execute(
                "SELECT * FROM drama_orders WHERE owner=? ORDER BY created_at DESC,rowid DESC "
                "LIMIT 30",
                (owner,),
            ).fetchall()
        )
        return {"orders": [self.order_view(_row_dict(row)) for row in rows]}

    def submit_order(self, owner: str, order_id: str) -> dict[str, Any]:
        """created -> pending（幂等：已经是 pending 就回放当前状态）。"""

        def op(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute(
                "SELECT * FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
            if row is None:
                return {"error": "missing"}
            if str(row["status"]) == "pending":
                return {"row": _row_dict(row)}
            if str(row["status"]) != "created":
                return {"error": "state"}
            cursor = db.execute(
                "UPDATE drama_orders SET status='pending' "
                "WHERE id=? AND owner=? AND status='created'",
                (order_id, owner),
            )
            if cursor.rowcount != 1:
                return {"error": "state"}
            fresh = db.execute(
                "SELECT * FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
            return {"row": _row_dict(fresh)}

        result = self.store.run(op)
        if result.get("error") == "missing":
            raise AppError("payment_order_missing", "订单不存在", 404)
        if result.get("error") == "state":
            raise AppError("payment_order_state", "该订单当前状态不能发起支付", 409)
        return self.order_view(result["row"])

    def cancel_order(self, owner: str, order_id: str) -> dict[str, Any]:
        """仅 pending 可取消 -> cancelled。"""

        def op(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute(
                "SELECT * FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
            if row is None:
                return {"error": "missing"}
            if str(row["status"]) != "pending":
                return {"error": "state"}
            cursor = db.execute(
                "UPDATE drama_orders SET status='cancelled' "
                "WHERE id=? AND owner=? AND status='pending'",
                (order_id, owner),
            )
            if cursor.rowcount != 1:
                return {"error": "state"}
            fresh = db.execute(
                "SELECT * FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
            return {"row": _row_dict(fresh)}

        result = self.store.run(op)
        if result.get("error") == "missing":
            raise AppError("payment_order_missing", "订单不存在", 404)
        if result.get("error") == "state":
            raise AppError("payment_order_state", "只有待支付的订单可以取消", 409)
        return self.order_view(result["row"])

    async def handle_callback(
        self, owner: str, order_id: str, supplied: dict[str, Any] | None
    ) -> dict[str, Any]:
        """验签 -> pending -> paid -> credited，并把到账与状态更新放进**同一个事务**。

        重放：订单已经是 credited 时直接返回与首次同形的结果，不再碰账本。
        验签 / 负载一致性全部在任何写库之前完成，失败时订单状态与余额都不变。
        """
        order = await asyncio.to_thread(self._read_order, owner, order_id)
        if order is None:
            raise AppError("payment_order_missing", "订单不存在", 404)
        if str(order["status"]) == "credited":
            return self.credited_view(order)
        if str(order["status"]) != "pending":
            raise AppError("payment_order_state", "该订单当前状态不能确认支付", 409)

        if supplied is not None:
            payload = supplied.get("payload")
            signature = str(supplied.get("signature") or "")
            if not isinstance(payload, dict):
                raise AppError("payment_signature_invalid", "支付回调验签失败，订单未变动", 401)
        else:
            built = self.channel.build_callback(order)
            if built is None:
                # 真实渠道不自造回调：没收到网关通知就是没付钱，必须让支付方重发。
                raise AppError(
                    "payment_callback_required",
                    "该支付渠道需要由支付方发起回调，请稍后重试",
                    400,
                )
            payload, signature = built

        if not self.channel.verify_callback(payload, signature):
            raise AppError("payment_signature_invalid", "支付回调验签失败，订单未变动", 401)
        if (
            str(payload.get("order_id") or "") != order_id
            or str(payload.get("owner") or "") != owner
            or payload.get("amount_points") != int(order["amount_points"])
            or str(payload.get("request_id") or "") != str(order["request_id"])
            or str(payload.get("event") or "") != CALLBACK_EVENT
        ):
            raise AppError("payment_payload_mismatch", "支付回调内容与订单不一致", 403)

        amount = int(order["amount_points"])
        rid = str(order["request_id"])

        def mark_credited(db: sqlite3.Connection) -> None:
            # 并发重放：两个回调同时到达时第二个走到这里，订单已经是 credited。
            # 此时账本那边按 requestId 判重后没有重复入账，这里也必须是无副作用
            # 的 no-op，否则会把一次合法的重复回调变成 409，反而让支付方以为失败。
            current = db.execute(
                "SELECT status FROM drama_orders WHERE id=? AND owner=?", (order_id, owner)
            ).fetchone()
            if current is None:
                raise AppError("payment_order_missing", "订单不存在", 404)
            if str(current["status"]) == "credited":
                return
            if str(current["status"]) != "pending":
                raise AppError("payment_order_state", "订单状态已变化，请刷新后重试", 409)
            now = time.time()
            paid = db.execute(
                "UPDATE drama_orders SET status='paid',paid_at=? "
                "WHERE id=? AND owner=? AND status='pending'",
                (now, order_id, owner),
            )
            if paid.rowcount != 1:
                raise AppError("payment_order_state", "订单状态已变化，请刷新后重试", 409)
            credited = db.execute(
                "UPDATE drama_orders SET status='credited',credited_at=? "
                "WHERE id=? AND owner=? AND status='paid'",
                (now, order_id, owner),
            )
            if credited.rowcount != 1:
                raise AppError("payment_order_state", "订单状态已变化，请刷新后重试", 409)

        # 唯一的资金入口：余额、credit 流水、守恒自检与订单状态在同一个事务里提交。
        # 任一步失败（包括 mark_credited 抛错）都会整笔 ROLLBACK，不留半截到账。
        await self.drama.credit_order(owner, amount, rid, mark_credited)
        fresh = await asyncio.to_thread(self._read_order, owner, order_id)
        return self.credited_view(fresh if fresh is not None else order)
