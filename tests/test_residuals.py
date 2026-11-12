"""复测的四个「充值前必须补」残留问题，逐条固化成断言。

来源：同事独立探测（服务层 + HTTP 层各跑一轮）给出的结论 ——
  1) 上游结果未知（超时）时不留记录，可重复扣上游的钱；
  2) 重抽 / 重试绕过项目预算与单日上限；单日上限还不统计关键帧；
  3) 非法输入仍返回 500（只修了 saveSettings）；
  4) 非管理员可以凭空加余额（导入伪造流水）或把单价改成 0。
另外：同一 requestId 换动作必须 409，不能回放上一个动作的结果。

每条测试的 docstring 都写了「修复前会怎样」，删掉就等于允许这些问题回来。
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.pricing import PRICE_T2I, PRICE_VIDEO, cap_of
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService


class TimeoutProvider(MockDramaProvider):
    """在 mock 之上让指定动作抛「上游结果未知」。

    用 AppError("drama_generation_timeout", ..., 504) 是因为真实适配器
    （providers/remote.py）轮询超时时抛的正是这个码。mock 永远成功，
    所以必须在测试里造一个「钱可能花了、但没拿到结果」的场景。
    """

    def __init__(self, failing: str = "script-doctor", *, times: int = 1) -> None:
        super().__init__()
        self.failing = failing
        self.times = times
        self.calls = 0

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        if self.failing == "script-doctor":
            self.calls += 1
            if self.calls <= self.times:
                raise AppError("drama_generation_timeout", "upstream task timed out", 504)
        return super().script_doctor(layers, owner=owner)

    def tts_preview(self, character: str, voice: str, text: str, *, owner: str) -> dict[str, Any]:
        if self.failing == "tts-preview":
            self.calls += 1
            if self.calls <= self.times:
                raise AppError("drama_generation_held", "upstream result is indeterminate", 503)
        return super().tts_preview(character, voice, text, owner=owner)


def make(tmp_path: Path, provider: MockDramaProvider, **kwargs: Any) -> DramaService:
    service = DramaService(
        tmp_path / "drama.sqlite",
        provider,
        admins=frozenset({"local"}),
        autopump=False,
        **kwargs,
    )
    service.store.install()
    return service


@pytest.fixture
def billed(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 mock 以「真实 Provider」的身份计费。

    日上限只统计真实上游消费（用户裁决：上限的存在理由是防止烧上游真钱），
    `provider.name == "mock"` 的本地模拟既不占额也不被额拦。而下面几条用例考的是
    「关键帧 / 重试是否进同一本账」这条计费口径，与 Provider 真假无关，所以只把
    身份改成非 mock，断言与 daily_cap 的取值都不动。
    """
    monkeypatch.setattr(MockDramaProvider, "name", "remote")


async def act(
    service: DramaService,
    name: str,
    *args: Any,
    owner: str = "local",
    project_id: str | None = None,
    request_id: str | None = None,
):
    return await service.action(owner, name, list(args), project_id, request_id)


def pending_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in state.get("charges", []) if c["state"] == "pending_review"]


def shot_ids(state: dict[str, Any], count: int) -> list[str]:
    project = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
    return [s["id"] for s in project["shots"][:count]]


async def settle_all(service: DramaService) -> None:
    for _ in range(6):
        await act(service, "tick")


# ---------------------------------------------------------------- 残留 1
async def test_unknown_upstream_outcome_leaves_a_pending_record_and_blocks_retry(tmp_path):
    """上游超时：504 + 一条待复核凭据 + 禁止重复提交（修复前静默回滚，再点就调两次）。"""
    provider = TimeoutProvider("script-doctor", times=1)
    service = make(tmp_path, provider)
    try:
        start = (await service.state("local"))["ledger"]
        with pytest.raises(AppError) as caught:
            await act(service, "script-doctor", request_id="req-1")
        assert caught.value.code == "upstream_unknown"
        assert caught.value.status == 504

        state = await service.state("local")
        records = pending_of(state)
        assert len(records) == 1, "必须留下待复核凭据，否则这笔钱没人知道花没花"
        record = records[0]
        assert record["action"] == "script-doctor"
        assert record["cost"] == 0
        cap = record["cap"]
        # 钱不能退：可能已经付给上游了。冻结保留、余额仍少 cap。
        assert state["ledger"]["frozen"] == start["frozen"] + cap
        assert state["ledger"]["balance"] == start["balance"] - cap

        # 换一个 requestId 再点：必须被挡住，且上游只被调用过一次。
        with pytest.raises(AppError) as again:
            await act(service, "script-doctor", request_id="req-2")
        assert again.value.code == "pending_review"
        assert again.value.status == 409
        # 用同一个 requestId 再点也不能被当成「幂等命中」回放一个空结果：
        # 第一次是 504，同一个键必须还是 409 pending_review 的语义。
        with pytest.raises(AppError) as same:
            await act(service, "script-doctor", request_id="req-1")
        assert same.value.code == "pending_review"
        assert same.value.status == 409
        assert provider.calls == 1, "重复提交不能真的再打一次上游"

        # 管理员复核为「未执行」-> 全额退回，动作恢复可用。
        await act(service, "resolveCharge", record["id"], "failed")
        state = await service.state("local")
        assert pending_of(state) == []
        assert state["ledger"]["frozen"] == start["frozen"]
        assert state["ledger"]["balance"] == start["balance"]
        await act(service, "script-doctor", request_id="req-3")
    finally:
        await service.close()


async def test_resolve_succeeded_settles_at_price_and_keeps_conservation(tmp_path):
    """复核为「已执行」：按定价结算，差额退回，守恒仍然成立。"""
    provider = TimeoutProvider("tts-preview", times=1)
    service = make(tmp_path, provider)
    try:
        start = (await service.state("local"))["ledger"]
        with pytest.raises(AppError):
            await act(service, "tts-preview", "v1", request_id="tts-1")
        record = pending_of(await service.state("local"))[0]
        price, cap = record["price"], record["cap"]
        assert cap >= price

        await act(service, "resolveCharge", record["id"], "succeeded")
        state = await service.state("local")
        assert pending_of(state) == []
        assert state["ledger"]["frozen"] == start["frozen"]
        assert state["ledger"]["balance"] == start["balance"] - price
        settled = next(c for c in state["charges"] if c["id"] == record["id"])
        assert settled["state"] == "settled" and settled["cost"] == price
        # 复核后同动作可以再次提交（不再被 pending_review 挡住）
        await act(service, "tts-preview", "v1", request_id="tts-2")
    finally:
        await service.close()


async def test_resolve_charge_requires_admin(tmp_path):
    provider = TimeoutProvider("script-doctor", times=1)
    service = make(tmp_path, provider)
    try:
        with pytest.raises(AppError):
            await act(service, "script-doctor", request_id="req-1")
        record = pending_of(await service.state("local"))[0]
        with pytest.raises(AppError) as caught:
            await act(service, "resolveCharge", record["id"], "failed", owner="stranger")
        assert caught.value.code == "forbidden"
        assert caught.value.status == 403
        # 结论只接受两种
        with pytest.raises(AppError) as bad:
            await act(service, "resolveCharge", record["id"], "maybe")
        assert bad.value.code == "invalid_outcome"
        # 待复核期间不能取消/重试把它绕过去
        await act(service, "resolveCharge", record["id"], "failed")
    finally:
        await service.close()


async def test_object_params_reject_string_with_readable_400(tmp_path):
    """对象参数收到字符串：必须是 400 invalid_field，且库里什么都没变。

    回归：`arg(0, {}) or {}` 对非空字符串原样返回，
    随后 `.get()` 抛 AttributeError；兜底只接了 4 种异常，于是仍是 500。
    """
    service = make(tmp_path, MockDramaProvider())
    try:
        before = await service.state("local")
        with pytest.raises(AppError) as caught:
            await act(service, "saveScript", "不是对象")
        assert caught.value.code == "invalid_field"
        assert caught.value.status == 400
        after = await service.state("local")
        assert after["projects"] == before["projects"], "非法入参不能改到任何项目数据"
    finally:
        await service.close()


# ------------------------------------------------- 残留 2 的反面：配置错误不算「结果未知」
class MisconfiguredProvider(MockDramaProvider):
    """模拟 remote Provider 在出网之前就失败（密钥没配 / 地址没配 / 模板没配）。"""

    def __init__(self, code: str = "drama_credentials", status: int = 503) -> None:
        super().__init__()
        self.code = code
        self.status = status
        self.calls = 0

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self.calls += 1
        raise AppError(self.code, f"配置缺失（{self.code}）", self.status)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("drama_credentials", 503),
        ("drama_config", 503),
        ("drama_tts_config", 503),
        ("drama_ffmpeg", 503),
    ],
)
async def test_local_config_failure_rolls_back_and_keeps_action_usable(tmp_path, code, status):
    """配置没配好 = 一定没调到上游：整笔回滚、不留待复核、不锁动作。

    复测指出按状态码一刀切（502/503/504 全转待复核）会把这种「自己抛的 5xx」
    也判成结果未知：首单漏配一项，动作就被冻结 + 上锁，要管理员逐条复核。
    """
    provider = MisconfiguredProvider(code, status)
    service = make(tmp_path, provider)
    try:
        start = (await service.state("local"))["ledger"]
        with pytest.raises(AppError) as caught:
            await act(service, "script-doctor", request_id="cfg-1")
        assert caught.value.code == code, "必须原样透出配置错误，不能改成 upstream_unknown"
        assert caught.value.status == status

        state = await service.state("local")
        assert pending_of(state) == [], "配置错误不能留待复核记录"
        assert state["ledger"]["frozen"] == start["frozen"]
        assert state["ledger"]["balance"] == start["balance"], "预占必须全额退回"

        # 关键：动作没有被锁住。配好之后立刻就能再试，不需要管理员先复核。
        with pytest.raises(AppError) as again:
            await act(service, "script-doctor", request_id="cfg-2")
        assert again.value.code == code, f"不应该变成 pending_review：{again.value.code}"
        assert provider.calls == 2
    finally:
        await service.close()


# ---------------------------------------------------------------- 残留 2
async def test_reroll_respects_project_budget(tmp_path):
    """修复前：预算 40 的项目重抽 6 次全部通过，实际花到 70。"""
    service = make(tmp_path, MockDramaProvider())
    try:
        state = await service.state("local")
        await act(service, "submitTasks", shot_ids(state, 3))
        await settle_all(service)
        # 3 镜 × 10 积分 = 30，预算压到 40 之后只余 10，重抽需预占 12 → 必须被挡。
        await act(service, "saveSettings", {"budget": 40})
        state = await service.state("local")
        target = next(t for t in state["tasks"] if t["settled"])
        with pytest.raises(AppError) as caught:
            await act(service, "rerollTask", target["id"], request_id="re-budget")
        assert caught.value.code == "budget_exceeded"
        assert "预算" in caught.value.message
    finally:
        await service.close()


async def test_reroll_respects_daily_cap(tmp_path, billed):
    """修复前：单日上限 60 时重抽 10 次全部通过，当天关键帧花到 140。

    日上限按「5 镜 × 单镜硬上限」取值，这样它仍然只够跑完这一批、不够再重抽一次，
    与售价解耦——改价不该让这条断言失效。

    用 `billed` 把 mock 的身份改成真实 Provider：离线 mock 按裁决不占日上限，
    直接用默认 mock 这条永远不触发闸门。
    """
    service = make(tmp_path, MockDramaProvider(), daily_cap=cap_of(PRICE_T2I) * 5)
    try:
        state = await service.state("local")
        await act(service, "submitTasks", shot_ids(state, 5))
        await settle_all(service)
        spent = service.store.daily_spend("local", time.strftime("%Y-%m-%d"))
        assert spent == PRICE_T2I * 5, "5 镜的关键帧售价应全部计入当日消耗"
        state = await service.state("local")
        target = next(t for t in state["tasks"] if t["settled"])
        with pytest.raises(AppError) as caught:
            await act(service, "rerollTask", target["id"], request_id="re-cap")
        assert caught.value.code == "daily_cap_exceeded"
        assert "关键帧" in caught.value.message, "提示里要说清楚关键帧也计入日上限"
    finally:
        await service.close()


async def test_daily_spend_counts_settled_keyframe_tasks(tmp_path, billed):
    """修复前：今日已用只看 drama_charge，关键帧花了 140 仍显示 0。

    用 `billed` 把 mock 的身份改成真实 Provider：`daily_spend` 只统计真实消费，
    用默认 mock 时这里会恒为 0，反而验不出「关键帧要计入」。
    """
    service = make(tmp_path, MockDramaProvider())
    try:
        state = await service.state("local")
        await act(service, "submitTasks", shot_ids(state, 2))
        await settle_all(service)
        state = await service.state("local")
        costs = sum(t["cost"] for t in state["tasks"] if t["settled"])
        assert costs == PRICE_T2I * 2
        day = time.strftime("%Y-%m-%d", time.localtime())
        assert service.store.daily_spend("local", day) == costs
        config = await service.config("local")
        assert config["daily_used"] == costs, "页面「今日已用」必须含关键帧"
    finally:
        await service.close()


async def test_retry_respects_daily_cap(tmp_path, billed):
    """重试同样要过闸门（原来只看了余额）。

    用 `billed` 把 mock 的身份改成真实 Provider：离线 mock 按裁决不占日上限，
    直接用默认 mock 这条永远不触发闸门。
    """
    # 临界值：第一天跑完两笔关键帧（第一笔失败释放、第二笔结算 1× 售价）后，
    # 剩下的额度刚好不够第三笔（重试）的预占，于是必须被拒。
    service = make(tmp_path, MockDramaProvider(), daily_cap=PRICE_T2I + cap_of(PRICE_T2I) - 1)
    try:
        state = await service.state("local")
        body = await act(service, "submitTasks", shot_ids(state, 1))
        failed = body["state"]["tasks"][0]
        await act(service, "injectFault", failed["id"], "reject")
        # 再花掉一笔售价，让今天只剩不足一次预占的额度，重试因此必须被拒
        state = await service.state("local")
        await act(service, "submitTasks", shot_ids(state, 1))
        await settle_all(service)
        with pytest.raises(AppError) as caught:
            await act(service, "retryTask", failed["id"])
        assert caught.value.code == "daily_cap_exceeded"
    finally:
        await service.close()


# ---------------------------------------------------------------- 残留 3
@pytest.mark.parametrize(
    ("name", "args", "field"),
    [
        ("createProject", ({"name": "x", "episode": "abc"},), "episode"),
        ("createProject", ({"name": "x", "eps": "abc"},), "eps"),
        ("createProject", ({"name": "x", "season": "abc"},), "season"),
        ("addShot", ({"dur": "abc"},), "dur"),
        ("toggleQA", ("abc",), "qa index"),
        ("savePricing", ({"t2i": 10, "cap": 12, "qps": "abc"},), "qps"),
        ("moveShot", ("shot-x", "abc"), "offset"),
        ("purgeMedia", ("abc",), "days"),
        # 回归：入参本该是对象却收到字符串，`f.get(...)` 抛 AttributeError
        ("createProject", ("不是对象",), "createProject 的参数"),
        ("saveScript", ("不是对象",), "saveScript 的参数"),
        ("saveSettings", ("不是对象",), "saveSettings 的参数"),
        ("addShot", ("不是对象",), "addShot 的参数"),
        ("addVoice", ("不是对象",), "addVoice 的参数"),
        ("addSubtitle", ("不是对象",), "addSubtitle 的参数"),
        ("saveBgm", ("不是对象",), "saveBgm 的参数"),
    ],
)
async def test_illegal_inputs_raise_readable_400(tmp_path, name, args, field):
    """非法输入必须是 400 + 可读错误，不能是 500（修复前 9 组里 8 组 500）。"""
    service = make(tmp_path, MockDramaProvider())
    try:
        with pytest.raises(AppError) as caught:
            await act(service, name, *args)
        assert caught.value.status == 400, f"{name} 应返回 400，实际 {caught.value.status}"
        assert caught.value.code in ("invalid_field", "invalid_args")
        assert field in caught.value.message
    finally:
        await service.close()


async def test_move_shot_still_accepts_upward_offset(tmp_path):
    """回归：把 offset 校验成「必须 >= 0」会把前端「上移」按钮打成 400。

    前端 A.ACTIONS["move-shot"] 传的是 Number(d.dir)，上移就是 -1；
    所以在统一走 _as_int 时必须允许负数，只挡非数字。
    """
    service = make(tmp_path, MockDramaProvider())
    try:
        state = await service.state("local")
        ids = shot_ids(state, 3)
        await act(service, "moveShot", ids[2], -1)
        state = await service.state("local")
        assert shot_ids(state, 3) == [ids[0], ids[2], ids[1]]
        # 反向仍然可以
        await act(service, "moveShot", ids[2], 1)
        assert shot_ids(await service.state("local"), 3) == ids
    finally:
        await service.close()


# ---------------------------------------------------------------- 残留 4
async def test_save_pricing_requires_admin(tmp_path):
    """非管理员把 t2i 设成 0 = 免费无限生成。修复前这个调用直接成功。"""
    service = make(tmp_path, MockDramaProvider())
    try:
        with pytest.raises(AppError) as caught:
            await act(service, "savePricing", {"t2i": 0, "cap": 1}, owner="stranger")
        assert caught.value.code == "forbidden"
        assert caught.value.status == 403
        assert (await service.state("stranger"))["pricing"]["t2i"] == PRICE_T2I
    finally:
        await service.close()


async def test_import_requires_admin_even_with_self_consistent_ledger(tmp_path):
    """伪造一条「加余额 100 万」的 credit 流水，账本自洽也照样拒。

    修复前：非管理员导入被接受，余额变 1,000,840 —— 因为导入只校验
    「余额 = 980 + 加余额 - 成本」，而加余额那条流水本身是伪造的。
    """
    service = make(tmp_path, MockDramaProvider())
    try:
        doc = await service.export_doc("local")
        doc["ledger"]["tx"].append(
            {
                "id": "tx-forged",
                "kind": "credit",
                "label": "凭空加余额",
                "amount": 1_000_000,
                "note": "伪造 / requestId=forge",
            }
        )
        doc["ledger"]["balance"] += 1_000_000

        with pytest.raises(AppError) as caught:
            await service.import_doc("stranger", doc)
        assert caught.value.code == "forbidden"
        assert caught.value.status == 403
        stranger = await service.state("stranger")
        assert stranger["ledger"]["balance"] == 980, "非管理员不能借导入改余额"

        # 管理员仍然可以导入（迁移 / 恢复需要），账本自洽校验仍然生效
        await service.import_doc("local", doc)
        assert (await service.state("local"))["ledger"]["balance"] > 980
    finally:
        await service.close()


# ---------------------------------------------------------------- 幂等键
async def test_same_request_id_with_another_action_is_conflict_not_replay(tmp_path):
    """同一个 requestId 换动作：必须 409，不能回放上一个动作的结果。"""
    service = make(tmp_path, MockDramaProvider())
    try:
        await act(service, "script-doctor", request_id="same-rid")
        with pytest.raises(AppError) as caught:
            await act(service, "creative-start", "idea", "一封迟到的信", request_id="same-rid")
        assert caught.value.code == "idempotency_conflict"
        assert caught.value.status == 409
    finally:
        await service.close()


# ------------------------------------------------------- 生成期间不挡别人
class SlowProvider(MockDramaProvider):
    """把「出网那一下」拉长成可控的一段。修复前这段是在 owner 锁里跑的。"""

    def __init__(self, hold: threading.Event, entered: threading.Event) -> None:
        super().__init__()
        self.hold = hold
        self.entered = entered

    def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
        self.entered.set()
        # 等测试放行。10s 只是兜底，正常路径由 hold.set() 立即放行。
        self.hold.wait(10)
        return super().script_doctor(layers, owner=owner)


async def _wait_entered(entered: threading.Event, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if entered.is_set():
            return
        await asyncio.sleep(0.01)


def charges_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("charges", []))


async def test_generation_does_not_block_the_same_user_saving_the_script(tmp_path):
    """同一用户在生成（2 秒）期间保存剧本，不能被挡住。

    修复前：`action()` 把「读快照 -> 状态机（含 Provider 调用）-> 写库」整段
    挂在同一 owner 的锁上，2 秒的生成里保存剧本要等 1.8 秒。
    修复后：预占落库就把锁放开，出网在锁外跑；保存剧本只等两次读写的毫秒级窗口。

    这条断言的是**可量化的上限**（< 0.2s），不是「感觉快了」——
    以后谁把出网挪回锁里，这里会直接红。
    """
    hold, entered = threading.Event(), threading.Event()
    service = make(tmp_path, SlowProvider(hold, entered))
    try:
        project_id = (await service.state("local"))["activeProjectId"]
        generation = asyncio.create_task(act(service, "script-doctor", request_id="slow-1"))
        await _wait_entered(entered)
        assert entered.is_set(), "生成动作没有进入出网阶段"

        # 此刻生成正在锁外等 hold；同一用户的保存必须立刻返回。
        started = time.perf_counter()
        await act(service, "saveScript", {"body": "生成期间存下的正文"}, request_id="quick-1")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, f"生成期间保存剧本被挡了 {elapsed:.2f}s（要求 < 0.2s）"

        hold.set()
        await asyncio.wait_for(generation, timeout=20)

        state = await service.state("local")
        project = next(p for p in state["projects"] if p["id"] == project_id)
        # 后写的那一份不能被生成的「第二段」用旧快照覆盖回去。
        assert project["script"]["layers"]["body"] == "生成期间存下的正文"
        # 生成结果也必须真的落地：放开锁不等于放弃结果。
        assert project["lastResult"]["action"] == "script-doctor"
        # 钱只扣一次：2 秒的等待没有变成两次结算。
        assert [c["state"] for c in charges_of(state)] == ["settled"]
        assert state["ledger"]["frozen"] == 0
        assert state["ledger"]["balance"] == 980 - 2
    finally:
        hold.set()
        await service.close()


async def test_preoccupied_charge_is_persisted_before_calling_upstream(tmp_path):
    """出网之前预占必须已经落库（in_flight），不能等结果回来再记账。

    这条是分锁改造的**资金前提**：既然锁放开了，就没法再靠「一次性写库」
    保证「钱花了但没结果」也留痕。所以预占先落一条 in_flight 凭据，
    进程在出网期间被杀，下次启动能把它扫成待复核。
    """
    hold, entered = threading.Event(), threading.Event()
    service = make(tmp_path, SlowProvider(hold, entered))
    try:
        generation = asyncio.create_task(act(service, "script-doctor", request_id="slow-2"))
        await _wait_entered(entered)
        assert entered.is_set()

        mid = await service.state("local")
        in_flight = [c for c in charges_of(mid) if c["state"] == "in_flight"]
        assert len(in_flight) == 1, "出网期间必须已经有一条 in_flight 凭据"
        assert in_flight[0]["cap"] == 3
        assert mid["ledger"]["frozen"] == 3, "预占要真的冻结住，否则并发动作能把它花掉"

        hold.set()
        await asyncio.wait_for(generation, timeout=20)
        after = await service.state("local")
        assert [c["state"] for c in charges_of(after)] == ["settled"]
        assert after["ledger"]["frozen"] == 0
    finally:
        hold.set()
        await service.close()


async def test_restart_promotes_orphan_in_flight_to_pending_review(tmp_path):
    """进程在出网中被杀留下的 in_flight，重启后必须变成待复核，不能永久冻结。

    这里直接构造「崩溃瞬间」的库内容：预占已落库（凭据 in_flight、3 积分从余额
    挪进冻结），结果列还是空的。这是两段式唯一无法自愈的状态。
    不扫的话：这条凭据既不在待复核列表里（管理页看不到），`resolveCharge`
    也只收 pending_review，那 3 积分就永远冻着，谁都解不开。
    """
    service = make(tmp_path, MockDramaProvider())
    charge_id = "CH-orphan"
    try:
        # 先让 owner 开户（saveSettings 不扣费，账本停在 980 / 冻结 0）。
        await act(service, "saveSettings", {"name": "演示项目"})
        assert (await service.state("local"))["ledger"]["frozen"] == 0

        def orphan(db: Any) -> None:
            db.execute(
                "INSERT INTO drama_charge(id,owner,project_id,action,request_id,price,cap,"
                "cost,state,result,note,created_at,day) "
                "VALUES(?,?,?,?,?,?,?,0,'in_flight','','出网中被杀',0,'')",
                (charge_id, "local", None, "script-doctor", "orphan-1", 2, 3),
            )
            db.execute(
                "UPDATE drama_ledger SET balance=balance-3, frozen=frozen+3 WHERE owner='local'"
            )

        service.store.run(orphan)
    finally:
        await service.close()

    # 重启：start() 里要把孤儿凭据扫成待复核。
    restarted = make(tmp_path, MockDramaProvider())
    try:
        await restarted.start()
        state = await restarted.state("local")
        orphan_charge = next(c for c in charges_of(state) if c["id"] == charge_id)
        assert orphan_charge["state"] == "pending_review"
        assert orphan_charge["cost"] == 0, "待复核的成本必须是 0，否则守恒等式会被算两遍"
        # 钱没多也没少：预占那 3 分仍在冻结里，等管理员复核。
        assert state["ledger"]["frozen"] == 3
        assert state["ledger"]["balance"] == 980 - 3
        # 复核出口必须能真的把它放掉（否则就是「能看见但点不动」）。
        await act(restarted, "resolveCharge", charge_id, "failed")
        resolved = await restarted.state("local")
        assert resolved["ledger"]["frozen"] == 0
        assert resolved["ledger"]["balance"] == 980
    finally:
        await restarted.close()


async def test_save_script_during_generation_survives_a_failing_generation(tmp_path):
    """生成最后失败，也不能把期间保存的剧本一起回滚掉。

    两段式最容易踩的坑是「第二段拿旧快照整份覆盖」：如果第二段不重新读，
    生成失败时写回的是 2 秒前的快照，用户这段时间存的东西就凭空消失了。
    """

    class FailingSlow(SlowProvider):
        def script_doctor(self, layers: dict[str, Any], *, owner: str) -> dict[str, Any]:
            self.entered.set()
            self.hold.wait(10)
            raise AppError("drama_generation_timeout", "upstream task timed out", 504)

    hold, entered = threading.Event(), threading.Event()
    service = make(tmp_path, FailingSlow(hold, entered))
    try:
        project_id = (await service.state("local"))["activeProjectId"]
        generation = asyncio.create_task(act(service, "script-doctor", request_id="slow-3"))
        await _wait_entered(entered)
        assert entered.is_set()

        await act(service, "saveScript", {"body": "超时前存下的正文"})
        hold.set()
        with pytest.raises(AppError) as caught:
            await asyncio.wait_for(generation, timeout=20)
        assert caught.value.code == "upstream_unknown"
        assert caught.value.status == 504

        state = await service.state("local")
        project = next(p for p in state["projects"] if p["id"] == project_id)
        assert project["script"]["layers"]["body"] == "超时前存下的正文"
        # 结果未知：预占保留 + 一条待复核，这是原有的资金语义，不能被改造改坏。
        assert [c["state"] for c in charges_of(state)] == ["pending_review"]
        assert state["ledger"]["frozen"] == 3
    finally:
        hold.set()
        await service.close()


async def test_equal_request_id_is_refused_while_the_first_call_is_in_flight(tmp_path):
    """同一个 requestId 在第一次调用还没结束时重提交：409，不执行第二次。"""
    hold, entered = threading.Event(), threading.Event()
    service = make(tmp_path, SlowProvider(hold, entered))
    try:
        generation = asyncio.create_task(act(service, "script-doctor", request_id="dup-1"))
        await _wait_entered(entered)
        assert entered.is_set()

        with pytest.raises(AppError) as caught:
            await act(service, "script-doctor", request_id="dup-1")
        assert caught.value.code == "action_in_flight"
        assert caught.value.status == 409

        hold.set()
        await asyncio.wait_for(generation, timeout=20)
        state = await service.state("local")
        assert [c["state"] for c in charges_of(state)] == ["settled"], "上游只应被结算一次"
        assert state["ledger"]["balance"] == 980 - 2
    finally:
        hold.set()
        await service.close()


async def test_project_deleted_during_generation_refunds_the_reservation(tmp_path):
    """生成期间项目被删：续跑必须把预占退掉，不能把 3 积分永久冻在一个不存在的项目上。

    这条走的是 `_charged` 之外的失败分支（project_missing 在预占之后、
    进 `_dispatch_inner` 之前抛），所以只有 `_action_sync` 的兜底会救它。
    """
    hold, entered = threading.Event(), threading.Event()
    service = make(tmp_path, SlowProvider(hold, entered))
    try:
        await act(service, "createProject", {"name": "会被删掉的项目"})
        target = (await service.state("local"))["activeProjectId"]
        await act(service, "createProject", {"name": "留下的项目"})

        generation = asyncio.create_task(
            act(service, "script-doctor", project_id=target, request_id="slow-4")
        )
        await _wait_entered(entered)
        assert entered.is_set()

        # 出网期间项目被删（另一台设备 / 另一个标签页都可能这么干）。
        await act(service, "deleteProject", target)
        hold.set()

        with pytest.raises(AppError) as caught:
            await asyncio.wait_for(generation, timeout=20)
        assert caught.value.code == "project_missing"
        assert caught.value.status == 404

        state = await service.state("local")
        assert state["ledger"]["frozen"] == 0, "预占必须退回，不能冻在一个已删除的项目上"
        assert state["ledger"]["balance"] == 980
        assert [c["state"] for c in charges_of(state)] == ["released"]
        # 当前项目不能被指向那个已删掉的项目。
        assert all(p["id"] != target for p in state["projects"])
        assert state["activeProjectId"] in {p["id"] for p in state["projects"]}
    finally:
        hold.set()
        await service.close()


# ------------------------------------------- 质量缺口
# 1x1 透明 PNG：给「@角色」造一张真实锚点图，验证它真的作为首帧字节传给 Provider。
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class SlowShotProvider(MockDramaProvider):
    """只让第一次图生视频停住；后续调用立即返回，便于测「不同镜头不误伤」。"""

    def __init__(self, hold: threading.Event, entered: threading.Event) -> None:
        super().__init__()
        self.hold = hold
        self.entered = entered
        self.calls = 0

    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.hold.wait(10)
        return super().image_to_video(shot, size, owner=owner, first_frame=first_frame)


class RemoteOnlyProvider(MockDramaProvider):
    """只把 name 改成 remote：验证「两种模式共用一条关键帧路径」，不出网、不读凭据。"""

    name = "remote"


class FakeRemoteKeyframeProvider(RemoteOnlyProvider):
    """真实模式的关键帧桩：只回一个图片 URL，不调 FFmpeg、不碰上游。

    A3 是资金路径验收，不该被本机合成工具是否安装影响；示例素材的产出由
    test_remote_provider.py 与 test_offline_demo.py 单独覆盖。
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def keyframe(self, shot: dict[str, Any], *, owner: str, first_frame: bytes | None = None):
        self.calls += 1
        # 返回体形状与真实 / 离线 Provider 一致：_media() 要读 kind / label / provider。
        return {
            "provider": "remote",
            "kind": "keyframe",
            "label": "真实关键帧（测试桩）",
            "note": "不出网、不读凭据的资金路径桩",
            "payload": {
                "image_url": "/api/v1/drama/assets/" + "a" * 16,
                "sample": False,
                "shot_id": shot.get("id"),
            },
        }


def assert_conservation(state: dict[str, Any]) -> None:
    """独立算一遍资金守恒：980 种子 + 充值 - 已结算成本 = 可用 + 冻结。"""
    cost = sum(t["cost"] for t in state["tasks"] if t["settled"])
    cost += sum(c["cost"] for c in state.get("charges", []) if c["state"] == "settled")
    cost += state["ledger"].get("retiredCost", 0)
    credits = sum(
        x["amount"] for x in state["ledger"]["tx"] if x["kind"] == "credit" and not x["seed"]
    )
    assert 980 + credits - cost == state["ledger"]["balance"] + state["ledger"]["frozen"]


async def test_refreshed_request_id_cannot_resubmit_the_same_shot(tmp_path):
    """验收 1：同一镜头生成中，换 requestId 再提交 = 409；不同镜头不受影响。

    修复前：`_idempotent_replay` 只按同一个 requestId 查，刷新页面换了号就
    认不出来，上游被真调两次、各记一次账（真实 socket 实测 980 -> 976）。
    现在按 (action, projectId, target) 查在飞凭据；target 同时给前端显示「生成中」。
    """
    hold, entered = threading.Event(), threading.Event()
    provider = SlowShotProvider(hold, entered)
    service = make(tmp_path, provider)
    try:
        first = await service.state("local")
        shot_a, shot_b = shot_ids(first, 2)
        generation = asyncio.create_task(
            act(service, "image-to-video", shot_a, "", request_id="rq-first")
        )
        await _wait_entered(entered)
        assert entered.is_set(), "第一次生成没有进入出网阶段"

        flying = [c for c in charges_of(await service.state("local")) if c["state"] == "in_flight"]
        assert len(flying) == 1
        assert flying[0]["target"] == f"shot:{shot_a}", "目标键要能映射回具体镜头"

        # 刷新页面换了号：必须 409，且不能真的再打一次上游。
        with pytest.raises(AppError) as caught:
            await act(service, "image-to-video", shot_a, "", request_id="rq-after-refresh")
        assert caught.value.code == "action_in_flight"
        assert caught.value.status == 409
        assert provider.calls == 1

        # 边界：同一动作、同一项目、不同镜头不该被误伤。
        other = await act(service, "image-to-video", shot_b, "", request_id="rq-other-shot")
        assert other["result"]["kind"] == "image-to-video"

        hold.set()
        await asyncio.wait_for(generation, timeout=20)
        state = await service.state("local")
        assert [c["state"] for c in charges_of(state)] == ["settled", "settled"]
        assert provider.calls == 2, "只允许两次真实调用：两个不同镜头各一次"
        assert state["ledger"]["frozen"] == 0
        assert state["ledger"]["balance"] == 980 - PRICE_VIDEO * 2
        assert sorted(c["target"] for c in charges_of(state)) == sorted(
            [f"shot:{shot_a}", f"shot:{shot_b}"]
        )
    finally:
        hold.set()
        await service.close()


async def test_project_settings_and_character_refs_reach_the_provider(tmp_path):
    """验收 2：比例 / 画风 / 分辨率与 `@角色` 锚点图真的进了生成请求参数。

    修复前：这些只躺在项目设定页；出图固定 1024×1024、提示词不带画风、
    `shot.refs` 不参与生成也不参与首帧选择。这里用 mock 回显逐项断言。
    """
    # 锚点候选图 + 两次图生视频的价值远超单日上限 500，本用例验的是参数透传，
    # 不是日上限，故放开（闸门本身由 test_reroll_respects_daily_cap 等盯）。
    service = make(tmp_path, MockDramaProvider(), daily_cap=100000)
    try:
        expected = {
            "ratio": "9:16 竖屏",
            "style": "水墨国漫",
            "res": "1080 × 1920 / 24fps",
        }
        await act(service, "saveSettings", expected)

        state = await service.state("local")
        proj = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
        anchor = next(a for a in proj["assets"]["characters"] if a["name"] == "林砚")
        shot = next(s for s in proj["shots"] if "@角色:林砚" in s["refs"])

        # 造一张真实锚点图（模拟之前 generateAssetCandidates 已产出）。
        saved = service.media_store.store("local", "image", _PNG_1PX)
        # drama_assets 按 project_id 归属、没有 owner 列；资产 id 是主键。
        service.store.run(
            lambda db: db.execute(
                "UPDATE drama_assets SET image_url=? WHERE id=?",
                (saved["url"], anchor["id"]),
            )
        )

        candidates = await act(service, "generateAssetCandidates", "characters", anchor["id"])
        assert candidates["result"]["payload"]["project_settings"] == expected

        video = await act(service, "image-to-video", shot["id"], "")
        payload = video["result"]["payload"]
        assert payload["project_settings"] == expected
        assert "@角色:林砚" in payload["anchor_refs"]
        assert payload["anchor_asset_id"] == anchor["id"]
        # 交给上游的是锚点图的素材文件编号（镜头自己还没有关键帧时）
        anchor_url = candidates["result"]["payload"]["image_url"]
        assert payload["first_frame_asset_id"] == anchor_url.rsplit("/", 1)[-1]
        assert payload["first_frame_bytes"] > 0, "锚点图的字节要真的作为首帧传出去"

        # 显式选的 assetId 优先于 @角色 自动解析。
        explicit = await act(service, "image-to-video", shot["id"], anchor["id"])
        assert (
            explicit["result"]["payload"]["first_frame_asset_id"] == anchor_url.rsplit("/", 1)[-1]
        )
    finally:
        await service.close()


async def test_remote_keyframe_entries_share_one_provider_driven_settlement_path(tmp_path):
    """A3 验收 1：真实 Provider 下所有会预占积分的关键帧入口都能真实结算。

    修复前：真实模式 submitTasks 被 fail-closed 拒绝，而 rerollTask / retryTask
    仍预占积分并留下永远 queued 的任务（实测余额 948→936、冻结 12→24）。
    现在 submitTasks / tick / rerollTask 共用 Provider.keyframe() 两段式路径。
    """
    provider = FakeRemoteKeyframeProvider()
    service = make(tmp_path, provider)
    try:
        before = await service.state("local")
        assert before["ledger"]["balance"] == 980
        count = (await act(service, "submitTasks"))["result"]
        assert isinstance(count, int) and count > 0, "真实模式不再 fail-closed"
        state = await service.state("local")
        assert all(t["sourceProvider"] == "remote" for t in state["tasks"])
        assert state["ledger"]["frozen"] == count * state["pricing"]["cap"]

        for _ in range(count + 2):
            if (await act(service, "tick"))["result"] is not True:
                break
        state = await service.state("local")
        assert provider.calls == count, "每个任务必须真的调一次 Provider.keyframe()"
        assert all(t["state"] == "succeeded" and t["settled"] for t in state["tasks"])
        assert state["ledger"]["frozen"] == 0
        proj = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
        assert sum(1 for s in proj["shots"] if s.get("imageUrl")) == count
        assert_conservation(state)

        # 重抽：新任务新预占，再推进就能结算，不会留下收费的 queued 死任务。
        settled_id = state["tasks"][0]["id"]
        await act(service, "rerollTask", settled_id)
        state = await service.state("local")
        assert state["tasks"][-1]["state"] == "queued"
        assert state["tasks"][-1]["sourceProvider"] == "remote"
        assert state["ledger"]["frozen"] == state["pricing"]["cap"]
        for _ in range(3):
            if (await act(service, "tick"))["result"] is not True:
                break
        state = await service.state("local")
        assert state["tasks"][-1]["state"] == "succeeded"
        assert state["ledger"]["frozen"] == 0
        assert_conservation(state)
    finally:
        await service.close()


async def test_switching_provider_settles_legacy_unsettled_keyframe_tasks(tmp_path):
    """A3 验收 2：离线造已结算 / 失败 / 排队三种任务，再以真实 Provider 打开同一个库。

    排队那条必须自动全额退还预占并取消；已结算与失败的不受影响。
    随后逐个入口验证：cancelled 不能再收费，failed / succeeded 的重试与重抽都能结算，
    资金守恒在每个检查点成立。
    """
    offline = make(tmp_path, MockDramaProvider())
    try:
        state = await offline.state("local")
        s1, s2, s3 = shot_ids(state, 3)
        # 已结算
        await act(offline, "submitTasks", [s1])
        for _ in range(3):
            if (await act(offline, "tick"))["result"] is not True:
                break
        # 明确失败（injectFault reject 会自动退还预占）
        await act(offline, "submitTasks", [s2])
        failed_id = (await offline.state("local"))["tasks"][-1]["id"]
        await act(offline, "injectFault", failed_id, "reject")
        # 排队：不推进，切模式后就是「无声冻结」的那条
        await act(offline, "submitTasks", [s3])
        before = await offline.state("local")
        assert [t["state"] for t in before["tasks"]] == ["succeeded", "failed", "queued"]
        settled_id = before["tasks"][0]["id"]
        cap = before["pricing"]["cap"]
        frozen_before, balance_before = before["ledger"]["frozen"], before["ledger"]["balance"]
        assert frozen_before == cap
        assert_conservation(before)
    finally:
        await offline.close()

    provider = FakeRemoteKeyframeProvider()
    remote = make(tmp_path, provider)
    await remote.start()  # start() 会 reconcileMode，模拟「同一个库换 Provider 重启」
    try:
        state = await remote.state("local")
        queued = next(t for t in state["tasks"] if t["shotId"] == s3)
        assert queued["state"] == "cancelled"
        assert queued["reserve"] == 0
        assert "预占已自动全额退还" in queued["note"]
        assert state["ledger"]["frozen"] == frozen_before - cap
        assert state["ledger"]["balance"] == balance_before + cap
        assert next(t for t in state["tasks"] if t["shotId"] == s1)["state"] == "succeeded"
        assert next(t for t in state["tasks"] if t["shotId"] == s2)["state"] == "failed"
        assert_conservation(state)

        # 入口 1/2：已取消的遗留任务不能被重试或重抽，账本一分不动。
        for name in ("retryTask", "rerollTask"):
            with pytest.raises(AppError):
                await act(remote, name, queued["id"])
            now = await remote.state("local")
            assert now["ledger"] == state["ledger"], f"{name} 不得动账本"
        assert_conservation(state)

        # 入口 3：失败任务重试 -> 重新预占 -> 正常结算。
        await act(remote, "retryTask", failed_id)
        state = await remote.state("local")
        assert next(t for t in state["tasks"] if t["id"] == failed_id)["sourceProvider"] == "remote"
        assert state["ledger"]["frozen"] == cap
        for _ in range(3):
            if (await act(remote, "tick"))["result"] is not True:
                break
        state = await remote.state("local")
        assert next(t for t in state["tasks"] if t["id"] == failed_id)["state"] == "succeeded"
        assert state["ledger"]["frozen"] == 0
        assert_conservation(state)

        # 入口 4：已结算任务重抽 -> 新任务 -> 正常结算。
        await act(remote, "rerollTask", settled_id)
        for _ in range(3):
            if (await act(remote, "tick"))["result"] is not True:
                break
        state = await remote.state("local")
        assert state["ledger"]["frozen"] == 0
        assert all(t["state"] not in ("queued", "running", "in_flight") for t in state["tasks"]), (
            "不允许再留下无声冻结的任务"
        )
        assert_conservation(state)
    finally:
        await remote.close()
