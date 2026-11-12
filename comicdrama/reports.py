"""按日对账报表与定价建议：**纯函数、只读、绝不改价**。

为什么单独一个模块：`reconcile.py` 回答的是「两侧账本能不能合并」（答案是
不能，单位不同），这里回答的是「漫剧侧这本账，按天看售价与上游实扣差多少、
哪几笔缺数」。两件事的服务对象不同（前者给用户看，后者只给管理员看），
混在一个函数里会让「只读一致性」这条边界变得难验证。

三条口径，写在这里以免后来者改坏：

1. **未知 != 0。** `upstreamPoints` 为 `None` 表示「上游没回传」，不能当 0 求和，
   否则毛利凭空变高。因此 `marginPoints` 只覆盖「上游已知」的那几笔，
   并把覆盖了多少售价（`coveredSalePoints`）一起暴露出来，让读表的人知道
   「这个毛利率是基于多少比例的单算出来的」。
2. **mock 缺数与真实缺数是两种东西。** 本地模拟渠道根本没有上游账单，
   把它算进「缺数」会让真正的对账缺口永远淹没在噪声里。所以：
   `mockOrders` 单独计数，`actionableMissing` 只收真实渠道的缺数。
3. **建议不是改价。** `pricing_suggestion()` 只返回数字与依据，`applied` 恒为
   `False`。改价必须由管理员在「系统管理 → 定价」里显式保存，并且仍然走
   `savePricing` 的管理员闸门。任何「顺手把建议写进配置」的改动都是错的。
"""

from __future__ import annotations

import math
import time
from typing import Any

from .pricing import ACTION_PRICES

# 与 Provider.name 的离线标识严格等值：`source_provider` 存的是 Provider.name 原值。
MOCK_PROVIDER = "mock"
# 目标毛利率：与当前定价同源（售价 = 上游成本 / (1 - 0.2)，例：250 -> 300）。
TARGET_MARGIN = 0.2
# 样本数低于这个值就标 lowConfidence：一单两单算出来的「均价」不能当定价依据。
MIN_SAMPLES_FOR_CONFIDENCE = 3

REASON_MOCK = "本地模拟渠道，没有上游账单"
REASON_REAL = "上游未回传实际扣费"


def _int_or_none(value: Any) -> int | None:
    """上游积分口径：None / 负数 / bool / 非整数一律算「未知」，绝不当 0。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _source_of(charge: dict[str, Any]) -> str:
    """这笔凭据是哪个 Provider 干的。空串 = 老库，按「真实」处理（与日上限同口径）。"""
    return str(charge.get("sourceProvider") or "")


def _is_mock(charge: dict[str, Any]) -> bool:
    return _source_of(charge) == MOCK_PROVIDER


def _blank_bucket() -> dict[str, Any]:
    return {
        "orders": 0,
        "salePoints": 0,
        "upstreamPoints": 0,
        "upstreamKnown": 0,
        "upstreamUnknown": 0,
        "coveredSalePoints": 0,
        "mockOrders": 0,
        "actionableMissing": 0,
        "missing": [],
    }


def _absorb(bucket: dict[str, Any], charge: dict[str, Any], points: int | None) -> None:
    sale = int(charge.get("cost") or 0)
    bucket["orders"] += 1
    bucket["salePoints"] += sale
    if _is_mock(charge):
        bucket["mockOrders"] += 1
    if points is None:
        bucket["upstreamUnknown"] += 1
        if not _is_mock(charge):
            bucket["actionableMissing"] += 1
        bucket["missing"].append(
            {
                "id": charge.get("id"),
                "action": charge.get("action"),
                "salePoints": sale,
                "sourceProvider": _source_of(charge) or "unknown",
                "reason": REASON_MOCK if _is_mock(charge) else REASON_REAL,
            }
        )
        return
    bucket["upstreamKnown"] += 1
    bucket["upstreamPoints"] += points
    bucket["coveredSalePoints"] += sale


def _finish(bucket: dict[str, Any]) -> dict[str, Any]:
    """把累计量算成比率。毛利率只按「上游已知」的那部分算，分母同步缩小。"""
    covered = int(bucket["coveredSalePoints"])
    margin = covered - int(bucket["upstreamPoints"])
    bucket["marginPoints"] = margin
    bucket["marginRate"] = round(margin / covered, 4) if covered > 0 else None
    return bucket


def _settled(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (state.get("charges") or []) if c.get("state") == "settled"]


def daily_report(
    state: dict[str, Any],
    *,
    days: int = 30,
    today: str | None = None,
) -> dict[str, Any]:
    """按日汇总本地售价与上游实扣，列差异与缺数项。

    `days` 只影响返回多少天：报表按 `day` 倒序取前 `days` 条，不做「窗口内重新
    求和」——汇总永远覆盖**全部**已结算凭据，这样 totals 与逐日表加总必然自洽。
    要看区间，读 `byDay` 自己加；不要指望 totals 跟着窗口变，那会让
    「总数对不上逐日加总」变成一个说不清的问题。

    `today` 只用于给「今天」这一行做标记，不参与任何计算（方便测试固定时间）。
    """
    if days < 1:
        raise ValueError("days 必须 >= 1")
    day_key_today = today or time.strftime("%Y-%m-%d", time.localtime())

    totals = _blank_bucket()
    grouped: dict[str, dict[str, Any]] = {}
    by_provider: dict[str, dict[str, Any]] = {}
    undated: list[dict[str, Any]] = []

    for charge in _settled(state):
        points = _int_or_none(charge.get("upstreamPoints"))
        _absorb(totals, charge, points)
        source = _source_of(charge) or "unknown"
        provider_bucket = by_provider.setdefault(source, _blank_bucket())
        _absorb(provider_bucket, charge, points)
        day = str(charge.get("day") or "").strip()
        if not day:
            # 老库没有 day：单独列出来，不塞进任何一天，否则「今天」会莫名其妙变大。
            undated.append(charge)
            continue
        bucket = grouped.setdefault(day, _blank_bucket())
        _absorb(bucket, charge, points)

    rows = []
    for day in sorted(grouped, reverse=True):
        row = _finish(grouped[day])
        row["day"] = day
        row["isToday"] = day == day_key_today
        rows.append(row)
    rows = rows[:days]

    totals = _finish(totals)
    for bucket in by_provider.values():
        _finish(bucket)

    return {
        "unit": "积分（100 积分 = 1 元）",
        "days": days,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "totals": totals,
        "byDay": rows,
        "byProvider": by_provider,
        # 老库无日期的凭据：如实列出来，不猜一个日期把它们塞进某天。
        "undatedCount": len(undated),
        "undatedSalePoints": sum(int(c.get("cost") or 0) for c in undated),
        "pricingSuggestion": pricing_suggestion(state),
        "note": (
            "本表只读，不写任何一张表。上游实扣未知的凭据已单列，未按 0 计入毛利；"
            "「本地模拟」与「真实渠道缺数」分开计数，前者没有上游账单可对。"
        ),
    }


def pricing_suggestion(state: dict[str, Any]) -> dict[str, Any]:
    """基于**已记录的上游实扣**给出定价建议，`applied` 恒为 False。

    只采信真实渠道（`sourceProvider != "mock"`）且上游回报了积分的已结算凭据：
    mock 的 upstreamPoints 是 None，根本没有「实扣」可言；老库空 sourceProvider
    按真实处理，与日上限口径一致。样本不足时仍然给数，但标 `lowConfidence`，
    并且 `basis` 会把样本数写清楚——让人能自己判断该不该信。
    """
    samples: dict[str, list[int]] = {}
    for charge in _settled(state):
        if _is_mock(charge):
            continue
        points = _int_or_none(charge.get("upstreamPoints"))
        if points is None or points == 0:
            # 0 是「上游免费」这个已知事实，对定价没有参考价值（不能推出售价），
            # 因此不进样本；但它也不算缺数，缺数在 daily_report 里另行统计。
            continue
        samples.setdefault(str(charge.get("action") or ""), []).append(points)

    items: list[dict[str, Any]] = []
    for action in sorted(ACTION_PRICES):
        current = int(ACTION_PRICES[action])
        values = samples.get(action) or []
        if not values:
            items.append(
                {
                    "action": action,
                    "currentPrice": current,
                    "avgUpstreamPoints": None,
                    "suggestedPrice": None,
                    "delta": None,
                    "samples": 0,
                    "lowConfidence": True,
                    "reason": "没有上游实扣记录，无法给出建议",
                }
            )
            continue
        avg = sum(values) / len(values)
        # 反推售价：售价 × (1 - 目标毛利率) >= 上游成本，向上取整，保证毛利不跌破目标。
        suggested = max(1, math.ceil(avg / (1 - TARGET_MARGIN)))
        items.append(
            {
                "action": action,
                "currentPrice": current,
                "avgUpstreamPoints": round(avg, 2),
                "suggestedPrice": suggested,
                "delta": suggested - current,
                "samples": len(values),
                "lowConfidence": len(values) < MIN_SAMPLES_FOR_CONFIDENCE,
                "reason": "按已记录上游实扣反推，含目标毛利率",
            }
        )

    changed = [item for item in items if item["delta"]]
    return {
        "applied": False,
        "requiresConfirmation": True,
        "targetMargin": TARGET_MARGIN,
        "unit": "积分（100 积分 = 1 元）",
        "items": items,
        "changedCount": len(changed),
        "note": (
            "建议只是数字，不会自动改价。改价必须由管理员在「系统管理」里显式保存，"
            "并且只改 pricing 单一源；本函数被调用一千次也不会写库。"
        ),
    }


def report_csv(report: dict[str, Any]) -> str:
    """把日报表导成 CSV（逐日一行）。只导出汇总列，不导出凭据 id 明细。

    刻意不导出 `missing` 明细：那里面是内部凭据 id，导出到文件后容易外流，
    而管理员真正需要的是「哪天缺了几笔、缺在哪个渠道」。
    """
    header = [
        "date",
        "orders",
        "sale_points",
        "upstream_points",
        "covered_sale_points",
        "margin_points",
        "margin_rate",
        "upstream_known",
        "upstream_unknown",
        "mock_orders",
        "actionable_missing",
    ]
    lines = [",".join(header)]
    for row in report.get("byDay") or []:
        rate = row.get("marginRate")
        lines.append(
            ",".join(
                [
                    str(row.get("day") or ""),
                    str(row.get("orders") or 0),
                    str(row.get("salePoints") or 0),
                    str(row.get("upstreamPoints") or 0),
                    str(row.get("coveredSalePoints") or 0),
                    str(row.get("marginPoints") or 0),
                    "" if rate is None else f"{float(rate):.4f}",
                    str(row.get("upstreamKnown") or 0),
                    str(row.get("upstreamUnknown") or 0),
                    str(row.get("mockOrders") or 0),
                    str(row.get("actionableMissing") or 0),
                ]
            )
        )
    return "\n".join(lines) + "\n"
