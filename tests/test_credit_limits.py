"""用户自设积分使用上限：单次上限 / 总量上限 / 未设置时回落到账户余额。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.pricing import CREDIT_LIMIT_MAX
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService


@pytest.fixture
def service(tmp_path: Path):
    svc = DramaService(
        tmp_path / "drama.sqlite",
        MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    svc.store.install()
    return svc


async def act(service: DramaService, name: str, *args: Any):
    return await service.action("local", name, list(args), None, None)


async def limits(service: DramaService, **opts: Any) -> dict[str, Any]:
    return (await act(service, "saveCreditLimits", opts))["result"]


async def refused(service: DramaService, name: str, *args: Any) -> str:
    with pytest.raises(AppError) as caught:
        await act(service, name, *args)
    return caught.value.code


def view(state: dict[str, Any]) -> dict[str, Any]:
    return DramaService.credit_limit_view(state)


async def first_shot(service: DramaService) -> str:
    state = await service.state("local")
    proj = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
    return proj["shots"][0]["id"]


@pytest.mark.needs_ffmpeg
async def test_unset_limits_fall_back_to_the_whole_balance(service: DramaService):
    state = await service.state("local")
    v = view(state)
    assert v["single"] is None and v["total"] is None
    assert v["usable"] == state["ledger"]["balance"]
    await act(service, "image-to-video", await first_shot(service))


async def test_zero_single_limit_blocks_every_paid_action_but_not_free_ones(service: DramaService):
    await limits(service, single=0, total="")
    before = (await service.state("local"))["ledger"]
    assert await refused(service, "script-doctor") == "credit_single_limit"
    await act(service, "preview-film")  # 免费动作不受上限约束
    after = await service.state("local")
    assert after["ledger"]["balance"] == before["balance"], "被拒时不能动账本"
    assert after["ledger"]["frozen"] == 0
    assert not [c for c in after["charges"] if c["action"] == "script-doctor"]


async def test_single_limit_compares_the_unit_price(service: DramaService):
    await limits(service, single=2)
    await act(service, "script-doctor")  # 单价 2，刚好等于上限
    assert (
        await refused(service, "image-to-video", await first_shot(service)) == "credit_single_limit"
    )


@pytest.mark.needs_ffmpeg
async def test_total_limit_counts_settled_spend_and_blocks_the_overflow(service: DramaService):
    await limits(service, total=500)
    shot = await first_shot(service)
    await act(service, "image-to-video", shot)
    state = await service.state("local")
    v = view(state)
    assert v["used"] == 300 and v["remaining"] == 200 and v["usable"] == 200
    assert await refused(service, "image-to-video", shot) == "credit_total_limit"


@pytest.mark.needs_ffmpeg
async def test_total_limit_above_the_balance_is_capped_by_the_balance(service: DramaService):
    await limits(service, total=1_000_000)
    state = await service.state("local")
    assert view(state)["usable"] == state["ledger"]["balance"]
    await act(service, "image-to-video", await first_shot(service))
    # 余额不够时报余额不足，而不是上限
    await limits(service, total=1_000_000, single="")
    for _ in range(2):
        await act(service, "image-to-video", await first_shot(service))
    assert (
        await refused(service, "image-to-video", await first_shot(service))
        == "insufficient_credits"
    )


async def test_changes_take_effect_on_the_very_next_request(service: DramaService):
    await limits(service, total=0)
    assert await refused(service, "script-doctor") == "credit_total_limit"
    await limits(service, total=10)
    await act(service, "script-doctor")
    await limits(service, single=1, total=10)
    assert await refused(service, "script-doctor") == "credit_single_limit"


@pytest.mark.needs_ffmpeg
async def test_editing_the_value_keeps_used_and_only_reset_clears_it(service: DramaService):
    await limits(service, total=1000)
    await act(service, "image-to-video", await first_shot(service))
    v = await limits(service, total=200)  # 调低到已用之下
    assert v["used"] == 300 and v["remaining"] == 0
    assert await refused(service, "script-doctor") == "credit_total_limit"
    v = await limits(service, total=1000)  # 再调高不能把已用清零
    assert v["used"] == 300
    v = await limits(service, total=1000, resetUsed=True)
    assert v["used"] == 0 and v["remaining"] == 1000
    v = await limits(service, total="", single="")  # 取消上限
    assert v["total"] is None and v["used"] == 0
    assert v["usable"] == (await service.state("local"))["ledger"]["balance"]


@pytest.mark.parametrize("bad", [-1, 1.5, "abc", True, 10**9, "nan", "inf", "1e400"])
async def test_invalid_values_are_rejected_without_changing_settings(service: DramaService, bad):
    await limits(service, total=500)
    with pytest.raises(AppError) as caught:
        await act(service, "saveCreditLimits", {"total": bad})
    assert caught.value.code == "invalid_credit_limit"
    assert (await service.state("local"))["creditLimits"]["total"] == 500


@pytest.mark.needs_ffmpeg
async def test_keyframe_batch_respects_the_total_and_settles_into_used(service: DramaService):
    state = await service.state("local")
    proj = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
    locked = [s["id"] for s in proj["shots"] if s["state"] == "locked"]
    t2i = int(state["pricing"]["t2i"])
    await limits(service, total=t2i * len(locked) - 1)
    assert await refused(service, "submitTasks", locked) == "credit_total_limit"
    await limits(service, total=t2i * len(locked))
    await act(service, "submitTasks", locked)
    v = view(await service.state("local"))
    assert v["pending"] == t2i * len(locked) and v["remaining"] == 0, "生成中的也要占额度"
    for _ in range(len(locked) + 2):
        await act(service, "tick")
    v = view(await service.state("local"))
    assert v["used"] == t2i * len(locked) and v["pending"] == 0


async def test_limits_survive_restart_and_export_import(service: DramaService, tmp_path: Path):
    await limits(service, single=50, total=800)
    doc = await service.export_doc("local")
    assert doc["creditLimits"]["single"] == 50
    reopened = DramaService(tmp_path / "drama.sqlite", MockDramaProvider(), autopump=False)
    reopened.store.install()
    assert (await reopened.state("local"))["creditLimits"]["total"] == 800
    await reopened.import_doc("local", doc)
    assert (await reopened.state("local"))["creditLimits"]["single"] == 50


async def test_import_rejects_malformed_limits(service: DramaService):
    doc = await service.export_doc("local")
    doc["creditLimits"] = {"total": "很多"}
    with pytest.raises(AppError) as caught:
        await service.import_doc("local", doc)
    assert caught.value.code == "invalid_import"


# 导入存档是「资金 + 额度」双敏感入口：一旦放进超限数值或 used > total，
# 后续每个付费动作都会被 credit_total_limit 锁死。
# 下面每条非法输入都必须收敛成 invalid_import，且用户可见文案不得出现
# 内部字段名（creditLimits / single / total / used / since / null / int ...）。
_INTERNAL_FIELD_WORDS = (
    "creditLimits",
    "single",
    "total",
    "used",
    "since",
    "null",
    "None",
    "int",
    "bool",
    "dict",
)


@pytest.mark.parametrize(
    "bad_limits",
    [
        pytest.param(
            {"single": 50, "total": CREDIT_LIMIT_MAX + 1, "used": 0, "since": "2026-11-12 10:00"},
            id="total-above-max",
        ),
        pytest.param(
            {"single": 50, "total": 10, "used": 999, "since": "2026-11-12 10:00"},
            id="used-above-total",
        ),
        pytest.param(
            {"single": 50, "total": 800, "used": 0, "since": {"x": 1}},
            id="since-not-str-dict",
        ),
        pytest.param(
            {"single": 50, "total": 800, "used": 0, "since": 123},
            id="since-not-str-int",
        ),
        pytest.param(
            {"single": True, "total": 800, "used": 0, "since": "2026-11-12 10:00"},
            id="single-bool",
        ),
        pytest.param(
            {"single": -1, "total": 800, "used": 0, "since": "2026-11-12 10:00"},
            id="single-negative",
        ),
        pytest.param(
            {"single": 1.5, "total": 800, "used": 0, "since": "2026-11-12 10:00"},
            id="single-float",
        ),
        pytest.param(
            {"single": "30", "total": 800, "used": 0, "since": "2026-11-12 10:00"},
            id="single-str",
        ),
        pytest.param(
            {"single": 50, "total": True, "used": 0, "since": "2026-11-12 10:00"},
            id="total-bool",
        ),
        pytest.param(
            {"single": 50, "total": 800, "used": 1.5, "since": "2026-11-12 10:00"},
            id="used-float",
        ),
        pytest.param(
            {"single": 50, "total": 10**12, "used": 0, "since": "2026-11-12 10:00"},
            id="total-far-above-max",
        ),
    ],
)
async def test_import_rejects_out_of_range_or_wrong_typed_limits(
    service: DramaService, bad_limits: dict[str, Any]
):
    doc = await service.export_doc("local")
    doc["creditLimits"] = bad_limits
    with pytest.raises(AppError) as caught:
        await service.import_doc("local", doc)
    assert caught.value.code == "invalid_import"
    message = caught.value.message
    leaked = [word for word in _INTERNAL_FIELD_WORDS if word in message]
    assert not leaked, f"导入错误文案泄漏内部字段名 {leaked}: {message}"


async def test_import_roundtrip_preserves_all_credit_limit_fields(service: DramaService):
    """合法全字段存档：single/total/used/since 四个键都要原样保住（现有用例只断言了 single）。"""
    full = {"single": 50, "total": 800, "used": 120, "since": "2026-11-12 10:00"}
    doc = await service.export_doc("local")
    doc["creditLimits"] = dict(full)
    state = await service.import_doc("local", doc)
    keys = ("single", "total", "used", "since")
    assert {k: state["creditLimits"].get(k) for k in keys} == full
    # 再导出一次，确认字段经历「落库 → 回包 → 再导出」都完好，不是只在内存里对。
    again = await service.export_doc("local")
    assert {k: again["creditLimits"].get(k) for k in keys} == full


# ---------------------------------------------------------------- 资金 / 上限联动
# 产品负责人要求：涉及资金的改动必须覆盖资金守恒、幂等、重试、取消、崩溃恢复，
# 以及积分使用上限的联动。下面的断言全部显式核对账本，不依赖服务内部的
# `_assert_conservation`——那道断言是兜底，不是验收。


def funds(state: dict[str, Any]) -> int:
    """资金守恒基数：余额 + 冻结 + 已退役成本。

    结算只把冻结转成「已花掉」，退差把多冻结的部分退回余额；释放/退款把冻结
    原路退回余额。所以「结算一次售价 price」之后这个和恰好比初始少 price，
    「全额退回」之后必须一分不少地回到初始值。
    """
    ledger = state["ledger"]
    return int(ledger["balance"]) + int(ledger["frozen"]) + int(ledger.get("retiredCost") or 0)


async def t2i_price(service: DramaService) -> int:
    """从服务端生效价取值：以后调价不用回来改这一组用例。"""
    return int((await service.state("local"))["pricing"]["t2i"])


async def locked_shots(service: DramaService) -> list[str]:
    state = await service.state("local")
    proj = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
    return [s["id"] for s in proj["shots"] if s["state"] == "locked"]


class _LocalFailureProvider(MockDramaProvider):
    """出网之前就失败的本地错误：上游一个字节都没收到，整笔必须全额退回。

    `drama_config` 在 `LOCAL_FAILURE_CODES` 里，`_unknown_outcome()` 会把它判成
    「明确没执行」，走 `_release_charge` 而不是 `pending_review`。
    """

    calls = 0

    def image_to_video(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AppError("drama_config", "t2i 模板 uuid 未配置，根本没有出网", 503)


def new_service(tmp_path: Path, provider: MockDramaProvider | None = None) -> DramaService:
    """独立建一个服务（故障注入 / 重启场景不能共用 fixture 的 provider 实例）。"""
    svc = DramaService(
        tmp_path / "drama.sqlite",
        provider or MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    svc.store.install()
    return svc


@pytest.mark.needs_ffmpeg
async def test_same_request_id_replays_without_a_second_charge_or_spend(
    service: DramaService,
):
    """幂等：同一 requestId 重放只结算一次，used 不加第二次，总资金只减一次售价。"""
    start = funds(await service.state("local"))
    shot = await first_shot(service)
    first = await service.action("local", "image-to-video", [shot], None, "idem-credit-1")
    after_first = first["state"]
    settled = [c for c in after_first["charges"] if c["state"] == "settled"]
    assert len(settled) == 1, "第一次请求必须只留一条已结算凭据"
    price = int(settled[0]["price"])
    assert view(after_first)["used"] == price
    assert funds(after_first) == start - price, "结算后总资金恰好减少一次售价"

    replay = await service.action("local", "image-to-video", [shot], None, "idem-credit-1")
    assert "幂等命中" in replay["message"]
    after = replay["state"]
    assert [c for c in after["charges"] if c["state"] == "settled"] == settled
    assert view(after)["used"] == price, "重放不能把同一笔消费再计一次"
    assert funds(after) == start - price, "重放不能动账本"


async def test_retry_task_is_blocked_by_the_single_limit_without_touching_the_ledger(
    service: DramaService,
):
    """重试入口也要过上限：被拒时不新增 charge/task、冻结与总资金一分不动。"""
    shots = await locked_shots(service)
    body = await act(service, "submitTasks", [shots[0]])
    failed = body["state"]["tasks"][0]["id"]
    await act(service, "injectFault", failed, "reject")
    await limits(service, single=(await t2i_price(service)) - 1, total="")

    before = await service.state("local")
    assert await refused(service, "retryTask", failed) == "credit_single_limit"
    after = await service.state("local")
    assert after["ledger"]["balance"] == before["ledger"]["balance"]
    assert after["ledger"]["frozen"] == before["ledger"]["frozen"]
    assert len(after["charges"]) == len(before["charges"]), "被拒时不能落新凭据"
    assert len(after["tasks"]) == len(before["tasks"]), "被拒时不能落新任务"
    assert next(t for t in after["tasks"] if t["id"] == failed)["state"] == "failed"
    assert funds(after) == funds(before), "被拒时账本零变动"


@pytest.mark.needs_ffmpeg
async def test_reroll_task_is_blocked_by_the_single_limit_without_touching_the_ledger(
    service: DramaService,
):
    """重抽入口也要过上限：已结算任务保持原状，账本零变动。"""
    shots = await locked_shots(service)
    await act(service, "submitTasks", shots)
    for _ in range(len(shots) + 2):
        await act(service, "tick")
    target = next(t["id"] for t in (await service.state("local"))["tasks"] if t["settled"])
    await limits(service, single=(await t2i_price(service)) - 1, total="")

    before = await service.state("local")
    assert await refused(service, "rerollTask", target) == "credit_single_limit"
    after = await service.state("local")
    assert after["ledger"]["balance"] == before["ledger"]["balance"]
    assert after["ledger"]["frozen"] == before["ledger"]["frozen"]
    assert len(after["charges"]) == len(before["charges"])
    assert len(after["tasks"]) == len(before["tasks"]), "重抽被拒不能追加候选任务"
    assert funds(after) == funds(before)


async def test_released_charge_does_not_count_as_used_and_refunds_everything(tmp_path: Path):
    """明确没执行的本地失败：released / used=0 / 冻结清零 / 总资金一分不少。"""
    service = new_service(tmp_path, _LocalFailureProvider())
    try:
        await limits(service, total=500)
        start = funds(await service.state("local"))
        shot = await first_shot(service)
        assert await refused(service, "image-to-video", shot) == "drama_config"
        state = await service.state("local")
        charge = next(c for c in state["charges"] if c["action"] == "image-to-video")
        assert charge["state"] == "released" and charge["cost"] == 0
        assert view(state)["used"] == 0, "退款不能计入累计已用"
        assert state["ledger"]["frozen"] == 0, "预占必须全额退回"
        assert funds(state) == start
        assert service.provider.calls == 1, "故障确实由 Provider 抛出，不是被上层吞掉"
    finally:
        await service.close()


async def make_pending_review(tmp_path: Path) -> tuple[DramaService, dict[str, Any], str]:
    """构造「上游结果未知」的待复核凭据，返回 (service, 失败前状态, charge_id)。"""
    service = new_service(tmp_path, MockDramaProvider(faults="script-doctor"))
    await limits(service, total=500)
    before = await service.state("local")
    with pytest.raises(AppError) as caught:
        await service.action("local", "script-doctor", [], None, "review-credit-1")
    assert caught.value.code == "upstream_unknown"
    state = await service.state("local")
    charge = next(c for c in state["charges"] if c["state"] == "pending_review")
    assert charge["cost"] == 0
    assert state["ledger"]["frozen"] == int(charge["cap"]), "钱还悬在冻结里"
    assert funds(state) == funds(before), "待复核阶段总资金不变，只是没判定花掉"
    return service, before, str(charge["id"])


async def test_resolve_succeeded_counts_the_spend_and_conserves_funds(tmp_path: Path):
    """复核为「已执行」：used += price、cost == price、总资金只减一次售价。"""
    service, before, cid = await make_pending_review(tmp_path)
    try:
        await act(service, "resolveCharge", cid, "succeeded")
        state = await service.state("local")
        charge = next(c for c in state["charges"] if c["id"] == cid)
        price = int(charge["price"])
        assert charge["state"] == "settled" and charge["cost"] == price
        assert view(state)["used"] == price
        assert state["ledger"]["frozen"] == 0
        assert funds(state) == funds(before) - price
    finally:
        await service.close()


async def test_resolve_failed_keeps_used_unchanged_and_refunds_everything(tmp_path: Path):
    """复核为「未执行」：used 不变、cost == 0、预占全额退回、总资金复原。"""
    service, before, cid = await make_pending_review(tmp_path)
    try:
        await act(service, "resolveCharge", cid, "failed")
        state = await service.state("local")
        charge = next(c for c in state["charges"] if c["id"] == cid)
        assert charge["state"] == "released" and charge["cost"] == 0
        assert view(state)["used"] == 0, "未执行不能计入累计已用"
        assert state["ledger"]["frozen"] == 0
        assert funds(state) == funds(before)
    finally:
        await service.close()


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_crash_orphan_charge_resolves_both_ways_without_double_counting(
    tmp_path: Path, outcome: str
):
    """崩溃恢复：出网中被杀留下的 in_flight，重启后两个复核分支都不能重复计数。"""
    service = new_service(tmp_path)
    charge_id = "CH-orphan-credit"
    try:
        await limits(service, total=500)
        before = await service.state("local")

        def orphan(db: Any) -> None:
            db.execute(
                "INSERT INTO drama_charge(id,owner,project_id,action,request_id,price,cap,"
                "cost,state,result,note,created_at,day) "
                "VALUES(?,?,?,?,?,?,?,0,'in_flight','','出网中被杀',0,'')",
                (charge_id, "local", None, "script-doctor", "orphan-limit-1", 2, 3),
            )
            db.execute(
                "UPDATE drama_ledger SET balance=balance-3, frozen=frozen+3 WHERE owner='local'"
            )

        service.store.run(orphan)
    finally:
        await service.close()

    restarted = new_service(tmp_path)
    try:
        await restarted.start()
        state = await restarted.state("local")
        charge = next(c for c in state["charges"] if c["id"] == charge_id)
        assert charge["state"] == "pending_review", "孤儿凭据必须被扫成待复核"
        assert funds(state) == funds(before)
        await act(restarted, "resolveCharge", charge_id, outcome)
        after = await restarted.state("local")
        resolved = next(c for c in after["charges"] if c["id"] == charge_id)
        if outcome == "succeeded":
            assert resolved["state"] == "settled" and resolved["cost"] == 2
            assert view(after)["used"] == 2
            assert funds(after) == funds(before) - 2
        else:
            assert resolved["state"] == "released" and resolved["cost"] == 0
            assert view(after)["used"] == 0
            assert funds(after) == funds(before)
        assert after["ledger"]["frozen"] == 0
    finally:
        await restarted.close()
