"""积分账本的读取端口：从 `drama_ledger` / `drama_tx` 两张表读出某个账号的账本视图。

`state["ledger"]` 就是这里返回的文档；换成其他账本实现时只需实现 `DramaWallet`。
"""

import sqlite3
from typing import Any, Protocol

# 新账号的初始积分，也是守恒自检的基数。
SEED_POINTS = 980


class DramaWallet(Protocol):
    """在既有事务里读取某 owner 的账本视图。"""

    def ledger(self, db: sqlite3.Connection, owner: str) -> dict[str, Any]:
        """返回 {"balance": int, "frozen": int, "retiredCost": int, "tx": [...]}，单位积分。"""
        ...


class LegacyWallet:
    """默认实现：读 drama_ledger + drama_tx。"""

    def ledger(self, db: sqlite3.Connection, owner: str) -> dict[str, Any]:
        tx = [
            {
                "id": x["id"],
                "seed": bool(x["seed"]),
                "time": x["created_at"],
                "kind": x["kind"],
                "label": x["label"],
                "amount": x["amount"],
                "note": x["note"],
                "projectId": x["project_id"],
                "taskId": x["task_id"],
            }
            for x in db.execute("SELECT * FROM drama_tx WHERE owner=? ORDER BY rowid", (owner,))
        ]
        row = db.execute("SELECT * FROM drama_ledger WHERE owner=?", (owner,)).fetchone()
        if row is None:
            # 开户由 ensure_owner 负责；这里不造默认值，免得把「漏开户」悄悄变成初始积分。
            raise KeyError(f"drama ledger for {owner!r} is not seeded")
        # retired_cost 是「已删除项目里那些已结算成本」的累计值。任务行随项目删除后
        # 成本不再可枚举，守恒自检需要这个基数，否则删一次项目就会永久 409。
        retired = row["retired_cost"] if "retired_cost" in row.keys() else 0
        return {
            "balance": row["balance"],
            "frozen": row["frozen"],
            "retiredCost": int(retired or 0),
            "tx": tx,
        }
