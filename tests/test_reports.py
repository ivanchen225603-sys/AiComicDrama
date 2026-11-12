"""按日对账报表与定价建议的纯函数契约。

这组用例守的是三条钱的口径，不是实现细节：

1. 未知 != 0。上游没回传的凭据不能被当 0 求和，否则毛利凭空变高。
2. mock 缺数 != 真实缺数。模拟渠道没有上游账单，把它算进「缺数」会让
   真正的对账缺口永远淹没在噪声里。
3. 建议 != 改价。`pricingSuggestion` 只返回数字，`applied` 恒为 False，
   被调用多少次都不会写任何配置。
"""

from __future__ import annotations

import copy

import pytest

from comicdrama.pricing import ACTION_PRICES
from comicdrama.reports import (
    REASON_MOCK,
    REASON_REAL,
    daily_report,
    pricing_suggestion,
    report_csv,
)


def _charge(
    cid: str,
    *,
    action: str = "image-to-video",
    cost: int = 300,
    day: str = "2026-11-12",
    source: str = "remote",
    upstream: int | None = 250,
    state: str = "settled",
) -> dict:
    return {
        "id": cid,
        "action": action,
        "cost": cost,
        "state": state,
        "day": day,
        "sourceProvider": source,
        "upstreamPoints": upstream,
    }


def _state(*charges: dict) -> dict:
    return {"charges": list(charges)}


def test_unknown_upstream_is_not_summed_as_zero():
    """一笔已知 250、一笔未知：上游合计只能是 250，毛利只按已知那笔算。"""
    report = daily_report(
        _state(
            _charge("c1", cost=300, upstream=250),
            _charge("c2", cost=300, upstream=None),
        ),
        today="2026-11-12",
    )
    totals = report["totals"]
    assert totals["orders"] == 2
    assert totals["salePoints"] == 600
    assert totals["upstreamPoints"] == 250
    assert totals["upstreamKnown"] == 1
    assert totals["upstreamUnknown"] == 1
    # 毛利率的分母必须同步缩到「已知那部分售价」，不是 600。
    assert totals["coveredSalePoints"] == 300
    assert totals["marginPoints"] == 50
    assert totals["marginRate"] == pytest.approx(0.1667, abs=1e-4)


def test_mock_missing_is_separated_from_real_missing():
    """mock 的缺数不进 actionableMissing，但要出现在 missing 明细里并注明原因。"""
    report = daily_report(
        _state(
            _charge("m1", action="generateAssetCandidates", cost=30, source="mock", upstream=None),
            _charge("r1", cost=300, source="remote", upstream=None),
        ),
        today="2026-11-12",
    )
    totals = report["totals"]
    assert totals["mockOrders"] == 1
    assert totals["upstreamUnknown"] == 2
    assert totals["actionableMissing"] == 1
    reasons = {item["id"]: item["reason"] for item in totals["missing"]}
    assert reasons["m1"] == REASON_MOCK
    assert reasons["r1"] == REASON_REAL
    assert report["byProvider"]["mock"]["actionableMissing"] == 0
    assert report["byProvider"]["remote"]["actionableMissing"] == 1


def test_empty_source_provider_counts_as_real():
    """空 sourceProvider 是老库：与日上限同口径，按真实处理。"""
    report = daily_report(_state(_charge("old", source="", upstream=None)))
    assert report["totals"]["actionableMissing"] == 1
    assert report["byProvider"]["unknown"]["orders"] == 1


def test_only_settled_charges_are_reported():
    """in_flight / pending_review / released 都不是「花了钱」，一律不进报表。"""
    report = daily_report(
        _state(
            _charge("s1"),
            _charge("f1", state="in_flight", upstream=None),
            _charge("p1", state="pending_review", upstream=None),
            _charge("x1", state="released", cost=0, upstream=None),
        )
    )
    assert report["totals"]["orders"] == 1
    assert [c["id"] for c in report["totals"]["missing"]] == []


def test_days_are_descending_and_truncated():
    state = _state(
        _charge("a", day="2026-11-10"),
        _charge("b", day="2026-11-12"),
        _charge("c", day="2026-11-11"),
    )
    report = daily_report(state, days=2, today="2026-11-12")
    assert [row["day"] for row in report["byDay"]] == ["2026-11-12", "2026-11-11"]
    # totals 覆盖全部已结算凭据，不随窗口变化——否则「总数对不上逐日加总」无法解释。
    assert report["totals"]["orders"] == 3
    assert report["byDay"][0]["isToday"] is True
    assert report["byDay"][1]["isToday"] is False


def test_days_must_be_positive():
    with pytest.raises(ValueError):
        daily_report(_state(), days=0)


def test_undated_charges_are_listed_not_folded_into_today():
    """老库无 day 的凭据不能塞进「今天」，否则今天会莫名变大且无法追溯。"""
    report = daily_report(
        _state(
            _charge("today", day="2026-11-12"),
            _charge("ancient", day="", cost=300),
        ),
        today="2026-11-12",
    )
    assert report["undatedCount"] == 1
    assert report["undatedSalePoints"] == 300
    assert [row["day"] for row in report["byDay"]] == ["2026-11-12"]
    assert report["byDay"][0]["orders"] == 1
    # 总量仍然包含它（总量是「这本账」，逐日表是「有日期的部分」）。
    assert report["totals"]["orders"] == 2


def test_daily_rows_sum_to_totals_for_dated_charges():
    """逐日加总必须等于 totals 减去 undated：这是报表自洽的唯一可验证关系。"""
    state = _state(
        _charge("a", day="2026-11-11", cost=300, upstream=250),
        _charge("b", day="2026-11-12", cost=300, upstream=250),
        _charge("c", day="2026-11-12", action="generateAssetCandidates", cost=30, upstream=25),
        _charge("d", day="", cost=300, upstream=250),
    )
    report = daily_report(state, today="2026-11-12")
    dated_orders = sum(row["orders"] for row in report["byDay"])
    dated_sale = sum(row["salePoints"] for row in report["byDay"])
    assert dated_orders + report["undatedCount"] == report["totals"]["orders"]
    assert dated_sale + report["undatedSalePoints"] == report["totals"]["salePoints"]


def test_report_is_pure_and_does_not_mutate_state():
    state = _state(_charge("c1"), _charge("c2", upstream=None))
    before = copy.deepcopy(state)
    daily_report(state)
    pricing_suggestion(state)
    assert state == before


def test_pricing_suggestion_never_applies():
    suggestion = pricing_suggestion(_state(_charge("c1", upstream=250)))
    assert suggestion["applied"] is False
    assert suggestion["requiresConfirmation"] is True


def test_pricing_suggestion_ignores_mock_samples():
    """mock 没有上游实扣，不能拿去反推售价。"""
    suggestion = pricing_suggestion(
        _state(
            _charge("m1", source="mock", upstream=999),
            _charge("m2", source="mock", upstream=999),
        )
    )
    video = next(item for item in suggestion["items"] if item["action"] == "image-to-video")
    assert video["samples"] == 0
    assert video["suggestedPrice"] is None
    assert video["reason"] == "没有上游实扣记录，无法给出建议"


def test_pricing_suggestion_excludes_free_and_unknown_from_samples():
    """0 = 上游免费（已知但推不出售价），None = 未知；两者都不进样本。"""
    suggestion = pricing_suggestion(
        _state(
            _charge("free", upstream=0),
            _charge("unknown", upstream=None),
        )
    )
    video = next(item for item in suggestion["items"] if item["action"] == "image-to-video")
    assert video["samples"] == 0
    assert video["suggestedPrice"] is None


def test_pricing_suggestion_math_keeps_target_margin():
    """250 的上游成本、20% 目标毛利：售价至少 313（ceil(250/0.8)），不能跌破。"""
    suggestion = pricing_suggestion(
        _state(
            _charge("a", upstream=250),
            _charge("b", upstream=250),
            _charge("c", upstream=250),
        )
    )
    video = next(item for item in suggestion["items"] if item["action"] == "image-to-video")
    assert video["avgUpstreamPoints"] == 250
    assert video["suggestedPrice"] == 313
    assert video["suggestedPrice"] * (1 - suggestion["targetMargin"]) >= 250
    assert video["lowConfidence"] is False
    assert video["samples"] == 3


def test_pricing_suggestion_marks_low_confidence_below_three_samples():
    suggestion = pricing_suggestion(_state(_charge("a", upstream=250)))
    video = next(item for item in suggestion["items"] if item["action"] == "image-to-video")
    assert video["lowConfidence"] is True


def test_pricing_suggestion_covers_every_priced_action():
    """每个有定价的动作都要有一行，包括「没有样本」的那些——不能只列有数据的。"""
    suggestion = pricing_suggestion(_state())
    actions = {item["action"] for item in suggestion["items"]}
    assert actions == set(ACTION_PRICES)


def test_pricing_suggestion_does_not_touch_action_prices():
    before = dict(ACTION_PRICES)
    pricing_suggestion(_state(_charge("a", upstream=250)))
    assert dict(ACTION_PRICES) == before


def test_report_csv_header_and_rows():
    report = daily_report(
        _state(
            _charge("a", day="2026-11-12", upstream=250),
            _charge("b", day="2026-11-11", upstream=None),
            _charge("c", day="2026-11-11", source="mock", cost=30, upstream=None),
        ),
        today="2026-11-12",
    )
    lines = report_csv(report).rstrip("\n").split("\n")
    assert lines[0] == (
        "date,orders,sale_points,upstream_points,covered_sale_points,margin_points,"
        "margin_rate,upstream_known,upstream_unknown,mock_orders,actionable_missing"
    )
    assert len(lines) == 1 + len(report["byDay"])
    assert lines[1].startswith("2026-11-12,1,300,250,300,50,0.1667,")
    # marginRate 为 None 的那天留空，不写 0——0% 毛利和「算不出来」是两件事。
    assert ",,0,0," in lines[2] or lines[2].split(",")[6] == ""


def test_report_csv_has_no_internal_charge_ids():
    """导出文件不带内部凭据 id：管理员要的是「哪天缺几笔」，不是 id 清单。"""
    report = daily_report(_state(_charge("secret-charge-id", upstream=None)))
    assert "secret-charge-id" not in report_csv(report)
