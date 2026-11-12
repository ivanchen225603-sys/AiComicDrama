"""积分账本的只读概览：余额、冻结、已结算成本、上游成本与毛利、待复核凭据。

只读，不写任何一张表；管理员据此判断待复核的凭据该退款还是该结算。
"""

from __future__ import annotations

import time
from typing import Any

# 展示口径：100 积分 = 1 元。
POINTS_PER_CNY = 100


def reconcile_doc(
    owner: str,
    drama_state: dict[str, Any],
    *,
    daily_cap: int = 0,
    daily_used: int = 0,
) -> dict[str, Any]:
    """把某个账号的账本整理成一份只读概览。"""
    ledger = drama_state.get("ledger") or {}
    all_charges = list(drama_state.get("charges") or [])
    charges = [c for c in all_charges if c.get("state") == "settled"]

    def points_of(charge: dict[str, Any]) -> int | None:
        """上游自报积分；None = 未知（老库 / mock / 上游没回传），绝不当 0。"""
        value = charge.get("upstreamPoints")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    known = [(c, points_of(c)) for c in charges]
    known = [(c, points) for c, points in known if points is not None]
    settled_cost = sum(int(c.get("cost") or 0) for c in charges)
    unknown_upstream = len(charges) - len(known)
    upstream_total = sum(points for _, points in known if points is not None)
    # 毛利 = cost - upstream_points（售价减上游成本，单位都是积分，100 积分 = 1 元）。
    # 不存字段、只在这里算：它是纯函数，存了会在改价后失真；两份真相必然漂移。
    # 上游未知的凭据**排除在外**，不按 0 求和——否则毛利会被凭空抬高。
    margin_total = sum(int(c.get("cost") or 0) - int(points or 0) for c, points in known)

    drama = {
        "unit": "整数积分",
        "storage": "drama.sqlite::drama_ledger",
        "balance": int(ledger.get("balance") or 0),
        "frozen": int(ledger.get("frozen") or 0),
        "retiredCost": int(ledger.get("retiredCost") or 0),
        "totalBalance": int(ledger.get("balance") or 0) + int(ledger.get("frozen") or 0),
        "settledCharges": len(charges),
        "settledCost": settled_cost,
        # 上游真实成本（与售价分开记）：只汇总上游明确回报过积分的那几笔。
        # 键名与同对象里的 settledCost / dailyCap / cnyHint 保持一致用 camelCase：
        # 混两套命名会让前端读错键名后静默拿到 undefined，再被 `|| 0` 兜成
        # 一个看起来正常的 0 —— 和「把 NULL 当 0」是同一类事故，只是发生在传输层。
        "upstreamPointsTotal": upstream_total,
        # 已结算但上游成本未知的笔数：这个数字必须显式暴露，
        # 否则「毛利为零」可能只是「全都不知道」。
        "unknownUpstreamCount": unknown_upstream,
        "marginPointsTotal": margin_total,
        "marginNote": (
            f"毛利 = cost - upstream_points（积分，100 积分 = 1 元）；"
            f"已排除 {unknown_upstream} 笔上游成本未知的凭据，未按 0 计入。"
        ),
        # 「钱花了、结果不明」：待复核凭据。管理员正是靠这一条判断该退款还是
        # 该结算，所以单独列出来，不混进上面的汇总。
        # 注意：**列表包含上游成本未知的待复核**（upstreamPoints 为 NULL）。
        # 它们同样要人工处理，只是属于「不知道扣没扣」那一类；不要「顺手」
        # 过滤成只留有钱的，否则这类凭据会从管理页消失、钱永远悬着。
        "pendingReview": [
            {
                "id": c.get("id"),
                "action": c.get("action"),
                "cost": int(c.get("cost") or 0),
                "upstreamPoints": points_of(c),
            }
            for c in all_charges
            if c.get("state") == "pending_review"
        ],
        "dailyCap": int(daily_cap),
        "dailyUsed": int(daily_used),
        "cnyHint": round((int(ledger.get("balance") or 0)) / POINTS_PER_CNY, 2),
        "pricingSource": "本地定价表 ACTION_PRICES（不是上游真实账单）",
    }

    return {
        "owner": owner,
        "pointsPerCny": POINTS_PER_CNY,
        "drama": drama,
        "note": "本视图只读，不写任何一张表。",
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
