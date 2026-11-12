"""批量生成视频（submitVideoTasks）：语义与 submitTasks 严格对齐，只是产物是视频。

口径（全部写死手算，不与自己比）：
- 种子余额 980，视频售价 300、单次硬上限 cap_of(300)=360；
- 一次批量只落**一条** `reserve` 流水，金额 = 镜数 × 360；
- 每镜一条 `kind="video"` 的 queued 任务，由 `tick` 逐条认领、逐条结算；
- 结算按 300 核销、退差 60，所以 N 镜成功后的余额 = 980 − 300N。

为什么这些用例值得存在：视频预占是 360 量级（关键帧只有 36），一旦「闸门在动账本
之后」或者「失败没退预占」，滚一次就是几百积分，且守恒断言会当场把库打成 409。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comicdrama.errors import AppError
from comicdrama.pricing import PRICE_T2I, PRICE_VIDEO, cap_of
from comicdrama.providers.mock import MockDramaProvider
from comicdrama.service import DramaService

SEED_BALANCE = 980
T2I_CAP = cap_of(PRICE_T2I)
VIDEO_CAP = cap_of(PRICE_VIDEO)

# 测试用的「已经有关键帧」地址：只要是个非空字符串就足以证明镜头有关键帧
# （tick 的首帧字节来自资产锚点图，不读这个字段）。
KEYFRAME = "/api/v1/drama/assets/00000000000000aa"


class FailingVideoProvider(MockDramaProvider):
    """第一次图生视频必定「明确失败」：本地凭据没配，上游一个字节都没收到。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def image_to_video(
        self,
        shot: dict[str, Any],
        size: str,
        *,
        owner: str,
        first_frame: bytes | None = None,
    ) -> dict[str, Any]:
        if self.fail_once:
            self.fail_once = False
            raise AppError("drama_credentials", "测试注入：凭据未配置（明确失败）", 400)
        return super().image_to_video(shot, size, owner=owner, first_frame=first_frame)


def make_service(tmp_path: Path, provider: MockDramaProvider | None = None) -> DramaService:
    service = DramaService(
        tmp_path / "drama.sqlite",
        provider or MockDramaProvider(),
        admins=frozenset({"local"}),
        autopump=False,
    )
    service.store.install()
    return service


@pytest.fixture
def service(tmp_path: Path) -> Iterator[DramaService]:
    made = make_service(tmp_path)
    try:
        yield made
    finally:
        made.store.close()


async def act(service: DramaService, name: str, *args: Any) -> dict[str, Any]:
    return await service.action("local", name, list(args), None, None)


async def project_of(service: DramaService) -> dict[str, Any]:
    state = await service.state("local")
    return next(p for p in state["projects"] if p["id"] == state["activeProjectId"])


async def shots(service: DramaService, count: int | None = None) -> list[str]:
    ids = [s["id"] for s in (await project_of(service))["shots"]]
    return ids if count is None else ids[:count]


def give_keyframe(service: DramaService, shot_ids: list[str]) -> None:
    """把镜头标成「已经生成过关键帧」：直接写库，等价于上一轮真的出过图。

    不走 updateShot：那个动作是给前台编辑分镜用的，允许它任意改产物地址本身
    就是一个漏洞（见 restoreShotMedia 的白名单校验）。
    """

    def op(db: Any) -> None:
        for shot_id in shot_ids:
            db.execute(
                "UPDATE drama_shots SET image_url=? WHERE id=?",
                (KEYFRAME, shot_id),
            )

    service.store.run(op)


async def tick_until_idle(service: DramaService) -> list[Any]:
    """逐条推进到没有 queued 任务；返回每次 tick 的 result，便于断言收敛。"""
    seen: list[Any] = []
    for _ in range(8):
        body = await act(service, "tick")
        seen.append(body["result"])
        if body["result"] is not True:
            break
    return seen


# ---------------------------------------------------------------- 1. 正常批量


@pytest.mark.needs_ffmpeg
async def test_batch_video_reserves_once_and_settles_per_shot(service: DramaService) -> None:
    """两镜 × 360 = 720 预占；逐镜结算后余额 380、冻结 0。

    手算：980 − 720 = 260；每次结算退 360 − 300 = 60，260 + 60 + 60 = 380。
    """
    ids = await shots(service, 2)
    give_keyframe(service, ids)

    body = await act(service, "submitVideoTasks", ids)
    assert body["result"] == {"queued": 2, "reserve": 2 * VIDEO_CAP, "skipped": []}
    state = body["state"]
    assert state["ledger"]["balance"] == SEED_BALANCE - 720 == 260
    assert state["ledger"]["frozen"] == 720

    video_tasks = [t for t in state["tasks"] if t["kind"] == "video"]
    assert len(video_tasks) == 2
    for task in video_tasks:
        assert task["state"] == "queued"
        assert task["reserve"] == VIDEO_CAP == 360
        assert task["cost"] == PRICE_VIDEO == 300
        assert task["frames"] == []
        assert task["picked"] is None

    # 一条批量预占流水，金额就是总额：不是每镜一条。
    # 种子里也有一条演示用 reserve，只看本次请求产生的（seed=False）。
    reserves = [x for x in state["ledger"]["tx"] if x["kind"] == "reserve" and not x["seed"]]
    assert len(reserves) == 1
    assert reserves[0]["amount"] == -720
    assert reserves[0]["label"] == "批量视频预占"
    assert "2 镜 × 360 积分" in reserves[0]["note"]

    seen = await tick_until_idle(service)
    assert seen[0] is True
    assert seen[-1] is False

    state = await service.state("local")
    assert state["ledger"]["balance"] == 380
    assert state["ledger"]["frozen"] == 0
    assert all(t["state"] == "succeeded" for t in state["tasks"])
    assert all(t["settled"] for t in state["tasks"])
    prod = next(p for p in state["projects"] if p["id"] == state["activeProjectId"])
    got = {s["id"]: s["videoUrl"] for s in prod["shots"]}
    for shot_id in ids:
        assert str(got[shot_id]).startswith("/api/v1/drama/assets/")


# ---------------------------------------------------------------- 2. 没有关键帧


async def test_shot_without_keyframe_is_skipped_not_queued(service: DramaService) -> None:
    """没关键帧的镜头进 skipped[no_keyframe]：不建任务、不占预占。"""
    ids = await shots(service, 2)
    give_keyframe(service, [ids[0]])

    body = await act(service, "submitVideoTasks", ids)
    assert body["result"] == {
        "queued": 1,
        "reserve": VIDEO_CAP,
        "skipped": [{"shotId": ids[1], "reason": "no_keyframe"}],
    }
    state = body["state"]
    assert [t["shotId"] for t in state["tasks"]] == [ids[0]]
    assert state["ledger"]["balance"] == SEED_BALANCE - VIDEO_CAP == 620
    assert state["ledger"]["frozen"] == VIDEO_CAP


# ---------------------------------------------------------------- 3. 闸门在动账本之前


async def test_insufficient_balance_refuses_before_touching_anything(
    service: DramaService,
) -> None:
    """3 镜需要 1080 > 980：409，且余额 / 冻结 / 任务数 / 流水全部不变。"""
    ids = await shots(service, 3)
    give_keyframe(service, ids)
    before = await service.state("local")

    with pytest.raises(AppError) as caught:
        await act(service, "submitVideoTasks", ids)
    assert caught.value.code == "insufficient_credits"
    assert caught.value.status == 409

    after = await service.state("local")
    assert after["ledger"]["balance"] == before["ledger"]["balance"] == SEED_BALANCE
    assert after["ledger"]["frozen"] == 0
    assert len(after["tasks"]) == len(before["tasks"]) == 0
    assert after["ledger"]["tx"] == before["ledger"]["tx"], "被拒时不能留任何流水"


# ---------------------------------------------------------------- 4. 重复提交


async def test_repeat_submit_marks_busy_without_a_second_reserve(service: DramaService) -> None:
    """同一批再提交一次：两镜都 busy，不重复预占。"""
    ids = await shots(service, 2)
    give_keyframe(service, ids)
    first = await act(service, "submitVideoTasks", ids)
    assert first["result"]["queued"] == 2
    frozen_after_first = first["state"]["ledger"]["frozen"]

    again = await act(service, "submitVideoTasks", ids)
    assert again["result"] == {
        "queued": 0,
        "reserve": 0,
        "skipped": [
            {"shotId": ids[0], "reason": "busy"},
            {"shotId": ids[1], "reason": "busy"},
        ],
    }
    state = again["state"]
    assert state["ledger"]["frozen"] == frozen_after_first == 720
    assert state["ledger"]["balance"] == SEED_BALANCE - 720 == 260
    assert len([t for t in state["tasks"] if t["kind"] == "video"]) == 2


# ---------------------------------------------------------------- 5. 单条失败


@pytest.mark.needs_ffmpeg
async def test_one_failed_video_releases_only_its_own_reserve(tmp_path: Path) -> None:
    """第一条视频明确失败：它的 360 全额退还，另一条的冻结不受影响，守恒成立。

    手算：980 − 720 = 260 → 退 360 = 620 → 第二条结算退 60 = 680 = 980 − 300。
    """
    service = make_service(tmp_path, FailingVideoProvider())
    try:
        ids = await shots(service, 2)
        give_keyframe(service, ids)
        await act(service, "submitVideoTasks", ids)

        await tick_until_idle(service)
        state = await service.state("local")
        failed = next(t for t in state["tasks"] if t["shotId"] == ids[0])
        ok = next(t for t in state["tasks"] if t["shotId"] == ids[1])
        assert failed["state"] == "failed"
        assert failed["settled"] is False
        assert failed["reserve"] == 0, "明确失败后预占必须清零"
        assert "预占全额退还" in failed["note"]
        assert ok["state"] == "succeeded" and ok["settled"] is True

        releases = [x for x in state["ledger"]["tx"] if x["kind"] == "release" and not x["seed"]]
        assert len(releases) == 1
        assert releases[0]["amount"] == VIDEO_CAP == 360
        assert state["ledger"]["balance"] == 680
        assert state["ledger"]["frozen"] == 0
        # 守恒：980 + Σcredit(0) − Σsettled cost(300) == balance + frozen
        assert SEED_BALANCE - 300 == state["ledger"]["balance"] + state["ledger"]["frozen"]
    finally:
        service.store.close()


# ---------------------------------------------------------------- 6. 整批守恒


@pytest.mark.needs_ffmpeg
async def test_batch_conservation_and_no_surprise_ledger_kinds(service: DramaService) -> None:
    """整批结束后守恒成立，且只出现 reserve / settle / refund 三种流水。"""
    ids = await shots(service, 2)
    give_keyframe(service, ids)
    await act(service, "submitVideoTasks", ids)
    await tick_until_idle(service)

    state = await service.state("local")
    kinds = {x["kind"] for x in state["ledger"]["tx"] if not x["seed"]}
    assert kinds == {"reserve", "settle", "refund"}

    settled_cost = sum(int(t["cost"]) for t in state["tasks"] if t["settled"])
    assert settled_cost == 600
    assert SEED_BALANCE - settled_cost == state["ledger"]["balance"] + state["ledger"]["frozen"]
    assert state["ledger"]["balance"] == 380
